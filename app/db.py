"""Database access.

Every business query goes through TenantDB so a missing
`WHERE tenant_id = $1` cannot silently leak one client's leads into
another client's dashboard. Raw helpers are reserved for tenants,
sessions and migrations.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import time
from typing import Any, Sequence

import asyncpg

from .config import settings

log = logging.getLogger("crm.db")

_pool: asyncpg.Pool | None = None

SLOW_QUERY_MS = 300


async def _init_connection(conn: asyncpg.Connection) -> None:
    """Teach asyncpg the types this schema uses.

    Without these, jsonb comes back as a raw string and citext raises
    'unknown type' on the first query that touches an email column.
    """
    await conn.set_type_codec(
        "jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
    )
    await conn.set_type_codec(
        "json", encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
    )
    for schema in ("public", "pg_catalog"):
        try:
            await conn.set_type_codec(
                "citext", encoder=str, decoder=str, schema=schema, format="text"
            )
            break
        except (asyncpg.UndefinedObjectError, ValueError):
            continue


async def connect() -> asyncpg.Pool:
    global _pool
    if _pool is not None:
        return _pool
    if not settings.database_url:
        raise RuntimeError("DATABASE_URL is not set. Copy .env.example to .env first.")

    _pool = await asyncpg.create_pool(
        dsn=settings.database_url,
        min_size=settings.pg_pool_min,
        max_size=settings.pg_pool_max,
        command_timeout=15,
        init=_init_connection,
        ssl="require" if settings.pg_ssl == "require" else None,
    )
    return _pool


async def disconnect() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("Database pool is not initialised")
    return _pool


def _log_slow(sql: str, started: float) -> None:
    elapsed = (time.monotonic() - started) * 1000
    if elapsed > SLOW_QUERY_MS:
        log.warning("slow query %.0fms: %s", elapsed, " ".join(sql.split())[:90])


async def fetch(sql: str, *args: Any) -> list[dict]:
    """Run a query and return rows as plain dicts."""
    started = time.monotonic()
    try:
        rows = await pool().fetch(sql, *args)
    except asyncpg.PostgresError as exc:
        # Never echo the argument tuple — it can hold PII or password hashes.
        log.error("query failed: %s", exc)
        raise
    _log_slow(sql, started)
    return [dict(row) for row in rows]


async def fetch_one(sql: str, *args: Any) -> dict | None:
    rows = await fetch(sql, *args)
    return rows[0] if rows else None


async def execute(sql: str, *args: Any) -> str:
    started = time.monotonic()
    try:
        result = await pool().execute(sql, *args)
    except asyncpg.PostgresError as exc:
        log.error("statement failed: %s", exc)
        raise
    _log_slow(sql, started)
    return result


class TenantDB:
    """Binds $1 to a tenant id. Call sites number their own args from $2."""

    __slots__ = ("tenant_id",)

    def __init__(self, tenant_id: int) -> None:
        if not tenant_id:
            raise ValueError("TenantDB requires a tenant id")
        self.tenant_id = tenant_id

    async def fetch(self, sql: str, *args: Any) -> list[dict]:
        return await fetch(sql, self.tenant_id, *args)

    async def fetch_one(self, sql: str, *args: Any) -> dict | None:
        return await fetch_one(sql, self.tenant_id, *args)

    async def execute(self, sql: str, *args: Any) -> str:
        return await execute(sql, self.tenant_id, *args)


def to_inet(value: str | None) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """asyncpg maps inet to ipaddress objects, so coerce before binding."""
    if not value:
        return None
    try:
        return ipaddress.ip_address(value)
    except ValueError:
        return None


def placeholders(start: int, count: int) -> Sequence[str]:
    return [f"${n}" for n in range(start, start + count)]
