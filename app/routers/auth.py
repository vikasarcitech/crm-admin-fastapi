import re

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request, Response

from .. import db, events
from ..config import settings
from ..ratelimit import login_limiter, signup_limiter
from ..schemas import LoginRequest, RegisterRequest, collapse
from ..security import (
    CurrentUser,
    client_ip,
    create_session,
    destroy_session,
    hash_password,
    optional_user,
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
    login_limiter.check(client_ip(request) or "unknown")

    tenant = await _resolve_tenant(request, payload.tenant)
    # One generic message whether the workspace, the email or the password
    # is wrong — no enumerating which clients live on this install.
    generic = "Those details do not match an account."
    if not tenant:
        raise HTTPException(401, generic)

    user = await verify_credentials(tenant["id"], payload.email, payload.password)
    if not user:
        raise HTTPException(401, generic)

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
    signup_limiter.check(client_ip(request) or "unknown")

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
