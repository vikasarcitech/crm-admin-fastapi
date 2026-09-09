"""Public lead intake — the endpoint your static frontends POST to.

    POST /api/public/{tenant_slug}/forms/{form_slug}

Defences in order of cheapness: CORS allow-list (middleware), per-IP
rate limit, honeypot, minimum fill time, then field caps and required
checks from the stored form definition. Each of those is configurable
per form (``forms.settings``), because a newsletter box and a quote
request do not want the same friction.

A submission produces up to five things:

  * a ``form_submissions`` row — the raw log, kept even for spam;
  * a ``leads`` row — the first-class CRM record, unless quarantined;
  * a ``conversion_events`` row of kind 'form', so conversion rate is
    measured rather than inferred;
  * notification email to the form's recipients plus any conditional
    rule that matched;
  * an autoresponder to the submitter, when the form has one.
"""

import logging
import time
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request

from .. import db, events, mail, templating, tenancy
from ..config import settings
from ..ratelimit import intake_limiter
from ..schemas import IntakeRequest, collapse, keep_lines, valid_email
from ..security import client_ip

log = logging.getLogger("crm.intake")

router = APIRouter(prefix="/api/public", tags=["intake"])

DEFAULT_MIN_FILL_MS = 2500  # humans take longer than this
CORE_FIELDS = {"full_name", "email", "phone", "company", "message"}
TURNSTILE_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"

FORM_QUERY = """SELECT f.id, f.name, f.slug::text AS slug, f.fields, f.notify_emails,
                       f.settings, f.lead_mapping, f.notification_rules, f.autoresponder,
                       t.id AS tenant_id, t.slug::text AS tenant_slug, t.name AS tenant_name
                  FROM forms f JOIN tenants t ON t.id = f.tenant_id
                 WHERE t.slug = $1 AND t.is_active AND f.slug = $2 AND f.is_active"""


async def _verify_captcha(token: str | None, ip: str | None, provider: str) -> bool:
    """True when no provider is configured, so local dev still works."""
    if provider == "none" or not settings.turnstile_secret:
        return True
    if not token:
        return False
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            response = await client.post(
                TURNSTILE_URL,
                json={"secret": settings.turnstile_secret, "response": token, "remoteip": ip},
            )
            return bool(response.json().get("success"))
    except (httpx.HTTPError, ValueError) as exc:
        # Fail closed: an unreachable captcha service must not open the gate.
        log.error("captcha check failed: %s", exc)
        return False


@router.post("/{tenant_slug}/forms/{form_slug}", status_code=201)
async def submit(
    tenant_slug: str,
    form_slug: str,
    payload: IntakeRequest,
    request: Request,
) -> dict:
    ip = client_ip(request)
    await intake_limiter.check(ip or "unknown")

    form = await db.fetch_one(
        FORM_QUERY, collapse(tenant_slug, 60), collapse(form_slug, 60)
    )
    if not form:
        raise HTTPException(404, "This form is not accepting submissions.")

    config = form["settings"] if isinstance(form["settings"], dict) else {}
    tenant_id = form["tenant_id"]

    # ---- bot checks ---------------------------------------------------
    spam_reason: str | None = None
    if config.get("honeypot", True) and collapse(payload.hp, 10):
        spam_reason = "honeypot"

    min_fill = int(config.get("min_fill_ms", DEFAULT_MIN_FILL_MS) or 0)
    started_at = payload.rendered_at or 0
    if not spam_reason and min_fill and started_at > 0:
        if (time.time() * 1000 - started_at) < min_fill:
            spam_reason = "submitted too fast"

    if not await _verify_captcha(payload.captcha, ip, config.get("captcha", "turnstile")):
        raise HTTPException(400, "We could not verify that submission. Please try again.")

    # ---- validate against the stored form definition ------------------
    raw = payload.model_dump(by_alias=False)
    extras = payload.model_extra or {}
    fields = form["fields"] if isinstance(form["fields"], list) else []

    values: dict[str, Any] = {}
    missing: list[str] = []
    consents: list[tuple[str, bool]] = []

    for field in fields:
        name = field.get("name")
        if not name:
            continue
        kind = field.get("type", "text")
        source = raw.get(name, extras.get(name))
        limit = int(field.get("max") or (4000 if kind == "textarea" else 200))

        if kind == "email":
            value = valid_email(source)
        elif kind == "textarea":
            value = keep_lines(source, limit)
        elif kind in {"checkbox", "consent"}:
            value = _truthy(source)
            if kind == "consent":
                consents.append((name, bool(value)))
        elif kind == "number":
            value = _number(source)
        elif kind in {"select", "radio"}:
            value = collapse(source, limit)
            options = field.get("options") or []
            if value and options and value not in options:
                # A value outside the offered options means the request
                # did not come from the rendered form.
                raise HTTPException(400, f"“{field.get('label') or name}” has an unexpected value.")
        else:
            value = collapse(source, limit)

        if field.get("required") and value in (None, "", False):
            missing.append(field.get("label") or name)
        values[name] = value

    if missing:
        raise HTTPException(400, f"Please fill in: {', '.join(missing)}")

    if config.get("consent_required") and consents and not all(granted for _, granted in consents):
        raise HTTPException(400, "Please accept the consent checkbox to continue.")

    # ---- map onto lead columns ----------------------------------------
    mapping = form["lead_mapping"] if isinstance(form["lead_mapping"], dict) else {}
    core: dict[str, Any] = {name: values.get(name) for name in CORE_FIELDS}
    for field_name, column in mapping.items():
        if column in CORE_FIELDS and values.get(field_name) not in (None, ""):
            core[column] = values[field_name]

    mapped_sources = set(mapping) | CORE_FIELDS
    extra = {
        key: value
        for key, value in values.items()
        if key not in mapped_sources and value not in (None, "")
    }
    meta = payload.meta
    is_spam = spam_reason is not None

    lead = await db.fetch_one(
        """INSERT INTO leads (
               tenant_id, form_id, full_name, email, phone, company, message, extra,
               source_page, referrer, utm_source, utm_medium, utm_campaign,
               utm_term, utm_content, ip, user_agent, is_spam)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9, $10, $11, $12, $13,
                   $14, $15, $16, $17, $18)
           RETURNING id, full_name, email, created_at, is_spam""",
        tenant_id,
        form["id"],
        core.get("full_name") or "Unnamed lead",
        valid_email(core.get("email")) if core.get("email") else None,
        _as_text(core.get("phone")),
        _as_text(core.get("company")),
        _as_text(core.get("message")),
        extra,  # jsonb codec encodes; a pre-dumped string double-encodes
        meta.source_page or meta.landing_page,
        meta.referrer,
        meta.utm_source,
        meta.utm_medium,
        meta.utm_campaign,
        meta.utm_term,
        meta.utm_content,
        db.to_inet(ip),
        (request.headers.get("user-agent") or "")[:400],
        is_spam,
    )

    # ---- raw submission log -------------------------------------------
    if config.get("store_submission", True):
        await db.execute(
            """INSERT INTO form_submissions
                   (tenant_id, form_id, lead_id, payload, is_spam, spam_reason, ip, user_agent)
               VALUES ($1, $2, $3, $4::jsonb, $5, $6, $7, $8)""",
            tenant_id, form["id"], lead["id"],
            {k: v for k, v in values.items() if v is not None},
            is_spam, spam_reason, db.to_inet(ip),
            (request.headers.get("user-agent") or "")[:400],
        )

    await db.execute(
        """UPDATE forms
              SET submit_count = submit_count + 1,
                  spam_count = spam_count + CASE WHEN $2 THEN 1 ELSE 0 END
            WHERE id = $1""",
        form["id"], is_spam,
    )

    # A soft limit on purpose: refusing a real enquiry because the
    # site is over its monthly lead ceiling would cost the client more
    # than the overage costs us. The notification makes it visible.
    if not is_spam:
        await tenancy.warn_if_over(tenant_id, "leads_per_month")

    await events.log_activity(
        tenant_id, "lead.created",
        object_type="lead", object_id=lead["id"],
        meta={"form": form["slug"], "spam": is_spam, "spam_reason": spam_reason,
              "utm_source": meta.utm_source},
        ip=db.to_inet(ip),
    )

    # ---- consent evidence (2.12) --------------------------------------
    for field_name, granted in consents:
        await _record_consent(tenant_id, core.get("email"), field_name, granted, meta, ip, request)

    origin = settings.app_base_url or (
        f"{request.url.scheme}://{(request.headers.get('host') or '').strip()}"
    )

    # Quarantined submissions never reach the pipeline view or the CRM.
    if not is_spam:
        await _record_conversion(tenant_id, form, lead, meta, values)
        await _notify(tenant_id, form, lead, core, extra, values, meta, origin)
        await _autorespond(tenant_id, form, lead, core, origin)
        await events.notify(
            tenant_id, "lead.created",
            f"New lead: {lead['full_name']}",
            body=f"From “{form['name']}”" + (f" · {meta.utm_source}" if meta.utm_source else ""),
            level="success", link=f"#/leads?open={lead['id']}",
        )
        await events.emit(
            tenant_id, "lead.created",
            {
                "id": lead["id"],
                "form": form["slug"],
                "full_name": lead["full_name"],
                "email": lead["email"],
                "phone": core.get("phone"),
                "company": core.get("company"),
                "message": core.get("message"),
                "extra": extra,
                "utm": {
                    "source": meta.utm_source, "medium": meta.utm_medium,
                    "campaign": meta.utm_campaign, "term": meta.utm_term,
                    "content": meta.utm_content,
                },
                "source_page": meta.source_page,
                "created_at": lead["created_at"],
            },
        )
        await events.emit(
            tenant_id, "form.submitted",
            {"form": form["slug"], "lead_id": lead["id"], "fields": list(values)},
        )

    # Identical response either way, so a bot cannot learn it was flagged.
    return {
        "ok": True,
        "message": config.get("success_message") or "Thanks — we will be in touch shortly.",
        "redirect": config.get("redirect_url") or None,
    }


# --------------------------------------------------------------- helpers
def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "checked"}


def _number(value: Any) -> float | int | None:
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return int(number) if number.is_integer() else number


def _as_text(value: Any) -> str | None:
    """Lead columns are text; a mapped checkbox or number must not be
    bound as a bool/float or asyncpg raises a type error."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)[:4000]


async def _record_conversion(
    tenant_id: int, form: dict, lead: dict, meta, values: dict
) -> None:
    try:
        await db.execute(
            """INSERT INTO conversion_events
                   (tenant_id, kind, name, label, source_page, referrer, lead_id,
                    utm_source, utm_medium, utm_campaign, meta)
               VALUES ($1, 'form', $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb)""",
            tenant_id, form["slug"], form["name"],
            meta.source_page or meta.landing_page, meta.referrer, lead["id"],
            meta.utm_source, meta.utm_medium, meta.utm_campaign,
            {"fields": len(values)},
        )
    except Exception as exc:
        log.error("conversion write failed: %s", exc)


async def _record_consent(
    tenant_id: int, email: str | None, purpose: str, granted: bool, meta, ip, request: Request
) -> None:
    """A consent checkbox on a form is consent evidence, and has to be
    provable later — so it is stored with the page, policy and time."""
    import hashlib  # noqa: PLC0415

    try:
        address = valid_email(email)
        subject_hash = hashlib.sha256((address or f"anon:{ip or ''}").encode()).hexdigest()
        version = await db.fetch_one(
            "SELECT value->>'policy_version' AS v FROM settings"
            " WHERE tenant_id = $1 AND key = 'cookie_consent'",
            tenant_id,
        )
        await db.execute(
            """INSERT INTO consent_records
                   (tenant_id, subject_email, subject_hash, purpose, granted,
                    policy_version, source_page, evidence, ip, user_agent)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9, $10)""",
            tenant_id, address, subject_hash, f"form.{purpose}", granted,
            (version or {}).get("v"), meta.source_page or meta.landing_page,
            {"channel": "form", "referrer": meta.referrer},
            db.to_inet(ip), (request.headers.get("user-agent") or "")[:300] or None,
        )
    except Exception as exc:
        log.error("consent write failed: %s", exc)


def _rule_matches(rule: dict, values: dict) -> bool:
    """Evaluate one conditional notification rule."""
    condition = rule.get("when")
    if not isinstance(condition, dict) or not condition.get("field"):
        return True  # no condition means "always"

    actual = values.get(condition["field"])
    expected = condition.get("value") or ""
    operator = condition.get("op", "present")

    if operator == "present":
        return actual not in (None, "", False)
    if operator == "absent":
        return actual in (None, "", False)

    text = str(actual or "").strip().lower()
    wanted = str(expected).strip().lower()

    if operator == "eq":
        return text == wanted
    if operator == "ne":
        return text != wanted
    if operator == "contains":
        return wanted in text
    if operator == "not_contains":
        return wanted not in text

    # Numeric comparisons; a non-numeric value simply does not match.
    try:
        left, right = float(actual), float(expected)
    except (TypeError, ValueError):
        return False
    return {
        "gt": left > right, "gte": left >= right,
        "lt": left < right, "lte": left <= right,
    }.get(operator, False)


async def _notify(
    tenant_id: int, form: dict, lead: dict, core: dict, extra: dict,
    values: dict, meta, origin: str,
) -> None:
    """Notify the form's recipients, the workspace list, and any rule
    whose condition matched."""
    setting = await db.fetch_one(
        "SELECT value FROM settings WHERE tenant_id = $1 AND key = 'notify_emails'", tenant_id
    )
    workspace_list = setting["value"] if setting and isinstance(setting["value"], list) else []

    recipients = set(form["notify_emails"] or []) | {str(e) for e in workspace_list}
    matched: list[str] = []
    for rule in form["notification_rules"] or []:
        if isinstance(rule, dict) and _rule_matches(rule, values):
            recipients |= set(rule.get("to") or [])
            if rule.get("label"):
                matched.append(rule["label"])

    if not recipients:
        return

    context = {
        "lead": {
            "full_name": lead["full_name"],
            "first_name": str(lead["full_name"] or "").split(" ")[0],
            "email": lead["email"] or "—",
            "phone": core.get("phone") or "—",
            "company": core.get("company") or "—",
            "message": core.get("message") or "—",
            "source_page": meta.source_page or "—",
            "utm_source": meta.utm_source or "direct",
            "utm_medium": meta.utm_medium or "—",
        },
        "form": {"name": form["name"], "slug": form["slug"]},
        "site": {"name": form["tenant_name"]},
        "app": {"lead_url": f"{origin}/#/leads?open={lead['id']}"},
    }

    template = await db.fetch_one(
        """SELECT subject, body_text FROM email_templates
            WHERE tenant_id = $1 AND slug = 'lead-notification' AND is_active""",
        tenant_id,
    )
    if template:
        subject = templating.render_text(template["subject"], context)
        body = templating.render_text(template["body_text"], context)
        if extra:
            body += "\n\nOther fields:\n" + "\n".join(
                f"  {key}: {value}" for key, value in list(extra.items())[:20]
            )
    else:
        # No template configured: the built-in composer still works.
        subject, body = mail.lead_notification(
            {**lead, **core, "utm_source": meta.utm_source,
             "utm_medium": meta.utm_medium, "utm_campaign": meta.utm_campaign},
            form["name"], origin,
        )

    if matched:
        body += f"\n\n(Routed by rule: {', '.join(matched)})"

    await mail.enqueue(tenant_id, recipients, subject, body, kind="lead.notification")


async def _autorespond(
    tenant_id: int, form: dict, lead: dict, core: dict, origin: str
) -> None:
    """Send the form's autoresponder to the submitter, if configured."""
    config = form["autoresponder"] if isinstance(form["autoresponder"], dict) else {}
    if not config.get("enabled"):
        return
    address = valid_email(lead["email"])
    if not address:
        return  # nothing to reply to

    template = await db.fetch_one(
        """SELECT subject, body_text FROM email_templates
            WHERE tenant_id = $1 AND slug = $2 AND is_active""",
        tenant_id, config.get("template_slug"),
    )
    if not template:
        log.warning(
            "form %s has an autoresponder pointing at a missing template %r",
            form["slug"], config.get("template_slug"),
        )
        return

    context = {
        "lead": {
            "full_name": lead["full_name"],
            "first_name": str(lead["full_name"] or "there").split(" ")[0],
            "email": address,
            "message": core.get("message") or "",
            "company": core.get("company") or "",
        },
        "form": {"name": form["name"], "slug": form["slug"]},
        "site": {"name": form["tenant_name"], "url": origin},
    }
    await mail.enqueue(
        tenant_id,
        [address],
        templating.render_text(template["subject"], context),
        templating.render_text(template["body_text"], context),
        kind="lead.autoresponder",
    )
