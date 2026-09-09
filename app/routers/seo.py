"""SEO & site discovery (2.2).

Per-item meta, Open Graph and Schema.org fields live on the item itself
(``content_items.seo``, validated in app/content.py). This router owns
the site-wide pieces:

* the 301/302 redirect manager, including the redirects the content
  router creates automatically when a published slug changes;
* the folded 404 log, and one-click "turn this 404 into a redirect";
* sitemap regeneration and serving, plus robots.txt;
* breadcrumb configuration, which the frontend reads from the public
  config endpoint.

Redirect *serving* is a frontend/CDN concern for a static site, so the
resolve endpoint is deliberately a lookup the frontend (or an edge
function) calls rather than a 302 this app issues.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse, Response

from .. import db, events, publishing, tenancy
from ..permissions import require_perm
from ..schemas import (
    NotFoundResolve,
    RedirectCreate,
    RedirectUpdate,
    collapse,
)
from ..security import CurrentUser, client_ip, tenant_db

router = APIRouter(prefix="/api/seo", tags=["seo"])
public_router = APIRouter(tags=["seo-public"])

MAX_REDIRECT_HOPS = 5


def normalise_path(raw: str) -> str:
    """'/About/?utm=1' → '/about'.

    Redirects match on path only: query strings are the visitor's, and a
    trailing-slash mismatch is the single most common reason a redirect
    silently fails to fire.
    """
    value = (raw or "").strip()
    if not value:
        raise HTTPException(400, "A path is required.")
    if re.match(r"^https?://", value, re.IGNORECASE):
        parsed = urlparse(value)
        value = parsed.path or "/"
    value = value.split("?")[0].split("#")[0]
    if not value.startswith("/"):
        value = f"/{value}"
    value = re.sub(r"/{2,}", "/", value).lower()
    if len(value) > 1:
        value = value.rstrip("/") or "/"
    if len(value) > 500:
        raise HTTPException(400, "That path is too long.")
    return value


def normalise_target(raw: str) -> str:
    """Targets may be absolute (off-site) or a local path."""
    value = (raw or "").strip()
    if not value:
        raise HTTPException(400, "A destination is required.")
    if re.match(r"^https?://", value, re.IGNORECASE):
        if len(value) > 500:
            raise HTTPException(400, "That URL is too long.")
        return value
    return normalise_path(value)


# ============================================================= redirects
@router.get("/redirects")
async def list_redirects(
    q: str | None = Query(default=None, max_length=200),
    automatic: bool | None = None,
    scoped: db.TenantDB = Depends(tenant_db),
) -> dict:
    rows = await scoped.fetch(
        """SELECT r.id, r.from_path, r.to_path, r.status_code, r.is_active,
                  r.is_automatic, r.note, r.hits, r.last_hit_at, r.created_at,
                  u.display_name AS created_by_name
             FROM redirects r LEFT JOIN users u ON u.id = r.created_by
            WHERE r.tenant_id = $1
              AND ($2::text IS NULL OR r.from_path ILIKE '%' || $2 || '%'
                                    OR r.to_path ILIKE '%' || $2 || '%')
              AND ($3::boolean IS NULL OR r.is_automatic = $3)
            ORDER BY r.hits DESC, r.created_at DESC
            LIMIT 500""",
        q.strip() if q else None,
        automatic,
    )
    return {"redirects": rows}


@router.post("/redirects", status_code=201)
async def create_redirect(
    payload: RedirectCreate,
    request: Request,
    user: CurrentUser = Depends(require_perm("redirects.manage")),
) -> dict:
    scoped = db.TenantDB(user.tenant_id)
    from_path = normalise_path(payload.from_path)
    to_path = normalise_target(payload.to_path)

    if from_path == to_path:
        raise HTTPException(400, "A redirect cannot point at itself.")

    # A → B where B → A already exists is an infinite loop for the
    # visitor's browser, so refuse it at the point of creation.
    if await _would_loop(scoped, from_path, to_path):
        raise HTTPException(
            400, f"That would create a redirect loop back to {from_path}."
        )

    row = await scoped.fetch_one(
        """INSERT INTO redirects (tenant_id, from_path, to_path, status_code, note, created_by)
           VALUES ($1, $2, $3, $4, $5, $6)
           ON CONFLICT (tenant_id, from_path) DO UPDATE
              SET to_path = EXCLUDED.to_path, status_code = EXCLUDED.status_code,
                  note = EXCLUDED.note, is_active = TRUE, is_automatic = FALSE
           RETURNING id, from_path, to_path, status_code, is_active, is_automatic,
                     note, hits, created_at""",
        from_path, to_path, payload.status_code, collapse(payload.note, 200), user.id,
    )
    await events.log_activity(
        user.tenant_id, "redirect.created", user_id=user.id,
        object_type="redirect", object_id=row["id"],
        meta={"from": from_path, "to": to_path}, ip=db.to_inet(client_ip(request)),
    )
    return {"redirect": row}


async def _would_loop(scoped: db.TenantDB, from_path: str, to_path: str) -> bool:
    """Walk the chain forward from the new target."""
    seen = {from_path}
    cursor = to_path
    for _ in range(MAX_REDIRECT_HOPS):
        if cursor in seen:
            return True
        seen.add(cursor)
        row = await scoped.fetch_one(
            "SELECT to_path FROM redirects WHERE tenant_id = $1 AND from_path = $2 AND is_active",
            cursor,
        )
        if not row:
            return False
        cursor = row["to_path"]
    return True  # longer than MAX_REDIRECT_HOPS is a loop for practical purposes


@router.patch("/redirects/{redirect_id}")
async def update_redirect(
    redirect_id: int,
    payload: RedirectUpdate,
    user: CurrentUser = Depends(require_perm("redirects.manage")),
) -> dict:
    scoped = db.TenantDB(user.tenant_id)
    existing = await scoped.fetch_one(
        "SELECT from_path FROM redirects WHERE tenant_id = $1 AND id = $2", redirect_id
    )
    if not existing:
        raise HTTPException(404, "That redirect no longer exists.")

    sent = payload.model_dump(exclude_unset=True)
    if not sent:
        raise HTTPException(400, "Nothing to save.")

    to_path = normalise_target(payload.to_path) if payload.to_path else None
    if to_path and to_path == existing["from_path"]:
        raise HTTPException(400, "A redirect cannot point at itself.")

    row = await scoped.fetch_one(
        """UPDATE redirects
              SET to_path = coalesce($3, to_path),
                  status_code = coalesce($4, status_code),
                  is_active = coalesce($5, is_active),
                  note = CASE WHEN $6 THEN $7 ELSE note END,
                  -- Editing an automatic redirect makes it a manual one,
                  -- so a later publish will not overwrite the change.
                  is_automatic = FALSE
            WHERE tenant_id = $1 AND id = $2
            RETURNING id, from_path, to_path, status_code, is_active, is_automatic,
                      note, hits, last_hit_at, created_at""",
        redirect_id, to_path, payload.status_code,
        payload.is_active, "note" in sent, collapse(payload.note, 200),
    )
    return {"redirect": row}


@router.delete("/redirects/{redirect_id}")
async def delete_redirect(
    redirect_id: int, user: CurrentUser = Depends(require_perm("redirects.manage"))
) -> dict:
    scoped = db.TenantDB(user.tenant_id)
    removed = await scoped.fetch(
        "DELETE FROM redirects WHERE tenant_id = $1 AND id = $2 RETURNING from_path",
        redirect_id,
    )
    if not removed:
        raise HTTPException(404, "That redirect no longer exists.")
    return {"ok": True}


@router.post("/redirects/bulk-delete")
async def bulk_delete_redirects(
    ids: list[int],
    user: CurrentUser = Depends(require_perm("redirects.manage")),
) -> dict:
    scoped = db.TenantDB(user.tenant_id)
    removed = await scoped.fetch(
        "DELETE FROM redirects WHERE tenant_id = $1 AND id = ANY($2::bigint[]) RETURNING id",
        sorted(set(ids))[:500],
    )
    return {"ok": True, "deleted": len(removed)}


# ============================================================== 404 log
@router.get("/not-found")
async def list_not_found(
    include_ignored: bool = False,
    scoped: db.TenantDB = Depends(tenant_db),
) -> dict:
    """Missing paths with hit counts, plus a suggested destination.

    The suggestion is a slug match against published content: most 404s
    on a site rebuild are a path that moved, not a path that vanished.
    """
    rows = await scoped.fetch(
        """SELECT n.id, n.path, n.hits, n.last_referrer, n.is_ignored,
                  n.first_seen_at, n.last_seen_at, n.resolved_redirect_id
             FROM not_found_log n
            WHERE n.tenant_id = $1
              AND ($2::boolean OR NOT n.is_ignored)
            ORDER BY n.resolved_redirect_id NULLS FIRST, n.hits DESC
            LIMIT 300""",
        include_ignored,
    )
    for row in rows:
        row["suggestion"] = await _suggest_target(scoped, row["path"])
    return {"notFound": rows}


async def _suggest_target(scoped: db.TenantDB, path: str) -> dict | None:
    slug = (path or "").rstrip("/").rsplit("/", 1)[-1].lower()
    if not slug:
        return None
    row = await scoped.fetch_one(
        """SELECT i.slug::text AS slug, i.title, t.route_prefix
             FROM content_items i JOIN content_types t ON t.id = i.type_id
            WHERE i.tenant_id = $1 AND i.status = 'published'
              AND t.route_prefix IS NOT NULL
              AND (i.slug = $2 OR i.slug ILIKE $2 || '%' OR $2 ILIKE i.slug::text || '%')
            ORDER BY (i.slug = $2) DESC, length(i.slug::text)
            LIMIT 1""",
        slug,
    )
    if not row:
        return None
    from ..content import public_path  # noqa: PLC0415 — avoids a cycle at import

    target = public_path(row["route_prefix"], row["slug"])
    return {"toPath": target, "title": row["title"]} if target else None


@router.post("/not-found/{entry_id}/resolve")
async def resolve_not_found(
    entry_id: int,
    payload: NotFoundResolve,
    user: CurrentUser = Depends(require_perm("redirects.manage")),
) -> dict:
    """Create the redirect and link it to the logged 404 in one step."""
    scoped = db.TenantDB(user.tenant_id)
    entry = await scoped.fetch_one(
        "SELECT path FROM not_found_log WHERE tenant_id = $1 AND id = $2", entry_id
    )
    if not entry:
        raise HTTPException(404, "That entry no longer exists.")

    to_path = normalise_target(payload.to_path)
    if to_path == entry["path"]:
        raise HTTPException(400, "A redirect cannot point at itself.")

    redirect = await scoped.fetch_one(
        """INSERT INTO redirects (tenant_id, from_path, to_path, status_code, note, created_by)
           VALUES ($1, $2, $3, $4, 'Created from the 404 log', $5)
           ON CONFLICT (tenant_id, from_path) DO UPDATE
              SET to_path = EXCLUDED.to_path, is_active = TRUE, is_automatic = FALSE
           RETURNING id, from_path, to_path, status_code""",
        entry["path"], to_path, payload.status_code, user.id,
    )
    await scoped.execute(
        "UPDATE not_found_log SET resolved_redirect_id = $3 WHERE tenant_id = $1 AND id = $2",
        entry_id, redirect["id"],
    )
    return {"redirect": redirect}


@router.post("/not-found/{entry_id}/ignore")
async def ignore_not_found(
    entry_id: int, user: CurrentUser = Depends(require_perm("redirects.manage"))
) -> dict:
    """Most 404 volume is bot probing (/wp-login.php); ignoring keeps the
    list about real broken links."""
    scoped = db.TenantDB(user.tenant_id)
    await scoped.execute(
        "UPDATE not_found_log SET is_ignored = TRUE WHERE tenant_id = $1 AND id = $2",
        entry_id,
    )
    return {"ok": True}


@router.delete("/not-found")
async def clear_not_found(
    resolved_only: bool = True,
    user: CurrentUser = Depends(require_perm("redirects.manage")),
) -> dict:
    scoped = db.TenantDB(user.tenant_id)
    removed = await scoped.fetch(
        """DELETE FROM not_found_log
            WHERE tenant_id = $1
              AND (NOT $2::boolean OR resolved_redirect_id IS NOT NULL OR is_ignored)
           RETURNING id""",
        resolved_only,
    )
    return {"ok": True, "deleted": len(removed)}


# ============================================================== sitemaps
@router.get("/sitemaps")
async def sitemap_status(scoped: db.TenantDB = Depends(tenant_db)) -> dict:
    rows = await scoped.fetch(
        """SELECT name, url_count, generated_at, length(xml) AS bytes
             FROM sitemap_cache WHERE tenant_id = $1 ORDER BY name"""
    )
    base = await publishing.site_base_url(scoped.tenant_id)
    return {
        "sitemaps": rows,
        "siteUrl": base or None,
        "indexUrl": f"{base}/sitemap.xml" if base else None,
        # Without an absolute origin a sitemap is invalid, so surface the
        # missing setting rather than generating something Google rejects.
        "ready": bool(base),
    }


@router.post("/sitemaps/regenerate")
async def regenerate(
    user: CurrentUser = Depends(require_perm("seo.manage")),
) -> dict:
    result = await publishing.regenerate_sitemaps(user.tenant_id)
    if not result.get("ok"):
        raise HTTPException(
            400,
            "Set the site URL in Settings → Site identity before generating a sitemap.",
        )
    await events.log_activity(
        user.tenant_id, "sitemap.regenerated", user_id=user.id, meta=result
    )
    return result


# ============================================================ robots.txt
@router.get("/robots")
async def get_robots(scoped: db.TenantDB = Depends(tenant_db)) -> dict:
    body = await publishing.robots_txt(scoped.tenant_id)
    row = await scoped.fetch_one(
        "SELECT value FROM settings WHERE tenant_id = $1 AND key = 'robots_txt'"
    )
    return {"body": body, "isCustom": bool(row and (row["value"] or {}).get("body"))}


@router.put("/robots")
async def put_robots(
    payload: dict,
    user: CurrentUser = Depends(require_perm("seo.manage")),
) -> dict:
    body = str(payload.get("body") or "")
    if len(body) > 20_000:
        raise HTTPException(400, "robots.txt is limited to 20 KB.")
    # A stray 'Disallow: /' takes the whole site out of search, so warn
    # rather than block — it is occasionally what an admin wants.
    warnings = []
    if re.search(r"^\s*Disallow:\s*/\s*$", body, re.IGNORECASE | re.MULTILINE):
        warnings.append("This blocks search engines from the entire site.")
    if body and "sitemap:" not in body.lower():
        warnings.append("No Sitemap: line — crawlers will not find your sitemap.")

    scoped = db.TenantDB(user.tenant_id)
    if body.strip():
        await scoped.execute(
            """INSERT INTO settings (tenant_id, key, value) VALUES ($1, 'robots_txt', $2::jsonb)
               ON CONFLICT (tenant_id, key)
               DO UPDATE SET value = EXCLUDED.value, updated_at = now()""",
            {"body": body},
        )
    else:
        # Empty means "go back to the generated default".
        await scoped.execute(
            "DELETE FROM settings WHERE tenant_id = $1 AND key = 'robots_txt'"
        )

    await events.log_activity(user.tenant_id, "robots.updated", user_id=user.id)
    return {"body": await publishing.robots_txt(user.tenant_id), "warnings": warnings}


# ====================================================== public endpoints
async def _tenant_by_slug(slug: str | None, request: Request) -> dict:
    """Slug first, then the Host header — see tenancy.resolve_public."""
    return await tenancy.resolve_public(slug, request.headers.get("host"))


@public_router.get("/api/v1/{tenant_slug}/sitemap.xml", include_in_schema=False)
@public_router.get("/api/v1/{tenant_slug}/sitemap-{name}.xml", include_in_schema=False)
async def public_sitemap(
    tenant_slug: str, request: Request, name: str = "index"
) -> Response:
    tenant = await _tenant_by_slug(tenant_slug, request)
    row = await db.fetch_one(
        "SELECT xml FROM sitemap_cache WHERE tenant_id = $1 AND name = $2",
        tenant["id"], collapse(name, 60) or "index",
    )
    if not row:
        # Generate on the first request rather than 404 before the first
        # publish has happened.
        await publishing.regenerate_sitemaps(tenant["id"])
        row = await db.fetch_one(
            "SELECT xml FROM sitemap_cache WHERE tenant_id = $1 AND name = $2",
            tenant["id"], collapse(name, 60) or "index",
        )
    if not row:
        raise HTTPException(404, "No sitemap has been generated yet.")

    return Response(
        row["xml"],
        media_type="application/xml",
        headers={"cache-control": "public, max-age=600"},
    )


@public_router.get("/api/v1/{tenant_slug}/robots.txt", include_in_schema=False)
async def public_robots(tenant_slug: str, request: Request) -> PlainTextResponse:
    tenant = await _tenant_by_slug(tenant_slug, request)
    return PlainTextResponse(
        await publishing.robots_txt(tenant["id"]),
        headers={"cache-control": "public, max-age=3600"},
    )


@public_router.get("/api/v1/{tenant_slug}/redirect", include_in_schema=False)
async def resolve_redirect(
    tenant_slug: str, request: Request, path: str = Query(max_length=500)
) -> dict:
    """Look up one path. A static frontend or edge function calls this on
    a 404 and issues the real redirect itself."""
    tenant = await _tenant_by_slug(tenant_slug, request)
    from_path = normalise_path(path)

    row = await db.fetch_one(
        """SELECT id, to_path, status_code FROM redirects
            WHERE tenant_id = $1 AND from_path = $2 AND is_active""",
        tenant["id"], from_path,
    )
    if not row:
        return {"match": False}

    # Counting hits is what makes "which redirects still matter" answerable.
    await db.execute(
        "UPDATE redirects SET hits = hits + 1, last_hit_at = now() WHERE id = $1", row["id"]
    )
    return {"match": True, "toPath": row["to_path"], "statusCode": row["status_code"]}


@public_router.post("/api/v1/{tenant_slug}/not-found", include_in_schema=False)
async def report_not_found(tenant_slug: str, payload: dict, request: Request) -> dict:
    """Called by the frontend's 404 page. Folded by path with a counter,
    so bot noise costs one row rather than one row per hit."""
    tenant = await _tenant_by_slug(tenant_slug, request)
    try:
        path = normalise_path(str(payload.get("path") or ""))
    except HTTPException:
        return {"ok": True}  # a malformed report is not worth a 400

    await db.execute(
        """INSERT INTO not_found_log (tenant_id, path, last_referrer, last_user_agent)
           VALUES ($1, $2, $3, $4)
           ON CONFLICT (tenant_id, path) DO UPDATE
              SET hits = not_found_log.hits + 1, last_seen_at = now(),
                  last_referrer = coalesce(EXCLUDED.last_referrer, not_found_log.last_referrer)""",
        tenant["id"], path,
        collapse(payload.get("referrer"), 500),
        (request.headers.get("user-agent") or "")[:300] or None,
    )
    return {"ok": True}
