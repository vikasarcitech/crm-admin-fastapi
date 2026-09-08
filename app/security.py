"""Session auth as FastAPI dependencies.

- Opaque 32-byte token in an httpOnly, SameSite=Lax cookie.
- Only the SHA-256 of the token is stored, so a database dump does not
  hand an attacker live sessions.
- CSRF: the session carries a token the SPA echoes in X-CSRF-Token on
  every state-changing request.
"""

import asyncio
import hashlib
import hmac
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import bcrypt
from fastapi import Depends, HTTPException, Request, Response, status

from . import db
from .config import settings

log = logging.getLogger("crm.auth")

COOKIE = "crm_session"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
# Coarse ordering for the original require_role() gates. Named
# permissions (app/permissions.py) are what new code should check;
# these ranks only keep the pre-existing owner/admin/agent/viewer
# guards behaving exactly as before while the newer roles slot in
# below 'admin' so an Editor cannot pass require_role("admin").
ROLE_RANK = {
    "viewer": 1,
    "contributor": 1,
    "agent": 2,
    "author": 2,
    "editor": 3,
    "admin": 4,
    "owner": 5,
    "super_admin": 6,
}

# bcrypt silently ignores bytes past 72; reject rather than truncate so a
# long passphrase can't be matched by its own prefix.
MAX_PASSWORD_BYTES = 72
MIN_PASSWORD_CHARS = 12

# Constant work for unknown accounts, so response time doesn't reveal
# which emails are registered.
_DUMMY_HASH = bcrypt.hashpw(b"invalid-placeholder", bcrypt.gensalt(rounds=12))


@dataclass(slots=True)
class CurrentUser:
    id: int
    email: str
    name: str
    role: str
    tenant_id: int
    tenant_name: str
    tenant_slug: str
    session_id: str
    csrf: str

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "email": self.email,
            "name": self.name,
            "role": self.role,
            "tenantId": self.tenant_id,
            "tenantName": self.tenant_name,
            "tenantSlug": self.tenant_slug,
        }


def sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def client_ip(request: Request) -> str | None:
    """Real client address, honouring exactly TRUST_PROXY_HOPS forwarded hops."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded and settings.trust_proxy_hops > 0:
        hops = [part.strip() for part in forwarded.split(",") if part.strip()]
        if hops:
            index = max(0, len(hops) - settings.trust_proxy_hops)
            return hops[index]
    return request.client.host if request.client else None


# --------------------------------------------------------------- passwords
async def hash_password(plain: str) -> str:
    if len(plain) < MIN_PASSWORD_CHARS:
        raise HTTPException(400, "Passwords need at least 12 characters.")
    if len(plain.encode()) > MAX_PASSWORD_BYTES:
        raise HTTPException(400, "That password is too long. Use 72 bytes or fewer.")
    # bcrypt is deliberately slow; keep it off the event loop.
    digest = await asyncio.to_thread(bcrypt.hashpw, plain.encode(), bcrypt.gensalt(rounds=12))
    return digest.decode()


async def _check_password(plain: str, hashed: str | None) -> bool:
    candidate = (hashed or _DUMMY_HASH.decode()).encode()
    try:
        return await asyncio.to_thread(bcrypt.checkpw, plain.encode()[:MAX_PASSWORD_BYTES], candidate)
    except ValueError:
        return False


# ---------------------------------------------------------------- sessions
async def create_session(user: dict, request: Request, response: Response) -> str:
    token = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(24)
    expires = datetime.now(timezone.utc) + timedelta(days=settings.session_days)

    await db.execute(
        """INSERT INTO sessions (token_hash, csrf_token, user_id, tenant_id, ip, user_agent, expires_at)
           VALUES ($1, $2, $3, $4, $5, $6, $7)""",
        sha256(token),
        csrf,
        user["id"],
        user["tenant_id"],
        db.to_inet(client_ip(request)),
        (request.headers.get("user-agent") or "")[:400],
        expires,
    )

    response.set_cookie(
        COOKIE,
        token,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,  # production must terminate TLS
        max_age=settings.session_days * 86400,
        path="/",
    )
    return csrf


async def destroy_session(request: Request, response: Response) -> None:
    token = request.cookies.get(COOKIE)
    if token:
        await db.execute("DELETE FROM sessions WHERE token_hash = $1", sha256(token))
    response.delete_cookie(COOKIE, path="/")


async def prune_sessions() -> None:
    try:
        await db.execute("DELETE FROM sessions WHERE expires_at < now()")
        await db.execute(
            "DELETE FROM password_resets WHERE expires_at < now() - interval '1 day'"
        )
    except Exception as exc:  # housekeeping must never take the app down
        log.error("session prune failed: %s", exc)


# ------------------------------------------------------------ dependencies
async def optional_user(request: Request) -> CurrentUser | None:
    token = request.cookies.get(COOKIE)
    if not token:
        return None

    row = await db.fetch_one(
        """SELECT s.id, s.csrf_token, s.tenant_id,
                  u.id AS user_id, u.email, u.display_name, u.role, u.is_active,
                  t.name AS tenant_name, t.slug AS tenant_slug
             FROM sessions s
             JOIN users u   ON u.id = s.user_id
             JOIN tenants t ON t.id = s.tenant_id
            WHERE s.token_hash = $1 AND s.expires_at > now()""",
        sha256(token),
    )
    if not row or not row["is_active"]:
        return None

    return CurrentUser(
        id=row["user_id"],
        email=row["email"],
        name=row["display_name"],
        role=row["role"],
        tenant_id=row["tenant_id"],
        tenant_name=row["tenant_name"],
        tenant_slug=row["tenant_slug"],
        session_id=str(row["id"]),
        csrf=row["csrf_token"],
    )


async def require_user(
    request: Request, user: CurrentUser | None = Depends(optional_user)
) -> CurrentUser:
    """Signed in, plus a matching CSRF header on writes."""
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Sign in to continue")

    if request.method not in SAFE_METHODS:
        sent = request.headers.get("x-csrf-token", "")
        if not hmac.compare_digest(sent, user.csrf):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                "Your session expired. Reload the page and try again.",
            )
    return user


def require_role(minimum: str):
    """Dependency factory: require_role('admin') admits admin and owner."""

    async def _guard(user: CurrentUser = Depends(require_user)) -> CurrentUser:
        if ROLE_RANK.get(user.role, 0) < ROLE_RANK[minimum]:
            raise HTTPException(403, f"This action needs {minimum} access.")
        return user

    return _guard


def tenant_db(user: CurrentUser = Depends(require_user)) -> db.TenantDB:
    return db.TenantDB(user.tenant_id)


# ------------------------------------------------------------- credentials
async def verify_credentials(tenant_id: int, email: str, password: str) -> dict | None:
    user = await db.fetch_one(
        """SELECT id, tenant_id, email, password_hash, display_name, role, is_active,
                  failed_logins, locked_until
             FROM users WHERE tenant_id = $1 AND email = $2""",
        tenant_id,
        email,
    )

    matched = await _check_password(password, user["password_hash"] if user else None)

    if not user or not user["is_active"]:
        return None

    locked_until = user["locked_until"]
    if locked_until and locked_until > datetime.now(timezone.utc):
        minutes = max(1, int((locked_until - datetime.now(timezone.utc)).total_seconds() // 60) + 1)
        raise HTTPException(429, f"Account locked. Try again in {minutes} min.")

    if not matched:
        failed = user["failed_logins"] + 1
        lock = (
            datetime.now(timezone.utc) + timedelta(minutes=settings.lockout_minutes)
            if failed >= settings.max_failed_logins
            else None
        )
        await db.execute(
            "UPDATE users SET failed_logins = $2, locked_until = $3 WHERE id = $1",
            user["id"],
            0 if lock else failed,
            lock,
        )
        return None

    await db.execute(
        "UPDATE users SET failed_logins = 0, locked_until = NULL, last_login_at = now() WHERE id = $1",
        user["id"],
    )
    return user
