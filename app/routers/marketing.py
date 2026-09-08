"""Marketing & newsletter (2.8).

Subscribers with double opt-in, campaigns, announcement bars/popups and
the UTM link builder.

Campaign sending reuses the existing ``email_outbox`` worker rather than
introducing a second delivery path: a campaign fans out into one outbox
row per recipient plus a ``campaign_recipients`` row for reporting, so
retries, backoff and provider configuration are the same code that
sends a password reset. That keeps the platform provider-agnostic —
switching to SES, Brevo or SendGrid is an SMTP setting, not a rewrite.
"""

from __future__ import annotations

import csv
import io
import logging
import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from .. import db, events, mail, templating
from ..permissions import require_perm
from ..ratelimit import RateLimiter
from ..sanitize import clean_html
from ..schemas import (
    AnnouncementCreate,
    AnnouncementUpdate,
    CampaignCreate,
    CampaignSchedule,
    CampaignUpdate,
    PublicSubscribe,
    SubscriberCreate,
    SubscriberImport,
    SubscriberUpdate,
    UtmLinkCreate,
    collapse,
    keep_lines,
    valid_email,
)
from ..security import CurrentUser, client_ip

log = logging.getLogger("crm.marketing")

router = APIRouter(prefix="/api/marketing", tags=["marketing"])
public_router = APIRouter(tags=["marketing-public"])

subscribe_limiter = RateLimiter(max_requests=5, window_seconds=600)

MAX_IMPORT_ROWS = 20_000


# =========================================================== subscribers
@router.get("/subscribers")
async def list_subscribers(
    q: str | None = Query(default=None, max_length=120),
    status: str | None = Query(default=None, max_length=20),
    tag: str | None = Query(default=None, max_length=40),
    page: int = Query(default=1, ge=1, le=1000),
    per_page: int = Query(default=50, ge=10, le=200),
    user: CurrentUser = Depends(require_perm("marketing.view")),
) -> dict:
    scoped = db.TenantDB(user.tenant_id)
    offset = (page - 1) * per_page
    rows = await scoped.fetch(
        """SELECT id, email::text AS email, name, status::text AS status, source, tags,
                  confirmed_at, unsubscribed_at, created_at
             FROM subscribers
            WHERE tenant_id = $1
              AND ($2::text IS NULL OR email::text ILIKE '%' || $2 || '%'
                                    OR name ILIKE '%' || $2 || '%')
              AND ($3::text IS NULL OR status::text = $3)
              AND ($4::text IS NULL OR $4 = ANY(tags))
            ORDER BY created_at DESC LIMIT $5 OFFSET $6""",
        q.strip() if q else None, status, tag.strip().lower() if tag else None,
        per_page, offset,
    )
    counts = await scoped.fetch(
        """SELECT status::text AS status, count(*)::int AS n
             FROM subscribers WHERE tenant_id = $1 GROUP BY 1"""
    )
    tags = await scoped.fetch(
        """SELECT tag, count(*)::int AS n FROM subscribers, unnest(tags) AS tag
            WHERE tenant_id = $1 GROUP BY tag ORDER BY n DESC LIMIT 100"""
    )
    total = sum(row["n"] for row in counts)
    return {
        "subscribers": rows,
        "counts": {row["status"]: row["n"] for row in counts},
        "tags": tags,
        "page": page,
        "total": total,
        "pages": max(1, -(-total // per_page)),
    }


@router.post("/subscribers", status_code=201)
async def add_subscriber(
    payload: SubscriberCreate,
    user: CurrentUser = Depends(require_perm("subscribers.manage")),
) -> dict:
    """Admin-added subscriber. `confirmed` skips double opt-in, which is
    only defensible when the admin has consent from elsewhere."""
    address = valid_email(payload.email)
    if not address:
        raise HTTPException(400, "That email address is not valid.")

    scoped = db.TenantDB(user.tenant_id)
    row = await scoped.fetch_one(
        """INSERT INTO subscribers (tenant_id, email, name, status, source, tags, confirmed_at)
           VALUES ($1, $2, $3, $4::subscriber_status, $5, $6,
                   CASE WHEN $7 THEN now() END)
           ON CONFLICT (tenant_id, email) DO UPDATE
              SET name = coalesce(EXCLUDED.name, subscribers.name),
                  tags = (SELECT array_agg(DISTINCT t)
                            FROM unnest(subscribers.tags || EXCLUDED.tags) AS t)
           RETURNING id, email::text AS email, name, status::text AS status, tags,
                     confirmed_at, created_at""",
        address, collapse(payload.name, 120),
        "subscribed" if payload.confirmed else "pending",
        collapse(payload.source, 80) or "admin",
        _clean_tags(payload.tags),
        payload.confirmed,
    )
    await events.log_activity(
        user.tenant_id, "subscriber.created", user_id=user.id,
        object_type="subscriber", object_id=row["id"], meta={"email": address},
    )
    return {"subscriber": row}


def _clean_tags(raw) -> list[str]:
    if not raw:
        return []
    seen: dict[str, None] = {}
    for entry in (raw if isinstance(raw, list) else str(raw).split(","))[:25]:
        tag = collapse(str(entry), 40)
        if tag:
            seen.setdefault(tag.lower(), None)
    return list(seen)


@router.patch("/subscribers/{subscriber_id}")
async def update_subscriber(
    subscriber_id: int,
    payload: SubscriberUpdate,
    user: CurrentUser = Depends(require_perm("subscribers.manage")),
) -> dict:
    scoped = db.TenantDB(user.tenant_id)
    sent = payload.model_dump(exclude_unset=True)
    if not sent:
        raise HTTPException(400, "Nothing to save.")

    row = await scoped.fetch_one(
        """UPDATE subscribers
              SET name = coalesce($3, name),
                  status = coalesce($4::subscriber_status, status),
                  tags = CASE WHEN $5 THEN $6::text[] ELSE tags END,
                  confirmed_at = CASE WHEN $4 = 'subscribed' AND confirmed_at IS NULL
                                      THEN now() ELSE confirmed_at END,
                  unsubscribed_at = CASE WHEN $4 = 'unsubscribed' THEN now()
                                         ELSE unsubscribed_at END
            WHERE tenant_id = $1 AND id = $2
            RETURNING id, email::text AS email, name, status::text AS status, tags,
                      confirmed_at, unsubscribed_at, created_at""",
        subscriber_id, collapse(payload.name, 120),
        payload.status.value if payload.status else None,
        "tags" in sent, _clean_tags(payload.tags),
    )
    if not row:
        raise HTTPException(404, "That subscriber no longer exists.")
    return {"subscriber": row}


@router.delete("/subscribers/{subscriber_id}")
async def delete_subscriber(
    subscriber_id: int, user: CurrentUser = Depends(require_perm("subscribers.manage"))
) -> dict:
    scoped = db.TenantDB(user.tenant_id)
    removed = await scoped.fetch(
        "DELETE FROM subscribers WHERE tenant_id = $1 AND id = $2 RETURNING email::text AS email",
        subscriber_id,
    )
    if not removed:
        raise HTTPException(404, "That subscriber no longer exists.")
    return {"ok": True}


@router.post("/subscribers/import")
async def import_subscribers(
    payload: SubscriberImport,
    user: CurrentUser = Depends(require_perm("subscribers.manage")),
) -> dict:
    """Import pasted CSV or newline-separated 'email,name' rows.

    Reports what it skipped rather than failing the whole import on one
    bad row — a 5,000-row paste with three typos should still land.
    """
    reader = csv.reader(io.StringIO(payload.rows))
    tags = _clean_tags(payload.tags)

    added = updated = skipped = 0
    invalid: list[str] = []
    scoped = db.TenantDB(user.tenant_id)

    for index, row in enumerate(reader):
        if index >= MAX_IMPORT_ROWS:
            break
        if not row:
            continue
        # A header line is the most common first row. Test it before
        # validating, or "email" is reported as an invalid address.
        if index == 0 and row[0].strip().lower() in {"email", "email address", "e-mail"}:
            continue

        address = valid_email(row[0])
        if not address:
            skipped += 1
            if len(invalid) < 10 and row[0].strip():
                invalid.append(row[0].strip()[:80])
            continue

        name = collapse(row[1], 120) if len(row) > 1 else None
        result = await scoped.fetch_one(
            """INSERT INTO subscribers (tenant_id, email, name, status, source, tags, confirmed_at)
               VALUES ($1, $2, $3, $4::subscriber_status, 'import', $5,
                       CASE WHEN $6 THEN now() END)
               ON CONFLICT (tenant_id, email) DO UPDATE
                  SET name = coalesce(EXCLUDED.name, subscribers.name),
                      tags = (SELECT array_agg(DISTINCT t)
                                FROM unnest(subscribers.tags || EXCLUDED.tags) AS t)
               RETURNING (created_at = updated_at) AS is_new""",
            address, name,
            "subscribed" if payload.confirmed else "pending",
            tags, payload.confirmed,
        )
        if result and result["is_new"]:
            added += 1
        else:
            updated += 1

    await events.log_activity(
        user.tenant_id, "subscribers.imported", user_id=user.id,
        meta={"added": added, "updated": updated, "skipped": skipped},
    )
    return {
        "added": added, "updated": updated, "skipped": skipped,
        "invalidSamples": invalid,
        "confirmed": payload.confirmed,
    }


@router.get("/subscribers/export")
async def export_subscribers(
    status: str | None = Query(default=None, max_length=20),
    user: CurrentUser = Depends(require_perm("subscribers.manage")),
) -> StreamingResponse:
    scoped = db.TenantDB(user.tenant_id)
    rows = await scoped.fetch(
        """SELECT email::text AS email, name, status::text AS status, source,
                  array_to_string(tags, '|') AS tags, confirmed_at, created_at
             FROM subscribers
            WHERE tenant_id = $1 AND ($2::text IS NULL OR status::text = $2)
            ORDER BY created_at DESC LIMIT 100000""",
        status,
    )
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["email", "name", "status", "source", "tags", "confirmed_at", "created_at"])
    for row in rows:
        writer.writerow(
            [
                row["email"], row["name"] or "", row["status"], row["source"] or "",
                row["tags"] or "",
                row["confirmed_at"].isoformat() if row["confirmed_at"] else "",
                row["created_at"].isoformat(),
            ]
        )
    buffer.seek(0)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={"content-disposition": f'attachment; filename="subscribers-{stamp}.csv"'},
    )


# ============================================================= campaigns
CAMPAIGN_COLUMNS = """id, name, subject, preheader, body_text, body_html, from_name,
                      from_email::text AS from_email, audience, status::text AS status,
                      scheduled_for, sent_at, stats, created_at, updated_at"""


@router.get("/campaigns")
async def list_campaigns(user: CurrentUser = Depends(require_perm("marketing.view"))) -> dict:
    scoped = db.TenantDB(user.tenant_id)
    rows = await scoped.fetch(
        f"""SELECT {CAMPAIGN_COLUMNS.replace("body_text,", "left(body_text, 200) AS body_text,")},
                   (SELECT count(*) FROM campaign_recipients r
                     WHERE r.campaign_id = campaigns.id)::int AS recipient_count
              FROM campaigns WHERE tenant_id = $1 ORDER BY created_at DESC"""
    )
    return {"campaigns": rows}


@router.post("/campaigns", status_code=201)
async def create_campaign(
    payload: CampaignCreate, user: CurrentUser = Depends(require_perm("marketing.manage"))
) -> dict:
    scoped = db.TenantDB(user.tenant_id)
    row = await scoped.fetch_one(
        f"""INSERT INTO campaigns (tenant_id, name, subject, preheader, body_text, body_html,
                                   from_name, from_email, audience, created_by)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10)
            RETURNING {CAMPAIGN_COLUMNS}""",
        collapse(payload.name, 120), collapse(payload.subject, 300),
        collapse(payload.preheader, 200), keep_lines(payload.body_text, 200_000),
        _clean_campaign_html(payload.body_html),
        collapse(payload.from_name, 120), valid_email(payload.from_email),
        _clean_audience(payload.audience), user.id,
    )
    return {"campaign": {**row, "recipient_count": 0}}


def _clean_campaign_html(raw: str | None) -> str | None:
    if raw is None:
        return None
    try:
        return clean_html(raw, limit=400_000)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


def _clean_audience(raw) -> dict:
    source = raw if isinstance(raw, dict) else {}
    status = collapse(source.get("status"), 20) or "subscribed"
    if status not in {"subscribed", "pending", "all"}:
        raise HTTPException(400, "Audience status must be subscribed, pending or all.")
    return {
        "status": status,
        "tags": _clean_tags(source.get("tags")),
        "exclude_tags": _clean_tags(source.get("exclude_tags")),
    }


@router.get("/campaigns/{campaign_id}")
async def campaign_detail(
    campaign_id: int, user: CurrentUser = Depends(require_perm("marketing.view"))
) -> dict:
    scoped = db.TenantDB(user.tenant_id)
    row = await scoped.fetch_one(
        f"SELECT {CAMPAIGN_COLUMNS} FROM campaigns WHERE tenant_id = $1 AND id = $2",
        campaign_id,
    )
    if not row:
        raise HTTPException(404, "That campaign no longer exists.")

    row["audienceSize"] = await _audience_size(scoped, row["audience"])
    row["delivery"] = await scoped.fetch(
        """SELECT status::text AS status, count(*)::int AS n
             FROM campaign_recipients
            WHERE tenant_id = $1 AND campaign_id = $2 GROUP BY 1""",
        campaign_id,
    )
    return {"campaign": row}


def _audience_clause(audience: dict) -> tuple[str, list]:
    """Shared WHERE for counting and for selecting recipients — one
    definition, so the preview count cannot disagree with the send."""
    audience = audience or {}
    clauses = ["tenant_id = $1", "unsubscribed_at IS NULL"]
    args: list = []

    status = audience.get("status", "subscribed")
    if status != "all":
        args.append(status)
        clauses.append(f"status = ${len(args) + 1}::subscriber_status")
    else:
        clauses.append("status IN ('subscribed', 'pending')")

    if audience.get("tags"):
        args.append(audience["tags"])
        clauses.append(f"tags && ${len(args) + 1}::text[]")
    if audience.get("exclude_tags"):
        args.append(audience["exclude_tags"])
        clauses.append(f"NOT (tags && ${len(args) + 1}::text[])")

    return " AND ".join(clauses), args


async def _audience_size(scoped: db.TenantDB, audience: dict) -> int:
    clause, args = _audience_clause(audience)
    row = await scoped.fetch_one(
        f"SELECT count(*)::int AS n FROM subscribers WHERE {clause}", *args
    )
    return row["n"]


@router.patch("/campaigns/{campaign_id}")
async def update_campaign(
    campaign_id: int,
    payload: CampaignUpdate,
    user: CurrentUser = Depends(require_perm("marketing.manage")),
) -> dict:
    scoped = db.TenantDB(user.tenant_id)
    existing = await scoped.fetch_one(
        "SELECT status::text AS status FROM campaigns WHERE tenant_id = $1 AND id = $2",
        campaign_id,
    )
    if not existing:
        raise HTTPException(404, "That campaign no longer exists.")
    if existing["status"] in {"sending", "sent"}:
        raise HTTPException(400, "A campaign that has been sent cannot be edited.")

    sent = payload.model_dump(exclude_unset=True)
    if not sent:
        raise HTTPException(400, "Nothing to save.")

    row = await scoped.fetch_one(
        f"""UPDATE campaigns
               SET name = coalesce($3, name),
                   subject = coalesce($4, subject),
                   preheader = CASE WHEN $5 THEN $6 ELSE preheader END,
                   body_text = coalesce($7, body_text),
                   body_html = CASE WHEN $8 THEN $9 ELSE body_html END,
                   from_name = CASE WHEN $10 THEN $11 ELSE from_name END,
                   from_email = CASE WHEN $12 THEN $13 ELSE from_email END,
                   audience = CASE WHEN $14 THEN $15::jsonb ELSE audience END
             WHERE tenant_id = $1 AND id = $2 RETURNING {CAMPAIGN_COLUMNS}""",
        campaign_id,
        collapse(payload.name, 120), collapse(payload.subject, 300),
        "preheader" in sent, collapse(payload.preheader, 200),
        keep_lines(payload.body_text, 200_000),
        "body_html" in sent, _clean_campaign_html(payload.body_html),
        "from_name" in sent, collapse(payload.from_name, 120),
        "from_email" in sent, valid_email(payload.from_email),
        "audience" in sent, _clean_audience(payload.audience),
    )
    return {"campaign": row}


@router.post("/campaigns/{campaign_id}/test")
async def test_campaign(
    campaign_id: int,
    user: CurrentUser = Depends(require_perm("marketing.manage")),
) -> dict:
    """Send the campaign to the requester only."""
    scoped = db.TenantDB(user.tenant_id)
    row = await scoped.fetch_one(
        "SELECT subject, body_text, body_html FROM campaigns WHERE tenant_id = $1 AND id = $2",
        campaign_id,
    )
    if not row:
        raise HTTPException(404, "That campaign no longer exists.")

    context = _campaign_context(user.tenant_name, {"email": user.email, "name": user.name}, None)
    queued = await mail.enqueue(
        user.tenant_id, [user.email],
        f"[TEST] {templating.render_text(row['subject'], context)}",
        templating.render_text(row["body_text"], context),
        kind="campaign.test",
    )
    return {"ok": True, "queued": queued, "to": user.email}


def _campaign_context(site_name: str, subscriber: dict, unsubscribe_url: str | None) -> dict:
    name = subscriber.get("name") or ""
    return {
        "site": {"name": site_name},
        "subscriber": {
            "name": name,
            "name_suffix": f" {name}" if name else "",
            "email": subscriber.get("email") or "",
            "unsubscribe_url": unsubscribe_url or "",
        },
    }


@router.post("/campaigns/{campaign_id}/send")
async def send_campaign(
    campaign_id: int,
    payload: CampaignSchedule,
    request: Request,
    user: CurrentUser = Depends(require_perm("campaigns.send")),
) -> dict:
    """Send now, or schedule. Scheduling is a status + timestamp the
    campaign worker picks up, not an in-process timer."""
    scoped = db.TenantDB(user.tenant_id)
    campaign = await scoped.fetch_one(
        f"SELECT {CAMPAIGN_COLUMNS} FROM campaigns WHERE tenant_id = $1 AND id = $2",
        campaign_id,
    )
    if not campaign:
        raise HTTPException(404, "That campaign no longer exists.")
    if campaign["status"] in {"sending", "sent"}:
        raise HTTPException(400, "That campaign has already been sent.")

    size = await _audience_size(scoped, campaign["audience"])
    if not size:
        raise HTTPException(400, "No subscribers match that audience.")

    when = payload.scheduled_for
    if when and when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)

    if when and when > datetime.now(timezone.utc):
        await scoped.execute(
            """UPDATE campaigns SET status = 'scheduled', scheduled_for = $3
                WHERE tenant_id = $1 AND id = $2""",
            campaign_id, when,
        )
        await events.log_activity(
            user.tenant_id, "campaign.scheduled", user_id=user.id,
            object_type="campaign", object_id=campaign_id,
            meta={"for": when.isoformat(), "audience": size},
            ip=db.to_inet(client_ip(request)),
        )
        return {"ok": True, "scheduled": True, "scheduledFor": when.isoformat(), "audience": size}

    queued = await dispatch_campaign(user.tenant_id, campaign_id)
    await events.log_activity(
        user.tenant_id, "campaign.sent", user_id=user.id,
        object_type="campaign", object_id=campaign_id, meta={"queued": queued},
        ip=db.to_inet(client_ip(request)),
    )
    return {"ok": True, "scheduled": False, "queued": queued}


async def dispatch_campaign(tenant_id: int, campaign_id: int) -> int:
    """Fan a campaign out into the outbox. Also called by the worker.

    Claims the campaign with a conditional UPDATE first: two workers (or
    a worker and an admin clicking Send) must not both fan it out.
    """
    claimed = await db.fetch_one(
        """UPDATE campaigns SET status = 'sending'
            WHERE tenant_id = $1 AND id = $2 AND status IN ('draft', 'scheduled')
            RETURNING id, name, subject, body_text, body_html, audience""",
        tenant_id, campaign_id,
    )
    if not claimed:
        return 0

    site = await db.fetch_one("SELECT name FROM tenants WHERE id = $1", tenant_id)
    base = _public_base()

    slug = await _tenant_slug(tenant_id)
    clause, args = _audience_clause(claimed["audience"])
    recipients = await db.fetch(
        f"""SELECT id, email::text AS email, name, unsubscribe_token
              FROM subscribers WHERE {clause} LIMIT 100000""",
        tenant_id, *args,
    )

    queued = 0
    for subscriber in recipients:
        unsubscribe = (
            f"{base}/api/v1/{slug}/unsubscribe/{subscriber['unsubscribe_token']}"
            if base else None
        )
        context = _campaign_context(site["name"], subscriber, unsubscribe)
        body = templating.render_text(claimed["body_text"], context)
        if unsubscribe and "unsubscribe" not in body.lower():
            # Bulk email without an unsubscribe link is a spam complaint
            # waiting to happen, and in most jurisdictions unlawful.
            body += f"\n\n---\nUnsubscribe: {unsubscribe}"

        sent = await mail.enqueue(
            tenant_id, [subscriber["email"]],
            templating.render_text(claimed["subject"], context),
            body, kind="campaign",
        )
        await db.execute(
            """INSERT INTO campaign_recipients (tenant_id, campaign_id, subscriber_id, status, sent_at)
               VALUES ($1, $2, $3, $4::delivery_status, now())
               ON CONFLICT (campaign_id, subscriber_id) DO NOTHING""",
            tenant_id, campaign_id, subscriber["id"],
            "pending" if sent else "failed",
        )
        queued += sent

    await db.execute(
        """UPDATE campaigns
              SET status = 'sent', sent_at = now(),
                  stats = jsonb_build_object('recipients', $3::int, 'queued', $4::int)
            WHERE tenant_id = $1 AND id = $2""",
        tenant_id, campaign_id, len(recipients), queued,
    )
    await events.emit(
        tenant_id, "campaign.sent",
        {"id": campaign_id, "name": claimed["name"], "recipients": len(recipients)},
    )
    await events.notify(
        tenant_id, "campaign.sent", f"Campaign “{claimed['name']}” sent",
        body=f"{queued} of {len(recipients)} recipients queued.",
        level="success", link="#/marketing",
    )
    return queued


async def _tenant_slug(tenant_id: int) -> str:
    row = await db.fetch_one("SELECT slug::text AS slug FROM tenants WHERE id = $1", tenant_id)
    return (row or {}).get("slug") or ""


def _public_base() -> str:
    """Absolute origin for links inside campaign email."""
    from ..config import settings  # noqa: PLC0415

    return settings.app_base_url or ""


@router.post("/campaigns/{campaign_id}/cancel")
async def cancel_campaign(
    campaign_id: int, user: CurrentUser = Depends(require_perm("marketing.manage"))
) -> dict:
    scoped = db.TenantDB(user.tenant_id)
    row = await scoped.fetch_one(
        """UPDATE campaigns SET status = 'cancelled', scheduled_for = NULL
            WHERE tenant_id = $1 AND id = $2 AND status = 'scheduled'
            RETURNING id""",
        campaign_id,
    )
    if not row:
        raise HTTPException(400, "Only a scheduled campaign can be cancelled.")
    return {"ok": True}


@router.delete("/campaigns/{campaign_id}")
async def delete_campaign(
    campaign_id: int, user: CurrentUser = Depends(require_perm("marketing.manage"))
) -> dict:
    scoped = db.TenantDB(user.tenant_id)
    removed = await scoped.fetch(
        """DELETE FROM campaigns
            WHERE tenant_id = $1 AND id = $2 AND status <> 'sending' RETURNING id""",
        campaign_id,
    )
    if not removed:
        raise HTTPException(400, "That campaign is missing, or is being sent right now.")
    return {"ok": True}


# ========================================================= announcements
ANNOUNCEMENT_COLUMNS = """id, name, kind, content, placement, priority,
                          starts_at, ends_at, is_active, created_at, updated_at"""


@router.get("/announcements")
async def list_announcements(user: CurrentUser = Depends(require_perm("marketing.view"))) -> dict:
    scoped = db.TenantDB(user.tenant_id)
    rows = await scoped.fetch(
        f"""SELECT {ANNOUNCEMENT_COLUMNS},
                   (is_active
                    AND (starts_at IS NULL OR starts_at <= now())
                    AND (ends_at IS NULL OR ends_at > now())) AS is_live
              FROM announcements WHERE tenant_id = $1
             ORDER BY priority DESC, created_at DESC"""
    )
    return {"announcements": rows}


@router.post("/announcements", status_code=201)
async def create_announcement(
    payload: AnnouncementCreate, user: CurrentUser = Depends(require_perm("marketing.manage"))
) -> dict:
    scoped = db.TenantDB(user.tenant_id)
    row = await scoped.fetch_one(
        f"""INSERT INTO announcements (tenant_id, name, kind, content, placement,
                                       priority, starts_at, ends_at, created_by)
            VALUES ($1, $2, $3, $4::jsonb, $5::jsonb, $6, $7, $8, $9)
            RETURNING {ANNOUNCEMENT_COLUMNS}""",
        collapse(payload.name, 120), payload.kind,
        _clean_announcement_content(payload.content),
        _clean_placement(payload.placement),
        payload.priority, payload.starts_at, payload.ends_at, user.id,
    )
    return {"announcement": row}


def _clean_announcement_content(raw) -> dict:
    source = raw if isinstance(raw, dict) else {}
    href = collapse(source.get("cta_href"), 500)
    if href and not href.startswith(("https://", "http://", "/", "#", "mailto:", "tel:")):
        raise HTTPException(400, "The CTA link is not a valid URL or path.")
    body = source.get("body")
    return {
        "heading": collapse(source.get("heading"), 200),
        # The body renders on the public site, so it is sanitized like
        # any other stored markup.
        "body": clean_html(str(body), limit=5000) if body else None,
        "cta_label": collapse(source.get("cta_label"), 80),
        "cta_href": href,
        "dismissible": bool(source.get("dismissible", True)),
        "theme": collapse(source.get("theme"), 30) or "default",
    }


def _clean_placement(raw) -> dict:
    source = raw if isinstance(raw, dict) else {}
    frequency = collapse(source.get("frequency"), 20) or "session"
    if frequency not in {"always", "session", "once"}:
        raise HTTPException(400, "Frequency must be always, session or once.")

    def paths(key: str) -> list[str]:
        return [
            collapse(str(p), 300)
            for p in (source.get(key) or [])[:50]
            if collapse(str(p), 300)
        ]

    return {
        "paths": paths("paths"),
        "exclude_paths": paths("exclude_paths"),
        "delay_ms": max(0, min(int(source.get("delay_ms") or 0), 120_000)),
        "frequency": frequency,
    }


@router.patch("/announcements/{announcement_id}")
async def update_announcement(
    announcement_id: int,
    payload: AnnouncementUpdate,
    user: CurrentUser = Depends(require_perm("marketing.manage")),
) -> dict:
    scoped = db.TenantDB(user.tenant_id)
    sent = payload.model_dump(exclude_unset=True)
    if not sent:
        raise HTTPException(400, "Nothing to save.")

    row = await scoped.fetch_one(
        f"""UPDATE announcements
               SET name = coalesce($3, name),
                   kind = coalesce($4, kind),
                   content = CASE WHEN $5 THEN $6::jsonb ELSE content END,
                   placement = CASE WHEN $7 THEN $8::jsonb ELSE placement END,
                   priority = coalesce($9, priority),
                   starts_at = CASE WHEN $10 THEN $11 ELSE starts_at END,
                   ends_at = CASE WHEN $12 THEN $13 ELSE ends_at END,
                   is_active = coalesce($14, is_active)
             WHERE tenant_id = $1 AND id = $2 RETURNING {ANNOUNCEMENT_COLUMNS}""",
        announcement_id, collapse(payload.name, 120), payload.kind,
        "content" in sent, _clean_announcement_content(payload.content),
        "placement" in sent, _clean_placement(payload.placement),
        payload.priority,
        "starts_at" in sent, payload.starts_at,
        "ends_at" in sent, payload.ends_at,
        payload.is_active,
    )
    if not row:
        raise HTTPException(404, "That announcement no longer exists.")
    return {"announcement": row}


@router.delete("/announcements/{announcement_id}")
async def delete_announcement(
    announcement_id: int, user: CurrentUser = Depends(require_perm("marketing.manage"))
) -> dict:
    scoped = db.TenantDB(user.tenant_id)
    removed = await scoped.fetch(
        "DELETE FROM announcements WHERE tenant_id = $1 AND id = $2 RETURNING id",
        announcement_id,
    )
    if not removed:
        raise HTTPException(404, "That announcement no longer exists.")
    return {"ok": True}


# ============================================================ UTM links
@router.get("/utm-links")
async def list_utm_links(user: CurrentUser = Depends(require_perm("marketing.view"))) -> dict:
    scoped = db.TenantDB(user.tenant_id)
    rows = await scoped.fetch(
        """SELECT l.id, l.name, l.base_url, l.utm_source, l.utm_medium, l.utm_campaign,
                  l.utm_term, l.utm_content, l.short_code, l.clicks, l.last_click_at,
                  l.created_at, u.display_name AS created_by_name
             FROM utm_links l LEFT JOIN users u ON u.id = l.created_by
            WHERE l.tenant_id = $1 ORDER BY l.created_at DESC"""
    )
    tenant_slug = await _tenant_slug(user.tenant_id)
    for row in rows:
        row["url"] = _build_utm(row)
        row["shortUrl"] = f"/api/v1/{tenant_slug}/l/{row['short_code']}"
    # Real attribution: leads that actually arrived carrying the campaign.
    stats = await scoped.fetch(
        """SELECT utm_campaign, count(*)::int AS leads
             FROM leads WHERE tenant_id = $1 AND NOT is_spam AND utm_campaign IS NOT NULL
            GROUP BY 1"""
    )
    by_campaign = {row["utm_campaign"]: row["leads"] for row in stats}
    for row in rows:
        row["leads"] = by_campaign.get(row["utm_campaign"], 0)
    return {"links": rows}


def _build_utm(row: dict) -> str:
    from urllib.parse import urlencode  # noqa: PLC0415

    params = {
        key: row[key]
        for key in ("utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content")
        if row.get(key)
    }
    separator = "&" if "?" in row["base_url"] else "?"
    return f"{row['base_url']}{separator}{urlencode(params)}"


@router.post("/utm-links", status_code=201)
async def create_utm_link(
    payload: UtmLinkCreate, user: CurrentUser = Depends(require_perm("marketing.manage"))
) -> dict:
    base_url = collapse(payload.base_url, 500) or ""
    if not base_url.lower().startswith(("https://", "http://")):
        raise HTTPException(400, "The destination must be a full https:// URL.")

    scoped = db.TenantDB(user.tenant_id)
    for _ in range(6):
        code = secrets.token_urlsafe(6).replace("-", "a").replace("_", "b")[:8]
        try:
            row = await scoped.fetch_one(
                """INSERT INTO utm_links (tenant_id, name, base_url, utm_source, utm_medium,
                                          utm_campaign, utm_term, utm_content, short_code, created_by)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                   RETURNING id, name, base_url, utm_source, utm_medium, utm_campaign,
                             utm_term, utm_content, short_code, clicks, created_at""",
                collapse(payload.name, 120), base_url.rstrip("/") if base_url.endswith("/") else base_url,
                collapse(payload.utm_source, 120), collapse(payload.utm_medium, 120),
                collapse(payload.utm_campaign, 160), collapse(payload.utm_term, 160),
                collapse(payload.utm_content, 160), code, user.id,
            )
            break
        except Exception as exc:
            if "short_code" not in str(exc):
                raise
    else:
        raise HTTPException(500, "Could not allocate a short code. Try again.")

    tenant_slug = await _tenant_slug(user.tenant_id)
    return {
        "link": {
            **row,
            "url": _build_utm(row),
            "shortUrl": f"/api/v1/{tenant_slug}/l/{row['short_code']}",
            "leads": 0,
        }
    }


@router.delete("/utm-links/{link_id}")
async def delete_utm_link(
    link_id: int, user: CurrentUser = Depends(require_perm("marketing.manage"))
) -> dict:
    scoped = db.TenantDB(user.tenant_id)
    removed = await scoped.fetch(
        "DELETE FROM utm_links WHERE tenant_id = $1 AND id = $2 RETURNING id", link_id
    )
    if not removed:
        raise HTTPException(404, "That link no longer exists.")
    return {"ok": True}


# ====================================================== public endpoints
async def _public_tenant(slug: str) -> dict:
    row = await db.fetch_one(
        "SELECT id, name, slug::text AS slug FROM tenants WHERE slug = $1 AND is_active",
        collapse(slug, 60),
    )
    if not row:
        raise HTTPException(404, "Unknown site.")
    return row


@public_router.post("/api/public/{tenant_slug}/subscribe", status_code=201)
async def public_subscribe(
    tenant_slug: str, payload: PublicSubscribe, request: Request
) -> dict:
    """Newsletter sign-up from the public site.

    Always double opt-in: a public endpoint that could mark an address
    confirmed would let anyone subscribe anyone. The response is the
    same whether the address is new or already known, so it cannot be
    used to test who is on the list.
    """
    ip = client_ip(request)
    subscribe_limiter.check(f"sub:{ip or 'unknown'}")

    generic = {"ok": True, "message": "Please check your inbox to confirm your subscription."}

    if collapse(payload.hp, 10):
        return generic  # honeypot filled

    tenant = await db.fetch_one(
        "SELECT id, name, slug::text AS slug FROM tenants WHERE slug = $1 AND is_active",
        collapse(tenant_slug, 60),
    )
    address = valid_email(payload.email)
    if not tenant or not address or not payload.consent:
        return generic

    token = secrets.token_urlsafe(24)
    row = await db.fetch_one(
        """INSERT INTO subscribers (tenant_id, email, name, status, source, confirm_hash, meta)
           VALUES ($1, $2, $3, 'pending', $4, $5, $6::jsonb)
           ON CONFLICT (tenant_id, email) DO UPDATE
              SET confirm_hash = CASE WHEN subscribers.status = 'subscribed'
                                      THEN subscribers.confirm_hash ELSE EXCLUDED.confirm_hash END,
                  name = coalesce(EXCLUDED.name, subscribers.name)
           RETURNING id, email::text AS email, name, status::text AS status,
                     unsubscribe_token""",
        tenant["id"], address, collapse(payload.name, 120),
        collapse(payload.source_page, 80) or "website",
        _hash(token),
        {"source_page": collapse(payload.source_page, 500)},
    )

    # Already confirmed: send nothing rather than a pointless second
    # confirmation email, but answer identically.
    if row["status"] == "subscribed":
        return generic

    base = _public_base() or f"{request.url.scheme}://{request.headers.get('host', '')}"
    template = await db.fetch_one(
        """SELECT subject, body_text FROM email_templates
            WHERE tenant_id = $1 AND slug = 'subscriber-confirm' AND is_active""",
        tenant["id"],
    )
    context = _campaign_context(
        tenant["name"], row,
        f"{base}/api/v1/{tenant['slug']}/unsubscribe/{row['unsubscribe_token']}",
    )
    context["subscriber"]["confirm_url"] = (
        f"{base}/api/v1/{tenant['slug']}/confirm/{token}"
    )

    if template:
        subject = templating.render_text(template["subject"], context)
        body = templating.render_text(template["body_text"], context)
    else:
        subject = f"Confirm your subscription to {tenant['name']}"
        body = (
            f"Please confirm you want to receive email from {tenant['name']}:\n\n"
            f"  {context['subscriber']['confirm_url']}\n\n"
            "If you did not sign up, ignore this email — nothing happens.\n"
        )
    await mail.enqueue(tenant["id"], [address], subject, body, kind="subscriber.confirm")

    await db.execute(
        """INSERT INTO consent_records (tenant_id, subject_email, subject_hash, purpose,
                                        granted, source_page, evidence, ip, user_agent)
           VALUES ($1, $2, $3, 'marketing.newsletter', TRUE, $4, $5::jsonb, $6, $7)""",
        tenant["id"], address, _hash(address),
        collapse(payload.source_page, 500),
        {"channel": "newsletter-form", "double_opt_in": True},
        db.to_inet(ip), (request.headers.get("user-agent") or "")[:300] or None,
    )
    return generic


def _hash(value: str) -> str:
    import hashlib  # noqa: PLC0415

    return hashlib.sha256(value.encode()).hexdigest()


@public_router.get("/api/v1/{tenant_slug}/confirm/{token}")
async def confirm_subscription(tenant_slug: str, token: str) -> dict:
    tenant = await _public_tenant(tenant_slug)
    row = await db.fetch_one(
        """UPDATE subscribers
              SET status = 'subscribed', confirmed_at = coalesce(confirmed_at, now()),
                  confirm_hash = NULL, unsubscribed_at = NULL
            WHERE tenant_id = $1 AND confirm_hash = $2
            RETURNING id, email::text AS email, name, unsubscribe_token""",
        tenant["id"], _hash(token),
    )
    if not row:
        raise HTTPException(400, "That confirmation link is no longer valid.")

    base = _public_base()
    template = await db.fetch_one(
        """SELECT subject, body_text FROM email_templates
            WHERE tenant_id = $1 AND slug = 'subscriber-welcome' AND is_active""",
        tenant["id"],
    )
    if template:
        context = _campaign_context(
            tenant["name"], row,
            f"{base}/api/v1/{tenant['slug']}/unsubscribe/{row['unsubscribe_token']}"
            if base else None,
        )
        await mail.enqueue(
            tenant["id"], [row["email"]],
            templating.render_text(template["subject"], context),
            templating.render_text(template["body_text"], context),
            kind="subscriber.welcome",
        )

    await events.emit(
        tenant["id"], "subscriber.created", {"email": row["email"], "name": row["name"]}
    )
    return {"ok": True, "email": row["email"], "message": "You are subscribed."}


@public_router.get("/api/v1/{tenant_slug}/unsubscribe/{token}")
@public_router.post("/api/v1/{tenant_slug}/unsubscribe/{token}")
async def unsubscribe(tenant_slug: str, token: str) -> dict:
    """One click, no sign-in, no confirmation step.

    An unsubscribe that needs a login is an unsubscribe that does not
    happen, and it is the token holder's own address either way.
    """
    tenant = await _public_tenant(tenant_slug)
    row = await db.fetch_one(
        """UPDATE subscribers
              SET status = 'unsubscribed', unsubscribed_at = now()
            WHERE tenant_id = $1 AND unsubscribe_token = $2
            RETURNING email::text AS email""",
        tenant["id"], collapse(token, 80),
    )
    if not row:
        # Idempotent: an already-removed address is still "unsubscribed".
        return {"ok": True, "message": "You are unsubscribed."}

    await db.execute(
        """INSERT INTO consent_records (tenant_id, subject_email, subject_hash, purpose,
                                        granted, evidence)
           VALUES ($1, $2, $3, 'marketing.newsletter', FALSE, '{"channel":"unsubscribe-link"}')""",
        tenant["id"], row["email"], _hash(row["email"]),
    )
    return {"ok": True, "email": row["email"], "message": "You are unsubscribed."}


@public_router.get("/api/v1/{tenant_slug}/l/{code}")
async def follow_utm_link(tenant_slug: str, code: str):
    """Resolve a short campaign link and count the click."""
    from fastapi.responses import RedirectResponse  # noqa: PLC0415

    tenant = await _public_tenant(tenant_slug)
    row = await db.fetch_one(
        """UPDATE utm_links SET clicks = clicks + 1, last_click_at = now()
            WHERE tenant_id = $1 AND short_code = $2
            RETURNING base_url, utm_source, utm_medium, utm_campaign, utm_term, utm_content""",
        tenant["id"], collapse(code, 20),
    )
    if not row:
        raise HTTPException(404, "Unknown link.")
    # 302, not 301: a campaign link's destination can be edited, and a
    # permanently cached redirect would strand it.
    return RedirectResponse(_build_utm(dict(row)), status_code=302)
