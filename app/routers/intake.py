"""Public lead intake — the endpoint your static frontends POST to.

    POST /api/public/{tenant_slug}/forms/{form_slug}

Defences in order of cheapness: CORS allow-list (middleware), per-IP rate
limit, honeypot, minimum fill time, field caps from the form definition.
Turnstile slots in at _verify_captcha once you have keys.
"""

import logging
import time

import httpx
from fastapi import APIRouter, HTTPException, Request

from .. import db, events
from ..config import settings
from ..ratelimit import intake_limiter
from ..schemas import IntakeRequest, collapse, keep_lines, valid_email
from ..security import client_ip

log = logging.getLogger("crm.intake")

router = APIRouter(prefix="/api/public", tags=["intake"])

MIN_FILL_MS = 2500  # humans take longer than this
CORE_FIELDS = {"full_name", "email", "phone", "company", "message"}
TURNSTILE_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"


async def _verify_captcha(token: str | None, ip: str | None) -> bool:
    """True when no provider is configured, so local dev still works."""
    if not settings.turnstile_secret:
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
    intake_limiter.check(ip or "unknown")

    form = await db.fetch_one(
        """SELECT f.id, f.fields, f.notify_emails, t.id AS tenant_id
             FROM forms f JOIN tenants t ON t.id = f.tenant_id
            WHERE t.slug = $1 AND t.is_active AND f.slug = $2 AND f.is_active""",
        collapse(tenant_slug, 60),
        collapse(form_slug, 60),
    )
    if not form:
        raise HTTPException(404, "This form is not accepting submissions.")

    # ---- bot checks ---------------------------------------------------
    honeypot_filled = bool(collapse(payload.hp, 10))
    started_at = payload.rendered_at or 0
    too_fast = started_at > 0 and (time.time() * 1000 - started_at) < MIN_FILL_MS

    if not await _verify_captcha(payload.captcha, ip):
        raise HTTPException(400, "We could not verify that submission. Please try again.")

    # ---- validate against the stored form definition ------------------
    raw = payload.model_dump(by_alias=False)
    extras = payload.model_extra or {}
    fields = form["fields"] if isinstance(form["fields"], list) else []

    values: dict[str, str | None] = {}
    missing: list[str] = []

    for field in fields:
        name = field.get("name")
        if not name:
            continue
        source = raw.get(name, extras.get(name))
        limit = int(field.get("max") or (4000 if field.get("type") == "textarea" else 200))

        if field.get("type") == "email":
            value = valid_email(source)
        elif field.get("type") == "textarea":
            value = keep_lines(source, limit)
        else:
            value = collapse(source, limit)

        if field.get("required") and not value:
            missing.append(field.get("label") or name)
        values[name] = value

    if missing:
        raise HTTPException(400, f"Please fill in: {', '.join(missing)}")

    # Anything the form defines beyond the core columns lands in `extra`.
    extra = {k: v for k, v in values.items() if k not in CORE_FIELDS and v is not None}
    meta = payload.meta
    is_spam = honeypot_filled or too_fast

    lead = await db.fetch_one(
        """INSERT INTO leads (
               tenant_id, form_id, full_name, email, phone, company, message, extra,
               source_page, referrer, utm_source, utm_medium, utm_campaign,
               utm_term, utm_content, ip, user_agent, is_spam)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9, $10, $11, $12, $13,
                   $14, $15, $16, $17, $18)
           RETURNING id, full_name, email, created_at, is_spam""",
        form["tenant_id"],
        form["id"],
        values.get("full_name") or "Unnamed lead",
        values.get("email"),
        values.get("phone"),
        values.get("company"),
        values.get("message"),
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

    await events.log_activity(
        form["tenant_id"],
        "lead.created",
        object_type="lead",
        object_id=lead["id"],
        meta={"form": form_slug, "spam": is_spam, "utm_source": meta.utm_source},
        ip=db.to_inet(ip),
    )

    # Quarantined submissions never reach the pipeline view or the CRM.
    if not is_spam:
        await events.emit(
            form["tenant_id"],
            "lead.created",
            {
                "id": lead["id"],
                "form": form_slug,
                "full_name": lead["full_name"],
                "email": lead["email"],
                "phone": values.get("phone"),
                "company": values.get("company"),
                "message": values.get("message"),
                "extra": extra,
                "utm": {
                    "source": meta.utm_source,
                    "medium": meta.utm_medium,
                    "campaign": meta.utm_campaign,
                    "term": meta.utm_term,
                    "content": meta.utm_content,
                },
                "source_page": meta.source_page,
                "created_at": lead["created_at"],
            },
        )

    # Identical response either way, so a bot cannot learn it was flagged.
    return {"ok": True, "message": "Thanks — we will be in touch shortly."}
