"""Activity log and the outbound webhook queue.

Deliveries are rows, not in-flight coroutines: a crash mid-send leaves a
pending row the worker picks up again. The idempotency key travels in a
header so receivers (Zoho, HubSpot, n8n) can dedupe.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
import uuid
from typing import Any

import httpx

from . import db
from .config import settings

log = logging.getLogger("crm.events")

MAX_ATTEMPTS = 6
BACKOFF_MINUTES = [1, 5, 15, 60, 360]  # then dead
DELIVERY_TIMEOUT = 10.0


async def log_activity(
    tenant_id: int,
    action: str,
    *,
    user_id: int | None = None,
    object_type: str | None = None,
    object_id: int | None = None,
    meta: dict | None = None,
    ip: Any = None,
) -> None:
    """Audit entry. Logging must never break the request that triggered it."""
    try:
        await db.execute(
            """INSERT INTO activity_log (tenant_id, user_id, action, object_type, object_id, meta, ip)
               VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7)""",
            tenant_id,
            user_id,
            action,
            object_type,
            object_id,
            # The pool's jsonb codec runs json.dumps itself; a pre-dumped
            # string would be stored double-encoded (a jsonb string).
            meta or {},
            ip,
        )
    except Exception as exc:
        log.error("activity write failed: %s", exc)


async def emit(tenant_id: int, event: str, payload: dict) -> None:
    """Fan one event out to every subscriber.

    Two destinations, deliberately separate:

      webhook_endpoints   raw HMAC-signed JSON to a URL you control
      connectors          the provider's own API, with field mapping

    Both queue rows with retries. This is the platform's single
    fan-out point, so a connector added later needs no new call site.
    """
    # Typed provider connectors. First, and in its own try, so a
    # connector problem cannot stop the raw webhooks going out.
    try:
        from .connectors import dispatch  # noqa: PLC0415 — avoids a cycle

        await dispatch.queue(tenant_id, event, payload)
    except Exception as exc:
        log.error("connector dispatch failed for %s: %s", event, exc)

    try:
        endpoints = await db.fetch(
            """SELECT id FROM webhook_endpoints
                WHERE tenant_id = $1 AND is_active AND $2 = ANY(events)""",
            tenant_id,
            event,
        )
        if not endpoints:
            return

        key = str(uuid.uuid4())
        body = json.dumps(
            {"event": event, "sent_at": _now_iso(), "data": payload}, default=str
        )

        for endpoint in endpoints:
            await db.execute(
                """INSERT INTO webhook_deliveries
                       (tenant_id, endpoint_id, event, idempotency_key, payload)
                   VALUES ($1, $2, $3, $4, $5::jsonb)
                   ON CONFLICT (endpoint_id, idempotency_key) DO NOTHING""",
                tenant_id,
                endpoint["id"],
                event,
                key,
                body,
            )
    except Exception as exc:
        log.error("webhook enqueue failed: %s", exc)


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def sign(secret: str, timestamp: int, body: str) -> str:
    """Signature covers the timestamp too, so a captured body can't be replayed."""
    mac = hmac.new(secret.encode(), f"{timestamp}.{body}".encode(), hashlib.sha256)
    return f"sha256={mac.hexdigest()}"


async def _deliver(client: httpx.AsyncClient, row: dict) -> tuple[bool, int | None, str | None]:
    payload = row["payload"]
    body = payload if isinstance(payload, str) else json.dumps(payload, default=str)
    timestamp = int(time.time())

    try:
        response = await client.post(
            row["url"],
            content=body,
            headers={
                "content-type": "application/json",
                "user-agent": "crm-admin-webhooks/1.0",
                "x-crm-event": row["event"],
                "x-crm-timestamp": str(timestamp),
                "x-crm-idempotency-key": str(row["idempotency_key"]),
                "x-crm-signature": sign(row["secret"], timestamp, body),
            },
            timeout=DELIVERY_TIMEOUT,
        )
    except httpx.TimeoutException:
        return False, None, "timeout"
    except httpx.HTTPError as exc:
        return False, None, str(exc)[:500]

    ok = 200 <= response.status_code < 300
    return ok, response.status_code, None if ok else f"HTTP {response.status_code}"


async def run_webhook_batch(batch_size: int = 20) -> int:
    """Drain due deliveries once. Returns how many were attempted."""
    try:
        due = await db.fetch(
            """SELECT d.id, d.event, d.payload, d.attempts, d.idempotency_key,
                      e.url, e.secret
                 FROM webhook_deliveries d
                 JOIN webhook_endpoints e ON e.id = d.endpoint_id AND e.is_active
                WHERE d.status = 'pending' AND d.next_attempt_at <= now()
                ORDER BY d.next_attempt_at
                LIMIT $1
                FOR UPDATE OF d SKIP LOCKED""",
            batch_size,
        )
    except Exception as exc:
        log.error("webhook poll failed: %s", exc)
        return 0

    if not due:
        return 0

    async with httpx.AsyncClient(follow_redirects=False) as client:
        for row in due:
            ok, code, error = await _deliver(client, row)
            attempts = row["attempts"] + 1

            if ok:
                await db.execute(
                    """UPDATE webhook_deliveries
                          SET status = 'delivered', attempts = $2,
                              response_code = $3, last_error = NULL
                        WHERE id = $1""",
                    row["id"],
                    attempts,
                    code,
                )
                continue

            dead = attempts >= MAX_ATTEMPTS
            delay = BACKOFF_MINUTES[min(attempts - 1, len(BACKOFF_MINUTES) - 1)]
            await db.execute(
                """UPDATE webhook_deliveries
                      SET status = $2::delivery_status, attempts = $3,
                          response_code = $4, last_error = $5,
                          next_attempt_at = now() + make_interval(mins => $6)
                    WHERE id = $1""",
                row["id"],
                "dead" if dead else "pending",
                attempts,
                code,
                (error or "")[:500],
                delay,
            )

    return len(due)


async def webhook_worker() -> None:
    """Background loop. Cancelled on shutdown by the lifespan handler."""
    while True:
        try:
            await run_webhook_batch()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # one bad batch must not kill the loop
            log.error("webhook worker error: %s", exc)
        await asyncio.sleep(settings.webhook_poll_seconds)


async def session_prune_worker() -> None:
    from .security import prune_sessions

    while True:
        await asyncio.sleep(3600)
        try:
            await prune_sessions()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("prune worker error: %s", exc)


# ===================================================================
# Notification centre and folded error log (2.11)
# ===================================================================

# Events an admin may subscribe a webhook to. Kept as an allow-list so a
# typo in the UI cannot create an endpoint that silently never fires.
PLATFORM_EVENTS: frozenset[str] = frozenset(
    {
        "lead.created", "lead.status_changed",
        "content.published", "content.unpublished", "content.scheduled",
        "form.submitted", "subscriber.created", "campaign.sent",
        "build.succeeded", "build.failed", "health.down",
    }
)


async def notify(
    tenant_id: int,
    kind: str,
    title: str,
    *,
    body: str | None = None,
    level: str = "info",
    link: str | None = None,
    user_id: int | None = None,
) -> None:
    """Row in the notification centre. user_id=None broadcasts.

    Like log_activity, this must never break its caller: a notification
    is a courtesy, not part of the transaction that triggered it.
    """
    try:
        await db.execute(
            """INSERT INTO notifications (tenant_id, user_id, kind, level, title, body, link)
               VALUES ($1, $2, $3, $4, $5, $6, $7)""",
            tenant_id, user_id, kind,
            level if level in {"info", "success", "warning", "error"} else "info",
            title[:200], (body or None) and body[:2000], link,
        )
    except Exception as exc:
        log.error("notification write failed: %s", exc)


def fingerprint(source: str, message: str) -> str:
    """Fold similar errors together: digits and hex ids are the parts
    that vary between occurrences of the same bug."""
    import re  # noqa: PLC0415

    normalised = re.sub(r"0x[0-9a-f]+|\b\d+\b", "N", (message or "").lower())[:300]
    return hashlib.sha256(f"{source}|{normalised}".encode()).hexdigest()[:32]


async def log_error(
    message: str,
    *,
    tenant_id: int | None = None,
    level: str = "error",
    source: str = "app",
    detail: dict | None = None,
    request_method: str | None = None,
    request_path: str | None = None,
    user_id: int | None = None,
) -> None:
    """Upsert into the folded error log for the admin's log viewer."""
    try:
        await db.execute(
            """INSERT INTO error_log (tenant_id, level, source, message, fingerprint, detail,
                                      request_method, request_path, user_id)
               VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8, $9)
               ON CONFLICT (coalesce(tenant_id, 0), fingerprint) DO UPDATE
                  SET count = error_log.count + 1,
                      last_seen_at = now(),
                      is_resolved = FALSE,
                      detail = EXCLUDED.detail""",
            tenant_id,
            level if level in {"warning", "error", "critical"} else "error",
            source[:60],
            (message or "unknown error")[:2000],
            fingerprint(source, message),
            detail or {},
            request_method,
            (request_path or "")[:300] or None,
            user_id,
        )
    except Exception as exc:
        # Deliberately only the app log here — recursing into log_error
        # while the database is the thing that is failing would loop.
        log.error("error-log write failed: %s", exc)
