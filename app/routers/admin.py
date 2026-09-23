"""Dashboard, users, webhooks, activity and settings."""

import ipaddress
import secrets
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response

from .. import db, events, pagebuilder, tenancy, xlsx
from ..schemas import SettingUpdate, UserCreate, UserRole, UserUpdate, WebhookCreate
from ..security import (
    CurrentUser,
    client_ip,
    hash_password,
    require_role,
    require_user,
    tenant_db,
)

router = APIRouter(prefix="/api", tags=["admin"])

# Roles that can create or grant other elevated roles.
ELEVATED_ROLES = {UserRole.owner, UserRole.super_admin}

# site_chrome is the site-wide header and footer: two page-builder
# blocks stored once and rendered around every page (app/pagebuilder.py,
# with_site_chrome). It is validated as those blocks on write.
SETTING_KEYS = {"notify_emails", "spam", "branding", "pipeline", "site_chrome"}
# The platform's full event catalogue (app/events.py), so a webhook
# can subscribe to publishes and build failures, not just leads.
ALLOWED_EVENTS = events.PLATFORM_EVENTS


# ============================================================== dashboard
async def _lead_volume(scoped: db.TenantDB, days: int) -> tuple[list, list]:
    """Leads per day (every day in the window, zero-filled) and per source.

    One query pair, used by the dashboard's chart and by the export
    underneath it, so the file someone downloads is the chart they saw.
    """
    by_day = await scoped.fetch(
        """SELECT to_char(d::date, 'YYYY-MM-DD') AS day, coalesce(c.n, 0)::int AS n
             FROM generate_series(
                    now()::date - make_interval(days => $2), now()::date, '1 day'
                  ) d
             LEFT JOIN (
               SELECT created_at::date AS day, count(*) AS n
                 FROM leads
                WHERE tenant_id = $1 AND NOT is_spam
                  AND created_at > now() - make_interval(days => $2)
                GROUP BY 1
             ) c ON c.day = d::date
            ORDER BY 1""",
        days,
    )

    by_source = await scoped.fetch(
        """SELECT coalesce(nullif(utm_source, ''), 'direct') AS source, count(*)::int AS n
             FROM leads
            WHERE tenant_id = $1 AND NOT is_spam
              AND created_at > now() - make_interval(days => $2)
            GROUP BY 1 ORDER BY n DESC LIMIT 6""",
        days,
    )
    return by_day, by_source


@router.get("/dashboard")
async def dashboard(
    days: int = Query(default=30, ge=7, le=365),
    scoped: db.TenantDB = Depends(tenant_db),
) -> dict:
    totals = await scoped.fetch_one(
        """SELECT count(*)::int AS total,
                  count(*) FILTER (WHERE created_at > now() - make_interval(days => $2))::int AS period,
                  count(*) FILTER (WHERE created_at > now() - interval '24 hours')::int AS today,
                  count(*) FILTER (WHERE status = 'new')::int AS unworked,
                  count(*) FILTER (WHERE status = 'won')::int AS won,
                  count(*) FILTER (WHERE status = 'lost')::int AS lost,
                  coalesce(sum(value_amount) FILTER (WHERE status = 'won'), 0)::float8 AS won_value
             FROM leads WHERE tenant_id = $1 AND NOT is_spam""",
        days,
    )

    by_day, by_source = await _lead_volume(scoped, days)

    recent = await scoped.fetch(
        """SELECT id, full_name, company, status, created_at, utm_source
             FROM leads WHERE tenant_id = $1 AND NOT is_spam
            ORDER BY created_at DESC LIMIT 8"""
    )

    follow_ups = await scoped.fetch(
        """SELECT id, full_name, follow_up_on, status
             FROM leads
            WHERE tenant_id = $1 AND NOT is_spam AND follow_up_on IS NOT NULL
              AND follow_up_on <= now()::date + 7 AND status NOT IN ('won', 'lost')
            ORDER BY follow_up_on LIMIT 8"""
    )

    closed = totals["won"] + totals["lost"]
    return {
        "days": days,
        "totals": {**totals, "winRate": round(totals["won"] / closed * 100) if closed else 0},
        "byDay": by_day,
        "bySource": by_source,
        "recent": recent,
        "followUps": follow_ups,
    }


# =============================================================== activity

@router.get("/dashboard/export", include_in_schema=False)
async def dashboard_export(
    days: int = Query(default=30, ge=7, le=365),
    format: str = Query(default="csv", pattern="^(csv|xlsx)$"),
    scoped: db.TenantDB = Depends(tenant_db),
) -> Response:
    """The Lead volume chart as a file — CSV, or an Excel workbook.

    Same numbers as the chart (same helper), plus the source breakdown:
    one sheet per table in the workbook, the two tables one after the
    other in the CSV. Cells go through the same formula-neutralising the
    submissions export uses; a UTM source is visitor-supplied text.
    """
    by_day, by_source = await _lead_volume(scoped, days)
    total = sum(row["n"] for row in by_source) or 1
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    filename = f"lead-volume-{days}d-{stamp}"

    daily: list[list] = [["Day", "Leads"], *[[row["day"], row["n"]] for row in by_day]]
    daily.append(["Total", sum(row["n"] for row in by_day)])
    sources: list[list] = [["Source", "Leads", "Share"],
                           *[[row["source"], row["n"], f"{round(row['n'] / total * 100)}%"] for row in by_source]]

    if format == "xlsx":
        body = xlsx.workbook([("Lead volume", daily), ("Sources", sources)])
        return Response(
            body,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"content-disposition": f'attachment; filename="{filename}.xlsx"'},
        )

    def cell(value) -> str:
        text = "" if value is None else str(value)
        if text[:1] in ("=", "+", "-", "@", "\t", "\r"):
            text = f"'{text}"
        return '"' + text.replace('"', '""') + '"' if any(ch in text for ch in ',"\n') else text

    lines = [",".join(cell(v) for v in row) for row in daily]
    lines += ["", *(",".join(cell(v) for v in row) for row in sources)]
    return Response(
        "\ufeff" + "\r\n".join(lines) + "\r\n",     # BOM: Excel then reads UTF-8 correctly
        media_type="text/csv; charset=utf-8",
        headers={"content-disposition": f'attachment; filename="{filename}.csv"'},
    )

@router.get("/activity")
async def activity(
    limit: int = Query(default=50, ge=10, le=200),
    scoped: db.TenantDB = Depends(tenant_db),
) -> dict:
    rows = await scoped.fetch(
        """SELECT a.id, a.action, a.object_type, a.object_id, a.meta, a.created_at,
                  u.display_name AS actor
             FROM activity_log a LEFT JOIN users u ON u.id = a.user_id
            WHERE a.tenant_id = $1
            ORDER BY a.created_at DESC LIMIT $2""",
        limit,
    )
    return {"activity": rows}


# ================================================================== users
@router.get("/users")
async def list_users(scoped: db.TenantDB = Depends(tenant_db)) -> dict:
    rows = await scoped.fetch(
        """SELECT u.id, u.email, u.display_name, u.role, u.is_active, u.last_login_at,
                  count(l.id)::int AS open_leads
             FROM users u
             LEFT JOIN leads l ON l.assigned_to = u.id AND l.status NOT IN ('won', 'lost')
            WHERE u.tenant_id = $1
            GROUP BY u.id ORDER BY u.display_name"""
    )
    return {"users": rows}


@router.post("/users", status_code=201)
async def create_user(
    payload: UserCreate,
    request: Request,
    user: CurrentUser = Depends(require_role("admin")),
) -> dict:
    if payload.role in ELEVATED_ROLES and user.role not in ELEVATED_ROLES:
        raise HTTPException(403, "Only an owner can add owners or super admins.")

    await tenancy.enforce_limit(user.tenant_id, "users")

    scoped = db.TenantDB(user.tenant_id)
    existing = await scoped.fetch_one(
        "SELECT 1 FROM users WHERE tenant_id = $1 AND email = $2", payload.email
    )
    if existing:
        raise HTTPException(400, "Someone already uses that email on this account.")

    created = await scoped.fetch_one(
        """INSERT INTO users (tenant_id, email, password_hash, display_name, role)
           VALUES ($1, $2, $3, $4, $5::user_role)
           RETURNING id, email, display_name, role, is_active, last_login_at""",
        payload.email,
        await hash_password(payload.password),
        payload.display_name,
        payload.role.value,
    )
    await events.log_activity(
        user.tenant_id,
        "user.created",
        user_id=user.id,
        object_type="user",
        object_id=created["id"],
        meta={"role": payload.role.value},
        ip=db.to_inet(client_ip(request)),
    )
    return {"user": {**created, "open_leads": 0}}


@router.patch("/users/{user_id}")
async def update_user(
    user_id: int,
    payload: UserUpdate,
    request: Request,
    user: CurrentUser = Depends(require_role("admin")),
) -> dict:
    scoped = db.TenantDB(user.tenant_id)
    target = await scoped.fetch_one(
        "SELECT id, role FROM users WHERE tenant_id = $1 AND id = $2", user_id
    )
    if not target:
        raise HTTPException(404, "That user no longer exists.")
    if target["role"] in {"owner", "super_admin"} and user.role not in {"owner", "super_admin"}:
        raise HTTPException(403, "Only an owner can change an owner or super admin.")

    sent = payload.model_dump(exclude_unset=True)
    if not sent:
        raise HTTPException(400, "Nothing to save.")

    args: list[Any] = [user.tenant_id, user_id]
    assignments: list[str] = []

    def assign(column: str, value: Any, cast: str = "") -> None:
        args.append(value)
        assignments.append(f"{column} = ${len(args)}{cast}")

    if "display_name" in sent:
        assign("display_name", payload.display_name or "Unnamed")
    if "role" in sent:
        if payload.role in ELEVATED_ROLES and user.role not in ELEVATED_ROLES:
            raise HTTPException(403, "Only an owner can grant owner or super admin access.")
        assign("role", payload.role.value, "::user_role")
    if "is_active" in sent:
        if user_id == user.id and payload.is_active is False:
            raise HTTPException(400, "You cannot deactivate your own account.")
        assign("is_active", bool(payload.is_active))
    if "password" in sent and payload.password:
        assign("password_hash", await hash_password(payload.password))
        # A password change invalidates that user's other sessions.
        await db.execute(
            "DELETE FROM sessions WHERE user_id = $1 AND id <> $2::uuid",
            user_id,
            user.session_id,
        )

    updated = await db.fetch_one(
        f"""UPDATE users SET {', '.join(assignments)}
             WHERE tenant_id = $1 AND id = $2
             RETURNING id, email, display_name, role, is_active, last_login_at""",
        *args,
    )
    await events.log_activity(
        user.tenant_id,
        "user.updated",
        user_id=user.id,
        object_type="user",
        object_id=user_id,
        meta={"fields": list(sent.keys())},
        ip=db.to_inet(client_ip(request)),
    )
    return {"user": {**updated, "open_leads": 0}}



@router.delete("/users/{user_id}")
async def delete_user(
    user_id: int,
    request: Request,
    user: CurrentUser = Depends(require_role("admin")),
) -> dict:
    """Remove a user from this workspace.

    What they *made* stays: pages, media and leads reference their
    author with ON DELETE SET NULL, so a deleted user leaves the work
    behind and only stops being named on it. What is theirs alone —
    sessions, password-reset tokens, notification preferences — goes
    with them, which is also what signs them out everywhere.

    Three refusals, in the order someone runs into them: your own
    account (deleting it would end the session mid-request), an owner
    or super admin when you are not one (the same rule as changing
    them), and the last owner a workspace has (a site nobody can fully
    administer is a support ticket, not a valid state). Deactivating a
    user is the reversible alternative.
    """
    if user_id == user.id:
        raise HTTPException(400, "You cannot delete your own account.")

    scoped = db.TenantDB(user.tenant_id)
    target = await scoped.fetch_one(
        "SELECT id, email, display_name, role::text AS role FROM users WHERE tenant_id = $1 AND id = $2",
        user_id,
    )
    if not target:
        raise HTTPException(404, "That user no longer exists.")
    if target["role"] in {"owner", "super_admin"} and user.role not in {"owner", "super_admin"}:
        raise HTTPException(403, "Only an owner can delete an owner or super admin.")

    if target["role"] == "owner":
        others = await scoped.fetch_one(
            """SELECT count(*)::int AS n FROM users
                WHERE tenant_id = $1 AND role = 'owner' AND id <> $2 AND is_active""",
            user_id,
        )
        if not others["n"]:
            raise HTTPException(400, "This is the only owner — make someone else an owner first.")

    await scoped.execute("DELETE FROM users WHERE tenant_id = $1 AND id = $2", user_id)
    await events.log_activity(
        user.tenant_id,
        "user.deleted",
        user_id=user.id,
        object_type="user",
        object_id=user_id,
        meta={"email": target["email"], "role": target["role"]},
        ip=db.to_inet(client_ip(request)),
    )
    return {"ok": True, "deleted": target["display_name"] or target["email"]}

# =============================================================== webhooks
def _reject_internal_url(raw: str) -> str:
    """Admin-supplied URLs are an SSRF vector — an endpoint pointing at
    169.254.169.254 would hand instance credentials to whoever set it."""
    parsed = urlparse(raw)
    if parsed.scheme != "https":
        raise HTTPException(400, "The URL must start with https://")
    host = (parsed.hostname or "").lower()
    if not host:
        raise HTTPException(400, "That URL has no host.")
    if host in {"localhost", "metadata.google.internal"} or host.endswith(".internal"):
        raise HTTPException(400, "Internal addresses are not allowed.")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return raw  # a name; DNS rebinding is the egress rules' problem
    if address.is_private or address.is_loopback or address.is_link_local or address.is_reserved:
        raise HTTPException(400, "Internal addresses are not allowed.")
    return raw


@router.get("/webhooks")
async def list_webhooks(user: CurrentUser = Depends(require_role("admin"))) -> dict:
    scoped = db.TenantDB(user.tenant_id)
    endpoints = await scoped.fetch(
        """SELECT e.id, e.name, e.url, e.events, e.is_active, e.created_at,
                  count(d.id) FILTER (WHERE d.status = 'pending')::int AS pending,
                  count(d.id) FILTER (WHERE d.status = 'dead')::int AS failed
             FROM webhook_endpoints e
             LEFT JOIN webhook_deliveries d ON d.endpoint_id = e.id
            WHERE e.tenant_id = $1 GROUP BY e.id ORDER BY e.created_at DESC"""
    )
    deliveries = await scoped.fetch(
        """SELECT d.id, d.event, d.status::text AS status, d.attempts, d.response_code,
                  d.last_error, d.created_at, e.name AS endpoint
             FROM webhook_deliveries d JOIN webhook_endpoints e ON e.id = d.endpoint_id
            WHERE d.tenant_id = $1 ORDER BY d.created_at DESC LIMIT 25"""
    )
    return {"endpoints": endpoints, "deliveries": deliveries}


@router.post("/webhooks", status_code=201)
async def create_webhook(
    payload: WebhookCreate,
    request: Request,
    user: CurrentUser = Depends(require_role("admin")),
) -> dict:
    url = _reject_internal_url(payload.url.strip())
    chosen = [e for e in payload.events if e in ALLOWED_EVENTS] or ["lead.created"]
    secret = f"whsec_{secrets.token_urlsafe(24)}"

    scoped = db.TenantDB(user.tenant_id)
    endpoint = await scoped.fetch_one(
        """INSERT INTO webhook_endpoints (tenant_id, name, url, secret, events)
           VALUES ($1, $2, $3, $4, $5)
           RETURNING id, name, url, events, is_active, created_at""",
        payload.name,
        url,
        secret,
        chosen,
    )
    await events.log_activity(
        user.tenant_id,
        "webhook.created",
        user_id=user.id,
        object_type="webhook",
        object_id=endpoint["id"],
        ip=db.to_inet(client_ip(request)),
    )
    # Returned once, at creation, and never listed again.
    return {"endpoint": {**endpoint, "pending": 0, "failed": 0}, "secret": secret}


@router.delete("/webhooks/{endpoint_id}")
async def delete_webhook(
    endpoint_id: int,
    request: Request,
    user: CurrentUser = Depends(require_role("admin")),
) -> dict:
    scoped = db.TenantDB(user.tenant_id)
    removed = await scoped.fetch(
        "DELETE FROM webhook_endpoints WHERE tenant_id = $1 AND id = $2 RETURNING id",
        endpoint_id,
    )
    if not removed:
        raise HTTPException(404, "That endpoint no longer exists.")
    await events.log_activity(
        user.tenant_id,
        "webhook.deleted",
        user_id=user.id,
        object_type="webhook",
        object_id=endpoint_id,
        ip=db.to_inet(client_ip(request)),
    )
    return {"ok": True}


# =============================================================== settings
@router.get("/settings")
async def get_settings(user: CurrentUser = Depends(require_user)) -> dict:
    scoped = db.TenantDB(user.tenant_id)
    rows = await scoped.fetch("SELECT key, value FROM settings WHERE tenant_id = $1")
    forms = await scoped.fetch(
        """SELECT id, slug, name, is_active, notify_emails
             FROM forms WHERE tenant_id = $1 ORDER BY name"""
    )
    return {
        "settings": {row["key"]: row["value"] for row in rows},
        "forms": forms,
        "tenant": {"name": user.tenant_name, "slug": user.tenant_slug},
    }


@router.put("/settings/{key}")
async def put_setting(
    key: str,
    payload: SettingUpdate,
    request: Request,
    user: CurrentUser = Depends(require_role("admin")),
) -> dict:
    if key not in SETTING_KEYS:
        raise HTTPException(400, "Unknown setting.")

    value = payload.value
    if key == "site_chrome":
        # The header and footer are page blocks: same cleaner as a page
        # save, so a bad link or an oversized field is refused here with
        # the block validator's own message.
        value = pagebuilder.clean_site_chrome(value)

    scoped = db.TenantDB(user.tenant_id)
    await scoped.execute(
        """INSERT INTO settings (tenant_id, key, value) VALUES ($1, $2, $3::jsonb)
           ON CONFLICT (tenant_id, key)
           DO UPDATE SET value = EXCLUDED.value, updated_at = now()""",
        key,
        value,  # jsonb codec encodes; a pre-dumped string double-encodes
    )
    await events.log_activity(
        user.tenant_id,
        "settings.updated",
        user_id=user.id,
        meta={"key": key},
        ip=db.to_inet(client_ip(request)),
    )
    return {"ok": True}
