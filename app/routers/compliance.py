"""Compliance & data protection (2.12).

Three obligations, each with an implementation that has to be provable
rather than merely present:

* **Consent** — every grant and withdrawal is a row with the page, the
  policy version, the time and the channel. A consent claim you cannot
  evidence is not consent.
* **Data-subject requests** — export and deletion, worked as a queue.
  Export gathers everything keyed to an email across leads, submissions,
  subscribers, consent and conversions. Deletion *anonymizes* rather
  than hard-deleting where a row is also a business record, and the
  response says exactly what happened to each table.
* **Retention** — per-scope age limits enforced by a worker, so "we
  keep leads for 24 months" is a setting rather than a promise.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from .. import db, events
from ..permissions import require_perm
from ..ratelimit import RateLimiter
from ..schemas import (
    ConsentRecordInput,
    DataRequestCreate,
    RetentionPolicyUpdate,
    collapse,
    keep_lines,
    valid_email,
)
from ..security import CurrentUser, client_ip, tenant_db

log = logging.getLogger("crm.compliance")

router = APIRouter(prefix="/api/compliance", tags=["compliance"])
public_router = APIRouter(tags=["compliance-public"])

consent_limiter = RateLimiter(max_requests=30, window_seconds=600)

RETENTION_SCOPES = (
    "leads", "form_submissions", "activity_log", "conversion_events",
    "not_found_log", "error_log", "visitor_days", "notifications",
)

# Which scopes can be anonymized rather than deleted, and how. Anything
# absent only supports 'delete'.
ANONYMIZABLE = {"leads", "form_submissions"}


def subject_hash(email: str) -> str:
    return hashlib.sha256(email.strip().lower().encode()).hexdigest()


# =============================================================== consent
@router.get("/consent")
async def list_consent(
    email: str | None = Query(default=None, max_length=254),
    purpose: str | None = Query(default=None, max_length=80),
    granted: bool | None = None,
    days: int = Query(default=365, ge=1, le=3650),
    page: int = Query(default=1, ge=1, le=500),
    per_page: int = Query(default=50, ge=10, le=200),
    user: CurrentUser = Depends(require_perm("compliance.view")),
) -> dict:
    scoped = db.TenantDB(user.tenant_id)
    address = valid_email(email) if email else None
    offset = (page - 1) * per_page

    rows = await scoped.fetch(
        """SELECT id, subject_email::text AS subject_email, purpose, granted,
                  policy_version, source_page, evidence, ip::text AS ip,
                  user_agent, created_at
             FROM consent_records
            WHERE tenant_id = $1
              AND created_at > now() - make_interval(days => $2)
              AND ($3::text IS NULL OR subject_hash = $3)
              AND ($4::text IS NULL OR purpose LIKE $4 || '%')
              AND ($5::boolean IS NULL OR granted = $5)
            ORDER BY created_at DESC LIMIT $6 OFFSET $7""",
        days, subject_hash(address) if address else None, purpose, granted,
        per_page, offset,
    )
    summary = await scoped.fetch(
        """SELECT purpose,
                  count(*) FILTER (WHERE granted)::int AS granted,
                  count(*) FILTER (WHERE NOT granted)::int AS withdrawn,
                  max(created_at) AS last_at
             FROM consent_records
            WHERE tenant_id = $1 AND created_at > now() - make_interval(days => $2)
            GROUP BY purpose ORDER BY purpose""",
        days,
    )
    total = await scoped.fetch_one(
        """SELECT count(*)::int AS n FROM consent_records
            WHERE tenant_id = $1 AND created_at > now() - make_interval(days => $2)
              AND ($3::text IS NULL OR subject_hash = $3)""",
        days, subject_hash(address) if address else None,
    )
    return {
        "records": rows,
        "summary": summary,
        "page": page,
        "total": total["n"],
        "pages": max(1, -(-total["n"] // per_page)),
    }


@router.get("/consent/{email}/current")
async def current_consent(
    email: str, user: CurrentUser = Depends(require_perm("compliance.view"))
) -> dict:
    """Latest state per purpose for one subject.

    "Latest wins" is what makes a withdrawal effective: the table is
    append-only, so the current position is the most recent row.
    """
    address = valid_email(email)
    if not address:
        raise HTTPException(400, "That email address is not valid.")

    scoped = db.TenantDB(user.tenant_id)
    rows = await scoped.fetch(
        """SELECT DISTINCT ON (purpose)
                  purpose, granted, policy_version, source_page, created_at
             FROM consent_records
            WHERE tenant_id = $1 AND subject_hash = $2
            ORDER BY purpose, created_at DESC""",
        subject_hash(address),
    )
    return {"email": address, "consent": rows}


@public_router.post("/api/public/{tenant_slug}/consent", status_code=201)
async def record_consent(
    tenant_slug: str, payload: ConsentRecordInput, request: Request
) -> dict:
    """Cookie banner and consent-checkbox endpoint.

    Unauthenticated: the caller is the public website. Append-only, so a
    later withdrawal never erases the evidence of the earlier grant.
    """
    ip = client_ip(request)
    consent_limiter.check(f"consent:{ip or 'unknown'}")

    tenant = await db.fetch_one(
        "SELECT id FROM tenants WHERE slug = $1 AND is_active", collapse(tenant_slug, 60)
    )
    if not tenant:
        return {"ok": True}  # same answer either way; no tenant enumeration

    address = valid_email(payload.email) if payload.email else None
    purpose = collapse(payload.purpose, 80) or "unspecified"
    evidence = payload.evidence if isinstance(payload.evidence, dict) else {}

    await db.execute(
        """INSERT INTO consent_records
               (tenant_id, subject_email, subject_hash, purpose, granted,
                policy_version, source_page, evidence, ip, user_agent)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9, $10)""",
        tenant["id"], address,
        subject_hash(address) if address else hashlib.sha256(
            f"anon:{ip or ''}".encode()
        ).hexdigest(),
        purpose, bool(payload.granted),
        collapse(payload.policy_version, 40),
        collapse(payload.source_page, 500),
        {k: collapse(str(v), 200) for k, v in list(evidence.items())[:20]},
        db.to_inet(ip), (request.headers.get("user-agent") or "")[:300] or None,
    )
    return {"ok": True}


# ====================================================== subject requests
@router.get("/requests")
async def list_requests(
    status: str | None = Query(default=None, max_length=20),
    user: CurrentUser = Depends(require_perm("compliance.view")),
) -> dict:
    scoped = db.TenantDB(user.tenant_id)
    rows = await scoped.fetch(
        """SELECT r.id, r.kind::text AS kind, r.subject_email::text AS subject_email,
                  r.status::text AS status, r.note, r.result, r.completed_at, r.created_at,
                  u.display_name AS requested_by_name,
                  c.display_name AS completed_by_name
             FROM data_requests r
             LEFT JOIN users u ON u.id = r.requested_by
             LEFT JOIN users c ON c.id = r.completed_by
            WHERE r.tenant_id = $1 AND ($2::text IS NULL OR r.status::text = $2)
            ORDER BY r.status, r.created_at DESC LIMIT 200""",
        status,
    )
    return {"requests": rows}


@router.post("/requests", status_code=201)
async def create_request(
    payload: DataRequestCreate,
    request: Request,
    user: CurrentUser = Depends(require_perm("compliance.manage")),
) -> dict:
    """Log a data-subject request. Working it is a separate, explicit step."""
    address = valid_email(payload.subject_email)
    if not address:
        raise HTTPException(400, "That email address is not valid.")

    scoped = db.TenantDB(user.tenant_id)
    row = await scoped.fetch_one(
        """INSERT INTO data_requests (tenant_id, kind, subject_email, note, requested_by)
           VALUES ($1, $2::request_kind, $3, $4, $5)
           RETURNING id, kind::text AS kind, subject_email::text AS subject_email,
                     status::text AS status, note, created_at""",
        payload.kind, address, keep_lines(payload.note, 1000), user.id,
    )
    row["found"] = await _footprint(scoped, address)
    await events.log_activity(
        user.tenant_id, f"data_request.{payload.kind}", user_id=user.id,
        object_type="data_request", object_id=row["id"],
        meta={"subject": address}, ip=db.to_inet(client_ip(request)),
    )
    return {"request": row}


async def _footprint(scoped: db.TenantDB, email: str) -> dict[str, int]:
    """How much data exists for a subject — shown before acting on it."""
    row = await scoped.fetch_one(
        """SELECT
             (SELECT count(*) FROM leads WHERE tenant_id = $1 AND email = $2)::int AS leads,
             (SELECT count(*) FROM form_submissions s
               JOIN leads l ON l.id = s.lead_id
              WHERE s.tenant_id = $1 AND l.email = $2)::int AS form_submissions,
             (SELECT count(*) FROM subscribers
               WHERE tenant_id = $1 AND email = $2)::int AS subscribers,
             (SELECT count(*) FROM consent_records
               WHERE tenant_id = $1 AND subject_hash = $3)::int AS consent_records,
             (SELECT count(*) FROM conversion_events e
               JOIN leads l ON l.id = e.lead_id
              WHERE e.tenant_id = $1 AND l.email = $2)::int AS conversion_events,
             (SELECT count(*) FROM lead_notes n
               JOIN leads l ON l.id = n.lead_id
              WHERE n.tenant_id = $1 AND l.email = $2)::int AS lead_notes""",
        email, subject_hash(email),
    )
    return dict(row)


@router.get("/requests/{request_id}/export")
async def export_subject_data(
    request_id: int,
    request: Request,
    user: CurrentUser = Depends(require_perm("compliance.manage")),
) -> StreamingResponse:
    """Everything held about a subject, as a single JSON download.

    JSON rather than CSV: the record set spans six tables with nested
    fields, and a flattened CSV would either lose structure or need six
    files.
    """
    scoped = db.TenantDB(user.tenant_id)
    row = await scoped.fetch_one(
        """SELECT id, subject_email::text AS subject_email, kind::text AS kind
             FROM data_requests WHERE tenant_id = $1 AND id = $2""",
        request_id,
    )
    if not row:
        raise HTTPException(404, "That request no longer exists.")

    email = row["subject_email"]
    bundle = {
        "subject": email,
        "site": user.tenant_slug,
        "requestId": request_id,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "leads": await scoped.fetch(
            """SELECT id, full_name, email::text AS email, phone, company, message, extra,
                      status::text AS status, source_page, referrer, utm_source, utm_medium,
                      utm_campaign, follow_up_on, value_amount::float8 AS value_amount,
                      created_at, updated_at
                 FROM leads WHERE tenant_id = $1 AND email = $2 ORDER BY created_at""",
            email,
        ),
        "leadNotes": await scoped.fetch(
            """SELECT n.id, n.body, n.created_at, u.display_name AS author
                 FROM lead_notes n
                 JOIN leads l ON l.id = n.lead_id
                 LEFT JOIN users u ON u.id = n.user_id
                WHERE n.tenant_id = $1 AND l.email = $2 ORDER BY n.created_at""",
            email,
        ),
        "formSubmissions": await scoped.fetch(
            """SELECT s.id, s.payload, s.created_at, f.name AS form_name
                 FROM form_submissions s
                 JOIN leads l ON l.id = s.lead_id
                 LEFT JOIN forms f ON f.id = s.form_id
                WHERE s.tenant_id = $1 AND l.email = $2 ORDER BY s.created_at""",
            email,
        ),
        "subscriptions": await scoped.fetch(
            """SELECT id, email::text AS email, name, status::text AS status, source, tags,
                      confirmed_at, unsubscribed_at, created_at
                 FROM subscribers WHERE tenant_id = $1 AND email = $2""",
            email,
        ),
        "consentRecords": await scoped.fetch(
            """SELECT id, purpose, granted, policy_version, source_page, evidence, created_at
                 FROM consent_records WHERE tenant_id = $1 AND subject_hash = $2
                ORDER BY created_at""",
            subject_hash(email),
        ),
        "conversionEvents": await scoped.fetch(
            """SELECT e.id, e.kind, e.name, e.label, e.source_page, e.created_at
                 FROM conversion_events e
                 JOIN leads l ON l.id = e.lead_id
                WHERE e.tenant_id = $1 AND l.email = $2 ORDER BY e.created_at""",
            email,
        ),
    }

    await scoped.execute(
        """UPDATE data_requests
              SET status = 'complete', completed_at = now(), completed_by = $3,
                  result = $4::jsonb
            WHERE tenant_id = $1 AND id = $2""",
        request_id, user.id,
        {key: len(value) for key, value in bundle.items() if isinstance(value, list)},
    )
    await events.log_activity(
        user.tenant_id, "data_request.exported", user_id=user.id,
        object_type="data_request", object_id=request_id,
        meta={"subject": email}, ip=db.to_inet(client_ip(request)),
    )

    body = json.dumps(bundle, indent=2, default=str)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    safe = "".join(ch if ch.isalnum() else "-" for ch in email)[:60]
    return StreamingResponse(
        iter([body]),
        media_type="application/json",
        headers={
            "content-disposition": f'attachment; filename="subject-data-{safe}-{stamp}.json"',
            "cache-control": "no-store",
        },
    )


@router.post("/requests/{request_id}/erase")
async def erase_subject_data(
    request_id: int,
    request: Request,
    confirm: bool = Query(default=False),
    mode: str = Query(default="anonymize"),
    user: CurrentUser = Depends(require_perm("compliance.manage")),
) -> dict:
    """Work a deletion request.

    ``mode=anonymize`` (default) strips identifiers but keeps the row,
    so pipeline history and revenue reporting survive an erasure —
    which is both the usual legal position and the one that does not
    silently rewrite last quarter's numbers. ``mode=delete`` removes
    the rows outright.
    """
    if not confirm:
        raise HTTPException(400, "Erasing subject data needs confirm=true.")
    if mode not in {"anonymize", "delete"}:
        raise HTTPException(400, "Mode must be anonymize or delete.")

    scoped = db.TenantDB(user.tenant_id)
    row = await scoped.fetch_one(
        """SELECT id, subject_email::text AS subject_email, kind::text AS kind, status::text AS status
             FROM data_requests WHERE tenant_id = $1 AND id = $2""",
        request_id,
    )
    if not row:
        raise HTTPException(404, "That request no longer exists.")
    if row["kind"] != "deletion":
        raise HTTPException(400, "Only a deletion request can be erased.")
    if row["status"] == "complete":
        raise HTTPException(400, "That request has already been completed.")

    email = row["subject_email"]
    before = await _footprint(scoped, email)
    result: dict[str, int] = {}

    if mode == "delete":
        for table, clause in (
            ("lead_notes", "lead_id IN (SELECT id FROM leads WHERE tenant_id = $1 AND email = $2)"),
            ("form_submissions",
             "lead_id IN (SELECT id FROM leads WHERE tenant_id = $1 AND email = $2)"),
            ("conversion_events",
             "lead_id IN (SELECT id FROM leads WHERE tenant_id = $1 AND email = $2)"),
            ("leads", "email = $2"),
            ("subscribers", "email = $2"),
        ):
            deleted = await scoped.fetch(
                f"DELETE FROM {table} WHERE tenant_id = $1 AND {clause} RETURNING id", email
            )
            result[table] = len(deleted)
    else:
        anonymised = await scoped.fetch(
            """UPDATE leads
                  SET full_name = 'Erased at the subject''s request',
                      email = NULL, phone = NULL, company = NULL,
                      message = NULL, extra = '{}'::jsonb,
                      ip = NULL, user_agent = NULL,
                      referrer = NULL, source_page = NULL
                WHERE tenant_id = $1 AND email = $2 RETURNING id""",
            email,
        )
        result["leads_anonymized"] = len(anonymised)

        if anonymised:
            ids = [lead["id"] for lead in anonymised]
            # The payload is the identifying part of a submission; the
            # row itself is kept so the form's counts stay correct.
            wiped = await scoped.fetch(
                """UPDATE form_submissions
                      SET payload = '{"erased": true}'::jsonb, ip = NULL, user_agent = NULL
                    WHERE tenant_id = $1 AND lead_id = ANY($2::bigint[]) RETURNING id""",
                ids,
            )
            result["form_submissions_anonymized"] = len(wiped)
            notes = await scoped.fetch(
                "DELETE FROM lead_notes WHERE tenant_id = $1 AND lead_id = ANY($2::bigint[]) RETURNING id",
                ids,
            )
            result["lead_notes_deleted"] = len(notes)

        subs = await scoped.fetch(
            "DELETE FROM subscribers WHERE tenant_id = $1 AND email = $2 RETURNING id", email
        )
        result["subscribers_deleted"] = len(subs)

    # Consent records are kept either way, and deliberately: they are
    # the evidence that the erasure itself was lawful and requested.
    await scoped.execute(
        """INSERT INTO consent_records (tenant_id, subject_email, subject_hash, purpose,
                                        granted, evidence)
           VALUES ($1, NULL, $2, 'data.erasure', FALSE, $3::jsonb)""",
        subject_hash(email), {"mode": mode, "request_id": request_id, "result": result},
    )
    await scoped.execute(
        """UPDATE data_requests
              SET status = 'complete', completed_at = now(), completed_by = $3, result = $4::jsonb
            WHERE tenant_id = $1 AND id = $2""",
        request_id, user.id, {"mode": mode, "before": before, "affected": result},
    )
    await events.log_activity(
        user.tenant_id, "data_request.erased", user_id=user.id,
        object_type="data_request", object_id=request_id,
        meta={"mode": mode, "affected": result}, ip=db.to_inet(client_ip(request)),
    )
    return {"ok": True, "mode": mode, "before": before, "affected": result}


@router.get("/lookup")
async def lookup_subject(
    email: str = Query(max_length=254),
    user: CurrentUser = Depends(require_perm("compliance.view")),
) -> dict:
    """What is held about an address, without creating a request."""
    address = valid_email(email)
    if not address:
        raise HTTPException(400, "That email address is not valid.")
    scoped = db.TenantDB(user.tenant_id)
    return {
        "email": address,
        "found": await _footprint(scoped, address),
        "consent": await scoped.fetch(
            """SELECT DISTINCT ON (purpose) purpose, granted, created_at
                 FROM consent_records WHERE tenant_id = $1 AND subject_hash = $2
                ORDER BY purpose, created_at DESC""",
            subject_hash(address),
        ),
    }


# ============================================================= retention
@router.get("/retention")
async def list_retention(scoped: db.TenantDB = Depends(tenant_db)) -> dict:
    rows = await scoped.fetch(
        """SELECT p.scope, p.days, p.action, p.is_active, p.last_run_at, p.last_affected,
                  u.display_name AS updated_by_name, p.updated_at
             FROM retention_policies p LEFT JOIN users u ON u.id = p.updated_by
            WHERE p.tenant_id = $1 ORDER BY p.scope"""
    )
    configured = {row["scope"] for row in rows}
    return {
        "policies": rows,
        "scopes": list(RETENTION_SCOPES),
        "missing": [scope for scope in RETENTION_SCOPES if scope not in configured],
        "anonymizable": sorted(ANONYMIZABLE),
    }


@router.put("/retention")
async def put_retention(
    payload: RetentionPolicyUpdate,
    request: Request,
    user: CurrentUser = Depends(require_perm("compliance.manage")),
) -> dict:
    if payload.scope not in RETENTION_SCOPES:
        raise HTTPException(400, f"Scope must be one of: {', '.join(RETENTION_SCOPES)}")
    if payload.action == "anonymize" and payload.scope not in ANONYMIZABLE:
        raise HTTPException(
            400, f"“{payload.scope}” can only be deleted, not anonymized."
        )

    scoped = db.TenantDB(user.tenant_id)
    row = await scoped.fetch_one(
        """INSERT INTO retention_policies (tenant_id, scope, days, action, is_active, updated_by)
           VALUES ($1, $2, $3, $4, $5, $6)
           ON CONFLICT (tenant_id, scope) DO UPDATE
              SET days = EXCLUDED.days, action = EXCLUDED.action,
                  is_active = EXCLUDED.is_active, updated_by = EXCLUDED.updated_by,
                  updated_at = now()
           RETURNING scope, days, action, is_active, last_run_at, last_affected, updated_at""",
        payload.scope, payload.days, payload.action, payload.is_active, user.id,
    )
    await events.log_activity(
        user.tenant_id, "retention.updated", user_id=user.id,
        meta={"scope": payload.scope, "days": payload.days, "action": payload.action,
              "active": payload.is_active},
        ip=db.to_inet(client_ip(request)),
    )
    return {"policy": row}


@router.post("/retention/preview")
async def preview_retention(
    scope: str = Query(max_length=40),
    days: int = Query(ge=1, le=36500),
    user: CurrentUser = Depends(require_perm("compliance.view")),
) -> dict:
    """How many rows a policy would affect — before turning it on."""
    if scope not in RETENTION_SCOPES:
        raise HTTPException(400, f"Scope must be one of: {', '.join(RETENTION_SCOPES)}")
    scoped = db.TenantDB(user.tenant_id)
    row = await scoped.fetch_one(
        f"""SELECT count(*)::int AS n FROM {scope}
             WHERE tenant_id = $1 AND {_age_column(scope)} < now() - make_interval(days => $2)""",
        days,
    )
    return {"scope": scope, "days": days, "wouldAffect": row["n"]}


def _age_column(scope: str) -> str:
    """The timestamp each scope is aged by. visitor_days is a DATE."""
    return {
        "leads": "created_at",
        "form_submissions": "created_at",
        "activity_log": "created_at",
        "conversion_events": "created_at",
        "not_found_log": "last_seen_at",
        "error_log": "last_seen_at",
        "visitor_days": "day",
        "notifications": "created_at",
    }[scope]


@router.post("/retention/run")
async def run_retention_now(
    request: Request,
    user: CurrentUser = Depends(require_perm("compliance.manage")),
) -> dict:
    """Apply this site's active policies immediately."""
    result = await apply_retention(user.tenant_id)
    await events.log_activity(
        user.tenant_id, "retention.run", user_id=user.id,
        meta=result, ip=db.to_inet(client_ip(request)),
    )
    return {"ok": True, "affected": result}


async def apply_retention(tenant_id: int) -> dict[str, int]:
    """Enforce every active policy for one tenant. Called by the worker."""
    policies = await db.fetch(
        """SELECT scope, days, action FROM retention_policies
            WHERE tenant_id = $1 AND is_active""",
        tenant_id,
    )
    affected: dict[str, int] = {}

    for policy in policies:
        scope, days, action = policy["scope"], policy["days"], policy["action"]
        column = _age_column(scope)
        try:
            if action == "anonymize" and scope == "leads":
                rows = await db.fetch(
                    f"""UPDATE leads
                           SET full_name = 'Removed by retention policy',
                               email = NULL, phone = NULL, company = NULL, message = NULL,
                               extra = '{{}}'::jsonb, ip = NULL, user_agent = NULL,
                               referrer = NULL
                         WHERE tenant_id = $1 AND {column} < now() - make_interval(days => $2)
                           AND email IS NOT NULL
                         RETURNING id""",
                    tenant_id, days,
                )
            elif action == "anonymize" and scope == "form_submissions":
                rows = await db.fetch(
                    f"""UPDATE form_submissions
                           SET payload = '{{"erased": true}}'::jsonb, ip = NULL, user_agent = NULL
                         WHERE tenant_id = $1 AND {column} < now() - make_interval(days => $2)
                           AND payload <> '{{"erased": true}}'::jsonb
                         RETURNING id""",
                    tenant_id, days,
                )
            else:
                rows = await db.fetch(
                    f"""DELETE FROM {scope}
                         WHERE tenant_id = $1 AND {column} < now() - make_interval(days => $2)
                         RETURNING {"tenant_id" if scope == "visitor_days" else "id"}""",
                    tenant_id, days,
                )
            affected[f"{scope}:{action}"] = len(rows)
            await db.execute(
                """UPDATE retention_policies
                      SET last_run_at = now(), last_affected = $3
                    WHERE tenant_id = $1 AND scope = $2""",
                tenant_id, scope, len(rows),
            )
        except Exception as exc:
            log.error("retention %s failed for tenant %s: %s", scope, tenant_id, exc)
            await events.log_error(
                f"retention sweep failed for {scope}: {exc}",
                tenant_id=tenant_id, source="retention",
            )

    return affected
