"""Cross-process cache invalidation over PostgreSQL LISTEN/NOTIFY.

Several caches hold per-tenant state in process memory — the permission
matrix, tenant resolution, which email connector a site sends through.
Each has a short TTL, which is safe but not correct: with eight
replicas, revoking a permission or suspending a site takes effect on
each of them at a different moment up to the TTL later.

NOTIFY closes that window without adding infrastructure. One dedicated
connection LISTENs; a write publishes; every replica drops the entry
within milliseconds.

Deliberately *invalidation only*, never the value. A payload is a key,
so there is no cache-coherency problem to get wrong and no risk of one
tenant's data arriving on a channel another tenant reads. A missed
notification degrades to the TTL behaviour that was already there,
which is why losing one is survivable.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable

import asyncpg

from . import db
from .config import settings

log = logging.getLogger("crm.cache")

CHANNEL = "crm_cache"

# topic -> handlers. A handler takes the payload dict and drops
# whatever it holds for that key.
_handlers: dict[str, list[Callable[[dict], None]]] = {}
_connection: asyncpg.Connection | None = None
_enabled = True


def subscribe(topic: str, handler: Callable[[dict], None]) -> None:
    """Register an invalidation handler. Called at import time."""
    _handlers.setdefault(topic, []).append(handler)


async def publish(topic: str, **payload: Any) -> None:
    """Tell every replica to drop something.

    Never raises: an invalidation that does not go out means other
    replicas fall back to their TTL, which is the behaviour that
    existed before this module. Failing the write that triggered it
    would be a worse trade.
    """
    if not _enabled:
        return
    try:
        message = json.dumps({"topic": topic, **payload}, default=str)
        if len(message.encode()) > 7000:
            # Postgres caps a notification payload at 8000 bytes.
            log.warning("cache notification for %s too large; skipped", topic)
            return
        await db.execute("SELECT pg_notify($1, $2)", CHANNEL, message)
    except Exception as exc:
        log.error("cache publish failed for %s: %s", topic, exc)


def _dispatch(payload: str) -> None:
    try:
        message = json.loads(payload)
    except ValueError:
        log.error("malformed cache notification")
        return

    topic = message.get("topic")
    for handler in _handlers.get(topic, []):
        try:
            handler(message)
        except Exception as exc:
            # One bad handler must not stop the others.
            log.error("cache handler for %s failed: %s", topic, exc)


async def listener() -> None:
    """Hold one connection open and dispatch notifications.

    Its own connection, not a pooled one: a LISTEN is connection state,
    and a pooled connection handed back to someone else stops
    listening. Reconnects on drop, because a listener that dies quietly
    is worse than one that never started — the caches would look
    correct and be stale.
    """
    global _connection

    if not settings.cache_notify:
        log.info("cache invalidation disabled (CACHE_NOTIFY=0); TTLs only")
        return

    backoff = 1
    while True:
        try:
            _connection = await asyncpg.connect(
                dsn=settings.database_url,
                ssl="require" if settings.pg_ssl == "require" else None,
            )
            await _connection.add_listener(
                CHANNEL, lambda _c, _p, _ch, payload: _dispatch(payload)
            )
            log.info("cache invalidation listening on %s", CHANNEL)
            backoff = 1

            # Nothing to do but stay alive; the callback does the work.
            while not _connection.is_closed():
                await asyncio.sleep(5)
            raise ConnectionError("listener connection closed")

        except asyncio.CancelledError:
            if _connection and not _connection.is_closed():
                await _connection.close()
            raise
        except Exception as exc:
            log.error("cache listener dropped (%s); retrying in %ds", exc, backoff)
            if _connection and not _connection.is_closed():
                try:
                    await _connection.close()
                except Exception:
                    pass
            _connection = None
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)


def status() -> dict:
    return {
        "enabled": settings.cache_notify,
        "connected": bool(_connection and not _connection.is_closed()),
        "topics": sorted(_handlers),
    }


# ------------------------------------------------------------ shortcuts
async def invalidate_permissions(tenant_id: int) -> None:
    await publish("permissions", tenant_id=tenant_id)


async def invalidate_tenant(slug: str | None = None) -> None:
    await publish("tenant", slug=slug)


async def invalidate_email_sender(tenant_id: int | None = None) -> None:
    await publish("email_sender", tenant_id=tenant_id)
