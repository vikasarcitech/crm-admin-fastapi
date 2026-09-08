"""Analytics & dashboard (2.9).

GA4 and Google Tag Manager still run client-side — their ids come from
the ``analytics`` setting and reach the frontend through the public
config endpoint. What lives here is the *first-party* measurement the
admin dashboard needs, because a dashboard that has to call the GA4
Data API for every KPI is slow, rate-limited and breaks whenever a
service-account credential rotates.

The beacon is aggregate-only: it increments daily counters rather than
writing a row per hit, and the visitor key is a salted daily hash, so
the tables stay small and carry no per-visitor trail.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response

from .. import db
from ..config import settings
from ..permissions import permissions_for, require_perm
from ..ratelimit import RateLimiter
from ..schemas import PageViewBeacon, collapse
from ..security import CurrentUser, client_ip

log = logging.getLogger("crm.analytics")

router = APIRouter(prefix="/api/analytics", tags=["analytics"])
public_router = APIRouter(tags=["analytics-public"])

# One beacon per page view; a normal session is a handful of these.
beacon_limiter = RateLimiter(max_requests=120, window_seconds=600)

MAX_PATH = 300


def _normalise_path(raw: str) -> str:
    """Strip query and fragment, cap length, lower-case.

    Query strings would explode the cardinality of page_view_daily —
    every ?utm_… variant of one page would get its own row.
    """
    value = (raw or "/").split("?")[0].split("#")[0].strip() or "/"
    if not value.startswith("/"):
        value = f"/{value}"
    return value[:MAX_PATH].lower()


def _visitor_hash(ip: str | None, user_agent: str | None) -> str:
    """Salted, day-scoped hash. Rotating ANALYTICS_SALT severs the link
    between old and new counts, and nothing here is reversible to an IP."""
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    salt = settings.analytics_salt or "unsalted"
    raw = f"{salt}|{day}|{ip or ''}|{(user_agent or '')[:120]}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


# ================================================================ beacon
@public_router.post("/api/public/{tenant_slug}/collect", response_class=Response)
async def collect(
    tenant_slug: str, payload: PageViewBeacon, request: Request
) -> Response:
    """Record one page view.

    Answers 204 with no body: a `fetch(..., {keepalive:true})` beacon
    does not read the response, and an empty answer gives a bot probing
    the endpoint nothing to learn.
    """
    empty = Response(status_code=204)
    ip = client_ip(request)
    beacon_limiter.check(f"pv:{ip or 'unknown'}")

    tenant = await db.fetch_one(
        "SELECT id FROM tenants WHERE slug = $1 AND is_active", collapse(tenant_slug, 60)
    )
    if not tenant:
        return empty

    # Respect the tenant's own switch: a workspace that only wants GA4
    # should not be accumulating first-party rows.
    setting = await db.fetch_one(
        "SELECT value FROM settings WHERE tenant_id = $1 AND key = 'analytics'", tenant["id"]
    )
    config = (setting or {}).get("value") or {}
    if config.get("first_party_beacon") is False:
        return empty

    path = _normalise_path(payload.path)
    visitor = _visitor_hash(ip, request.headers.get("user-agent"))

    # ON CONFLICT DO NOTHING doubles as the unique-visitor test: the row
    # only inserts the first time this visitor is seen today.
    is_new_visitor = await db.fetch(
        """INSERT INTO visitor_days (tenant_id, day, visitor_hash)
           VALUES ($1, now()::date, $2)
           ON CONFLICT DO NOTHING RETURNING visitor_hash""",
        tenant["id"], visitor,
    )

    await db.execute(
        """INSERT INTO page_view_daily (tenant_id, day, path, views, visitors)
           VALUES ($1, now()::date, $2, 1, $3)
           ON CONFLICT (tenant_id, day, path) DO UPDATE
              SET views = page_view_daily.views + 1,
                  visitors = page_view_daily.visitors + $3""",
        tenant["id"], path, 1 if is_new_visitor else 0,
    )

    if payload.is_new_session:
        source, medium = _classify(payload)
        await db.execute(
            """INSERT INTO traffic_source_daily
                   (tenant_id, day, source, medium, campaign, sessions)
               VALUES ($1, now()::date, $2, $3, $4, 1)
               ON CONFLICT (tenant_id, day, source, medium, campaign) DO UPDATE
                  SET sessions = traffic_source_daily.sessions + 1""",
            tenant["id"], source, medium,
            (collapse(payload.utm_campaign, 160) or "")[:160],
        )
    return empty


def _classify(payload: PageViewBeacon) -> tuple[str, str]:
    """UTM tags win; otherwise infer from the referrer host."""
    if payload.utm_source:
        return (
            (collapse(payload.utm_source, 120) or "unknown")[:120],
            (collapse(payload.utm_medium, 120) or "referral")[:120],
        )

    referrer = (payload.referrer or "").strip()
    if not referrer:
        return "direct", "none"

    from urllib.parse import urlparse  # noqa: PLC0415

    try:
        host = (urlparse(referrer).hostname or "").lower()
    except ValueError:
        return "direct", "none"
    if not host:
        return "direct", "none"

    engines = ("google.", "bing.", "duckduckgo.", "yahoo.", "baidu.", "yandex.", "ecosia.")
    socials = ("facebook.", "instagram.", "linkedin.", "twitter.", "x.com", "t.co",
               "reddit.", "youtube.", "pinterest.", "tiktok.", "whatsapp.")
    if any(engine in host for engine in engines):
        return host.split(".")[0] or host, "organic"
    if any(social in host for social in socials):
        return host, "social"
    return host[:120], "referral"


# ============================================================= dashboard
@router.get("/overview")
async def overview(
    days: int = Query(default=30, ge=1, le=365),
    user: CurrentUser = Depends(require_perm("analytics.view")),
) -> dict:
    """The KPI set from 2.9: visitors, leads, conversion rate,
    subscribers, blog views, top pages and traffic sources."""
    scoped = db.TenantDB(user.tenant_id)

    traffic = await scoped.fetch_one(
        """SELECT coalesce(sum(views), 0)::int AS views,
                  coalesce(sum(visitors), 0)::int AS visitors
             FROM page_view_daily
            WHERE tenant_id = $1 AND day > now()::date - $2::int""",
        days,
    )
    previous = await scoped.fetch_one(
        """SELECT coalesce(sum(views), 0)::int AS views,
                  coalesce(sum(visitors), 0)::int AS visitors
             FROM page_view_daily
            WHERE tenant_id = $1
              AND day > now()::date - ($2::int * 2)
              AND day <= now()::date - $2::int""",
        days,
    )

    leads = await scoped.fetch_one(
        """SELECT count(*)::int AS total,
                  count(*) FILTER (WHERE status = 'won')::int AS won,
                  count(*) FILTER (WHERE status NOT IN ('won','lost'))::int AS open,
                  coalesce(sum(value_amount) FILTER (WHERE status = 'won'), 0)::float8 AS won_value
             FROM leads
            WHERE tenant_id = $1 AND NOT is_spam
              AND created_at > now() - make_interval(days => $2)""",
        days,
    )

    conversions = await scoped.fetch_one(
        """SELECT count(*)::int AS total,
                  count(*) FILTER (WHERE kind = 'form')::int AS forms,
                  count(*) FILTER (WHERE kind = 'phone')::int AS phone,
                  count(*) FILTER (WHERE kind = 'whatsapp')::int AS whatsapp,
                  count(*) FILTER (WHERE kind = 'email')::int AS email,
                  count(*) FILTER (WHERE kind = 'cta')::int AS cta
             FROM conversion_events
            WHERE tenant_id = $1 AND created_at > now() - make_interval(days => $2)""",
        days,
    )

    subscribers = await scoped.fetch_one(
        """SELECT count(*) FILTER (WHERE status = 'subscribed')::int AS active,
                  count(*) FILTER (WHERE status = 'subscribed'
                                     AND created_at > now() - make_interval(days => $2))::int AS new,
                  count(*) FILTER (WHERE status = 'unsubscribed'
                                     AND unsubscribed_at > now() - make_interval(days => $2))::int
                    AS churned
             FROM subscribers WHERE tenant_id = $1""",
        days,
    )

    content = await scoped.fetch_one(
        """SELECT count(*) FILTER (WHERE i.status = 'published')::int AS published,
                  count(*) FILTER (WHERE i.status = 'draft')::int AS drafts,
                  count(*) FILTER (WHERE i.status = 'scheduled')::int AS scheduled,
                  count(*) FILTER (WHERE i.status = 'published'
                                     AND t.slug = 'post')::int AS posts
             FROM content_items i JOIN content_types t ON t.id = i.type_id
            WHERE i.tenant_id = $1"""
    )

    # "Blog views": traffic to the post route prefix, whatever it is set to.
    blog = await scoped.fetch_one(
        """SELECT coalesce(sum(v.views), 0)::int AS views
             FROM page_view_daily v
            WHERE v.tenant_id = $1 AND v.day > now()::date - $2::int
              AND v.path LIKE (
                    coalesce((SELECT lower(route_prefix) FROM content_types
                               WHERE tenant_id = $1 AND slug = 'post'), '/blog')
                  ) || '/%'""",
        days,
    )

    by_day = await scoped.fetch(
        """SELECT to_char(d::date, 'YYYY-MM-DD') AS day,
                  coalesce(v.views, 0)::int AS views,
                  coalesce(v.visitors, 0)::int AS visitors,
                  coalesce(l.n, 0)::int AS leads
             FROM generate_series(now()::date - make_interval(days => $2), now()::date, '1 day') d
             LEFT JOIN (
               SELECT day, sum(views) AS views, sum(visitors) AS visitors
                 FROM page_view_daily WHERE tenant_id = $1 GROUP BY day
             ) v ON v.day = d::date
             LEFT JOIN (
               SELECT created_at::date AS day, count(*) AS n FROM leads
                WHERE tenant_id = $1 AND NOT is_spam GROUP BY 1
             ) l ON l.day = d::date
            ORDER BY 1""",
        days,
    )

    top_pages = await scoped.fetch(
        """SELECT path, sum(views)::int AS views, sum(visitors)::int AS visitors
             FROM page_view_daily
            WHERE tenant_id = $1 AND day > now()::date - $2::int
            GROUP BY path ORDER BY views DESC LIMIT 15""",
        days,
    )
    sources = await scoped.fetch(
        """SELECT source, medium, sum(sessions)::int AS sessions
             FROM traffic_source_daily
            WHERE tenant_id = $1 AND day > now()::date - $2::int
            GROUP BY source, medium ORDER BY sessions DESC LIMIT 15""",
        days,
    )
    lead_sources = await scoped.fetch(
        """SELECT coalesce(nullif(utm_source, ''), 'direct') AS source, count(*)::int AS n
             FROM leads
            WHERE tenant_id = $1 AND NOT is_spam
              AND created_at > now() - make_interval(days => $2)
            GROUP BY 1 ORDER BY n DESC LIMIT 10""",
        days,
    )

    visitors = traffic["visitors"]
    return {
        "days": days,
        "kpis": {
            "visitors": visitors,
            "views": traffic["views"],
            "visitorsChange": _change(visitors, previous["visitors"]),
            "viewsChange": _change(traffic["views"], previous["views"]),
            "leads": leads["total"],
            "leadsOpen": leads["open"],
            "leadsWon": leads["won"],
            "wonValue": leads["won_value"],
            # Leads per visitor. Reported as null rather than 0 when
            # there is no traffic data, so an empty beacon table does
            # not read as "0% conversion".
            "conversionRate": round(leads["total"] / visitors * 100, 2) if visitors else None,
            "conversions": conversions["total"],
            "subscribers": subscribers["active"],
            "subscribersNew": subscribers["new"],
            "subscribersChurned": subscribers["churned"],
            "blogViews": blog["views"],
            "publishedContent": content["published"],
            "drafts": content["drafts"],
            "scheduled": content["scheduled"],
            "posts": content["posts"],
        },
        "conversionsByKind": {
            key: conversions[key] for key in ("forms", "phone", "whatsapp", "email", "cta")
        },
        "byDay": by_day,
        "topPages": top_pages,
        "trafficSources": sources,
        "leadSources": lead_sources,
        "hasTrafficData": bool(traffic["views"]),
    }


def _change(current: int, previous: int) -> float | None:
    """Percentage change, or None when there is no baseline to compare."""
    if not previous:
        return None
    return round((current - previous) / previous * 100, 1)


@router.get("/pages")
async def page_report(
    days: int = Query(default=30, ge=1, le=365),
    limit: int = Query(default=100, ge=10, le=500),
    user: CurrentUser = Depends(require_perm("analytics.view")),
) -> dict:
    """Per-page traffic joined to the content item at that path, so an
    editor can see which posts actually earn their traffic."""
    scoped = db.TenantDB(user.tenant_id)
    rows = await scoped.fetch(
        """SELECT v.path, sum(v.views)::int AS views, sum(v.visitors)::int AS visitors,
                  i.id AS item_id, i.title, t.slug::text AS type_slug,
                  (SELECT count(*) FROM conversion_events e
                    WHERE e.tenant_id = $1 AND lower(e.source_page) = v.path
                      AND e.created_at > now() - make_interval(days => $2))::int AS conversions
             FROM page_view_daily v
             LEFT JOIN content_types t
                    ON t.tenant_id = $1 AND t.route_prefix IS NOT NULL
             LEFT JOIN content_items i
                    ON i.tenant_id = $1 AND i.type_id = t.id
                   AND v.path = lower(CASE WHEN t.route_prefix = '/'
                                           THEN '/' || i.slug::text
                                           ELSE t.route_prefix || '/' || i.slug::text END)
            WHERE v.tenant_id = $1 AND v.day > now()::date - $2::int
            GROUP BY v.path, i.id, i.title, t.slug
            ORDER BY views DESC LIMIT $3""",
        days, limit,
    )
    return {"pages": rows, "days": days}


# ================================================= portfolio reporting
@router.get("/portfolio")
async def portfolio(
    days: int = Query(default=30, ge=1, le=365),
    user: CurrentUser = Depends(require_perm("analytics.view")),
) -> dict:
    """One row per site the operator can see — 2.9's portfolio view.

    Restricted to sites the account actually has access to: a Super
    Admin sees the whole install, everyone else sees their memberships.
    """
    granted = await permissions_for(user.tenant_id, user.role)
    if "sites.manage" in granted:
        allowed = await db.fetch("SELECT id FROM tenants WHERE is_active")
    else:
        allowed = await db.fetch(
            """SELECT t.id FROM tenants t
                LEFT JOIN tenant_memberships m ON m.tenant_id = t.id AND m.user_id = $1
               WHERE t.is_active AND (m.user_id IS NOT NULL OR t.id = $2)""",
            user.id, user.tenant_id,
        )
    ids = [row["id"] for row in allowed]
    if not ids:
        return {"sites": [], "days": days}

    rows = await db.fetch(
        """SELECT t.id, t.slug::text AS slug, t.name,
                  coalesce(v.views, 0)::int AS views,
                  coalesce(v.visitors, 0)::int AS visitors,
                  coalesce(l.leads, 0)::int AS leads,
                  coalesce(l.won, 0)::int AS won,
                  coalesce(s.subscribers, 0)::int AS subscribers,
                  coalesce(c.published, 0)::int AS published_content
             FROM tenants t
             LEFT JOIN (
               SELECT tenant_id, sum(views) AS views, sum(visitors) AS visitors
                 FROM page_view_daily WHERE day > now()::date - $2::int GROUP BY 1
             ) v ON v.tenant_id = t.id
             LEFT JOIN (
               SELECT tenant_id, count(*) AS leads,
                      count(*) FILTER (WHERE status = 'won') AS won
                 FROM leads
                WHERE NOT is_spam AND created_at > now() - make_interval(days => $2)
                GROUP BY 1
             ) l ON l.tenant_id = t.id
             LEFT JOIN (
               SELECT tenant_id, count(*) AS subscribers FROM subscribers
                WHERE status = 'subscribed' GROUP BY 1
             ) s ON s.tenant_id = t.id
             LEFT JOIN (
               SELECT tenant_id, count(*) AS published FROM content_items
                WHERE status = 'published' GROUP BY 1
             ) c ON c.tenant_id = t.id
            WHERE t.id = ANY($1::bigint[])
            ORDER BY leads DESC, t.name""",
        ids, days,
    )
    for row in rows:
        row["conversionRate"] = (
            round(row["leads"] / row["visitors"] * 100, 2) if row["visitors"] else None
        )
    return {
        "sites": rows,
        "days": days,
        "totals": {
            "views": sum(r["views"] for r in rows),
            "visitors": sum(r["visitors"] for r in rows),
            "leads": sum(r["leads"] for r in rows),
            "subscribers": sum(r["subscribers"] for r in rows),
        },
    }


# ============================================================ integration
@router.get("/integrations")
async def integrations(user: CurrentUser = Depends(require_perm("analytics.view"))) -> dict:
    """GA4 / GTM / Search Console configuration and the snippet to add.

    The snippet is returned rather than injected: the frontend is a
    separate static site, so the CMS's job is to hand it the ids.
    """
    scoped = db.TenantDB(user.tenant_id)
    row = await scoped.fetch_one(
        "SELECT value FROM settings WHERE tenant_id = $1 AND key = 'analytics'"
    )
    config = (row or {}).get("value") or {}
    tenant = await db.fetch_one(
        "SELECT slug::text AS slug FROM tenants WHERE id = $1", user.tenant_id
    )

    ga4 = config.get("ga4_measurement_id")
    gtm = config.get("gtm_container_id")
    return {
        "config": config,
        "status": {
            "ga4": bool(ga4),
            "gtm": bool(gtm),
            "searchConsole": bool(config.get("search_console_verification")),
            "firstParty": config.get("first_party_beacon", True) is not False,
        },
        "beaconEndpoint": f"/api/public/{tenant['slug']}/collect",
        "conversionEndpoint": f"/api/public/{tenant['slug']}/conversions",
        "snippets": {
            "gtm": _gtm_snippet(gtm) if gtm else None,
            "ga4": _ga4_snippet(ga4) if ga4 else None,
            "searchConsole": (
                f'<meta name="google-site-verification" '
                f'content="{config["search_console_verification"]}">'
                if config.get("search_console_verification") else None
            ),
        },
    }


def _gtm_snippet(container_id: str) -> str:
    safe = collapse(container_id, 30) or ""
    return (
        "<!-- Google Tag Manager -->\n"
        "<script>(function(w,d,s,l,i){w[l]=w[l]||[];w[l].push({'gtm.start':\n"
        "new Date().getTime(),event:'gtm.js'});var f=d.getElementsByTagName(s)[0],\n"
        "j=d.createElement(s),dl=l!='dataLayer'?'&l='+l:'';j.async=true;j.src=\n"
        "'https://www.googletagmanager.com/gtm.js?id='+i+dl;f.parentNode.insertBefore(j,f);\n"
        f"}})(window,document,'script','dataLayer','{safe}');</script>"
    )


def _ga4_snippet(measurement_id: str) -> str:
    safe = collapse(measurement_id, 30) or ""
    return (
        f'<script async src="https://www.googletagmanager.com/gtag/js?id={safe}"></script>\n'
        "<script>window.dataLayer=window.dataLayer||[];\n"
        "function gtag(){dataLayer.push(arguments);}gtag('js',new Date());\n"
        f"gtag('config','{safe}');</script>"
    )


@router.delete("/data")
async def purge_analytics(
    confirm: bool = Query(default=False),
    user: CurrentUser = Depends(require_perm("analytics.manage")),
) -> dict:
    """Delete this site's first-party analytics. Needs confirm=true."""
    if not confirm:
        raise HTTPException(400, "Deleting analytics data needs confirm=true.")
    scoped = db.TenantDB(user.tenant_id)
    counts = {}
    for table in ("page_view_daily", "traffic_source_daily", "visitor_days"):
        rows = await scoped.fetch(
            f"DELETE FROM {table} WHERE tenant_id = $1 RETURNING tenant_id"
        )
        counts[table] = len(rows)
    return {"ok": True, "deleted": counts}
