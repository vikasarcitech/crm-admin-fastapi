"""Public API versioning.

A static frontend is deployed on its own cadence and can lag the
platform by months, so a version cannot be retired the moment its
replacement ships. Three things make that manageable:

* **Every `/api/v1` response says which version served it**, so a
  frontend can assert on it rather than discovering a change at
  runtime.
* **Deprecation is signalled in headers** — `Deprecation` and `Sunset`,
  per RFC 8594 — before anything is removed, so a client library or a
  build log surfaces it without anyone reading a changelog.
* **Usage is recorded per version per site**, which turns "can we drop
  v1?" into a query instead of a guess.

Retiring a version without knowing who still calls it is how a client's
site breaks on a Saturday.
"""

from __future__ import annotations

import logging
import re
from datetime import date

from . import db

log = logging.getLogger("crm.versioning")

CURRENT = "v1"
SUPPORTED = ("v1",)

# version -> (deprecated_on, sunset_on, what to do instead)
DEPRECATED: dict[str, tuple[date, date, str]] = {
    # "v1": (date(2027, 1, 1), date(2027, 7, 1), "Move to /api/v2."),
}

VERSION_IN_PATH = re.compile(r"^/api/(v\d+)(?:/|$)")


def version_of(path: str) -> str | None:
    match = VERSION_IN_PATH.match(path or "")
    return match.group(1) if match else None


def headers_for(version: str) -> dict[str, str]:
    """Version and, when it applies, deprecation headers."""
    headers = {"x-api-version": version}
    entry = DEPRECATED.get(version)
    if not entry:
        return headers

    deprecated_on, sunset_on, advice = entry
    # RFC 8594: Sunset is an HTTP-date; Deprecation carries the date the
    # policy took effect.
    headers["deprecation"] = deprecated_on.strftime("%a, %d %b %Y 00:00:00 GMT")
    headers["sunset"] = sunset_on.strftime("%a, %d %b %Y 00:00:00 GMT")
    headers["link"] = '</api/docs>; rel="deprecation"'
    headers["warning"] = f'299 - "{version} is deprecated. {advice}"'
    return headers


async def record(tenant_id: int, version: str, path: str, agent: str | None) -> None:
    """Count one call. Aggregated per day, never per request.

    A row per request would make this the biggest table in the system
    and tell you nothing a daily count does not.
    """
    try:
        await db.execute(
            """INSERT INTO api_version_usage
                   (tenant_id, version, day, requests, last_path, last_agent)
               VALUES ($1, $2, now()::date, 1, $3, $4)
               ON CONFLICT (tenant_id, version, day) DO UPDATE
                  SET requests = api_version_usage.requests + 1,
                      last_path = EXCLUDED.last_path,
                      last_agent = EXCLUDED.last_agent,
                      last_seen_at = now()""",
            tenant_id, version, (path or "")[:300], (agent or "")[:200] or None,
        )
    except Exception as exc:
        log.error("api version usage write failed: %s", exc)


async def usage(days: int = 30) -> list[dict]:
    """Who is still calling which version.

    Reads from a replica when one exists: it is reporting, and a few
    seconds of lag on a 30-day count changes nothing.
    """
    return await db.fetch(
        """SELECT u.version, t.slug::text AS site, t.name,
                  sum(u.requests)::bigint AS requests,
                  max(u.last_seen_at) AS last_seen_at,
                  max(u.last_agent) AS last_agent
             FROM api_version_usage u
             JOIN tenants t ON t.id = u.tenant_id
            WHERE u.day > now()::date - $1::int
            GROUP BY u.version, t.slug, t.name
            ORDER BY u.version, requests DESC""",
        days,
        replica=True,
    )


def status() -> dict:
    return {
        "current": CURRENT,
        "supported": list(SUPPORTED),
        "deprecated": {
            version: {
                "deprecatedOn": entry[0].isoformat(),
                "sunsetOn": entry[1].isoformat(),
                "advice": entry[2],
            }
            for version, entry in DEPRECATED.items()
        },
    }
