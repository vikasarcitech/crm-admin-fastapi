"""Users, roles & security (2.4).

The original owner/admin/agent/viewer roles still work; this adds the
Super Admin / Admin / Editor / Author / Contributor set alongside them
and moves enforcement from role *rank* to named permissions
(app/permissions.py), so a workspace can widen or narrow any single
capability without inventing a role.

Also here: user profiles, optional TOTP two-factor, the active-session
list, and cross-site (tenant) membership for operators who work across
a portfolio.
"""

from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from .. import db, events, storage, totp
from ..permissions import (
    DEFAULTS,
    PERMISSIONS,
    ROLE_LABELS,
    ROLES,
    permissions_for,
    require_perm,
    set_override,
)
from ..ratelimit import login_limiter
from ..schemas import (
    MembershipCreate,
    PasswordChange,
    ProfileUpdate,
    RolePermissionUpdate,
    TotpDisable,
    TotpVerify,
    collapse,
    keep_lines,
    valid_email,
)
from ..security import (
    CurrentUser,
    client_ip,
    create_session,
    hash_password,
    require_user,
    sha256,
    verify_credentials,
)

log = logging.getLogger("crm.users")

router = APIRouter(prefix="/api", tags=["users"])

SOCIAL_KEYS = ("website", "linkedin", "x", "github", "instagram", "facebook", "youtube")
CHALLENGE_MINUTES = 10
MAX_TOTP_ATTEMPTS = 5


# ================================================================ profile
@router.get("/profile")
async def get_profile(user: CurrentUser = Depends(require_user)) -> dict:
    scoped = db.TenantDB(user.tenant_id)
    row = await scoped.fetch_one(
        """SELECT u.id, u.email, u.display_name, u.role::text AS role, u.last_login_at,
                  u.created_at,
                  p.bio, p.job_title, p.phone, p.avatar_media_id, p.social,
                  p.locale, p.timezone,
                  m.storage_key AS avatar_key,
                  (t.user_id IS NOT NULL AND t.confirmed_at IS NOT NULL) AS totp_enabled
             FROM users u
             LEFT JOIN user_profiles p ON p.user_id = u.id
             LEFT JOIN media m ON m.id = p.avatar_media_id
             LEFT JOIN user_totp t ON t.user_id = u.id
            WHERE u.tenant_id = $1 AND u.id = $2""",
        user.id,
    )
    if not row:
        raise HTTPException(404, "That account no longer exists.")

    row["avatar_url"] = storage.public_url(row.pop("avatar_key")) if row.get("avatar_key") else None
    row["permissions"] = sorted(await permissions_for(user.tenant_id, user.role))
    row["sites"] = await _sites_for(user.id, user.tenant_id)
    return {"profile": row}


async def _sites_for(user_id: int, home_tenant_id: int) -> list[dict]:
    """Every workspace this account can switch into."""
    return await db.fetch(
        """SELECT t.id, t.slug::text AS slug, t.name,
                  coalesce(m.role::text, u.role::text) AS role,
                  (t.id = $2) AS is_home
             FROM tenants t
             LEFT JOIN tenant_memberships m ON m.tenant_id = t.id AND m.user_id = $1
             LEFT JOIN users u ON u.id = $1 AND u.tenant_id = t.id
            WHERE t.is_active AND (m.user_id IS NOT NULL OR t.id = $2)
            ORDER BY is_home DESC, t.name""",
        user_id, home_tenant_id,
    )


@router.patch("/profile")
async def update_profile(
    payload: ProfileUpdate,
    request: Request,
    user: CurrentUser = Depends(require_user),
) -> dict:
    scoped = db.TenantDB(user.tenant_id)
    sent = payload.model_dump(exclude_unset=True)
    if not sent:
        raise HTTPException(400, "Nothing to save.")

    if "display_name" in sent and payload.display_name:
        await scoped.execute(
            "UPDATE users SET display_name = $3 WHERE tenant_id = $1 AND id = $2",
            user.id, collapse(payload.display_name, 120),
        )

    if payload.avatar_media_id:
        avatar = await scoped.fetch_one(
            "SELECT id FROM media WHERE tenant_id = $1 AND id = $2 AND deleted_at IS NULL",
            payload.avatar_media_id,
        )
        if not avatar:
            raise HTTPException(400, "That image is not in the media library.")

    await scoped.execute(
        """INSERT INTO user_profiles (user_id, tenant_id, bio, job_title, phone,
                                      avatar_media_id, social, locale, timezone)
           -- coalesce, not the column default: passing NULL explicitly
           -- would override DEFAULT and hit the NOT NULL constraint.
           VALUES ($2, $1, $3, $4, $5, $6, $7::jsonb,
                   coalesce($8, 'en'), coalesce($9, 'UTC'))
           ON CONFLICT (user_id) DO UPDATE
              SET bio        = CASE WHEN $10 THEN EXCLUDED.bio ELSE user_profiles.bio END,
                  job_title  = CASE WHEN $11 THEN EXCLUDED.job_title ELSE user_profiles.job_title END,
                  phone      = CASE WHEN $12 THEN EXCLUDED.phone ELSE user_profiles.phone END,
                  avatar_media_id = CASE WHEN $13 THEN EXCLUDED.avatar_media_id
                                         ELSE user_profiles.avatar_media_id END,
                  social     = CASE WHEN $14 THEN EXCLUDED.social ELSE user_profiles.social END,
                  locale     = coalesce(EXCLUDED.locale, user_profiles.locale),
                  timezone   = coalesce(EXCLUDED.timezone, user_profiles.timezone),
                  updated_at = now()""",
        user.id,
        keep_lines(payload.bio, 1000),
        collapse(payload.job_title, 120),
        collapse(payload.phone, 40),
        payload.avatar_media_id,
        _clean_social(payload.social),
        collapse(payload.locale, 10),
        collapse(payload.timezone, 60),
        "bio" in sent, "job_title" in sent, "phone" in sent,
        "avatar_media_id" in sent, "social" in sent,
    )
    await events.log_activity(
        user.tenant_id, "profile.updated", user_id=user.id,
        meta={"fields": list(sent)}, ip=db.to_inet(client_ip(request)),
    )
    return await get_profile(user)


def _clean_social(raw: dict[str, str] | None) -> dict:
    """Known networks only, https only — a profile link is rendered on
    the public site, so a javascript: URL here would be stored XSS."""
    if not raw:
        return {}
    out: dict[str, str] = {}
    for key in SOCIAL_KEYS:
        value = collapse(raw.get(key), 300)
        if value and value.lower().startswith("https://"):
            out[key] = value
    return out


@router.post("/profile/password")
async def change_password(
    payload: PasswordChange,
    request: Request,
    user: CurrentUser = Depends(require_user),
) -> dict:
    """Requires the current password, and ends every other session."""
    ip = client_ip(request)
    login_limiter.check(f"pw:{ip or 'unknown'}")

    verified = await verify_credentials(user.tenant_id, user.email, payload.current_password)
    if not verified:
        raise HTTPException(400, "That is not your current password.")
    if payload.current_password == payload.new_password:
        raise HTTPException(400, "The new password must be different.")

    await db.execute(
        "UPDATE users SET password_hash = $2 WHERE id = $1",
        user.id, await hash_password(payload.new_password),
    )
    removed = await db.fetch(
        "DELETE FROM sessions WHERE user_id = $1 AND id <> $2::uuid RETURNING id",
        user.id, user.session_id,
    )
    await events.log_activity(
        user.tenant_id, "password.changed", user_id=user.id,
        meta={"sessions_ended": len(removed)}, ip=db.to_inet(ip),
    )
    return {"ok": True, "sessionsEnded": len(removed)}


# ================================================================ sessions
@router.get("/sessions")
async def list_sessions(user: CurrentUser = Depends(require_user)) -> dict:
    """Where this account is signed in. Self-service, so no permission
    check: seeing your own sessions is part of owning the account."""
    rows = await db.fetch(
        """SELECT s.id::text AS id, s.ip::text AS ip, s.user_agent,
                  s.created_at, s.expires_at, s.last_seen_at,
                  (s.id = $2::uuid) AS is_current
             FROM sessions s
            WHERE s.user_id = $1 AND s.expires_at > now()
            ORDER BY is_current DESC, s.created_at DESC""",
        user.id, user.session_id,
    )
    return {"sessions": rows}


@router.delete("/sessions/{session_id}")
async def revoke_session(
    session_id: str, user: CurrentUser = Depends(require_user)
) -> dict:
    if session_id == user.session_id:
        raise HTTPException(400, "Use sign out to end the session you are using.")
    try:
        removed = await db.fetch(
            "DELETE FROM sessions WHERE user_id = $1 AND id = $2::uuid RETURNING id",
            user.id, session_id,
        )
    except Exception as exc:  # a malformed uuid must be a 400, not a 500
        raise HTTPException(400, "That is not a valid session id.") from exc
    if not removed:
        raise HTTPException(404, "That session has already ended.")
    return {"ok": True}


@router.post("/sessions/revoke-others")
async def revoke_other_sessions(user: CurrentUser = Depends(require_user)) -> dict:
    removed = await db.fetch(
        "DELETE FROM sessions WHERE user_id = $1 AND id <> $2::uuid RETURNING id",
        user.id, user.session_id,
    )
    await events.log_activity(
        user.tenant_id, "sessions.revoked", user_id=user.id, meta={"count": len(removed)}
    )
    return {"ok": True, "revoked": len(removed)}


# ============================================================ two-factor
@router.post("/profile/2fa/start")
async def start_totp(user: CurrentUser = Depends(require_user)) -> dict:
    """Generate (or replace) an unconfirmed secret and return its URI.

    The secret is stored unconfirmed: until a valid code proves the
    authenticator has it, sign-in is unaffected — otherwise a half-done
    enrolment would lock the account out.
    """
    existing = await db.fetch_one(
        "SELECT confirmed_at FROM user_totp WHERE user_id = $1", user.id
    )
    if existing and existing["confirmed_at"]:
        raise HTTPException(400, "Two-factor authentication is already on.")

    secret = totp.generate_secret()
    await db.execute(
        """INSERT INTO user_totp (user_id, tenant_id, secret)
           VALUES ($1, $2, $3)
           ON CONFLICT (user_id) DO UPDATE
              SET secret = EXCLUDED.secret, confirmed_at = NULL,
                  recovery_hashes = '{}', last_used_step = NULL""",
        user.id, user.tenant_id, secret,
    )
    return {
        "secret": secret,
        "uri": totp.provisioning_uri(secret, user.email, user.tenant_name or "CRM Admin"),
        "digits": totp.DIGITS,
        "period": totp.PERIOD,
    }


@router.post("/profile/2fa/confirm")
async def confirm_totp(
    payload: TotpVerify,
    request: Request,
    user: CurrentUser = Depends(require_user),
) -> dict:
    row = await db.fetch_one(
        "SELECT secret, confirmed_at, last_used_step FROM user_totp WHERE user_id = $1",
        user.id,
    )
    if not row:
        raise HTTPException(400, "Start the setup first.")
    if row["confirmed_at"]:
        raise HTTPException(400, "Two-factor authentication is already on.")

    step = totp.verify(row["secret"], payload.code, last_used_step=row["last_used_step"])
    if step is None:
        raise HTTPException(400, "That code is not right. Check your authenticator app.")

    codes = totp.generate_recovery_codes()
    await db.execute(
        """UPDATE user_totp
              SET confirmed_at = now(), last_used_step = $2, recovery_hashes = $3
            WHERE user_id = $1""",
        user.id, step, [totp.hash_recovery(code) for code in codes],
    )
    await events.log_activity(
        user.tenant_id, "2fa.enabled", user_id=user.id, ip=db.to_inet(client_ip(request))
    )
    # Shown exactly once; only their hashes are kept.
    return {"ok": True, "recoveryCodes": codes}


@router.post("/profile/2fa/disable")
async def disable_totp(
    payload: TotpDisable,
    request: Request,
    user: CurrentUser = Depends(require_user),
) -> dict:
    login_limiter.check(f"2fa-off:{client_ip(request) or 'unknown'}")
    if not await verify_credentials(user.tenant_id, user.email, payload.password):
        raise HTTPException(400, "That password is not right.")

    await db.execute("DELETE FROM user_totp WHERE user_id = $1", user.id)
    await events.log_activity(
        user.tenant_id, "2fa.disabled", user_id=user.id, ip=db.to_inet(client_ip(request))
    )
    return {"ok": True}


@router.post("/profile/2fa/recovery-codes")
async def regenerate_recovery(
    payload: TotpDisable, user: CurrentUser = Depends(require_user)
) -> dict:
    """Replaces every existing code — the old ones stop working."""
    if not await verify_credentials(user.tenant_id, user.email, payload.password):
        raise HTTPException(400, "That password is not right.")
    row = await db.fetch_one(
        "SELECT confirmed_at FROM user_totp WHERE user_id = $1", user.id
    )
    if not row or not row["confirmed_at"]:
        raise HTTPException(400, "Two-factor authentication is not on.")

    codes = totp.generate_recovery_codes()
    await db.execute(
        "UPDATE user_totp SET recovery_hashes = $2 WHERE user_id = $1",
        user.id, [totp.hash_recovery(code) for code in codes],
    )
    return {"recoveryCodes": codes}


# ------------------------------------------------- second login step
async def create_totp_challenge(user_row: dict) -> str:
    """Called by the login handler when an account has 2FA on."""
    token = secrets.token_urlsafe(32)
    await db.execute(
        """INSERT INTO totp_challenges (token_hash, user_id, tenant_id, expires_at)
           VALUES ($1, $2, $3, $4)""",
        sha256(token), user_row["id"], user_row["tenant_id"],
        datetime.now(timezone.utc) + timedelta(minutes=CHALLENGE_MINUTES),
    )
    return token


async def has_totp(user_id: int) -> bool:
    row = await db.fetch_one(
        "SELECT 1 FROM user_totp WHERE user_id = $1 AND confirmed_at IS NOT NULL", user_id
    )
    return bool(row)


async def complete_totp_login(
    challenge: str, code: str, request: Request, response: Response
) -> dict:
    """Second step: exchange a challenge plus a code for a real session."""
    ip = client_ip(request)
    login_limiter.check(f"2fa:{ip or 'unknown'}")

    row = await db.fetch_one(
        """SELECT c.id, c.user_id, c.tenant_id, c.attempts,
                  u.email, u.display_name, u.role::text AS role, u.is_active,
                  t.name AS tenant_name, t.slug::text AS tenant_slug,
                  s.secret, s.last_used_step, s.recovery_hashes
             FROM totp_challenges c
             JOIN users u   ON u.id = c.user_id
             JOIN tenants t ON t.id = c.tenant_id
             JOIN user_totp s ON s.user_id = c.user_id
            WHERE c.token_hash = $1 AND c.expires_at > now()""",
        sha256(challenge),
    )
    if not row or not row["is_active"]:
        raise HTTPException(400, "That sign-in attempt expired. Start again.")

    if row["attempts"] >= MAX_TOTP_ATTEMPTS:
        await db.execute("DELETE FROM totp_challenges WHERE id = $1", row["id"])
        raise HTTPException(429, "Too many incorrect codes. Sign in again.")

    step = totp.verify(row["secret"], code, last_used_step=row["last_used_step"])
    recovery = None if step else totp.match_recovery(code, list(row["recovery_hashes"] or []))

    if step is None and recovery is None:
        await db.execute(
            "UPDATE totp_challenges SET attempts = attempts + 1 WHERE id = $1", row["id"]
        )
        raise HTTPException(400, "That code is not right.")

    if step is not None:
        await db.execute(
            "UPDATE user_totp SET last_used_step = $2 WHERE user_id = $1", row["user_id"], step
        )
    else:
        # A used recovery code must not work twice.
        await db.execute(
            """UPDATE user_totp
                  SET recovery_hashes = array_remove(recovery_hashes, $2)
                WHERE user_id = $1""",
            row["user_id"], recovery,
        )

    await db.execute("DELETE FROM totp_challenges WHERE id = $1", row["id"])

    csrf = await create_session(
        {"id": row["user_id"], "tenant_id": row["tenant_id"]}, request, response
    )
    await events.log_activity(
        row["tenant_id"], "auth.login_2fa", user_id=row["user_id"],
        meta={"recovery_code_used": recovery is not None}, ip=db.to_inet(ip),
    )
    return {
        "csrfToken": csrf,
        "usedRecoveryCode": recovery is not None,
        "user": {
            "id": row["user_id"], "email": row["email"], "name": row["display_name"],
            "role": row["role"], "tenantId": row["tenant_id"],
            "tenantName": row["tenant_name"], "tenantSlug": row["tenant_slug"],
        },
    }


# ============================================================ permissions
@router.get("/roles")
async def list_roles(user: CurrentUser = Depends(require_perm("users.view"))) -> dict:
    """The permission matrix: defaults plus this workspace's overrides."""
    overrides = await db.fetch(
        """SELECT role::text AS role, permission, allowed
             FROM role_permissions WHERE tenant_id = $1""",
        user.tenant_id,
    )
    override_map = {(row["role"], row["permission"]): row["allowed"] for row in overrides}

    matrix = {}
    for role in ROLES:
        effective = await permissions_for(user.tenant_id, role)
        matrix[role] = {
            "label": ROLE_LABELS[role],
            "default": sorted(DEFAULTS[role]),
            "effective": sorted(effective),
            "overrides": {
                permission: allowed
                for (row_role, permission), allowed in override_map.items()
                if row_role == role
            },
        }
    return {
        "roles": matrix,
        "groups": {group: list(perms) for group, perms in PERMISSIONS.items()},
    }


@router.put("/roles/permissions")
async def update_role_permission(
    payload: RolePermissionUpdate,
    request: Request,
    user: CurrentUser = Depends(require_perm("roles.manage")),
) -> dict:
    """Override one cell. allowed=null restores the role default."""
    await set_override(
        user.tenant_id, payload.role, payload.permission, payload.allowed, user.id
    )
    await events.log_activity(
        user.tenant_id, "role.permission_changed", user_id=user.id,
        meta={"role": payload.role, "permission": payload.permission, "allowed": payload.allowed},
        ip=db.to_inet(client_ip(request)),
    )
    return {"ok": True, "effective": sorted(await permissions_for(user.tenant_id, payload.role))}


@router.post("/roles/permissions/reset")
async def reset_role_permissions(
    role: str = Query(max_length=30),
    user: CurrentUser = Depends(require_perm("roles.manage")),
) -> dict:
    if role not in DEFAULTS:
        raise HTTPException(400, "Unknown role.")
    from ..permissions import invalidate  # noqa: PLC0415

    await db.execute(
        "DELETE FROM role_permissions WHERE tenant_id = $1 AND role = $2::user_role",
        user.tenant_id, role,
    )
    invalidate(user.tenant_id)
    return {"ok": True, "effective": sorted(DEFAULTS[role])}


# ======================================================= site membership
@router.get("/sites")
async def list_sites(user: CurrentUser = Depends(require_user)) -> dict:
    sites = await _sites_for(user.id, user.tenant_id)
    can_manage = "sites.manage" in await permissions_for(user.tenant_id, user.role)
    if can_manage:
        # A Super Admin needs to see every site to grant access to it.
        sites = await db.fetch(
            """SELECT t.id, t.slug::text AS slug, t.name, t.primary_domain, t.is_active,
                      count(DISTINCT u.id)::int AS user_count,
                      count(DISTINCT l.id)::int AS lead_count
                 FROM tenants t
                 LEFT JOIN users u ON u.tenant_id = t.id
                 LEFT JOIN leads l ON l.tenant_id = t.id AND NOT l.is_spam
                GROUP BY t.id ORDER BY t.name"""
        )
    return {"sites": sites, "canManage": can_manage}


@router.post("/sites/switch")
async def switch_site(
    tenant_slug: str = Query(max_length=60),
    user: CurrentUser = Depends(require_user),
) -> dict:
    """Re-point the current session at another workspace.

    The session row carries tenant_id, so switching is a single update
    rather than a new sign-in — and every TenantDB built afterwards is
    scoped to the new site automatically.
    """
    target = await db.fetch_one(
        "SELECT id, slug::text AS slug, name FROM tenants WHERE slug = $1 AND is_active",
        collapse(tenant_slug, 60),
    )
    if not target:
        raise HTTPException(404, "Unknown site.")

    if target["id"] != user.tenant_id:
        granted = await permissions_for(user.tenant_id, user.role)
        membership = await db.fetch_one(
            "SELECT role::text AS role FROM tenant_memberships WHERE tenant_id = $1 AND user_id = $2",
            target["id"], user.id,
        )
        if not membership and "sites.manage" not in granted:
            raise HTTPException(403, "You do not have access to that site.")

    await db.execute(
        "UPDATE sessions SET tenant_id = $2 WHERE id = $1::uuid", user.session_id, target["id"]
    )
    await events.log_activity(
        target["id"], "site.switched", user_id=user.id,
        meta={"from": user.tenant_slug, "to": target["slug"]},
    )
    return {"ok": True, "site": target}


@router.get("/sites/{tenant_slug}/members")
async def list_members(
    tenant_slug: str, user: CurrentUser = Depends(require_perm("users.view"))
) -> dict:
    target = await db.fetch_one(
        "SELECT id FROM tenants WHERE slug = $1", collapse(tenant_slug, 60)
    )
    if not target:
        raise HTTPException(404, "Unknown site.")
    granted = await permissions_for(user.tenant_id, user.role)
    if target["id"] != user.tenant_id and "sites.manage" not in granted:
        raise HTTPException(403, "You do not have access to that site.")

    rows = await db.fetch(
        """SELECT m.id, m.role::text AS role, m.created_at,
                  u.id AS user_id, u.email, u.display_name,
                  g.display_name AS granted_by_name
             FROM tenant_memberships m
             JOIN users u ON u.id = m.user_id
             LEFT JOIN users g ON g.id = m.granted_by
            WHERE m.tenant_id = $1 ORDER BY u.display_name""",
        target["id"],
    )
    return {"members": rows}


@router.post("/sites/members", status_code=201)
async def grant_membership(
    payload: MembershipCreate,
    request: Request,
    user: CurrentUser = Depends(require_perm("sites.manage")),
) -> dict:
    email = valid_email(payload.user_email)
    if not email:
        raise HTTPException(400, "That email address is not valid.")

    target = await db.fetch_one(
        "SELECT id, name FROM tenants WHERE slug = $1", collapse(payload.tenant_slug, 60)
    )
    if not target:
        raise HTTPException(404, "Unknown site.")

    account = await db.fetch_one(
        "SELECT id, display_name FROM users WHERE email = $1 AND is_active ORDER BY id LIMIT 1",
        email,
    )
    if not account:
        raise HTTPException(404, "No active account uses that email address.")

    role = payload.role if isinstance(payload.role, str) else payload.role.value
    if role not in DEFAULTS:
        raise HTTPException(400, "Unknown role.")

    row = await db.fetch_one(
        """INSERT INTO tenant_memberships (tenant_id, user_id, role, granted_by)
           VALUES ($1, $2, $3::user_role, $4)
           ON CONFLICT (tenant_id, user_id) DO UPDATE SET role = EXCLUDED.role
           RETURNING id, role::text AS role, created_at""",
        target["id"], account["id"], role, user.id,
    )
    await events.log_activity(
        target["id"], "site.member_added", user_id=user.id,
        meta={"user": email, "role": role}, ip=db.to_inet(client_ip(request)),
    )
    return {"member": {**row, "user_id": account["id"], "email": email,
                       "display_name": account["display_name"]}}


@router.delete("/sites/members/{membership_id}")
async def revoke_membership(
    membership_id: int, user: CurrentUser = Depends(require_perm("sites.manage"))
) -> dict:
    removed = await db.fetch(
        "DELETE FROM tenant_memberships WHERE id = $1 RETURNING tenant_id, user_id",
        membership_id,
    )
    if not removed:
        raise HTTPException(404, "That membership no longer exists.")
    # Any session that had switched into the site loses it immediately.
    await db.execute(
        "DELETE FROM sessions WHERE user_id = $1 AND tenant_id = $2",
        removed[0]["user_id"], removed[0]["tenant_id"],
    )
    return {"ok": True}


# ============================================================= audit log
@router.get("/audit")
async def audit_log(
    action: str | None = Query(default=None, max_length=60),
    user_id: int | None = None,
    object_type: str | None = Query(default=None, max_length=40),
    days: int = Query(default=30, ge=1, le=730),
    page: int = Query(default=1, ge=1, le=200),
    per_page: int = Query(default=50, ge=10, le=200),
    user: CurrentUser = Depends(require_perm("audit.view")),
) -> dict:
    """The activity log, filterable — 2.4's audit trail.

    /api/activity stays as the dashboard's recent-events feed; this is
    the searchable view with paging and filters.
    """
    scoped = db.TenantDB(user.tenant_id)
    offset = (page - 1) * per_page
    rows = await scoped.fetch(
        """SELECT a.id, a.action, a.object_type, a.object_id, a.meta, a.ip::text AS ip,
                  a.created_at, u.display_name AS actor, u.email AS actor_email
             FROM activity_log a LEFT JOIN users u ON u.id = a.user_id
            WHERE a.tenant_id = $1
              AND a.created_at > now() - make_interval(days => $2)
              AND ($3::text IS NULL OR a.action LIKE $3 || '%')
              AND ($4::bigint IS NULL OR a.user_id = $4)
              AND ($5::text IS NULL OR a.object_type = $5)
            ORDER BY a.created_at DESC
            LIMIT $6 OFFSET $7""",
        days, action, user_id, object_type, per_page, offset,
    )
    total = await scoped.fetch_one(
        """SELECT count(*)::int AS n FROM activity_log a
            WHERE a.tenant_id = $1
              AND a.created_at > now() - make_interval(days => $2)
              AND ($3::text IS NULL OR a.action LIKE $3 || '%')
              AND ($4::bigint IS NULL OR a.user_id = $4)
              AND ($5::text IS NULL OR a.object_type = $5)""",
        days, action, user_id, object_type,
    )
    actions = await scoped.fetch(
        """SELECT DISTINCT split_part(action, '.', 1) AS prefix
             FROM activity_log WHERE tenant_id = $1 ORDER BY 1"""
    )
    return {
        "entries": rows,
        "page": page,
        "total": total["n"],
        "pages": max(1, -(-total["n"] // per_page)),
        "actionPrefixes": [row["prefix"] for row in actions],
    }
