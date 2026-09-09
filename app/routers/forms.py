"""Forms & conversion (2.7).

The admin-managed form builder: field definitions, validation rules,
lead mapping, spam protection, notification rules, autoresponders and
the submission log. Every submission becomes a first-class lead (2.6)
unless it is quarantined as spam.

Conversion events are separate from leads on purpose. A form submit is
one kind of conversion; a CTA click, a phone tap, an email link and a
WhatsApp tap are the others, and none of those produce a lead record —
so conversion rate has to be measured from its own table rather than
inferred from lead count.
"""

from __future__ import annotations

import csv
import io
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from .. import db, events, templating, tenancy
from ..content import slugify
from ..permissions import require_perm
from ..ratelimit import RateLimiter
from ..sanitize import clean_html
from ..schemas import (
    ConversionEvent,
    FormCreate,
    FormUpdate,
    TemplateCreate,
    TemplateUpdate,
    collapse,
    keep_lines,
    valid_email,
)
from ..security import CurrentUser, client_ip, tenant_db

log = logging.getLogger("crm.forms")

router = APIRouter(prefix="/api/forms", tags=["forms"])
templates_router = APIRouter(prefix="/api/templates", tags=["templates"])
conversions_router = APIRouter(prefix="/api/conversions", tags=["conversions"])
public_router = APIRouter(tags=["conversions-public"])

# The public conversion beacon is unauthenticated, so it gets its own
# budget — generous enough for a real visitor, tight enough that it is
# not a free write endpoint.
conversion_limiter = RateLimiter(max_requests=60, window_seconds=600)

FORM_COLUMNS = """f.id, f.slug::text AS slug, f.name, f.fields, f.notify_emails,
                  f.settings, f.lead_mapping, f.notification_rules, f.autoresponder,
                  f.is_active, f.submit_count, f.spam_count, f.created_at, f.updated_at"""

CORE_LEAD_FIELDS = ("full_name", "email", "phone", "company", "message")

RULE_OPERATORS = frozenset(
    {"eq", "ne", "contains", "not_contains", "gt", "gte", "lt", "lte", "present", "absent"}
)


# ================================================================= forms
@router.get("")
async def list_forms(scoped: db.TenantDB = Depends(tenant_db)) -> dict:
    rows = await scoped.fetch(
        f"""SELECT {FORM_COLUMNS},
                   count(s.id) FILTER (WHERE s.created_at > now() - interval '30 days')::int
                     AS submissions_30d,
                   count(l.id)::int AS lead_count
              FROM forms f
              LEFT JOIN form_submissions s ON s.form_id = f.id
              LEFT JOIN leads l ON l.form_id = f.id AND NOT l.is_spam
             WHERE f.tenant_id = $1
             GROUP BY f.id ORDER BY f.name"""
    )
    return {
        "forms": rows,
        "fieldTypes": [
            "text", "email", "tel", "textarea", "number", "url", "date",
            "select", "radio", "checkbox", "hidden", "consent",
        ],
        "coreFields": list(CORE_LEAD_FIELDS),
    }


@router.post("", status_code=201)
async def create_form(
    payload: FormCreate, user: CurrentUser = Depends(require_perm("forms.manage"))
) -> dict:
    scoped = db.TenantDB(user.tenant_id)
    slug = slugify(payload.slug or payload.name, "form")
    if await scoped.fetch_one("SELECT 1 FROM forms WHERE tenant_id = $1 AND slug = $2", slug):
        raise HTTPException(400, "A form already uses that name.")

    fields = _clean_fields(payload.fields)
    row = await scoped.fetch_one(
        f"""INSERT INTO forms (tenant_id, slug, name, fields, notify_emails, settings)
            VALUES ($1, $2, $3, $4::jsonb, $5, $6::jsonb)
            RETURNING {FORM_COLUMNS.replace("f.", "")}""",
        slug, collapse(payload.name, 120), fields,
        _clean_emails(payload.notify_emails), _default_settings(),
    )
    await events.log_activity(
        user.tenant_id, "form.created", user_id=user.id,
        object_type="form", object_id=row["id"], meta={"slug": slug},
    )
    return {"form": {**row, "submissions_30d": 0, "lead_count": 0}}


def _default_settings() -> dict:
    return {
        "success_message": "Thanks — we will be in touch shortly.",
        "redirect_url": None,
        "submit_label": "Send",
        "honeypot": True,
        "captcha": "turnstile",
        "min_fill_ms": 2500,
        "store_submission": True,
        "consent_required": False,
        "consent_text": None,
    }


def _clean_fields(fields) -> list[dict]:
    """Field definitions, with a lead-mappable core field guaranteed.

    A form with no name and no email produces a lead nobody can act on,
    so at least one identifying field is required.
    """
    if not fields:
        return [
            {"name": "full_name", "label": "Name", "type": "text", "required": True, "max": 120},
            {"name": "email", "label": "Email", "type": "email", "required": True},
            {"name": "message", "label": "How can we help?", "type": "textarea",
             "required": True, "max": 4000},
        ]

    out: list[dict] = []
    seen: set[str] = set()
    for field in fields:
        data = field.model_dump() if hasattr(field, "model_dump") else dict(field)
        name = data["name"]
        if name in seen:
            raise HTTPException(400, f"Field “{name}” is defined twice.")
        seen.add(name)

        if data["type"] in {"select", "radio"} and not data.get("options"):
            raise HTTPException(400, f"“{data['label']}” needs at least one option.")

        cleaned = {
            "name": name,
            "label": collapse(data["label"], 120),
            "type": data["type"],
            "required": bool(data.get("required")),
        }
        cleaned["max"] = int(
            data.get("max") or (4000 if data["type"] == "textarea" else 200)
        )
        for key in ("placeholder", "help"):
            if data.get(key):
                cleaned[key] = collapse(data[key], 200)
        if data.get("options"):
            cleaned["options"] = [
                collapse(str(o), 120) for o in data["options"][:60] if collapse(str(o), 120)
            ]
        out.append(cleaned)

    if not seen & {"full_name", "email", "phone"}:
        raise HTTPException(
            400,
            "A form needs at least a name, email or phone field so the lead is contactable.",
        )
    return out


def _clean_emails(raw: list[str] | None) -> list[str]:
    if not raw:
        return []
    seen: dict[str, None] = {}
    for entry in raw[:20]:
        address = valid_email(entry)
        if address:
            seen.setdefault(address, None)
    return list(seen)


@router.get("/{form_id}")
async def form_detail(form_id: int, scoped: db.TenantDB = Depends(tenant_db)) -> dict:
    row = await scoped.fetch_one(
        f"SELECT {FORM_COLUMNS} FROM forms f WHERE f.tenant_id = $1 AND f.id = $2", form_id
    )
    if not row:
        raise HTTPException(404, "That form no longer exists.")

    tenant = await db.fetch_one(
        "SELECT slug::text AS slug FROM tenants WHERE id = $1", scoped.tenant_id
    )
    row["endpoint"] = f"/api/public/{tenant['slug']}/forms/{row['slug']}"
    row["recent"] = await scoped.fetch(
        """SELECT s.id, s.payload, s.is_spam, s.spam_reason, s.created_at, s.lead_id
             FROM form_submissions s
            WHERE s.tenant_id = $1 AND s.form_id = $2
            ORDER BY s.created_at DESC LIMIT 10""",
        form_id,
    )
    return {"form": row}


@router.patch("/{form_id}")
async def update_form(
    form_id: int,
    payload: FormUpdate,
    user: CurrentUser = Depends(require_perm("forms.manage")),
) -> dict:
    scoped = db.TenantDB(user.tenant_id)
    existing = await scoped.fetch_one(
        f"SELECT {FORM_COLUMNS} FROM forms f WHERE f.tenant_id = $1 AND f.id = $2", form_id
    )
    if not existing:
        raise HTTPException(404, "That form no longer exists.")

    sent = payload.model_dump(exclude_unset=True)
    if not sent:
        raise HTTPException(400, "Nothing to save.")

    args: list = [user.tenant_id, form_id]
    assignments: list[str] = []

    def assign(column: str, value, cast: str = "") -> None:
        args.append(value)
        assignments.append(f"{column} = ${len(args)}{cast}")

    if "name" in sent:
        assign("name", collapse(payload.name, 120))
    if "slug" in sent and payload.slug:
        slug = slugify(payload.slug, "form")
        taken = await scoped.fetch_one(
            "SELECT 1 FROM forms WHERE tenant_id = $1 AND slug = $2 AND id <> $3",
            slug, form_id,
        )
        if taken:
            raise HTTPException(400, "A form already uses that name.")
        assign("slug", slug)
    if "fields" in sent:
        assign("fields", _clean_fields(payload.fields), "::jsonb")
    if "notify_emails" in sent:
        assign("notify_emails", _clean_emails(payload.notify_emails))
    if "settings" in sent:
        assign("settings", _clean_settings(payload.settings, existing["settings"]), "::jsonb")
    if "lead_mapping" in sent:
        fields = payload.fields if "fields" in sent else existing["fields"]
        assign("lead_mapping", _clean_mapping(payload.lead_mapping, fields), "::jsonb")
    if "notification_rules" in sent:
        assign("notification_rules", _clean_rules(payload.notification_rules), "::jsonb")
    if "autoresponder" in sent:
        assign(
            "autoresponder",
            await _clean_autoresponder(scoped, payload.autoresponder),
            "::jsonb",
        )
    if "is_active" in sent:
        assign("is_active", bool(payload.is_active))

    row = await db.fetch_one(
        f"""UPDATE forms SET {", ".join(assignments)}
             WHERE tenant_id = $1 AND id = $2
             RETURNING {FORM_COLUMNS.replace("f.", "")}""",
        *args,
    )
    await events.log_activity(
        user.tenant_id, "form.updated", user_id=user.id,
        object_type="form", object_id=form_id, meta={"fields": list(sent)},
    )
    return {"form": row}


def _clean_settings(raw, current: dict | None) -> dict:
    source = raw if isinstance(raw, dict) else {}
    merged = {**_default_settings(), **(current or {})}

    for key in ("success_message", "submit_label", "consent_text"):
        if key in source:
            merged[key] = collapse(source[key], 500)
    if "redirect_url" in source:
        url = collapse(source["redirect_url"], 500)
        if url and not url.startswith(("https://", "/")):
            raise HTTPException(400, "The redirect URL must be an https:// URL or a path.")
        merged["redirect_url"] = url
    for key in ("honeypot", "store_submission", "consent_required"):
        if key in source:
            merged[key] = bool(source[key])
    if "captcha" in source:
        captcha = collapse(source["captcha"], 20) or "none"
        if captcha not in {"none", "turnstile", "recaptcha"}:
            raise HTTPException(400, "Captcha must be none, turnstile or recaptcha.")
        merged["captcha"] = captcha
    if "min_fill_ms" in source:
        merged["min_fill_ms"] = max(0, min(int(source["min_fill_ms"] or 0), 60_000))
    return merged


def _clean_mapping(raw, fields) -> dict:
    """Map form field names onto lead columns.

    Only the core lead columns are targets; everything else already
    lands in leads.extra, so mapping it would be a no-op.
    """
    source = raw if isinstance(raw, dict) else {}
    names = {
        (f.get("name") if isinstance(f, dict) else f.name)
        for f in (fields or [])
    }
    out: dict[str, str] = {}
    for field_name, column in list(source.items())[:20]:
        field_name = collapse(field_name, 40)
        column = collapse(column, 40)
        if not field_name or not column:
            continue
        if column not in CORE_LEAD_FIELDS:
            raise HTTPException(
                400, f"“{column}” is not a lead field. Choose one of: {', '.join(CORE_LEAD_FIELDS)}"
            )
        if names and field_name not in names:
            raise HTTPException(400, f"This form has no “{field_name}” field.")
        out[field_name] = column
    return out


def _clean_rules(raw) -> list[dict]:
    """Conditional notification rules: route a submission to a different
    inbox when a field matches."""
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for entry in raw[:20]:
        if not isinstance(entry, dict):
            continue
        recipients = _clean_emails(entry.get("to") or [])
        if not recipients:
            raise HTTPException(400, "Every notification rule needs at least one valid recipient.")

        condition = entry.get("when")
        cleaned: dict = {"to": recipients, "label": collapse(entry.get("label"), 80)}
        if isinstance(condition, dict) and condition.get("field"):
            operator = collapse(condition.get("op"), 20) or "present"
            if operator not in RULE_OPERATORS:
                raise HTTPException(
                    400, f"Rule operator must be one of: {', '.join(sorted(RULE_OPERATORS))}"
                )
            cleaned["when"] = {
                "field": collapse(condition["field"], 40),
                "op": operator,
                "value": collapse(str(condition.get("value", "")), 200),
            }
        out.append(cleaned)
    return out


async def _clean_autoresponder(scoped: db.TenantDB, raw) -> dict:
    source = raw if isinstance(raw, dict) else {}
    if not source.get("enabled"):
        return {"enabled": False, "template_slug": source.get("template_slug")}

    slug = collapse(source.get("template_slug"), 60)
    if not slug:
        raise HTTPException(400, "Choose an email template for the autoresponder.")
    template = await scoped.fetch_one(
        "SELECT 1 FROM email_templates WHERE tenant_id = $1 AND slug = $2 AND is_active",
        slug,
    )
    if not template:
        raise HTTPException(400, f"There is no active “{slug}” email template.")
    return {"enabled": True, "template_slug": slug,
            "reply_to": valid_email(source.get("reply_to"))}


@router.delete("/{form_id}")
async def delete_form(
    form_id: int, user: CurrentUser = Depends(require_perm("forms.manage"))
) -> dict:
    """Deactivates rather than deletes when leads still reference it —
    deleting would orphan the lead's provenance."""
    scoped = db.TenantDB(user.tenant_id)
    leads = await scoped.fetch_one(
        "SELECT count(*)::int AS n FROM leads WHERE tenant_id = $1 AND form_id = $2", form_id
    )
    if leads["n"]:
        await scoped.execute(
            "UPDATE forms SET is_active = FALSE WHERE tenant_id = $1 AND id = $2", form_id
        )
        return {
            "ok": True,
            "deactivated": True,
            "message": f"That form has {leads['n']} lead(s), so it was deactivated instead.",
        }

    removed = await scoped.fetch(
        "DELETE FROM forms WHERE tenant_id = $1 AND id = $2 RETURNING id", form_id
    )
    if not removed:
        raise HTTPException(404, "That form no longer exists.")
    return {"ok": True, "deactivated": False}


# =========================================================== submissions
@router.get("/{form_id}/submissions")
async def list_submissions(
    form_id: int,
    spam: bool | None = None,
    page: int = Query(default=1, ge=1, le=500),
    per_page: int = Query(default=50, ge=10, le=200),
    user: CurrentUser = Depends(require_perm("submissions.view")),
) -> dict:
    scoped = db.TenantDB(user.tenant_id)
    offset = (page - 1) * per_page
    rows = await scoped.fetch(
        """SELECT s.id, s.payload, s.is_spam, s.spam_reason, s.ip::text AS ip,
                  s.user_agent, s.created_at, s.lead_id,
                  l.full_name, l.status::text AS lead_status
             FROM form_submissions s
             LEFT JOIN leads l ON l.id = s.lead_id
            WHERE s.tenant_id = $1 AND s.form_id = $2
              AND ($3::boolean IS NULL OR s.is_spam = $3)
            ORDER BY s.created_at DESC LIMIT $4 OFFSET $5""",
        form_id, spam, per_page, offset,
    )
    total = await scoped.fetch_one(
        """SELECT count(*)::int AS n FROM form_submissions
            WHERE tenant_id = $1 AND form_id = $2 AND ($3::boolean IS NULL OR is_spam = $3)""",
        form_id, spam,
    )
    return {
        "submissions": rows,
        "page": page,
        "total": total["n"],
        "pages": max(1, -(-total["n"] // per_page)),
    }


@router.get("/{form_id}/submissions/export")
async def export_submissions(
    form_id: int,
    include_spam: bool = False,
    user: CurrentUser = Depends(require_perm("submissions.view")),
) -> StreamingResponse:
    """CSV of every submission, with one column per defined field."""
    scoped = db.TenantDB(user.tenant_id)
    form = await scoped.fetch_one(
        "SELECT slug::text AS slug, fields FROM forms WHERE tenant_id = $1 AND id = $2", form_id
    )
    if not form:
        raise HTTPException(404, "That form no longer exists.")

    field_names = [f["name"] for f in (form["fields"] or []) if f.get("name")]
    rows = await scoped.fetch(
        """SELECT s.created_at, s.is_spam, s.spam_reason, s.payload, s.lead_id
             FROM form_submissions s
            WHERE s.tenant_id = $1 AND s.form_id = $2
              AND ($3::boolean OR NOT s.is_spam)
            ORDER BY s.created_at DESC LIMIT 20000""",
        form_id, include_spam,
    )

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["submitted_at", "lead_id", "is_spam", "spam_reason", *field_names])
    for row in rows:
        payload = row["payload"] or {}
        writer.writerow(
            [
                row["created_at"].isoformat(),
                row["lead_id"] or "",
                "yes" if row["is_spam"] else "no",
                row["spam_reason"] or "",
                *[_csv_cell(payload.get(name)) for name in field_names],
            ]
        )

    buffer.seek(0)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={
            "content-disposition":
                f'attachment; filename="{form["slug"]}-submissions-{stamp}.csv"'
        },
    )


def _csv_cell(value) -> str:
    """Neutralise spreadsheet formula injection.

    A submitted value starting with = + - or @ is executed as a formula
    by Excel and Sheets, which turns a public form into remote code
    execution on whoever opens the export.
    """
    if value is None:
        return ""
    text = str(value)
    return f"'{text}" if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


@router.delete("/submissions/{submission_id}")
async def delete_submission(
    submission_id: int, user: CurrentUser = Depends(require_perm("forms.manage"))
) -> dict:
    scoped = db.TenantDB(user.tenant_id)
    removed = await scoped.fetch(
        "DELETE FROM form_submissions WHERE tenant_id = $1 AND id = $2 RETURNING id",
        submission_id,
    )
    if not removed:
        raise HTTPException(404, "That submission no longer exists.")
    return {"ok": True}


# ======================================================= email templates
@templates_router.get("")
async def list_templates(scoped: db.TenantDB = Depends(tenant_db)) -> dict:
    rows = await scoped.fetch(
        """SELECT t.id, t.slug::text AS slug, t.name, t.subject, t.body_text, t.body_html,
                  t.kind, t.is_active, t.updated_at, u.display_name AS updated_by_name
             FROM email_templates t LEFT JOIN users u ON u.id = t.updated_by
            WHERE t.tenant_id = $1 ORDER BY t.kind, t.name"""
    )
    for row in rows:
        row["placeholders"] = templating.placeholders_in(
            row["subject"], row["body_text"], row["body_html"]
        )
    return {"templates": rows, "available": sorted(SAMPLE_CONTEXT_KEYS)}


# The placeholders a template can use, shown in the editor as hints.
SAMPLE_CONTEXT: dict = {
    "lead": {
        "full_name": "Priya Nair", "first_name": "Priya", "email": "priya@example.com",
        "phone": "+971 50 118 2244", "company": "Northwind", "message": "…",
        "source_page": "/pricing", "utm_source": "google", "utm_medium": "cpc",
    },
    "form": {"name": "Contact form", "slug": "contact"},
    "site": {"name": "Your site", "url": "https://example.com", "email": "hello@example.com"},
    "app": {"lead_url": "https://admin.example.com/#/leads?open=1"},
    "subscriber": {
        "name": "Priya", "name_suffix": " Priya", "email": "priya@example.com",
        "confirm_url": "https://example.com/confirm/…",
        "unsubscribe_url": "https://example.com/unsubscribe/…",
    },
}
SAMPLE_CONTEXT_KEYS = set(templating.flatten(SAMPLE_CONTEXT))


@templates_router.post("", status_code=201)
async def create_template(
    payload: TemplateCreate, user: CurrentUser = Depends(require_perm("forms.manage"))
) -> dict:
    scoped = db.TenantDB(user.tenant_id)
    slug = slugify(payload.slug or payload.name, "template")
    if await scoped.fetch_one(
        "SELECT 1 FROM email_templates WHERE tenant_id = $1 AND slug = $2", slug
    ):
        raise HTTPException(400, "A template already uses that name.")

    row = await scoped.fetch_one(
        """INSERT INTO email_templates
               (tenant_id, slug, name, subject, body_text, body_html, kind, updated_by)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
           RETURNING id, slug::text AS slug, name, subject, body_text, body_html,
                     kind, is_active, updated_at""",
        slug, collapse(payload.name, 120), collapse(payload.subject, 300),
        keep_lines(payload.body_text, 40_000),
        _clean_template_html(payload.body_html), payload.kind, user.id,
    )
    row["placeholders"] = templating.placeholders_in(
        row["subject"], row["body_text"], row["body_html"]
    )
    return {"template": row}


def _clean_template_html(raw: str | None) -> str | None:
    """HTML bodies are sanitized on write, like page content: a template
    is authored by an Editor but sent to people outside the workspace."""
    if raw is None:
        return None
    try:
        return clean_html(raw, limit=200_000)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@templates_router.patch("/{template_id}")
async def update_template(
    template_id: int,
    payload: TemplateUpdate,
    user: CurrentUser = Depends(require_perm("forms.manage")),
) -> dict:
    scoped = db.TenantDB(user.tenant_id)
    sent = payload.model_dump(exclude_unset=True)
    if not sent:
        raise HTTPException(400, "Nothing to save.")

    row = await scoped.fetch_one(
        """UPDATE email_templates
              SET name      = coalesce($3, name),
                  subject   = coalesce($4, subject),
                  body_text = coalesce($5, body_text),
                  body_html = CASE WHEN $6 THEN $7 ELSE body_html END,
                  kind      = coalesce($8, kind),
                  is_active = coalesce($9, is_active),
                  updated_by = $10
            WHERE tenant_id = $1 AND id = $2
            RETURNING id, slug::text AS slug, name, subject, body_text, body_html,
                      kind, is_active, updated_at""",
        template_id, collapse(payload.name, 120), collapse(payload.subject, 300),
        keep_lines(payload.body_text, 40_000),
        "body_html" in sent, _clean_template_html(payload.body_html),
        payload.kind, payload.is_active, user.id,
    )
    if not row:
        raise HTTPException(404, "That template no longer exists.")
    row["placeholders"] = templating.placeholders_in(
        row["subject"], row["body_text"], row["body_html"]
    )
    row["unknownPlaceholders"] = [
        name for name in row["placeholders"] if name not in SAMPLE_CONTEXT_KEYS
    ]
    return {"template": row}


@templates_router.post("/{template_id}/preview")
async def preview_template(
    template_id: int, user: CurrentUser = Depends(require_perm("forms.manage"))
) -> dict:
    """Render with sample data, so an editor sees the result before it
    is sent to a real person."""
    scoped = db.TenantDB(user.tenant_id)
    row = await scoped.fetch_one(
        """SELECT subject, body_text, body_html FROM email_templates
            WHERE tenant_id = $1 AND id = $2""",
        template_id,
    )
    if not row:
        raise HTTPException(404, "That template no longer exists.")

    context = {**SAMPLE_CONTEXT, "site": {**SAMPLE_CONTEXT["site"], "name": user.tenant_name}}
    return {
        "subject": templating.render_text(row["subject"], context),
        "bodyText": templating.render_text(row["body_text"], context),
        "bodyHtml": templating.render_html(row["body_html"], context),
        "missing": templating.missing_placeholders(
            context, row["subject"], row["body_text"], row["body_html"]
        ),
    }


@templates_router.delete("/{template_id}")
async def delete_template(
    template_id: int, user: CurrentUser = Depends(require_perm("forms.manage"))
) -> dict:
    scoped = db.TenantDB(user.tenant_id)
    row = await scoped.fetch_one(
        "SELECT slug::text AS slug FROM email_templates WHERE tenant_id = $1 AND id = $2",
        template_id,
    )
    if not row:
        raise HTTPException(404, "That template no longer exists.")

    in_use = await scoped.fetch(
        """SELECT name FROM forms
            WHERE tenant_id = $1 AND autoresponder->>'template_slug' = $2
              AND (autoresponder->>'enabled')::boolean""",
        row["slug"],
    )
    if in_use:
        raise HTTPException(
            409,
            "That template is the autoresponder for: "
            + ", ".join(f"“{f['name']}”" for f in in_use[:5]),
        )

    await scoped.execute(
        "DELETE FROM email_templates WHERE tenant_id = $1 AND id = $2", template_id
    )
    return {"ok": True}


# ====================================================== conversion events
@conversions_router.get("")
async def list_conversions(
    days: int = Query(default=30, ge=1, le=365),
    kind: str | None = Query(default=None, max_length=20),
    user: CurrentUser = Depends(require_perm("analytics.view")),
) -> dict:
    scoped = db.TenantDB(user.tenant_id)
    totals = await scoped.fetch(
        """SELECT kind, count(*)::int AS n,
                  coalesce(sum(value_amount), 0)::float8 AS value
             FROM conversion_events
            WHERE tenant_id = $1 AND created_at > now() - make_interval(days => $2)
              AND ($3::text IS NULL OR kind = $3)
            GROUP BY kind ORDER BY n DESC""",
        days, kind,
    )
    by_day = await scoped.fetch(
        """SELECT to_char(d::date, 'YYYY-MM-DD') AS day, coalesce(c.n, 0)::int AS n
             FROM generate_series(now()::date - make_interval(days => $2), now()::date, '1 day') d
             LEFT JOIN (
               SELECT created_at::date AS day, count(*) AS n FROM conversion_events
                WHERE tenant_id = $1 AND created_at > now() - make_interval(days => $2)
                  AND ($3::text IS NULL OR kind = $3)
                GROUP BY 1
             ) c ON c.day = d::date
            ORDER BY 1""",
        days, kind,
    )
    top = await scoped.fetch(
        """SELECT name, kind, count(*)::int AS n
             FROM conversion_events
            WHERE tenant_id = $1 AND created_at > now() - make_interval(days => $2)
              AND ($3::text IS NULL OR kind = $3)
            GROUP BY name, kind ORDER BY n DESC LIMIT 20""",
        days, kind,
    )
    pages = await scoped.fetch(
        """SELECT coalesce(nullif(source_page, ''), '(unknown)') AS page, count(*)::int AS n
             FROM conversion_events
            WHERE tenant_id = $1 AND created_at > now() - make_interval(days => $2)
            GROUP BY 1 ORDER BY n DESC LIMIT 15""",
        days,
    )
    return {"days": days, "byKind": totals, "byDay": by_day, "top": top, "byPage": pages}


@public_router.post("/api/public/{tenant_slug}/conversions", status_code=201)
async def record_conversion(
    tenant_slug: str, payload: ConversionEvent, request: Request
) -> dict:
    """Record a CTA click, phone/email/WhatsApp tap or download.

    Unauthenticated by design — the caller is the public website. Rate
    limited per IP, and the response is deliberately uninformative so it
    cannot be used to probe anything.
    """
    ip = client_ip(request)
    conversion_limiter.check(f"conv:{ip or 'unknown'}")

    try:
        tenant = await tenancy.resolve_public(tenant_slug, request.headers.get("host"))
    except HTTPException:
        # Same answer as success: a 404 here would enumerate tenants.
        return {"ok": True}

    meta = payload.meta if isinstance(payload.meta, dict) else {}
    page = payload.page
    await db.execute(
        """INSERT INTO conversion_events
               (tenant_id, kind, name, label, source_page, referrer, value_amount,
                utm_source, utm_medium, utm_campaign, visitor_key, meta)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12::jsonb)""",
        tenant["id"],
        payload.kind.value,
        collapse(payload.name, 80),
        collapse(payload.label, 160),
        page.source_page or page.landing_page,
        page.referrer,
        payload.value_amount,
        page.utm_source,
        page.utm_medium,
        page.utm_campaign,
        # Hashed, salted and truncated: enough to dedupe, not enough to
        # re-identify a visitor from the table.
        _visitor_key(ip, request.headers.get("user-agent")),
        {k: collapse(str(v), 200) for k, v in list(meta.items())[:20]},
    )
    return {"ok": True}


def _visitor_key(ip: str | None, user_agent: str | None) -> str | None:
    import hashlib  # noqa: PLC0415

    from ..config import settings  # noqa: PLC0415

    if not ip:
        return None
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    salt = settings.analytics_salt or "unsalted"
    raw = f"{salt}|{day}|{ip}|{(user_agent or '')[:120]}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]
