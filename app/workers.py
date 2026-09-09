"""Background workers.

Each loop drains one queue or applies one schedule. They all follow the
same shape as the original webhook worker: poll, claim with
``FOR UPDATE SKIP LOCKED`` (or a conditional UPDATE), act, record the
outcome. Nothing is held in process memory, so a restart mid-batch
loses nothing and two replicas cannot double-send.

Run them in exactly one process. Set ``RUN_WORKERS=0`` on every other
replica — the queues are safe against concurrency, but polling from
twenty tasks is twenty times the query load for no benefit.
"""

from __future__ import annotations

import asyncio
import logging
import httpx

from . import db, events, publishing
from .config import settings
from .content import build_snapshot, public_path

log = logging.getLogger("crm.workers")


def _guard(name: str):
    """Wrap a loop body so one bad iteration cannot kill the worker."""

    def decorator(func):
        async def wrapper(*args, **kwargs):
            try:
                return await func(*args, **kwargs)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error("%s error: %s", name, exc)
                await events.log_error(f"{name} failed: {exc}", source=f"worker.{name}")
                return 0

        return wrapper

    return decorator


# ============================================ scheduled content publishing
@_guard("scheduled-publish")
async def run_scheduled_publishes(batch_size: int = 25) -> int:
    """Publish content whose scheduled_for has passed.

    Claims each row with a conditional UPDATE, so a second worker (or an
    admin clicking Publish at the same moment) cannot publish it twice.
    """
    due = await db.fetch(
        """SELECT i.id, i.tenant_id FROM content_items i
            WHERE i.status = 'scheduled' AND i.scheduled_for <= now()
            ORDER BY i.scheduled_for LIMIT $1
            FOR UPDATE OF i SKIP LOCKED""",
        batch_size,
    )
    if not due:
        return 0

    published = 0
    by_tenant: dict[int, list[str]] = {}

    for row in due:
        claimed = await db.fetch_one(
            """UPDATE content_items
                  SET status = 'published', scheduled_for = NULL,
                      published_at = coalesce(published_at, now())
                WHERE id = $1 AND status = 'scheduled'
                RETURNING id, tenant_id, type_id, slug, title, excerpt, body, fields, seo,
                          author_id, featured_media_id, menu_order, published_at, updated_at""",
            row["id"],
        )
        if not claimed:
            continue  # someone else got there first

        type_row = await db.fetch_one(
            "SELECT slug::text AS slug, route_prefix FROM content_types WHERE id = $1",
            claimed["type_id"],
        )
        terms = await db.fetch(
            """SELECT tm.id, tm.name, tm.slug::text AS slug, tx.slug::text AS taxonomy
                 FROM content_terms ct
                 JOIN terms tm ON tm.id = ct.term_id
                 JOIN taxonomies tx ON tx.id = tm.taxonomy_id
                WHERE ct.item_id = $1""",
            claimed["id"],
        )
        snapshot = build_snapshot(claimed, type_row, terms)
        await db.execute(
            "UPDATE content_items SET published_snapshot = $2::jsonb WHERE id = $1",
            claimed["id"], snapshot,
        )

        path = public_path(type_row["route_prefix"], str(claimed["slug"]))
        if path:
            by_tenant.setdefault(claimed["tenant_id"], []).append(path)

        await events.log_activity(
            claimed["tenant_id"], "content.published",
            object_type="content_item", object_id=claimed["id"],
            meta={"scheduled": True, "type": type_row["slug"]},
        )
        await events.emit(
            claimed["tenant_id"], "content.published",
            {"id": claimed["id"], "type": type_row["slug"], "slug": str(claimed["slug"]),
             "title": claimed["title"], "path": path, "scheduled": True},
        )
        await events.notify(
            claimed["tenant_id"], "content.published",
            f"“{claimed['title']}” went live as scheduled",
            level="success", link=f"#/content?open={claimed['id']}",
        )
        published += 1

    # One rebuild per tenant per batch, not one per item.
    for tenant_id, paths in by_tenant.items():
        await publishing.on_content_published(
            tenant_id, paths=paths + ["/sitemap.xml"], reason="content.published"
        )

    return published


async def scheduled_publish_worker() -> None:
    while True:
        await run_scheduled_publishes()
        await asyncio.sleep(settings.content_poll_seconds)


# ==================================================== scheduled campaigns
@_guard("scheduled-campaigns")
async def run_scheduled_campaigns(batch_size: int = 5) -> int:
    from .routers.marketing import dispatch_campaign  # noqa: PLC0415 — avoids a cycle

    due = await db.fetch(
        """SELECT id, tenant_id FROM campaigns
            WHERE status = 'scheduled' AND scheduled_for <= now()
            ORDER BY scheduled_for LIMIT $1
            FOR UPDATE SKIP LOCKED""",
        batch_size,
    )
    sent = 0
    for row in due:
        # dispatch_campaign claims the row itself, so a race is safe.
        queued = await dispatch_campaign(row["tenant_id"], row["id"])
        if queued:
            sent += 1
        await events.log_activity(
            row["tenant_id"], "campaign.sent",
            object_type="campaign", object_id=row["id"],
            meta={"scheduled": True, "queued": queued},
        )
    return sent


async def campaign_worker() -> None:
    while True:
        await run_scheduled_campaigns()
        await asyncio.sleep(max(30, settings.content_poll_seconds))


# ================================================= builds & invalidations
async def build_worker() -> None:
    """Drains build_runs and cdn_invalidations — both are "tell an
    external service the site changed", so they share a cadence."""
    while True:
        try:
            await publishing.run_build_batch()
            await publishing.run_invalidation_batch()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("build worker error: %s", exc)
        await asyncio.sleep(settings.build_poll_seconds)


# ========================================================= health checks
@_guard("health-checks")
async def run_health_checks(batch_size: int = 20) -> int:
    """Probe the checks whose interval has elapsed."""
    from .routers.ops import _probe  # noqa: PLC0415

    due = await db.fetch(
        """SELECT id, tenant_id, name, url, expect_status, expect_text, consecutive_failures
             FROM health_checks
            WHERE is_active
              AND (last_checked_at IS NULL
                   OR last_checked_at < now() - make_interval(secs => interval_seconds))
            ORDER BY last_checked_at NULLS FIRST LIMIT $1
            FOR UPDATE SKIP LOCKED""",
        batch_size,
    )
    if not due:
        return 0

    async with httpx.AsyncClient(follow_redirects=True) as client:
        for check in due:
            await _probe(client, check)
    return len(due)


async def health_worker() -> None:
    while True:
        await asyncio.sleep(settings.health_poll_seconds)
        await run_health_checks()


# ============================================================= retention
@_guard("retention")
async def run_retention_sweep() -> dict:
    """Apply every tenant's active retention policies, plus the global
    housekeeping that keeps unbounded tables bounded."""
    from .routers.compliance import apply_retention  # noqa: PLC0415

    tenants = await db.fetch(
        "SELECT DISTINCT tenant_id FROM retention_policies WHERE is_active"
    )
    totals: dict[str, int] = {}
    for tenant in tenants:
        for key, count in (await apply_retention(tenant["tenant_id"])).items():
            totals[key] = totals.get(key, 0) + count

    # Housekeeping that is not tenant-configurable: expired tokens and
    # long-settled queue rows serve no purpose and only grow.
    await db.execute("DELETE FROM preview_tokens WHERE expires_at < now() - interval '7 days'")
    await run_oauth_prune()
    # Settled connector deliveries: the log is useful for a month, not
    # forever, and it is the highest-volume table the queue produces.
    await db.execute(
        """DELETE FROM connector_deliveries
            WHERE status = 'delivered' AND created_at < now() - interval '30 days'"""
    )
    await db.execute("DELETE FROM totp_challenges WHERE expires_at < now() - interval '1 day'")
    await db.execute(
        """DELETE FROM email_outbox
            WHERE status = 'delivered' AND sent_at < now() - interval '30 days'"""
    )
    await db.execute(
        """DELETE FROM webhook_deliveries
            WHERE status = 'delivered' AND created_at < now() - interval '30 days'"""
    )
    await db.execute(
        """DELETE FROM build_runs
            WHERE status IN ('complete', 'failed') AND created_at < now() - interval '60 days'"""
    )
    await db.execute(
        """DELETE FROM cdn_invalidations
            WHERE status = 'complete' AND created_at < now() - interval '30 days'"""
    )
    # Unique-visitor hashes are only useful for the day they cover.
    await db.execute("DELETE FROM visitor_days WHERE day < now()::date - 45")
    return totals


async def retention_worker() -> None:
    """Hourly tick, acting every RETENTION_HOUR_INTERVAL hours."""
    if settings.retention_hour_interval <= 0:
        log.info("retention worker disabled (RETENTION_HOUR_INTERVAL=0)")
        return

    ticks = 0
    while True:
        await asyncio.sleep(3600)
        ticks += 1
        if ticks % settings.retention_hour_interval:
            continue
        result = await run_retention_sweep()
        if result:
            log.info("retention sweep affected %s", result)


# ================================================== scheduled backups
@_guard("scheduled-backup")
async def run_scheduled_backup() -> int:
    """One database backup a day, if none has succeeded in 24 hours.

    Deliberately simple: this is a safety net for installs with no
    external scheduler. On AWS, prefer RDS automated backups and an
    EventBridge schedule — this loop cannot survive the task it runs in
    being replaced mid-dump.
    """
    from .routers.ops import perform_backup  # noqa: PLC0415

    recent = await db.fetch_one(
        """SELECT id FROM backups
            WHERE kind IN ('database', 'full') AND status IN ('complete', 'running')
              AND created_at > now() - interval '24 hours'
            LIMIT 1"""
    )
    if recent:
        return 0

    tenant = await db.fetch_one("SELECT id FROM tenants ORDER BY id LIMIT 1")
    if not tenant:
        return 0

    row = await db.fetch_one(
        """INSERT INTO backups (tenant_id, kind, status, destination, trigger)
           VALUES ($1, 'database', 'pending', $2, 'scheduled') RETURNING id""",
        tenant["id"],
        f"s3://{settings.backup_s3_bucket}/{settings.backup_s3_prefix}"
        if settings.backup_s3_bucket else "local",
    )
    await perform_backup(row["id"], tenant["id"], "database")
    return 1


async def backup_worker() -> None:
    if not settings.backup_s3_bucket:
        # Without off-server storage a scheduled dump would just fill
        # the container's disk, so it stays opt-in.
        log.info("scheduled backups disabled (BACKUP_S3_BUCKET is not set)")
        return
    while True:
        await asyncio.sleep(3600)
        await run_scheduled_backup()


# ================================================================ registry
def all_workers() -> list:
    """Coroutine factories the lifespan handler starts."""
    return [
        scheduled_publish_worker,
        campaign_worker,
        build_worker,
        health_worker,
        retention_worker,
        backup_worker,
        usage_worker,
        connector_worker,
    ]


# ========================================================== connectors
async def connector_worker() -> None:
    """Drains connector_deliveries — CRM pushes, automation webhooks.

    Its own loop rather than sharing the webhook worker's: a CRM that
    is slow to respond should not hold up a Zapier hook, and the poll
    interval is separately tunable.
    """
    from .connectors.dispatch import worker  # noqa: PLC0415

    await worker()


@_guard("oauth-prune")
async def run_oauth_prune() -> int:
    """Expired OAuth handshake state. Short-lived by design, so this
    only clears rows from abandoned connection attempts."""
    from .connectors.oauth import prune_states  # noqa: PLC0415

    return await prune_states()


# ======================================================= platform usage
@_guard("usage-refresh")
async def run_usage_refresh() -> int:
    """Recompute every site's usage rollup.

    The portfolio screen reads `tenant_usage` rather than counting
    across every table live, because that query is fine at ten sites
    and a problem at three hundred. This is what keeps the rollup
    honest.
    """
    from .tenancy import refresh_all_usage  # noqa: PLC0415

    return await refresh_all_usage()


async def usage_worker() -> None:
    """Hourly. Quota *gates* count live (see tenancy.enforce_limit), so
    a stale rollup only ever makes a dashboard number old, never lets a
    site past its ceiling."""
    while True:
        await run_usage_refresh()
        await asyncio.sleep(3600)
