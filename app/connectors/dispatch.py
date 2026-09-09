"""Queueing and delivering connector events.

`events.emit()` is the platform's single fan-out point, so hooking it
here means every event that already exists — and every one added later
— reaches connectors without another call site.

A delivery is a row, for the same reason webhook deliveries are: a
crash mid-send leaves work the worker picks up, and a retry is a fact
in the database rather than a coroutine that died with the process.

**Idempotency is derived, not random.** The key is
`{event}:{object_id}`, so re-emitting `lead.created` for lead 42 —
after a replay, a retry, or a second worker — hits the unique index and
does nothing, instead of creating a second record in someone's CRM.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

from .. import db
from ..config import settings
from .base import ConnectorError, Result, build, connector_columns, mark_result

log = logging.getLogger("crm.connectors.dispatch")

MAX_ATTEMPTS = 6
BACKOFF_MINUTES = [1, 5, 15, 60, 360]   # then dead
BATCH_SIZE = 20

# Events that carry a lead-shaped payload, so a CRM connector knows the
# object it is syncing and can link it for later updates.
OBJECT_KEYS = {
    "lead.created": ("lead", "id"),
    "lead.status_changed": ("lead", "id"),
    "form.submitted": ("lead", "lead_id"),
    "subscriber.created": ("subscriber", "id"),
    "content.published": ("content_item", "id"),
    "campaign.sent": ("campaign", "id"),
}


def idempotency_key(event: str, payload: dict) -> str:
    """Stable across retries and replays of the same logical change.

    Falls back to the event name plus a payload digest when there is no
    obvious object id, which still collapses an accidental double-emit
    of an identical payload.
    """
    mapping = OBJECT_KEYS.get(event)
    if mapping:
        _, id_field = mapping
        object_id = payload.get(id_field)
        if object_id:
            return f"{event}:{object_id}"

    import hashlib  # noqa: PLC0415
    import json  # noqa: PLC0415

    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode()
    ).hexdigest()[:16]
    return f"{event}:{digest}"


async def queue(tenant_id: int, event: str, payload: dict) -> int:
    """Queue one delivery per connector subscribed to this event.

    Never raises: a connector problem must not fail the request that
    produced the lead.
    """
    try:
        connectors = await db.fetch(
            """SELECT id FROM connectors
                WHERE tenant_id = $1 AND is_active
                  AND status IN ('connected', 'error')
                  AND $2 = ANY(events)""",
            tenant_id, event,
        )
        if not connectors:
            return 0

        key = idempotency_key(event, payload)
        queued = 0
        for connector in connectors:
            result = await db.fetch(
                """INSERT INTO connector_deliveries
                       (tenant_id, connector_id, event, idempotency_key, payload)
                   VALUES ($1, $2, $3, $4, $5::jsonb)
                   ON CONFLICT (connector_id, idempotency_key) DO NOTHING
                   RETURNING id""",
                tenant_id, connector["id"], event, key, payload,
            )
            queued += len(result)
        return queued
    except Exception as exc:
        log.error("connector queue failed for %s: %s", event, exc)
        return 0


async def enqueue_manual(
    tenant_id: int, connector_id: int, event: str, payload: dict, *, suffix: str = ""
) -> int | None:
    """Queue for one connector specifically — a replay or a manual sync.

    `suffix` makes the key distinct so a deliberate replay is allowed
    where an accidental duplicate is not.
    """
    key = idempotency_key(event, payload) + (f":{suffix}" if suffix else "")
    row = await db.fetch_one(
        """INSERT INTO connector_deliveries
               (tenant_id, connector_id, event, idempotency_key, payload)
           VALUES ($1, $2, $3, $4, $5::jsonb)
           ON CONFLICT (connector_id, idempotency_key) DO NOTHING
           RETURNING id""",
        tenant_id, connector_id, event, key, payload,
    )
    return row["id"] if row else None


# ===================================================================
# Delivery
# ===================================================================
async def deliver_one(client: httpx.AsyncClient, row: dict) -> Result:
    """Run one delivery. Never raises; a broken connector is a Result."""
    started = time.monotonic()
    try:
        connector = build(row)
    except (ConnectorError, KeyError) as exc:
        return Result(ok=False, retryable=False, error=str(exc))

    try:
        result = await connector.push(client, row["event"], row["payload"] or {})
    except ConnectorError as exc:
        result = Result(ok=False, retryable=False, error=str(exc))
    except Exception as exc:
        log.exception("connector %s raised", row["provider"])
        result = Result(
            ok=False, retryable=True, error=f"{type(exc).__name__}: {exc}"[:300]
        )

    elapsed = int((time.monotonic() - started) * 1000)
    await _record(row, result, elapsed)
    return result


async def _record(row: dict, result: Result, elapsed_ms: int) -> None:
    # `row` is a delivery joined to its connector: `delivery_id` is the
    # queue row, `id` is the connector.
    delivery_id = row["delivery_id"]
    connector_id = row["id"]

    if result.ok:
        await db.execute(
            """UPDATE connector_deliveries
                  SET status = 'delivered', attempts = attempts + 1,
                      response_code = $2, response = $3::jsonb, request = $4::jsonb,
                      external_id = $5, last_error = NULL,
                      duration_ms = $6, delivered_at = now()
                WHERE id = $1""",
            delivery_id, result.status_code, result.response or {},
            result.request or {}, result.external_id, elapsed_ms,
        )
        await mark_result(connector_id, result)

        # Remember which record the provider created, so a later event
        # about the same object updates it instead of duplicating.
        object_type, id_field = OBJECT_KEYS.get(row["event"], (None, None))
        object_id = (row["payload"] or {}).get(id_field) if id_field else None
        if result.external_id and object_type and object_id:
            await db.execute(
                """INSERT INTO connector_links
                       (tenant_id, connector_id, object_type, object_id,
                        external_id, external_url)
                   VALUES ($1, $2, $3, $4, $5, $6)
                   ON CONFLICT (connector_id, object_type, object_id) DO UPDATE
                      SET external_id = EXCLUDED.external_id,
                          external_url = EXCLUDED.external_url,
                          synced_at = now()""",
                row["tenant_id"], connector_id, object_type, int(object_id),
                result.external_id, result.external_url,
            )
        return

    attempts = row["delivery_attempts"] + 1
    # A permanent failure is dead on the first attempt: retrying sends
    # the identical request and gets the identical refusal.
    dead = not result.retryable or attempts >= MAX_ATTEMPTS
    delay = BACKOFF_MINUTES[min(attempts - 1, len(BACKOFF_MINUTES) - 1)]

    await db.execute(
        """UPDATE connector_deliveries
              SET status = $2::delivery_status, attempts = $3,
                  response_code = $4, response = $5::jsonb, request = $6::jsonb,
                  last_error = $7, duration_ms = $8,
                  next_attempt_at = now() + make_interval(mins => $9)
            WHERE id = $1""",
        delivery_id, "dead" if dead else "pending", attempts,
        result.status_code, result.response or {}, result.request or {},
        (result.error or "unknown error")[:500], elapsed_ms, delay,
    )
    await mark_result(connector_id, result)

    if dead:
        from .. import events  # noqa: PLC0415

        await events.notify(
            row["tenant_id"], "connector.failed",
            f"{row['name']} could not receive a {row['event']} event",
            body=(result.error or "unknown error")[:400],
            level="error", link="#/integrations",
        )


async def run_batch(batch_size: int = BATCH_SIZE) -> int:
    """Drain due deliveries once. Returns how many were attempted."""
    try:
        due = await db.fetch(
            # The delivery's own columns are aliased: `connectors` also
            # has id, tenant_id, event-adjacent and created_at columns,
            # and asyncpg lets the later duplicate win — so an
            # unaliased d.id silently became the connector's id and
            # every status update went to the wrong row.
            f"""SELECT d.id AS delivery_id, d.event, d.payload,
                       d.attempts AS delivery_attempts, d.idempotency_key,
                       {connector_columns("c")}
                  FROM connector_deliveries d
                  JOIN connectors c ON c.id = d.connector_id AND c.is_active
                 WHERE d.status = 'pending' AND d.next_attempt_at <= now()
                 ORDER BY d.next_attempt_at
                 LIMIT $1
                 FOR UPDATE OF d SKIP LOCKED""",
            batch_size,
        )
    except Exception as exc:
        log.error("connector poll failed: %s", exc)
        return 0

    if not due:
        return 0

    async with httpx.AsyncClient(follow_redirects=False) as client:
        for row in due:
            await deliver_one(client, row)
    return len(due)


async def worker() -> None:
    """Background loop. Cancelled on shutdown by the lifespan handler."""
    while True:
        try:
            await run_batch()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("connector worker error: %s", exc)
        await asyncio.sleep(settings.connector_poll_seconds)


# ===================================================================
# Ad-hoc calls
# ===================================================================
async def test_connector(row: dict) -> Result:
    """Run a connector's own health check."""
    try:
        connector = build(row)
    except (ConnectorError, KeyError) as exc:
        return Result(ok=False, retryable=False, error=str(exc))

    async with httpx.AsyncClient(follow_redirects=False) as client:
        try:
            result = await connector.test(client)
        except ConnectorError as exc:
            result = Result(ok=False, retryable=False, error=str(exc))
        except Exception as exc:
            log.exception("connector test raised for %s", row["provider"])
            result = Result(ok=False, error=f"{type(exc).__name__}: {exc}"[:300])

    await mark_result(row["id"], result)
    return result


async def preview(row: dict, payload: dict) -> dict[str, Any]:
    """What the mapping would send, without sending it.

    The answer to "why did the CRM reject this?" is almost always in
    this dict, so it is worth being able to see before a real lead
    arrives.
    """
    from .base import flatten_lead  # noqa: PLC0415

    try:
        connector = build(row)
    except (ConnectorError, KeyError) as exc:
        return {"error": str(exc)}

    flat = flatten_lead(payload)
    mapped = connector.mapped(payload)
    mapping = row.get("field_mapping") or connector.ctx.provider.default_mapping
    unmapped = [key for key in mapping if not flat.get(key)]
    return {
        "sources": flat,
        "mapped": mapped,
        "usingDefaults": not row.get("field_mapping"),
        "emptySources": unmapped,
    }
