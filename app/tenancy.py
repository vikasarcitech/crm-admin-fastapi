"""The control plane: sites, domains, limits and provisioning.

This is the one module that is deliberately *not* tenant-scoped. It
creates tenants, resolves them, suspends them and reports across them,
so it uses the raw `db` helpers rather than `TenantDB`.

Three things live here that the rest of the platform depends on:

**Resolution.** Nine routers were each running their own
`SELECT … FROM tenants WHERE slug = $1` before this existed. One
resolver, cached, that also understands hostnames — so a site answers
on its own domain without the slug in the path.

**Lifecycle.** Creating a site is provisioning: a tenant row, an owner,
a starter form, the built-in content types, default menus and email
templates. Suspending one has to end its live sessions, or "suspended"
means "suspended at next sign-in".

**Limits.** Per-site ceilings held on the tenant row, so raising one
client's media quota is an UPDATE rather than a platform change.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

from fastapi import HTTPException

from . import db
from .schemas import collapse, valid_email

log = logging.getLogger("crm.tenancy")

SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
# Hostnames only: no scheme, no port, no path. A domain with a port
# would never match a Host header we compare it against.
DOMAIN_RE = re.compile(r"^(?=.{1,253}$)[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?"
                       r"(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$")

# Slugs that would collide with a route or read as the platform itself.
RESERVED_SLUGS = frozenset(
    {
        "api", "admin", "www", "app", "static", "assets", "media", "public",
        "login", "logout", "register", "forgot", "reset", "healthz", "docs",
        "p", "v1", "js", "css", "platform", "system", "internal", "root",
        "preview", "collect", "conversions", "consent", "subscribe",
        "support", "help", "status", "billing", "account", "settings",
    }
)

TENANT_COLUMNS = """t.id, t.slug::text AS slug, t.name, t.status::text AS status,
                    t.is_active, t.plan, t.notes, t.limits, t.infra,
                    t.primary_domain, t.suspended_at, t.suspended_reason,
                    t.archived_at, t.created_at, t.updated_at"""

# Platform ceilings. A tenant's own `limits` object overrides any of
# these per key; 0 or a missing key means "no limit for that thing".
DEFAULT_LIMITS: dict[str, int] = {
    "users": 25,
    "content_items": 5_000,
    "media_files": 10_000,
    "media_bytes": 20 * 1024**3,        # 20 GiB
    "subscribers": 50_000,
    "leads_per_month": 20_000,
    "emails_per_month": 50_000,
    "domains": 5,
    "api_keys": 20,
}

# Which usage counter backs each limit, and the table to confirm
# against when a cached count is close to the ceiling.
LIMIT_SOURCES: dict[str, tuple[str, str]] = {
    "users": ("users", "users"),
    "content_items": ("content_items", "content_items"),
    "media_files": ("media_files", "media"),
    "media_bytes": ("media_bytes", "media"),
    "subscribers": ("subscribers", "subscribers"),
    "leads_per_month": ("leads_30d", "leads"),
    "emails_per_month": ("emails_30d", "email_outbox"),
    "domains": ("domains", "tenant_domains"),
    "api_keys": ("api_keys", "api_keys"),
}


class TenantSuspended(HTTPException):
    """A recognised site that is not currently serving.

    503 rather than 404: the site exists, so telling a crawler it is
    gone would be a lie that costs the client their rankings.
    """

    def __init__(self, name: str, reason: str | None = None) -> None:
        detail = f"“{name}” is temporarily unavailable."
        if reason:
            detail += f" ({reason})"
        super().__init__(503, detail, headers={"retry-after": "3600"})


# ===================================================================
# Resolution
# ===================================================================
_CACHE_TTL = 30.0
_by_slug: dict[str, tuple[float, dict | None]] = {}
_by_host: dict[str, tuple[float, dict | None]] = {}


def invalidate(slug: str | None = None) -> None:
    """Drop this process's cached resolutions."""
    if slug:
        _by_slug.pop(slug.lower(), None)
    else:
        _by_slug.clear()
    # A domain move changes which tenant a host maps to, and the host
    # cache has no way to know which entries were affected.
    _by_host.clear()


async def invalidate_everywhere(slug: str | None = None) -> None:
    """Drop it here and on every replica.

    Suspending a site is the case that matters: with per-process TTLs
    a suspended site keeps serving from other replicas for up to
    30 seconds after the admin says it stopped.
    """
    invalidate(slug)
    from . import cache  # noqa: PLC0415

    await cache.invalidate_tenant(slug)


def _on_invalidate(message: dict) -> None:
    invalidate(message.get("slug"))


def normalise_host(raw: str | None) -> str | None:
    """Host header → bare lowercase hostname, or None."""
    if not raw:
        return None
    host = raw.split(",")[0].strip().lower()
    host = host.removeprefix("http://").removeprefix("https://")
    host = host.split("/")[0]
    # Strip the port; IPv6 literals in brackets are not tenant domains.
    if host.startswith("["):
        return None
    host = host.split(":")[0]
    return host or None


def check_slug(value: str | None) -> str:
    cleaned = (value or "").strip().lower()
    if not SLUG_RE.match(cleaned):
        raise HTTPException(
            400, "A site slug uses lowercase letters, numbers and dashes only."
        )
    if len(cleaned) < 2 or len(cleaned) > 60:
        raise HTTPException(400, "A site slug is between 2 and 60 characters.")
    if cleaned in RESERVED_SLUGS:
        raise HTTPException(400, f"“{cleaned}” is reserved. Choose another slug.")
    return cleaned


def check_domain(value: str | None) -> str:
    host = normalise_host(value)
    if not host or not DOMAIN_RE.match(host):
        raise HTTPException(
            400, "Enter a bare hostname such as example.com — no scheme, port or path."
        )
    return host


async def by_slug(slug: str, *, cached: bool = True) -> dict | None:
    key = (collapse(slug, 60) or "").lower()
    if not key:
        return None
    if cached:
        hit = _by_slug.get(key)
        if hit and hit[0] > time.monotonic():
            return hit[1]

    row = await db.fetch_one(
        f"SELECT {TENANT_COLUMNS} FROM tenants t WHERE t.slug = $1", key
    )
    _by_slug[key] = (time.monotonic() + _CACHE_TTL, row)
    return row


async def by_host(host: str | None, *, cached: bool = True) -> dict | None:
    """Resolve a site from a Host header.

    Only *verified* domains resolve. An unverified row is a claim, and
    honouring a claim would let one client point a hostname at another
    client's content.
    """
    key = normalise_host(host)
    if not key:
        return None
    if cached:
        hit = _by_host.get(key)
        if hit and hit[0] > time.monotonic():
            return hit[1]

    row = await db.fetch_one(
        f"""SELECT {TENANT_COLUMNS} FROM tenant_domains d
             JOIN tenants t ON t.id = d.tenant_id
            WHERE d.domain = $1 AND d.is_verified""",
        key,
    )
    _by_host[key] = (time.monotonic() + _CACHE_TTL, row)
    return row


async def resolve_public(
    slug: str | None, host: str | None = None, request: Any = None
) -> dict:
    """The resolver every public endpoint uses.

    An explicit slug wins; otherwise the Host header decides. Raises
    404 for an unknown site and 503 for a suspended one — an archived
    site is treated as gone.
    """
    tenant = await by_slug(slug) if slug else None
    if tenant is None and host:
        tenant = await by_host(host)
    if tenant is None:
        raise HTTPException(404, "Unknown site.")

    if tenant["status"] == "archived":
        raise HTTPException(404, "Unknown site.")
    if tenant["status"] != "active":
        raise TenantSuspended(tenant["name"], tenant.get("suspended_reason"))

    # So the API-version middleware can attribute the call to a site
    # without resolving the tenant a second time.
    if request is not None:
        try:
            request.state.tenant_id = tenant["id"]
        except Exception:
            pass
    return tenant


async def require_tenant(slug: str) -> dict:
    """Admin-side lookup: exists, whatever its status."""
    tenant = await by_slug(slug)
    if tenant is None:
        raise HTTPException(404, "Unknown site.")
    return tenant


# ===================================================================
# Domains
# ===================================================================
async def domains_for(tenant_id: int) -> list[dict]:
    return await db.fetch(
        """SELECT id, domain::text AS domain, is_primary, is_verified,
                  verify_token, verified_at, created_at
             FROM tenant_domains WHERE tenant_id = $1
            ORDER BY is_primary DESC, domain""",
        tenant_id,
    )


async def allowed_origins(tenant_id: int) -> set[str]:
    """Origins allowed to call this site's public API.

    Per-site, not per-install: a single shared allow-list would let any
    client's frontend post to any other client's forms.
    """
    rows = await db.fetch(
        "SELECT domain::text AS domain FROM tenant_domains WHERE tenant_id = $1 AND is_verified",
        tenant_id,
    )
    origins: set[str] = set()
    for row in rows:
        host = row["domain"]
        origins.add(f"https://{host}")
        # The apex/www pair is the single most common cause of a
        # mystifying CORS failure, so both are accepted.
        origins.add(f"https://www.{host}" if not host.startswith("www.") else
                    f"https://{host.removeprefix('www.')}")
    return origins


async def add_domain(
    tenant_id: int, raw: str, *, make_primary: bool = False, actor_id: int | None = None
) -> dict:
    host = check_domain(raw)

    existing = await db.fetch_one(
        "SELECT tenant_id FROM tenant_domains WHERE domain = $1", host
    )
    if existing:
        if existing["tenant_id"] == tenant_id:
            raise HTTPException(400, f"{host} is already on this site.")
        # Do not say which site: that would leak the portfolio.
        raise HTTPException(409, f"{host} is already claimed by another site.")

    limit = await limit_for(tenant_id, "domains")
    if limit and len(await domains_for(tenant_id)) >= limit:
        raise HTTPException(400, f"This site is limited to {limit} domains.")

    row = await db.fetch_one(
        """INSERT INTO tenant_domains (tenant_id, domain, created_by)
           VALUES ($1, $2, $3)
           RETURNING id, domain::text AS domain, is_primary, is_verified,
                     verify_token, created_at""",
        tenant_id, host, actor_id,
    )
    if make_primary:
        await set_primary_domain(tenant_id, row["id"])
        row["is_primary"] = True
    invalidate()
    return row


async def verify_domain(tenant_id: int, domain_id: int) -> dict:
    """Mark a domain verified.

    The DNS/HTTP check itself is the operator's to perform — this
    platform has no way to prove a TXT record from inside a container
    without an egress path it may not have. What it does guarantee is
    that only a verified domain resolves or is trusted for CORS.
    """
    row = await db.fetch_one(
        """UPDATE tenant_domains SET is_verified = TRUE, verified_at = now()
            WHERE tenant_id = $1 AND id = $2
            RETURNING id, domain::text AS domain, is_primary, is_verified, verified_at""",
        tenant_id, domain_id,
    )
    if not row:
        raise HTTPException(404, "That domain is not on this site.")

    # First verified domain becomes the primary automatically.
    has_primary = await db.fetch_one(
        "SELECT 1 FROM tenant_domains WHERE tenant_id = $1 AND is_primary", tenant_id
    )
    if not has_primary:
        await set_primary_domain(tenant_id, domain_id)
        row["is_primary"] = True
    invalidate()
    return row


async def set_primary_domain(tenant_id: int, domain_id: int) -> None:
    """One primary per site; it also backfills tenants.primary_domain,
    which the original auth and sitemap code still reads."""
    row = await db.fetch_one(
        "SELECT domain::text AS domain, is_verified FROM tenant_domains WHERE tenant_id = $1 AND id = $2",
        tenant_id, domain_id,
    )
    if not row:
        raise HTTPException(404, "That domain is not on this site.")
    if not row["is_verified"]:
        raise HTTPException(400, "Verify the domain before making it primary.")

    async with db.pool().acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "UPDATE tenant_domains SET is_primary = FALSE WHERE tenant_id = $1 AND is_primary",
                tenant_id,
            )
            await conn.execute(
                "UPDATE tenant_domains SET is_primary = TRUE WHERE tenant_id = $1 AND id = $2",
                tenant_id, domain_id,
            )
            await conn.execute(
                "UPDATE tenants SET primary_domain = $2 WHERE id = $1",
                tenant_id, row["domain"],
            )
    invalidate()


async def remove_domain(tenant_id: int, domain_id: int) -> str:
    row = await db.fetch_one(
        "DELETE FROM tenant_domains WHERE tenant_id = $1 AND id = $2 "
        "RETURNING domain::text AS domain, is_primary",
        tenant_id, domain_id,
    )
    if not row:
        raise HTTPException(404, "That domain is not on this site.")
    if row["is_primary"]:
        await db.execute(
            "UPDATE tenants SET primary_domain = NULL WHERE id = $1", tenant_id
        )
    invalidate()
    return row["domain"]


# ===================================================================
# Limits and usage
# ===================================================================
def effective_limits(tenant: dict) -> dict[str, int]:
    """Platform defaults with the site's own overrides applied."""
    limits = dict(DEFAULT_LIMITS)
    override = tenant.get("limits") if isinstance(tenant.get("limits"), dict) else {}
    for key, value in override.items():
        if key in DEFAULT_LIMITS:
            try:
                limits[key] = max(0, int(value))
            except (TypeError, ValueError):
                continue
    return limits


async def limit_for(tenant_id: int, key: str) -> int:
    row = await db.fetch_one("SELECT limits FROM tenants WHERE id = $1", tenant_id)
    return effective_limits(row or {}).get(key, 0)


async def compute_usage(tenant_id: int) -> dict:
    """Count what a site is actually using, and cache it.

    One query rather than nine: at three hundred sites the portfolio
    screen is the query that matters, not any single site's dashboard.
    """
    row = await db.fetch_one(
        """SELECT
             (SELECT count(*) FROM users WHERE tenant_id = $1)::int AS users,
             (SELECT count(*) FROM content_items
               WHERE tenant_id = $1 AND status <> 'trashed')::int AS content_items,
             (SELECT count(*) FROM media
               WHERE tenant_id = $1 AND deleted_at IS NULL)::int AS media_files,
             (SELECT coalesce(sum(byte_size), 0) FROM media
               WHERE tenant_id = $1 AND deleted_at IS NULL)::bigint AS media_bytes,
             (SELECT count(*) FROM leads WHERE tenant_id = $1)::int AS leads_total,
             (SELECT count(*) FROM leads
               WHERE tenant_id = $1 AND created_at > now() - interval '30 days')::int AS leads_30d,
             (SELECT count(*) FROM subscribers
               WHERE tenant_id = $1 AND status = 'subscribed')::int AS subscribers,
             (SELECT count(*) FROM email_outbox
               WHERE tenant_id = $1 AND created_at > now() - interval '30 days')::int AS emails_30d,
             (SELECT coalesce(sum(views), 0) FROM page_view_daily
               WHERE tenant_id = $1 AND day > now()::date - 30)::bigint AS page_views_30d,
             (SELECT count(*) FROM tenant_domains WHERE tenant_id = $1)::int AS domains,
             (SELECT count(*) FROM api_keys
               WHERE tenant_id = $1 AND revoked_at IS NULL)::int AS api_keys""",
        tenant_id,
    )
    usage = dict(row or {})

    await db.execute(
        """INSERT INTO tenant_usage (tenant_id, users, content_items, media_files,
                                     media_bytes, leads_total, leads_30d, subscribers,
                                     emails_30d, page_views_30d, computed_at)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, now())
           ON CONFLICT (tenant_id) DO UPDATE
              SET users = EXCLUDED.users, content_items = EXCLUDED.content_items,
                  media_files = EXCLUDED.media_files, media_bytes = EXCLUDED.media_bytes,
                  leads_total = EXCLUDED.leads_total, leads_30d = EXCLUDED.leads_30d,
                  subscribers = EXCLUDED.subscribers, emails_30d = EXCLUDED.emails_30d,
                  page_views_30d = EXCLUDED.page_views_30d, computed_at = now()""",
        tenant_id, usage["users"], usage["content_items"], usage["media_files"],
        usage["media_bytes"], usage["leads_total"], usage["leads_30d"],
        usage["subscribers"], usage["emails_30d"], usage["page_views_30d"],
    )
    return usage


async def usage_report(tenant: dict) -> dict:
    """Usage against limits, with a percentage the UI can show."""
    usage = await compute_usage(tenant["id"])
    limits = effective_limits(tenant)

    lines = []
    for key, ceiling in limits.items():
        counter = LIMIT_SOURCES.get(key, (key, key))[0]
        used = int(usage.get(counter, 0) or 0)
        lines.append(
            {
                "key": key,
                "used": used,
                "limit": ceiling,
                "percent": round(used / ceiling * 100, 1) if ceiling else None,
                "overLimit": bool(ceiling) and used >= ceiling,
            }
        )
    return {"usage": usage, "limits": lines,
            "anyOverLimit": any(line["overLimit"] for line in lines)}


async def enforce_limit(tenant_id: int, key: str, *, adding: int = 1) -> None:
    """Raise 402 when a site is at its ceiling for `key`.

    Counted live rather than from the cached rollup: a stale count is
    fine for a dashboard and wrong for a gate.
    """
    limits = await limit_for(tenant_id, key)
    if not limits:
        return

    counter, table = LIMIT_SOURCES.get(key, (key, key))
    if key == "media_bytes":
        row = await db.fetch_one(
            "SELECT coalesce(sum(byte_size),0)::bigint AS n FROM media "
            "WHERE tenant_id = $1 AND deleted_at IS NULL",
            tenant_id,
        )
    elif key == "leads_per_month":
        row = await db.fetch_one(
            "SELECT count(*)::bigint AS n FROM leads "
            "WHERE tenant_id = $1 AND created_at > now() - interval '30 days'",
            tenant_id,
        )
    elif key == "emails_per_month":
        row = await db.fetch_one(
            "SELECT count(*)::bigint AS n FROM email_outbox "
            "WHERE tenant_id = $1 AND created_at > now() - interval '30 days'",
            tenant_id,
        )
    elif key == "content_items":
        row = await db.fetch_one(
            "SELECT count(*)::bigint AS n FROM content_items "
            "WHERE tenant_id = $1 AND status <> 'trashed'",
            tenant_id,
        )
    elif key == "media_files":
        row = await db.fetch_one(
            "SELECT count(*)::bigint AS n FROM media "
            "WHERE tenant_id = $1 AND deleted_at IS NULL",
            tenant_id,
        )
    elif key == "api_keys":
        row = await db.fetch_one(
            "SELECT count(*)::bigint AS n FROM api_keys "
            "WHERE tenant_id = $1 AND revoked_at IS NULL",
            tenant_id,
        )
    elif key == "subscribers":
        row = await db.fetch_one(
            "SELECT count(*)::bigint AS n FROM subscribers "
            "WHERE tenant_id = $1 AND status = 'subscribed'",
            tenant_id,
        )
    else:
        row = await db.fetch_one(
            f"SELECT count(*)::bigint AS n FROM {table} WHERE tenant_id = $1", tenant_id
        )

    used = int((row or {}).get("n", 0) or 0)
    if used + adding > limits:
        raise HTTPException(
            402,
            f"This site has reached its {key.replace('_', ' ')} limit "
            f"({used:,} of {limits:,}). Ask an administrator to raise it.",
        )


async def warn_if_over(tenant_id: int, key: str) -> bool:
    """Soft limit: notify, never refuse.

    Used for leads and outbound email, where blocking is the wrong
    answer — losing a client's enquiry because their plan is at its
    ceiling is worse for them than the overage is for us. The
    notification is what makes the overage visible so someone can
    raise the plan.
    """
    try:
        await enforce_limit(tenant_id, key, adding=0)
        return False
    except HTTPException:
        from . import events  # noqa: PLC0415

        limit = await limit_for(tenant_id, key)
        await events.notify(
            tenant_id,
            "limit.exceeded",
            f"Over the {key.replace('_', ' ')} limit",
            body=f"This site has passed its ceiling of {limit:,}. Nothing is being "
                 "blocked — ask an administrator to raise the plan.",
            level="warning",
            link="#/operations",
        )
        return True
    except Exception as exc:
        log.error("soft limit check failed for tenant %s: %s", tenant_id, exc)
        return False


async def set_limits(tenant_id: int, overrides: dict[str, Any]) -> dict:
    """Replace a site's limit overrides. Unknown keys are refused so a
    typo does not silently do nothing."""
    unknown = set(overrides) - set(DEFAULT_LIMITS)
    if unknown:
        raise HTTPException(
            400,
            f"Unknown limit(s): {', '.join(sorted(unknown))}. "
            f"Valid: {', '.join(sorted(DEFAULT_LIMITS))}",
        )
    cleaned: dict[str, int] = {}
    for key, value in overrides.items():
        if value in (None, ""):
            continue  # omitted means "back to the platform default"
        try:
            cleaned[key] = max(0, int(value))
        except (TypeError, ValueError) as exc:
            raise HTTPException(400, f"{key} must be a whole number.") from exc

    row = await db.fetch_one(
        "UPDATE tenants SET limits = $2::jsonb WHERE id = $1 RETURNING limits",
        tenant_id, cleaned,
    )
    invalidate()
    return (row or {}).get("limits") or {}


# ===================================================================
# Lifecycle
# ===================================================================
async def log_event(
    *,
    tenant_id: int | None,
    tenant_slug: str,
    action: str,
    actor_id: int | None = None,
    actor_email: str | None = None,
    detail: dict | None = None,
    ip: Any = None,
) -> None:
    """Site lifecycle audit.

    Separate from activity_log because that table is tenant-scoped, and
    a hard delete cascades it away — the record of the deletion has to
    outlive the tenant it describes, which is why tenant_slug is stored
    as plain text alongside the (nullable) id.
    """
    try:
        await db.execute(
            """INSERT INTO tenant_events
                   (tenant_id, tenant_slug, action, actor_id, actor_email, detail, ip)
               VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7)""",
            tenant_id, tenant_slug, action, actor_id, actor_email, detail or {}, ip,
        )
    except Exception as exc:
        log.error("tenant event write failed: %s", exc)


async def free_slug(wanted: str) -> str:
    """The slug, or the next free `slug-2`, `slug-3`…"""
    base = check_slug(wanted)
    candidates = [base] + [f"{base[:56]}-{n}" for n in range(2, 20)]
    taken = {
        str(row["slug"]).lower()
        for row in await db.fetch(
            "SELECT slug FROM tenants WHERE slug = ANY($1::citext[])", candidates
        )
    }
    for candidate in candidates:
        if candidate not in taken:
            return candidate
    raise HTTPException(400, "That name is taken. Try a different one.")


def slugify_name(name: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")[:56]
    return cleaned or "site"


async def create_site(
    *,
    name: str,
    slug: str | None,
    owner_email: str,
    owner_name: str | None,
    owner_password: str,
    domain: str | None = None,
    plan: str = "standard",
    limits: dict | None = None,
    notes: str | None = None,
    actor_id: int | None = None,
    actor_email: str | None = None,
    ip: Any = None,
) -> dict:
    """Create and fully provision a site.

    The tenant, its owner and its starter form go in one transaction:
    a failure part-way must not leave an ownerless workspace that
    nobody can sign in to. Provisioning the defaults (content types,
    taxonomies, menus, templates, settings) runs after it commits,
    because a workspace missing a default menu is usable and one
    missing its owner is not.
    """
    from . import bootstrap  # noqa: PLC0415 — bootstrap imports db, not tenancy
    from .security import hash_password  # noqa: PLC0415 — avoids a cycle

    site_name = collapse(name, 120)
    if not site_name:
        raise HTTPException(400, "Give the site a name.")

    email = valid_email(owner_email)
    if not email:
        raise HTTPException(400, "The owner needs a valid email address.")

    final_slug = await free_slug(slug or slugify_name(site_name))
    host = check_domain(domain) if domain else None
    if host:
        clash = await db.fetch_one(
            "SELECT 1 FROM tenant_domains WHERE domain = $1", host
        )
        if clash:
            raise HTTPException(409, f"{host} is already claimed by another site.")

    # Hash before opening the transaction: bcrypt takes ~250ms and
    # holding a transaction open across it wastes a pooled connection.
    password_hash = await hash_password(owner_password)

    import asyncpg  # noqa: PLC0415

    try:
        async with db.pool().acquire() as conn:
            async with conn.transaction():
                tenant = await conn.fetchrow(
                    """INSERT INTO tenants (slug, name, plan, notes, limits, created_by,
                                            primary_domain, status)
                       VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7, 'active')
                       RETURNING id, slug::text AS slug, name""",
                    final_slug, site_name, collapse(plan, 40) or "standard",
                    collapse(notes, 1000), limits or {}, actor_id, host,
                )
                owner = await conn.fetchrow(
                    """INSERT INTO users (tenant_id, email, password_hash, display_name, role)
                       VALUES ($1, $2, $3, $4, 'owner')
                       RETURNING id, email::text AS email, display_name""",
                    tenant["id"], email, password_hash,
                    collapse(owner_name, 120) or email.split("@")[0],
                )
                await conn.execute(
                    """INSERT INTO forms (tenant_id, slug, name, fields, notify_emails)
                       VALUES ($1, 'contact', 'Contact form', $2::jsonb, $3)""",
                    tenant["id"], STARTER_FORM_FIELDS, [email],
                )
                if host:
                    await conn.execute(
                        """INSERT INTO tenant_domains
                               (tenant_id, domain, is_primary, is_verified, verified_at, created_by)
                           VALUES ($1, $2, TRUE, TRUE, now(), $3)""",
                        tenant["id"], host, actor_id,
                    )
    except asyncpg.UniqueViolationError as exc:
        # Two creations raced for the same slug or domain.
        raise HTTPException(409, "That slug or domain was just taken. Try again.") from exc

    provisioned = await bootstrap.provision_tenant(tenant["id"], created_by=owner["id"])
    invalidate()

    await log_event(
        tenant_id=tenant["id"], tenant_slug=tenant["slug"], action="created",
        actor_id=actor_id, actor_email=actor_email,
        detail={"name": site_name, "owner": email, "domain": host, "plan": plan},
        ip=ip,
    )
    log.info("provisioned site %s (id %s) for %s", tenant["slug"], tenant["id"], email)

    return {
        "tenant": await by_slug(tenant["slug"], cached=False),
        "owner": {"id": owner["id"], "email": owner["email"],
                  "display_name": owner["display_name"]},
        "provisioned": provisioned,
    }


# Same shape the seed script and self-service sign-up create, so intake
# and the page builder work on a new site from the first minute.
STARTER_FORM_FIELDS = [
    {"name": "full_name", "label": "Name", "type": "text", "required": True, "max": 120},
    {"name": "email", "label": "Email", "type": "email", "required": True},
    {"name": "phone", "label": "Phone", "type": "tel", "required": False},
    {"name": "company", "label": "Company", "type": "text", "required": False},
    {"name": "message", "label": "How can we help?", "type": "textarea",
     "required": True, "max": 4000},
]


async def set_status(
    tenant_id: int,
    status: str,
    *,
    reason: str | None = None,
    actor_id: int | None = None,
    actor_email: str | None = None,
    ip: Any = None,
) -> dict:
    """Suspend, resume or archive a site.

    Suspending ends its live sessions. Without that, "suspended" would
    mean "suspended at next sign-in" and anyone already signed in would
    keep working — which is not what anyone means by the word.
    """
    if status not in {"active", "suspended", "archived"}:
        raise HTTPException(400, "Status must be active, suspended or archived.")

    row = await db.fetch_one(
        f"""UPDATE tenants
               SET status = $2::tenant_status,
                   suspended_reason = CASE WHEN $2 = 'suspended' THEN $3 ELSE NULL END
             WHERE id = $1
             RETURNING {TENANT_COLUMNS.replace("t.", "")}""",
        tenant_id, status, collapse(reason, 500),
    )
    if not row:
        raise HTTPException(404, "Unknown site.")

    ended = 0
    if status != "active":
        gone = await db.fetch(
            "DELETE FROM sessions WHERE tenant_id = $1 RETURNING id", tenant_id
        )
        ended = len(gone)

    await invalidate_everywhere(row["slug"])
    await log_event(
        tenant_id=tenant_id, tenant_slug=row["slug"], action=status,
        actor_id=actor_id, actor_email=actor_email,
        detail={"reason": reason, "sessions_ended": ended}, ip=ip,
    )
    return {"tenant": row, "sessionsEnded": ended}


async def delete_site(
    tenant_id: int,
    *,
    actor_id: int | None = None,
    actor_email: str | None = None,
    ip: Any = None,
) -> dict:
    """Permanently delete a site and everything in it.

    Every business table is `ON DELETE CASCADE` from tenants, so one
    DELETE removes the lot. Stored media is a separate matter: the
    objects are listed and deleted from storage first, because once the
    rows are gone nothing knows which keys belonged to this site.
    """
    from . import storage  # noqa: PLC0415

    tenant = await db.fetch_one(
        "SELECT id, slug::text AS slug, name, status::text AS status FROM tenants WHERE id = $1",
        tenant_id,
    )
    if not tenant:
        raise HTTPException(404, "Unknown site.")
    if tenant["status"] != "archived":
        raise HTTPException(
            400,
            "Archive the site first. Deleting straight from active is how the "
            "wrong site gets deleted.",
        )

    media = await db.fetch(
        "SELECT storage_key, variants FROM media WHERE tenant_id = $1", tenant_id
    )
    keys: list[str] = []
    for row in media:
        keys.append(row["storage_key"])
        keys += [v["key"] for v in (row["variants"] or []) if v.get("key")]

    counts = await db.fetch_one(
        """SELECT (SELECT count(*) FROM users WHERE tenant_id = $1)::int AS users,
                  (SELECT count(*) FROM leads WHERE tenant_id = $1)::int AS leads,
                  (SELECT count(*) FROM content_items WHERE tenant_id = $1)::int AS content""",
        tenant_id,
    )

    storage.delete_many(keys)
    await db.execute("DELETE FROM tenants WHERE id = $1", tenant_id)
    await invalidate_everywhere(tenant["slug"])

    await log_event(
        tenant_id=None, tenant_slug=tenant["slug"], action="deleted",
        actor_id=actor_id, actor_email=actor_email,
        detail={"name": tenant["name"], "objects_deleted": len(keys), **dict(counts or {})},
        ip=ip,
    )
    log.warning("deleted site %s (%s objects removed)", tenant["slug"], len(keys))
    return {"ok": True, "slug": tenant["slug"], "objectsDeleted": len(keys),
            "removed": dict(counts or {})}


# ===================================================================
# Portfolio
# ===================================================================
async def list_sites(*, include_archived: bool = False, q: str | None = None) -> list[dict]:
    """Every site with its cached usage — the portfolio list.

    Reads tenant_usage rather than counting live, so this stays one
    indexed join whether there are ten sites or three hundred.
    """
    # replica: the portfolio is a dashboard over a roll-up that is
    # already up to an hour old, so replication lag changes nothing.
    return await db.fetch(
        f"""SELECT {TENANT_COLUMNS},
                   u.users, u.content_items, u.media_files, u.media_bytes,
                   u.leads_total, u.leads_30d, u.subscribers, u.page_views_30d,
                   u.computed_at,
                   (SELECT count(*)::int FROM tenant_domains d WHERE d.tenant_id = t.id)
                     AS domain_count,
                   (SELECT count(*)::int FROM tenant_memberships m WHERE m.tenant_id = t.id)
                     AS member_count
              FROM tenants t
              LEFT JOIN tenant_usage u ON u.tenant_id = t.id
             WHERE ($1::boolean OR t.status <> 'archived')
               AND ($2::text IS NULL
                    OR t.name ILIKE '%' || $2 || '%'
                    OR t.slug::text ILIKE '%' || $2 || '%'
                    OR EXISTS (SELECT 1 FROM tenant_domains d
                                WHERE d.tenant_id = t.id
                                  AND d.domain::text ILIKE '%' || $2 || '%'))
             ORDER BY t.status, t.name""",
        include_archived, collapse(q, 120),
        replica=True,
    )


async def refresh_all_usage() -> int:
    """Recompute every site's usage. Called by the platform worker."""
    tenants = await db.fetch("SELECT id FROM tenants WHERE status <> 'archived' ORDER BY id")
    for tenant in tenants:
        try:
            await compute_usage(tenant["id"])
        except Exception as exc:
            log.error("usage refresh failed for tenant %s: %s", tenant["id"], exc)
    return len(tenants)


def _register() -> None:
    from . import cache  # noqa: PLC0415

    cache.subscribe("tenant", _on_invalidate)


_register()
