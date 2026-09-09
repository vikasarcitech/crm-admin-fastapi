"""Deployment & publishing (2.10).

The admin half — build hooks, CDN invalidation, API key management —
plus the versioned public content API a static frontend reads at build
time or runtime.

The public API is under ``/api/v1/`` and serves only
``published_snapshot``. Versioning the path matters here in a way it
does not for the admin API: a frontend is deployed separately and may
lag the CMS by weeks, so the shape it reads has to stay stable even
while the admin API changes.
"""

from __future__ import annotations

import ipaddress
import logging
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request

from .. import db, events, publishing, tenancy
from ..config import settings
from ..content import public_path
from ..permissions import require_perm
from ..schemas import (
    ApiKeyCreate,
    BuildHookCreate,
    BuildHookUpdate,
    BuildTrigger,
    InvalidationRequest,
    collapse,
)
from ..security import CurrentUser, client_ip, sha256, tenant_db

log = logging.getLogger("crm.deploy")

router = APIRouter(prefix="/api/publishing", tags=["publishing"])
public_router = APIRouter(prefix="/api/v1", tags=["content-public"])

API_SCOPES = (
    "content:read", "content:write", "media:read", "leads:write",
    "leads:read", "config:read", "deploy:trigger",
)

HOOK_COLUMNS = """id, name, provider, url, trigger_events, debounce_seconds,
                  is_active, last_triggered_at, created_at,
                  (auth_token IS NOT NULL) AS has_token"""


# ========================================================== build hooks
def _validate_hook_url(raw: str) -> str:
    """Admin-supplied URLs are an SSRF vector: a hook pointing at
    169.254.169.254 would hand instance credentials to whoever set it.

    Same rule as webhook_endpoints in app/routers/admin.py.
    """
    parsed = urlparse((raw or "").strip())
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
        return raw.strip()  # a name; DNS rebinding is the egress rules' problem
    if address.is_private or address.is_loopback or address.is_link_local or address.is_reserved:
        raise HTTPException(400, "Internal addresses are not allowed.")
    return raw.strip()


@router.get("/hooks")
async def list_hooks(user: CurrentUser = Depends(require_perm("deploy.manage"))) -> dict:
    scoped = db.TenantDB(user.tenant_id)
    hooks = await scoped.fetch(
        f"SELECT {HOOK_COLUMNS} FROM build_hooks WHERE tenant_id = $1 ORDER BY created_at DESC"
    )
    runs = await scoped.fetch(
        """SELECT r.id, r.status::text AS status, r.reason, r.attempts, r.response_code,
                  r.error, r.created_at, r.finished_at, h.name AS hook_name
             FROM build_runs r JOIN build_hooks h ON h.id = r.hook_id
            WHERE r.tenant_id = $1 ORDER BY r.created_at DESC LIMIT 30"""
    )
    invalidations = await scoped.fetch(
        """SELECT id, provider, paths, status::text AS status, reference, error,
                  created_at, finished_at
             FROM cdn_invalidations WHERE tenant_id = $1
            ORDER BY created_at DESC LIMIT 20"""
    )
    return {
        "hooks": hooks,
        "runs": runs,
        "invalidations": invalidations,
        "events": sorted(events.PLATFORM_EVENTS),
        "cdn": {
            "provider": "cloudfront" if settings.cloudfront_distribution_id else None,
            "configured": bool(settings.cloudfront_distribution_id),
        },
    }


@router.post("/hooks", status_code=201)
async def create_hook(
    payload: BuildHookCreate,
    request: Request,
    user: CurrentUser = Depends(require_perm("deploy.manage")),
) -> dict:
    url = _validate_hook_url(payload.url)
    chosen = [
        event for event in (payload.trigger_events or []) if event in events.PLATFORM_EVENTS
    ] or ["content.published"]

    scoped = db.TenantDB(user.tenant_id)
    row = await scoped.fetch_one(
        f"""INSERT INTO build_hooks (tenant_id, name, provider, url, auth_token,
                                     trigger_events, debounce_seconds, created_by)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            RETURNING {HOOK_COLUMNS}""",
        collapse(payload.name, 80), payload.provider, url,
        payload.auth_token or None, chosen, payload.debounce_seconds, user.id,
    )
    await events.log_activity(
        user.tenant_id, "build_hook.created", user_id=user.id,
        object_type="build_hook", object_id=row["id"],
        meta={"provider": payload.provider, "events": chosen},
        ip=db.to_inet(client_ip(request)),
    )
    return {"hook": row}


@router.patch("/hooks/{hook_id}")
async def update_hook(
    hook_id: int,
    payload: BuildHookUpdate,
    user: CurrentUser = Depends(require_perm("deploy.manage")),
) -> dict:
    scoped = db.TenantDB(user.tenant_id)
    sent = payload.model_dump(exclude_unset=True)
    if not sent:
        raise HTTPException(400, "Nothing to save.")

    chosen = (
        [e for e in (payload.trigger_events or []) if e in events.PLATFORM_EVENTS] or None
        if "trigger_events" in sent
        else None
    )
    row = await scoped.fetch_one(
        f"""UPDATE build_hooks
               SET name = coalesce($3, name),
                   url = coalesce($4, url),
                   -- An empty string clears the token; omitting the field
                   -- keeps whatever is stored (it is never read back).
                   auth_token = CASE WHEN $5 THEN nullif($6, '') ELSE auth_token END,
                   trigger_events = coalesce($7::text[], trigger_events),
                   debounce_seconds = coalesce($8, debounce_seconds),
                   is_active = coalesce($9, is_active)
             WHERE tenant_id = $1 AND id = $2 RETURNING {HOOK_COLUMNS}""",
        hook_id,
        collapse(payload.name, 80),
        _validate_hook_url(payload.url) if payload.url else None,
        "auth_token" in sent, payload.auth_token or "",
        chosen, payload.debounce_seconds, payload.is_active,
    )
    if not row:
        raise HTTPException(404, "That build hook no longer exists.")
    return {"hook": row}


@router.delete("/hooks/{hook_id}")
async def delete_hook(
    hook_id: int, user: CurrentUser = Depends(require_perm("deploy.manage"))
) -> dict:
    scoped = db.TenantDB(user.tenant_id)
    removed = await scoped.fetch(
        "DELETE FROM build_hooks WHERE tenant_id = $1 AND id = $2 RETURNING id", hook_id
    )
    if not removed:
        raise HTTPException(404, "That build hook no longer exists.")
    return {"ok": True}


@router.post("/trigger")
async def trigger_build(
    payload: BuildTrigger,
    request: Request,
    user: CurrentUser = Depends(require_perm("deploy.trigger")),
) -> dict:
    """Manual rebuild. Bypasses the event filter but honours debounce."""
    queued = await publishing.trigger_builds(
        user.tenant_id,
        event="manual",
        hook_id=payload.hook_id,
        reason=collapse(payload.reason, 200) or f"manual by {user.name}",
    )
    if not queued:
        raise HTTPException(400, "No active build hook is configured.")

    await events.log_activity(
        user.tenant_id, "build.triggered", user_id=user.id,
        meta={"hooks": len(queued)}, ip=db.to_inet(client_ip(request)),
    )
    return {"ok": True, "queued": queued}


@router.post("/invalidate")
async def invalidate_cdn(
    payload: InvalidationRequest,
    user: CurrentUser = Depends(require_perm("deploy.trigger")),
) -> dict:
    """Queue a CDN purge for specific paths ('/*' for everything)."""
    run_id = await publishing.queue_invalidation(user.tenant_id, payload.paths)
    if run_id is None:
        raise HTTPException(400, "Give at least one path to invalidate.")
    await events.log_activity(
        user.tenant_id, "cdn.invalidation_queued", user_id=user.id,
        meta={"paths": payload.paths[:20]},
    )
    return {
        "ok": True,
        "id": run_id,
        "configured": bool(settings.cloudfront_distribution_id),
    }


@router.post("/sitemap")
async def regenerate_sitemap(
    user: CurrentUser = Depends(require_perm("deploy.trigger")),
) -> dict:
    return await publishing.regenerate_sitemaps(user.tenant_id)


@router.get("/status")
async def publishing_status(scoped: db.TenantDB = Depends(tenant_db)) -> dict:
    """What is live, what is waiting, and what failed — the answer to
    "did my change actually reach the website?"."""
    content = await scoped.fetch_one(
        """SELECT count(*) FILTER (WHERE status = 'published')::int AS published,
                  count(*) FILTER (WHERE status = 'draft')::int AS drafts,
                  count(*) FILTER (WHERE status = 'scheduled')::int AS scheduled,
                  count(*) FILTER (WHERE status = 'trashed')::int AS trashed,
                  max(published_at) AS last_published_at
             FROM content_items WHERE tenant_id = $1"""
    )
    # Published content edited since the publish: live, but stale.
    stale = await scoped.fetch(
        """SELECT i.id, i.title, i.slug::text AS slug, i.updated_at, i.published_at,
                  t.route_prefix
             FROM content_items i JOIN content_types t ON t.id = i.type_id
            WHERE i.tenant_id = $1 AND i.status = 'published'
              AND i.updated_at > i.published_at + interval '2 seconds'
            ORDER BY i.updated_at DESC LIMIT 25"""
    )
    for row in stale:
        row["path"] = public_path(row.pop("route_prefix"), row["slug"])

    due = await scoped.fetch(
        """SELECT i.id, i.title, i.scheduled_for, t.slug::text AS type_slug
             FROM content_items i JOIN content_types t ON t.id = i.type_id
            WHERE i.tenant_id = $1 AND i.status = 'scheduled'
            ORDER BY i.scheduled_for LIMIT 25"""
    )
    builds = await scoped.fetch(
        """SELECT r.status::text AS status, count(*)::int AS n
             FROM build_runs r
            WHERE r.tenant_id = $1 AND r.created_at > now() - interval '7 days'
            GROUP BY 1"""
    )
    last_build = await scoped.fetch_one(
        """SELECT r.status::text AS status, r.created_at, r.finished_at, r.error,
                  h.name AS hook_name
             FROM build_runs r JOIN build_hooks h ON h.id = r.hook_id
            WHERE r.tenant_id = $1 ORDER BY r.created_at DESC LIMIT 1"""
    )
    sitemap = await scoped.fetch_one(
        "SELECT generated_at, url_count FROM sitemap_cache WHERE tenant_id = $1 AND name = 'index'"
    )
    return {
        "content": content,
        "staleContent": stale,
        "scheduled": due,
        "builds": {row["status"]: row["n"] for row in builds},
        "lastBuild": last_build,
        "sitemap": sitemap,
        "siteUrl": await publishing.site_base_url(scoped.tenant_id) or None,
    }


# ============================================================= API keys
@router.get("/api-keys")
async def list_api_keys(user: CurrentUser = Depends(require_perm("apikeys.manage"))) -> dict:
    scoped = db.TenantDB(user.tenant_id)
    rows = await scoped.fetch(
        """SELECT k.id, k.name, k.key_prefix, k.scopes, k.note, k.last_used_at,
                  k.last_used_ip::text AS last_used_ip, k.expires_at, k.revoked_at,
                  k.created_at, u.display_name AS created_by_name,
                  (k.revoked_at IS NULL
                   AND (k.expires_at IS NULL OR k.expires_at > now())) AS is_valid
             FROM api_keys k LEFT JOIN users u ON u.id = k.created_by
            WHERE k.tenant_id = $1 ORDER BY k.created_at DESC"""
    )
    return {"keys": rows, "scopes": list(API_SCOPES)}


@router.post("/api-keys", status_code=201)
async def create_api_key(
    payload: ApiKeyCreate,
    request: Request,
    user: CurrentUser = Depends(require_perm("apikeys.manage")),
) -> dict:
    """Mint a key. The secret is shown once and only its hash is kept."""
    await tenancy.enforce_limit(user.tenant_id, "api_keys")

    unknown = set(payload.scopes or []) - set(API_SCOPES)
    if unknown:
        raise HTTPException(400, f"Unknown scope(s): {', '.join(sorted(unknown))}")
    scopes = payload.scopes or ["content:read", "config:read"]

    secret = secrets.token_urlsafe(32)
    prefix = f"ck_{secret[:8]}"
    full_key = f"{prefix}.{secret}"
    expires = (
        datetime.now(timezone.utc) + timedelta(days=payload.expires_in_days)
        if payload.expires_in_days else None
    )

    scoped = db.TenantDB(user.tenant_id)
    row = await scoped.fetch_one(
        """INSERT INTO api_keys (tenant_id, name, key_prefix, key_hash, scopes,
                                 expires_at, note, created_by)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
           RETURNING id, name, key_prefix, scopes, note, expires_at, created_at""",
        collapse(payload.name, 80), prefix, sha256(full_key), scopes,
        expires, collapse(payload.note, 200), user.id,
    )
    await events.log_activity(
        user.tenant_id, "api_key.created", user_id=user.id,
        object_type="api_key", object_id=row["id"], meta={"scopes": scopes},
        ip=db.to_inet(client_ip(request)),
    )
    # Returned once, at creation, and never listed again.
    return {"key": {**row, "is_valid": True}, "secret": full_key}


@router.delete("/api-keys/{key_id}")
async def revoke_api_key(
    key_id: int,
    request: Request,
    user: CurrentUser = Depends(require_perm("apikeys.manage")),
) -> dict:
    """Revoked, not deleted: the row is the audit trail of what the key
    was used for and when."""
    scoped = db.TenantDB(user.tenant_id)
    row = await scoped.fetch_one(
        """UPDATE api_keys SET revoked_at = now()
            WHERE tenant_id = $1 AND id = $2 AND revoked_at IS NULL
            RETURNING id, name""",
        key_id,
    )
    if not row:
        raise HTTPException(404, "That key is missing or already revoked.")
    await events.log_activity(
        user.tenant_id, "api_key.revoked", user_id=user.id,
        object_type="api_key", object_id=key_id, ip=db.to_inet(client_ip(request)),
    )
    return {"ok": True}


# ================================================== public content API
async def resolve_api_key(
    tenant_id: int, authorization: str | None, request: Request, scope: str
) -> None:
    """Check the bearer token, if one is required.

    Published content is public by design, so a key is only *required*
    for reads when the tenant turns on `api.require_key`. When a key is
    presented it must still be valid and carry the scope — a bad key is
    an error, not a fallback to anonymous.
    """
    setting = await db.fetch_one(
        "SELECT value FROM settings WHERE tenant_id = $1 AND key = 'api'", tenant_id
    )
    required = bool(((setting or {}).get("value") or {}).get("require_key"))

    token = None
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()

    if not token:
        if required:
            raise HTTPException(401, "This site's content API requires an API key.")
        return

    row = await db.fetch_one(
        """SELECT id, scopes, expires_at, revoked_at FROM api_keys
            WHERE tenant_id = $1 AND key_hash = $2""",
        tenant_id, sha256(token),
    )
    if not row or row["revoked_at"]:
        raise HTTPException(401, "That API key is not valid.")
    if row["expires_at"] and row["expires_at"] <= datetime.now(timezone.utc):
        raise HTTPException(401, "That API key has expired.")
    if scope not in (row["scopes"] or []):
        raise HTTPException(403, f"That API key does not carry the {scope} scope.")

    await db.execute(
        "UPDATE api_keys SET last_used_at = now(), last_used_ip = $2 WHERE id = $1",
        row["id"], db.to_inet(client_ip(request)),
    )


async def _tenant(slug: str | None, request: Request) -> dict:
    """Resolve by slug, falling back to the Host header.

    Goes through tenancy.resolve_public so a suspended site answers 503
    rather than serving stale content, and an archived one is 404.
    """
    return await tenancy.resolve_public(slug, request.headers.get("host"))


CACHE_PUBLIC = {"cache-control": "public, max-age=60, stale-while-revalidate=300"}


@public_router.get("/{tenant_slug}/content/{type_slug}")
async def public_list(
    tenant_slug: str,
    type_slug: str,
    request: Request,
    term: str | None = Query(default=None, max_length=80),
    taxonomy: str | None = Query(default=None, max_length=60),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0, le=100_000),
    order: str = Query(default="published"),
    authorization: str | None = Header(default=None),
) -> dict:
    """Published items of one type. Serves the snapshot, never the draft."""
    tenant = await _tenant(tenant_slug, request)
    await resolve_api_key(tenant["id"], authorization, request, "content:read")

    content_type = await db.fetch_one(
        """SELECT id, slug::text AS slug, name, plural_name, route_prefix
             FROM content_types
            WHERE tenant_id = $1 AND slug = $2 AND is_active""",
        tenant["id"], collapse(type_slug, 60),
    )
    if not content_type:
        raise HTTPException(404, "Unknown content type.")

    ordering = {
        "published": "i.published_at DESC NULLS LAST",
        "oldest": "i.published_at ASC NULLS LAST",
        "title": "i.title ASC",
        "order": "i.menu_order ASC, i.title ASC",
    }.get(order, "i.published_at DESC NULLS LAST")

    rows = await db.fetch(
        f"""SELECT i.published_snapshot AS item
              FROM content_items i
             WHERE i.tenant_id = $1 AND i.type_id = $2
               AND i.status = 'published' AND i.published_snapshot IS NOT NULL
               AND ($5::text IS NULL OR EXISTS (
                     SELECT 1 FROM content_terms ct
                       JOIN terms tm ON tm.id = ct.term_id
                       JOIN taxonomies tx ON tx.id = tm.taxonomy_id
                      WHERE ct.item_id = i.id AND tm.slug::text = $5
                        AND ($6::text IS NULL OR tx.slug::text = $6)))
             ORDER BY {ordering}
             LIMIT $3 OFFSET $4""",
        tenant["id"], content_type["id"], limit, offset,
        collapse(term, 80), collapse(taxonomy, 60),
    )
    total = await db.fetch_one(
        """SELECT count(*)::int AS n FROM content_items
            WHERE tenant_id = $1 AND type_id = $2 AND status = 'published'
              AND published_snapshot IS NOT NULL""",
        tenant["id"], content_type["id"],
    )
    return {
        "type": {
            "slug": content_type["slug"],
            "name": content_type["name"],
            "pluralName": content_type["plural_name"],
            "routePrefix": content_type["route_prefix"],
        },
        "items": [row["item"] for row in rows],
        "total": total["n"],
        "limit": limit,
        "offset": offset,
    }


@public_router.get("/{tenant_slug}/content/{type_slug}/{item_slug}")
async def public_item(
    tenant_slug: str,
    type_slug: str,
    item_slug: str,
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict:
    tenant = await _tenant(tenant_slug, request)
    await resolve_api_key(tenant["id"], authorization, request, "content:read")

    row = await db.fetch_one(
        """SELECT i.published_snapshot AS item
             FROM content_items i JOIN content_types t ON t.id = i.type_id
            WHERE i.tenant_id = $1 AND t.slug = $2 AND i.slug = $3
              AND i.status = 'published' AND i.published_snapshot IS NOT NULL""",
        tenant["id"], collapse(type_slug, 60), collapse(item_slug, 80),
    )
    if not row:
        raise HTTPException(404, "That content is not published.")
    return {"item": row["item"]}


@public_router.get("/{tenant_slug}/all")
async def public_all(
    tenant_slug: str,
    request: Request,
    since: datetime | None = None,
    authorization: str | None = Header(default=None),
) -> dict:
    """Everything published, for a static build.

    `since` makes incremental builds possible: pass the previous build's
    `generatedAt` and only changed items come back.
    """
    tenant = await _tenant(tenant_slug, request)
    await resolve_api_key(tenant["id"], authorization, request, "content:read")

    rows = await db.fetch(
        """SELECT t.slug::text AS type_slug, i.published_snapshot AS item, i.published_at
             FROM content_items i JOIN content_types t ON t.id = i.type_id
            WHERE i.tenant_id = $1 AND i.status = 'published'
              AND i.published_snapshot IS NOT NULL
              AND ($2::timestamptz IS NULL OR i.updated_at > $2)
            ORDER BY t.slug, i.published_at DESC NULLS LAST
            LIMIT 20000""",
        tenant["id"], since,
    )
    grouped: dict[str, list] = {}
    for row in rows:
        grouped.setdefault(row["type_slug"], []).append(row["item"])

    terms = await db.fetch(
        """SELECT tx.slug::text AS taxonomy, tm.slug::text AS slug, tm.name,
                  tm.description, tm.parent_id, tm.seo,
                  count(ct.item_id)::int AS item_count
             FROM terms tm
             JOIN taxonomies tx ON tx.id = tm.taxonomy_id
             LEFT JOIN content_terms ct ON ct.term_id = tm.id
            WHERE tm.tenant_id = $1
            GROUP BY tx.slug, tm.id ORDER BY tx.slug, tm.sort_order, tm.name""",
        tenant["id"],
    )
    taxonomies: dict[str, list] = {}
    for row in terms:
        taxonomies.setdefault(row.pop("taxonomy"), []).append(row)

    return {
        "site": {"slug": tenant["slug"], "name": tenant["name"]},
        "content": grouped,
        "taxonomies": taxonomies,
        "counts": {key: len(value) for key, value in grouped.items()},
        "since": since.isoformat() if since else None,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
    }


@public_router.get("/preview/{token}")
async def preview_content(token: str) -> dict:
    """Draft preview by token — the unpublished state, on purpose.

    Cache-control is no-store: a CDN caching a draft preview would leak
    unpublished content to anyone who later hit the same URL.
    """
    from fastapi.responses import JSONResponse  # noqa: PLC0415

    from ..content import build_snapshot  # noqa: PLC0415

    row = await db.fetch_one(
        """SELECT p.item_id, p.tenant_id,
                  i.id, i.slug, i.title, i.excerpt, i.body, i.fields, i.seo,
                  i.status::text AS status, i.author_id, i.featured_media_id,
                  i.menu_order, i.published_at, i.updated_at,
                  t.slug AS type_slug, t.route_prefix
             FROM preview_tokens p
             JOIN content_items i ON i.id = p.item_id
             JOIN content_types t ON t.id = i.type_id
            WHERE p.token_hash = $1 AND p.expires_at > now()""",
        sha256(collapse(token, 200) or ""),
    )
    if not row:
        raise HTTPException(404, "That preview link has expired.")

    terms = await db.fetch(
        """SELECT tm.id, tm.name, tm.slug::text AS slug, tx.slug::text AS taxonomy
             FROM content_terms ct
             JOIN terms tm ON tm.id = ct.term_id
             JOIN taxonomies tx ON tx.id = tm.taxonomy_id
            WHERE ct.item_id = $1""",
        row["item_id"],
    )
    snapshot = build_snapshot(
        row, {"slug": row["type_slug"], "route_prefix": row["route_prefix"]}, terms
    )
    return JSONResponse(
        {"item": snapshot, "isPreview": True, "status": row["status"]},
        headers={"cache-control": "no-store", "x-robots-tag": "noindex, nofollow"},
    )
