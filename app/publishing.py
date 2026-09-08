"""Publishing pipeline: sitemaps, build triggers and CDN invalidation.

A publish in this platform is not just a status change — the static
frontend has to be rebuilt and the CDN has to forget the old copy.
:func:`on_content_published` is the single fan-out point every publish
path calls, so a new caller cannot forget one of the steps.

Everything downstream is queued as a row (``build_runs``,
``cdn_invalidations``) and drained by a worker, for the same reason the
webhook queue is: a publish must not fail because Vercel was briefly
unreachable, and a retry must not need the user to click again.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from xml.sax.saxutils import escape as xml_escape

import httpx

from . import db, events
from .config import settings
from .content import public_path

log = logging.getLogger("crm.publishing")

MAX_ATTEMPTS = 5
BACKOFF_MINUTES = [1, 3, 10, 30]
BUILD_TIMEOUT = 15.0

# Sitemaps cap at 50k URLs / 50 MB; well below that, but paginate anyway
# so a large catalogue does not produce one unservable file.
SITEMAP_PAGE_SIZE = 5000


# ------------------------------------------------------------- fan-out
async def on_content_published(
    tenant_id: int,
    *,
    item: dict | None = None,
    reason: str = "content.published",
    paths: list[str] | None = None,
) -> None:
    """Everything that must happen after content goes live.

    Never raises: a failure here has already been queued for retry or
    logged, and must not roll back the publish the user just made.
    """
    try:
        await regenerate_sitemaps(tenant_id)
    except Exception as exc:
        log.error("sitemap regeneration failed for tenant %s: %s", tenant_id, exc)
        await events.log_error(
            f"sitemap regeneration failed: {exc}", tenant_id=tenant_id, source="publishing"
        )

    invalidate = list(paths or [])
    if item:
        path = item.get("path") or public_path(item.get("route_prefix"), item.get("slug") or "")
        if path:
            invalidate += [path, "/sitemap.xml"]
    invalidate = invalidate or ["/*"]

    try:
        await queue_invalidation(tenant_id, invalidate)
    except Exception as exc:
        log.error("invalidation queue failed: %s", exc)

    try:
        await trigger_builds(tenant_id, event=reason)
    except Exception as exc:
        log.error("build trigger failed: %s", exc)


# ------------------------------------------------------------ sitemaps
def _abs_url(base: str, path: str) -> str:
    if re.match(r"^https?://", path, re.IGNORECASE):
        return path
    return f"{base.rstrip('/')}{path if path.startswith('/') else '/' + path}"


def _urlset(entries: list[dict]) -> str:
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
    ]
    for entry in entries:
        lines.append("  <url>")
        lines.append(f"    <loc>{xml_escape(entry['loc'])}</loc>")
        if entry.get("lastmod"):
            lines.append(f"    <lastmod>{entry['lastmod']}</lastmod>")
        if entry.get("changefreq"):
            lines.append(f"    <changefreq>{entry['changefreq']}</changefreq>")
        if entry.get("priority") is not None:
            lines.append(f"    <priority>{entry['priority']:.1f}</priority>")
        lines.append("  </url>")
    lines.append("</urlset>")
    return "\n".join(lines)


def _index(base: str, names: list[str], generated: datetime) -> str:
    stamp = generated.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
    ]
    for name in names:
        lines.append("  <sitemap>")
        lines.append(f"    <loc>{xml_escape(_abs_url(base, f'/sitemap-{name}.xml'))}</loc>")
        lines.append(f"    <lastmod>{stamp}</lastmod>")
        lines.append("  </sitemap>")
    lines.append("</sitemapindex>")
    return "\n".join(lines)


async def site_base_url(tenant_id: int) -> str:
    """Where the published site lives.

    Preference order: the tenant's own site_identity setting, its
    primary_domain, then SITE_BASE_URL. Absolute URLs are mandatory in a
    sitemap, so there is no relative fallback.
    """
    row = await db.fetch_one(
        """SELECT t.primary_domain, s.value AS identity
             FROM tenants t
             LEFT JOIN settings s ON s.tenant_id = t.id AND s.key = 'site_identity'
            WHERE t.id = $1""",
        tenant_id,
    )
    identity = (row or {}).get("identity") or {}
    if isinstance(identity, dict) and identity.get("site_url"):
        return str(identity["site_url"]).rstrip("/")
    domain = (row or {}).get("primary_domain")
    if domain:
        return f"https://{str(domain).lstrip('https://').lstrip('http://').strip('/')}"
    return settings.site_base_url or ""


async def regenerate_sitemaps(tenant_id: int) -> dict:
    """Rebuild every sitemap for a tenant into sitemap_cache.

    Called on publish rather than per request: generating XML for a few
    thousand URLs on every crawler hit is the kind of cost that only
    shows up under load.
    """
    base = await site_base_url(tenant_id)
    if not base:
        # Without an absolute origin a sitemap would be invalid; say so
        # rather than caching something Google will reject.
        return {"ok": False, "reason": "no site URL configured", "sitemaps": []}

    rows = await db.fetch(
        """SELECT i.slug::text AS slug, i.published_at, i.updated_at, i.seo,
                  t.slug::text AS type_slug, t.route_prefix
             FROM content_items i
             JOIN content_types t ON t.id = i.type_id
            WHERE i.tenant_id = $1 AND i.status = 'published'
              AND t.route_prefix IS NOT NULL AND t.is_active
            ORDER BY t.slug, i.published_at DESC NULLS LAST""",
        tenant_id,
    )

    by_type: dict[str, list[dict]] = {}
    for row in rows:
        seo = row["seo"] if isinstance(row["seo"], dict) else {}
        if seo.get("noindex"):
            continue  # honouring noindex in the sitemap, not just the tag
        path = public_path(row["route_prefix"], row["slug"])
        if not path:
            continue
        stamp = row["published_at"] or row["updated_at"]
        by_type.setdefault(row["type_slug"], []).append(
            {
                "loc": _abs_url(base, path),
                "lastmod": stamp.astimezone(timezone.utc).strftime("%Y-%m-%d") if stamp else None,
                "changefreq": "weekly" if row["type_slug"] == "post" else "monthly",
                "priority": 1.0 if path == "/" else 0.7,
            }
        )

    # Published block-builder pages (db/schema.sql `pages`) too, so both
    # content systems appear in one sitemap.
    tenant = await db.fetch_one("SELECT slug::text AS slug FROM tenants WHERE id = $1", tenant_id)
    builder_pages = await db.fetch(
        """SELECT slug::text AS slug, published_at FROM pages
            WHERE tenant_id = $1 AND status = 'published'""",
        tenant_id,
    )
    if builder_pages and tenant:
        by_type["builder-pages"] = [
            {
                "loc": _abs_url(base, f"/p/{tenant['slug']}/{row['slug']}"),
                "lastmod": row["published_at"].astimezone(timezone.utc).strftime("%Y-%m-%d")
                if row["published_at"] else None,
                "changefreq": "monthly",
                "priority": 0.5,
            }
            for row in builder_pages
        ]

    # Taxonomy term archives, which are real URLs when the type they
    # belong to has a route prefix.
    term_rows = await db.fetch(
        """SELECT DISTINCT tx.slug::text AS taxonomy, tm.slug::text AS slug
             FROM terms tm
             JOIN taxonomies tx ON tx.id = tm.taxonomy_id
             JOIN content_terms ct ON ct.term_id = tm.id
             JOIN content_items i ON i.id = ct.item_id AND i.status = 'published'
            WHERE tm.tenant_id = $1""",
        tenant_id,
    )
    if term_rows:
        by_type["terms"] = [
            {
                "loc": _abs_url(base, f"/{row['taxonomy']}/{row['slug']}"),
                "changefreq": "weekly",
                "priority": 0.4,
            }
            for row in term_rows
        ]

    written: list[str] = []
    for type_slug, entries in sorted(by_type.items()):
        for page, start in enumerate(range(0, len(entries), SITEMAP_PAGE_SIZE), start=1):
            chunk = entries[start : start + SITEMAP_PAGE_SIZE]
            name = type_slug if page == 1 else f"{type_slug}-{page}"
            await _store_sitemap(tenant_id, name, _urlset(chunk), len(chunk))
            written.append(name)

    now = datetime.now(timezone.utc)
    await _store_sitemap(tenant_id, "index", _index(base, written, now), len(written))

    # Drop sitemaps for types that no longer have published content.
    await db.execute(
        """DELETE FROM sitemap_cache
            WHERE tenant_id = $1 AND name <> 'index' AND NOT (name = ANY($2::text[]))""",
        tenant_id, written,
    )
    return {"ok": True, "sitemaps": written, "urls": sum(len(v) for v in by_type.values())}


async def _store_sitemap(tenant_id: int, name: str, xml: str, count: int) -> None:
    await db.execute(
        """INSERT INTO sitemap_cache (tenant_id, name, xml, url_count, generated_at)
           VALUES ($1, $2, $3, $4, now())
           ON CONFLICT (tenant_id, name) DO UPDATE
              SET xml = EXCLUDED.xml, url_count = EXCLUDED.url_count,
                  generated_at = now()""",
        tenant_id, name, xml, count,
    )


DEFAULT_ROBOTS = """User-agent: *
Allow: /

Sitemap: {sitemap}
"""


async def robots_txt(tenant_id: int) -> str:
    """The stored robots.txt, or a sane default with the sitemap line."""
    row = await db.fetch_one(
        "SELECT value FROM settings WHERE tenant_id = $1 AND key = 'robots_txt'", tenant_id
    )
    value = (row or {}).get("value")
    if isinstance(value, dict) and value.get("body"):
        return str(value["body"])
    base = await site_base_url(tenant_id)
    return DEFAULT_ROBOTS.format(sitemap=_abs_url(base or "", "/sitemap.xml"))


# -------------------------------------------------------- build triggers
async def trigger_builds(
    tenant_id: int, *, event: str = "manual", hook_id: int | None = None, reason: str | None = None
) -> list[dict]:
    """Queue a build_run per matching hook, honouring the debounce.

    Debouncing matters: bulk-publishing 40 posts should rebuild the site
    once, not 40 times, and most providers bill per build minute.
    """
    hooks = await db.fetch(
        """SELECT id, name, debounce_seconds, last_triggered_at
             FROM build_hooks
            WHERE tenant_id = $1 AND is_active
              AND ($2::bigint IS NULL OR id = $2)
              AND ($3 = 'manual' OR $3 = ANY(trigger_events))""",
        tenant_id, hook_id, event,
    )

    queued: list[dict] = []
    for hook in hooks:
        pending = await db.fetch_one(
            """SELECT id FROM build_runs
                WHERE tenant_id = $1 AND hook_id = $2 AND status = 'pending'
                LIMIT 1""",
            tenant_id, hook["id"],
        )
        if pending:
            # A run is already waiting; it will pick up this change too.
            queued.append({"hook": hook["name"], "status": "coalesced", "runId": pending["id"]})
            continue

        delay = int(hook["debounce_seconds"] or 0)
        row = await db.fetch_one(
            """INSERT INTO build_runs (tenant_id, hook_id, reason, next_attempt_at)
               VALUES ($1, $2, $3, now() + make_interval(secs => $4))
               RETURNING id""",
            tenant_id, hook["id"], (reason or event)[:200], delay,
        )
        queued.append({"hook": hook["name"], "status": "queued", "runId": row["id"]})

    return queued


async def _fire_hook(client: httpx.AsyncClient, run: dict) -> tuple[bool, int | None, str | None]:
    headers = {"content-type": "application/json", "user-agent": "crm-admin-publisher/1.0"}
    token = run.get("auth_token")
    if token:
        # GitHub wants a bearer; the deploy-hook providers take the token
        # in the URL, so a header is harmless there.
        headers["authorization"] = f"Bearer {token}"
        headers["accept"] = "application/vnd.github+json"

    body: dict = {"reason": run.get("reason") or "publish"}
    if run.get("provider") == "github":
        body = {"event_type": "cms-publish", "client_payload": body}

    try:
        response = await client.post(
            run["url"], json=body, headers=headers, timeout=BUILD_TIMEOUT
        )
    except httpx.TimeoutException:
        return False, None, "timeout"
    except httpx.HTTPError as exc:
        return False, None, str(exc)[:400]

    ok = 200 <= response.status_code < 300
    return ok, response.status_code, None if ok else f"HTTP {response.status_code}"


async def run_build_batch(batch_size: int = 10) -> int:
    """Fire due build hooks once. Returns how many were attempted."""
    try:
        due = await db.fetch(
            """SELECT r.id, r.tenant_id, r.attempts, r.reason,
                      h.url, h.auth_token, h.provider, h.name
                 FROM build_runs r
                 JOIN build_hooks h ON h.id = r.hook_id AND h.is_active
                WHERE r.status = 'pending' AND r.next_attempt_at <= now()
                ORDER BY r.next_attempt_at
                LIMIT $1
                FOR UPDATE OF r SKIP LOCKED""",
            batch_size,
        )
    except Exception as exc:
        log.error("build poll failed: %s", exc)
        return 0

    if not due:
        return 0

    async with httpx.AsyncClient(follow_redirects=False) as client:
        for run in due:
            ok, code, error = await _fire_hook(client, run)
            attempts = run["attempts"] + 1

            if ok:
                await db.execute(
                    """UPDATE build_runs SET status = 'complete', attempts = $2,
                              response_code = $3, error = NULL, finished_at = now()
                        WHERE id = $1""",
                    run["id"], attempts, code,
                )
                await db.execute(
                    "UPDATE build_hooks SET last_triggered_at = now() WHERE id = (SELECT hook_id FROM build_runs WHERE id = $1)",
                    run["id"],
                )
                await events.emit(
                    run["tenant_id"], "build.succeeded",
                    {"hook": run["name"], "reason": run["reason"]},
                )
                continue

            failed = attempts >= MAX_ATTEMPTS
            delay = BACKOFF_MINUTES[min(attempts - 1, len(BACKOFF_MINUTES) - 1)]
            await db.execute(
                """UPDATE build_runs
                      SET status = $2::job_status, attempts = $3, response_code = $4,
                          error = $5, next_attempt_at = now() + make_interval(mins => $6),
                          finished_at = CASE WHEN $2 = 'failed' THEN now() END
                    WHERE id = $1""",
                run["id"], "failed" if failed else "pending", attempts, code,
                (error or "")[:400], delay,
            )
            if failed:
                # A silently broken deploy hook means the website stops
                # reflecting the CMS, so this one is loud.
                await events.notify(
                    run["tenant_id"], "build.failed",
                    f"Build hook “{run['name']}” failed",
                    body=f"{error or 'unknown error'} after {attempts} attempts.",
                    level="error", link="#/publishing",
                )
                await events.emit(
                    run["tenant_id"], "build.failed",
                    {"hook": run["name"], "error": error, "attempts": attempts},
                )

    return len(due)


# --------------------------------------------------- CDN invalidation
async def queue_invalidation(tenant_id: int, paths: list[str]) -> int | None:
    """Queue a CDN purge. Returns the row id, or None when not configured."""
    cleaned = [p if p.startswith("/") else f"/{p}" for p in dict.fromkeys(paths) if p][:200]
    if not cleaned:
        return None
    row = await db.fetch_one(
        """INSERT INTO cdn_invalidations (tenant_id, provider, paths)
           VALUES ($1, $2, $3) RETURNING id""",
        tenant_id,
        "cloudfront" if settings.cloudfront_distribution_id else "none",
        cleaned,
    )
    return row["id"] if row else None


async def run_invalidation_batch(batch_size: int = 10) -> int:
    """Send queued invalidations to CloudFront.

    With no distribution configured the rows are marked complete and
    skipped — a pull CDN that honours cache-control needs no purge, and
    leaving them pending forever would look like a stuck queue.
    """
    try:
        due = await db.fetch(
            """SELECT id, tenant_id, paths, attempts FROM cdn_invalidations
                WHERE status = 'pending' AND next_attempt_at <= now()
                ORDER BY next_attempt_at LIMIT $1
                FOR UPDATE SKIP LOCKED""",
            batch_size,
        )
    except Exception as exc:
        log.error("invalidation poll failed: %s", exc)
        return 0

    if not due:
        return 0

    if not settings.cloudfront_distribution_id:
        await db.execute(
            """UPDATE cdn_invalidations
                  SET status = 'complete', finished_at = now(),
                      error = 'no CDN configured; skipped'
                WHERE id = ANY($1::bigint[])""",
            [row["id"] for row in due],
        )
        return len(due)

    for row in due:
        attempts = row["attempts"] + 1
        try:
            reference = await _cloudfront_invalidate(row["id"], row["paths"])
            await db.execute(
                """UPDATE cdn_invalidations SET status = 'complete', attempts = $2,
                          reference = $3, finished_at = now(), error = NULL
                    WHERE id = $1""",
                row["id"], attempts, reference,
            )
        except Exception as exc:
            failed = attempts >= MAX_ATTEMPTS
            delay = BACKOFF_MINUTES[min(attempts - 1, len(BACKOFF_MINUTES) - 1)]
            await db.execute(
                """UPDATE cdn_invalidations
                      SET status = $2::job_status, attempts = $3, error = $4,
                          next_attempt_at = now() + make_interval(mins => $5),
                          finished_at = CASE WHEN $2 = 'failed' THEN now() END
                    WHERE id = $1""",
                row["id"], "failed" if failed else "pending", attempts, str(exc)[:400], delay,
            )
            log.error("invalidation %s failed: %s", row["id"], exc)

    return len(due)


async def _cloudfront_invalidate(run_id: int, paths: list[str]) -> str:
    """Blocking boto3 call, kept off the event loop."""
    import asyncio  # noqa: PLC0415

    def _call() -> str:
        import boto3  # noqa: PLC0415

        client = boto3.client("cloudfront", region_name=settings.aws_region or None)
        response = client.create_invalidation(
            DistributionId=settings.cloudfront_distribution_id,
            InvalidationBatch={
                "Paths": {"Quantity": len(paths), "Items": paths},
                # Unique per row, so a retry does not create a duplicate
                # invalidation (which CloudFront bills for separately).
                "CallerReference": f"cms-{run_id}",
            },
        )
        return response["Invalidation"]["Id"]

    return await asyncio.to_thread(_call)
