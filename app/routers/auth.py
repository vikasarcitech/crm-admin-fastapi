import re
import secrets
from datetime import datetime, timedelta, timezone

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request, Response

from .. import bootstrap, db, events, mail
from . import users
from ..config import settings
from ..ratelimit import login_limiter, reset_limiter, signup_limiter
from ..schemas import (
    ForgotRequest,
    LoginRequest,
    RegisterRequest,
    ResetRequest,
    TwoFactorLogin,
    collapse,
)
from ..security import (
    CurrentUser,
    client_ip,
    create_session,
    destroy_session,
    hash_password,
    optional_user,
    sha256,
    verify_credentials,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])

# Fields for the contact form every new workspace starts with (same shape
# the seed script creates, so intake and the page builder work day one).
STARTER_FORM_FIELDS = [
    {"name": "full_name", "label": "Name", "type": "text", "required": True, "max": 120},
    {"name": "email", "label": "Email", "type": "email", "required": True},
    {"name": "phone", "label": "Phone", "type": "tel", "required": False},
    {"name": "company", "label": "Company", "type": "text", "required": False},
    {"name": "message", "label": "How can we help?", "type": "textarea", "required": True, "max": 4000},
]


async def _resolve_tenant(request: Request, slug: str | None) -> dict | None:
    """Explicit workspace slug wins; otherwise match the request host."""
    cleaned = collapse(slug, 60)
    if cleaned:
        return await db.fetch_one(
            "SELECT id, name, slug FROM tenants WHERE slug = $1 AND is_active", cleaned
        )
    host = (request.headers.get("host") or "").split(":")[0].lower()
    return await db.fetch_one(
        "SELECT id, name, slug FROM tenants WHERE lower(primary_domain) = $1 AND is_active",
        host,
    )


@router.post("/login")
async def login(payload: LoginRequest, request: Request, response: Response) -> dict:
    # Per-IP budget. Per-account lockout is separate, so a distributed
    # attack still trips the account gate.
    await login_limiter.check(client_ip(request) or "unknown")

    tenant = await _resolve_tenant(request, payload.tenant)
    # One generic message whether the workspace, the email or the password
    # is wrong — no enumerating which clients live on this install.
    generic = "Those details do not match an account."
    if not tenant:
        raise HTTPException(401, generic)

    user = await verify_credentials(tenant["id"], payload.email, payload.password)
    if not user:
        raise HTTPException(401, generic)

    # 2FA: the password was right, but no session is issued yet. The
    # challenge is a separate short-lived row, so a pending second
    # factor can never be mistaken for a signed-in session.
    if await users.has_totp(user["id"]):
        challenge = await users.create_totp_challenge(user)
        await events.log_activity(
            tenant["id"], "auth.2fa_required", user_id=user["id"],
            ip=db.to_inet(client_ip(request)),
        )
        return {"twoFactorRequired": True, "challenge": challenge}

    csrf = await create_session(user, request, response)
    await events.log_activity(
        tenant["id"],
        "auth.signed_in",
        user_id=user["id"],
        object_type="user",
        object_id=user["id"],
        ip=db.to_inet(client_ip(request)),
    )

    return {
        "user": {
            "id": user["id"],
            "name": user["display_name"],
            "email": user["email"],
            "role": user["role"],
            "tenantId": tenant["id"],
            "tenantName": tenant["name"],
            "tenantSlug": tenant["slug"],
        },
        "csrfToken": csrf,
    }


@router.post("/2fa")
async def two_factor(
    payload: TwoFactorLogin, request: Request, response: Response
) -> dict:
    """Second step of a 2FA sign-in: challenge + code (or recovery code)."""
    return await users.complete_totp_login(
        payload.challenge, payload.code, request, response
    )


def _slug_base(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:50]
    return slug or "workspace"


@router.post("/register", status_code=201)
async def register(payload: RegisterRequest, request: Request, response: Response) -> dict:
    """Self-service sign-up: creates a workspace with the registrant as owner.

    Turned off with ALLOW_SIGNUPS=0. There is no email verification yet, so
    keep it off on installs where workspace creation should be invite-only.
    """
    if not settings.allow_signups:
        raise HTTPException(403, "Sign-ups are disabled on this install. Ask an admin for an invite.")
    await signup_limiter.check(client_ip(request) or "unknown")

    workspace = collapse(payload.workspace, 60)
    if not workspace:
        raise HTTPException(400, "Give your workspace a name.")
    password_hash = await hash_password(payload.password)  # enforces the 12-char rule

    # Find a free slug: the plain name, then name-2 … name-9.
    base = _slug_base(workspace)
    candidates = [base] + [f"{base}-{n}" for n in range(2, 10)]
    taken = {
        str(row["slug"])
        for row in await db.fetch(
            "SELECT slug FROM tenants WHERE slug = ANY($1::citext[])", candidates
        )
    }
    slug = next((c for c in candidates if c not in taken), None)
    if slug is None:
        raise HTTPException(400, "That workspace name is taken. Try another.")

    # One transaction: a failure part-way must not leave an ownerless tenant.
    try:
        async with db.pool().acquire() as conn:
            async with conn.transaction():
                tenant = await conn.fetchrow(
                    "INSERT INTO tenants (slug, name) VALUES ($1, $2) RETURNING id, name, slug",
                    slug,
                    workspace,
                )
                owner = await conn.fetchrow(
                    """INSERT INTO users (tenant_id, email, password_hash, display_name, role)
                       VALUES ($1, $2, $3, $4, 'owner')
                       RETURNING id, tenant_id, email, display_name, role""",
                    tenant["id"],
                    payload.email,
                    password_hash,
                    payload.display_name.strip(),
                )
                await conn.execute(
                    """INSERT INTO forms (tenant_id, slug, name, fields)
                       VALUES ($1, 'contact', 'Contact form', $2::jsonb)""",
                    tenant["id"],
                    STARTER_FORM_FIELDS,
                )
    except asyncpg.UniqueViolationError:
        # Two sign-ups raced for the same slug between check and insert.
        raise HTTPException(400, "That workspace name is taken. Try another.") from None

    # Content types, taxonomies, menus, email templates and default
    # settings. Outside the transaction on purpose: a workspace that
    # provisions partially is usable and re-provisions on next sign-in,
    # whereas rolling back sign-up over a default menu would not be.
    await bootstrap.provision_tenant(tenant["id"], created_by=owner["id"])

    csrf = await create_session(dict(owner), request, response)
    await events.log_activity(
        tenant["id"],
        "auth.registered",
        user_id=owner["id"],
        object_type="user",
        object_id=owner["id"],
        meta={"workspace": workspace, "slug": slug},
        ip=db.to_inet(client_ip(request)),
    )

    return {
        "user": {
            "id": owner["id"],
            "name": owner["display_name"],
            "email": owner["email"],
            "role": owner["role"],
            "tenantId": tenant["id"],
            "tenantName": tenant["name"],
            "tenantSlug": str(tenant["slug"]),
        },
        "csrfToken": csrf,
    }


RESET_TOKEN_MINUTES = 30


@router.post("/forgot")
async def forgot_password(payload: ForgotRequest, request: Request) -> dict:
    """Send a reset link. The response never reveals whether the account exists."""
    await reset_limiter.check(client_ip(request) or "unknown")
    generic = {"ok": True, "message": "If that account exists, a reset link is on its way."}

    tenant = await _resolve_tenant(request, payload.tenant)
    if not tenant:
        return generic
    user = await db.fetch_one(
        "SELECT id, email FROM users WHERE tenant_id = $1 AND email = $2 AND is_active",
        tenant["id"],
        payload.email,
    )
    if not user:
        return generic

    token = secrets.token_urlsafe(32)
    await db.execute(
        """INSERT INTO password_resets (token_hash, user_id, tenant_id, expires_at)
           VALUES ($1, $2, $3, $4)""",
        sha256(token),
        user["id"],
        tenant["id"],
        datetime.now(timezone.utc) + timedelta(minutes=RESET_TOKEN_MINUTES),
    )

    host = (request.headers.get("host") or "").split(",")[0].strip()
    base = settings.app_base_url or f"{request.url.scheme}://{host}"
    subject, body = mail.password_reset(f"{base}/reset?token={token}", tenant["name"])
    await mail.enqueue(tenant["id"], [user["email"]], subject, body, kind="auth.reset")

    await events.log_activity(
        tenant["id"],
        "auth.password_reset_requested",
        object_type="user",
        object_id=user["id"],
        ip=db.to_inet(client_ip(request)),
    )
    return generic


@router.post("/reset")
async def reset_password(payload: ResetRequest, request: Request) -> dict:
    await reset_limiter.check(client_ip(request) or "unknown")

    row = await db.fetch_one(
        """SELECT r.id, r.user_id, r.tenant_id FROM password_resets r
             JOIN users u ON u.id = r.user_id AND u.is_active
            WHERE r.token_hash = $1 AND r.used_at IS NULL AND r.expires_at > now()""",
        sha256(payload.token),
    )
    if not row:
        raise HTTPException(400, "That reset link is invalid or has expired. Request a new one.")

    new_hash = await hash_password(payload.password)
    await db.execute(
        """UPDATE users SET password_hash = $2, failed_logins = 0, locked_until = NULL
            WHERE id = $1""",
        row["user_id"],
        new_hash,
    )
    await db.execute("UPDATE password_resets SET used_at = now() WHERE id = $1", row["id"])
    # A reset signs everyone out: whoever held the old password loses access.
    await db.execute("DELETE FROM sessions WHERE user_id = $1", row["user_id"])

    await events.log_activity(
        row["tenant_id"],
        "auth.password_reset",
        object_type="user",
        object_id=row["user_id"],
        ip=db.to_inet(client_ip(request)),
    )
    return {"ok": True, "message": "Password updated. Sign in with your new password."}


@router.post("/logout")
async def logout(
    request: Request,
    response: Response,
    user: CurrentUser | None = Depends(optional_user),
) -> dict:
    if user:
        await events.log_activity(
            user.tenant_id,
            "auth.signed_out",
            user_id=user.id,
            ip=db.to_inet(client_ip(request)),
        )
    await destroy_session(request, response)
    return {"ok": True}


@router.get("/me")
async def me(user: CurrentUser | None = Depends(optional_user)) -> dict:
    """Bootstrap for the SPA: who am I, and which CSRF token do I echo?"""
    if user is None:
        raise HTTPException(401, "Not signed in")
    return {"user": user.as_dict(), "csrfToken": user.csrf}
