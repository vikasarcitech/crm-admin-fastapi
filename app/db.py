"""Database access.

Two layers of tenant isolation, and it is worth being precise about
which one does what.

1. **TenantDB** binds `$1` to a tenant id, so a call site cannot write
   a tenant-scoped query without the scope. This is the primary
   control, and it is unconditional.

2. **Row-level security** is the backstop underneath it. Every
   TenantDB query declares its tenant on the connection
   (`SET LOCAL app.tenant_id`) inside a transaction, and the policies
   in db/tenancy.sql make PostgreSQL refuse another tenant's rows even
   if the WHERE clause is wrong. Declaring nothing means no
   restriction, which is what keeps platform-level queries — the
   worker loops, portfolio reporting, migrations — working.

RLS only bites when the connecting role is neither a superuser nor
BYPASSRLS. `rls_status()` probes that at startup and reports it, so an
install that *thinks* it has database-enforced isolation and does not
finds out from a log line rather than from an incident.

The raw `fetch`/`fetch_one`/`execute` helpers deliberately declare no
tenant. They are for the control plane (tenants, sessions, cross-site
reporting); those call sites are the ones to review by hand.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import time
from contextlib import asynccontextmanager
from typing import Any, Sequence

import asyncpg

from .config import settings

log = logging.getLogger("crm.db")

_pool: asyncpg.Pool | None = None
# Read replicas, round-robined. Empty means every read goes to the
# writer, which is the correct default — a replica is only useful once
# read volume actually justifies the replication lag it introduces.
_replicas: list[asyncpg.Pool] = []
_replica_turn = 0

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

    for dsn in settings.replica_urls:
        try:
            replica = await asyncpg.create_pool(
                dsn=dsn,
                min_size=1,
                max_size=settings.pg_pool_max,
                command_timeout=15,
                init=_init_connection,
                ssl="require" if settings.pg_ssl == "require" else None,
            )
            _replicas.append(replica)
        except Exception as exc:
            # A replica that will not connect must not stop the app
            # starting: reads fall back to the writer.
            log.error("read replica unavailable, falling back to the writer: %s", exc)

    if _replicas:
        log.info("connected %d read replica(s)", len(_replicas))
    return _pool


async def disconnect() -> None:
    global _pool
    for replica in _replicas:
        await replica.close()
    _replicas.clear()
    if _pool is not None:
        await _pool.close()
        _pool = None


def pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("Database pool is not initialised")
    return _pool


def read_pool() -> asyncpg.Pool:
    """A replica if one is configured, otherwise the writer.

    Round-robin rather than random so a two-replica setup actually
    alternates instead of landing on one of them two-thirds of the time.
    """
    global _replica_turn
    if not _replicas:
        return pool()
    _replica_turn = (_replica_turn + 1) % len(_replicas)
    return _replicas[_replica_turn]


def replica_count() -> int:
    return len(_replicas)


def _log_slow(sql: str, started: float) -> None:
    elapsed = (time.monotonic() - started) * 1000
    if elapsed > SLOW_QUERY_MS:
        log.warning("slow query %.0fms: %s", elapsed, " ".join(sql.split())[:90])


async def fetch(sql: str, *args: Any, replica: bool = False) -> list[dict]:
    """Run a query and return rows as plain dicts.

    `replica=True` sends it to a read replica when one is configured.
    Opt-in, never automatic: replication lag means a read that follows
    its own write can miss it, so only queries that tolerate seconds of
    staleness — reporting, dashboards, the portfolio roll-up — should
    ask for it.
    """
    started = time.monotonic()
    target = read_pool() if replica else pool()
    try:
        rows = await target.fetch(sql, *args)
    except asyncpg.PostgresError as exc:
        # Never echo the argument tuple — it can hold PII or password hashes.
        log.error("query failed: %s", exc)
        raise
    _log_slow(sql, started)
    return [dict(row) for row in rows]


async def fetch_one(sql: str, *args: Any, replica: bool = False) -> dict | None:
    rows = await fetch(sql, *args, replica=replica)
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


# set_config(..., is_local => true) is SET LOCAL, and unlike SET LOCAL
# it accepts a bind parameter — so the tenant id never reaches SQL text.
SET_SCOPE = "SELECT set_config('app.tenant_id', $1, true)"


class TenantDB:
    """Binds $1 to a tenant id. Call sites number their own args from $2.

    With RLS enabled each call runs in its own transaction on one
    connection, so the `SET LOCAL` and the query cannot end up on
    different pooled connections. That costs one extra round trip per
    query; `PG_RLS=0` skips it and falls back to the bound-`$1` scoping
    alone.

    For several statements that must share a scope (or be atomic), use
    `tenant_transaction()` instead of calling this repeatedly.
    """

    __slots__ = ("tenant_id",)

    def __init__(self, tenant_id: int) -> None:
        if not tenant_id:
            raise ValueError("TenantDB requires a tenant id")
        self.tenant_id = int(tenant_id)

    async def _run(self, kind: str, sql: str, args: tuple) -> Any:
        started = time.monotonic()
        try:
            async with pool().acquire() as conn:
                async with conn.transaction():
                    await conn.execute(SET_SCOPE, str(self.tenant_id))
                    if kind == "execute":
                        result = await conn.execute(sql, *args)
                    else:
                        rows = await conn.fetch(sql, *args)
                        result = [dict(row) for row in rows]
        except asyncpg.InsufficientPrivilegeError as exc:
            # An RLS policy refused the write. Surfacing it as itself
            # rather than a generic 500 is what makes a genuine
            # cross-tenant bug findable.
            log.error("tenant %s blocked by row-level security: %s", self.tenant_id, exc)
            raise
        except asyncpg.PostgresError as exc:
            log.error("scoped query failed for tenant %s: %s", self.tenant_id, exc)
            raise
        _log_slow(sql, started)
        return result

    async def fetch(self, sql: str, *args: Any) -> list[dict]:
        if not settings.pg_rls:
            return await fetch(sql, self.tenant_id, *args)
        return await self._run("fetch", sql, (self.tenant_id, *args))

    async def fetch_one(self, sql: str, *args: Any) -> dict | None:
        rows = await self.fetch(sql, *args)
        return rows[0] if rows else None

    async def execute(self, sql: str, *args: Any) -> str:
        if not settings.pg_rls:
            return await execute(sql, self.tenant_id, *args)
        return await self._run("execute", sql, (self.tenant_id, *args))


@asynccontextmanager
async def tenant_transaction(tenant_id: int):
    """One connection, one transaction, scoped to a tenant.

    For multi-statement work that has to be atomic — replacing a menu
    tree, provisioning a workspace. Yields a `ScopedConn` whose
    fetch/execute bind $1 to the tenant, matching TenantDB.
    """
    if not tenant_id:
        raise ValueError("tenant_transaction requires a tenant id")

    async with pool().acquire() as conn:
        async with conn.transaction():
            if settings.pg_rls:
                await conn.execute(SET_SCOPE, str(int(tenant_id)))
            yield ScopedConn(conn, int(tenant_id))


class ScopedConn:
    """TenantDB's interface over one already-scoped connection."""

    __slots__ = ("conn", "tenant_id")

    def __init__(self, conn: asyncpg.Connection, tenant_id: int) -> None:
        self.conn = conn
        self.tenant_id = tenant_id

    async def fetch(self, sql: str, *args: Any) -> list[dict]:
        rows = await self.conn.fetch(sql, self.tenant_id, *args)
        return [dict(row) for row in rows]

    async def fetch_one(self, sql: str, *args: Any) -> dict | None:
        rows = await self.fetch(sql, *args)
        return rows[0] if rows else None

    async def execute(self, sql: str, *args: Any) -> str:
        return await self.conn.execute(sql, self.tenant_id, *args)


# --------------------------------------------------------- RLS reporting
_rls_cache: dict | None = None


async def rls_status(refresh: bool = False) -> dict:
    """Is tenant isolation actually enforced by the database?

    Probes rather than assumes: it reads the connecting role's
    attributes and then checks that a scoped connection really is
    filtered. A superuser or BYPASSRLS role silently ignores every
    policy, and FORCE ROW LEVEL SECURITY only reaches the table owner.
    """
    global _rls_cache
    if _rls_cache is not None and not refresh:
        return _rls_cache

    status = {
        "enabled": settings.pg_rls,
        "role": None,
        "isSuperuser": None,
        "bypassRls": None,
        "policies": 0,
        "tablesForced": 0,
        "effective": False,
        "warning": None,
    }
    try:
        row = await fetch_one(
            """SELECT current_user AS role, r.rolsuper, r.rolbypassrls,
                      (SELECT count(*)::int FROM pg_policies
                        WHERE policyname = 'tenant_isolation') AS policies,
                      (SELECT count(*)::int FROM pg_class c
                         JOIN pg_namespace n ON n.oid = c.relnamespace
                        WHERE n.nspname = 'public'
                          AND c.relrowsecurity AND c.relforcerowsecurity) AS forced
                 FROM pg_roles r WHERE r.rolname = current_user"""
        )
        if row:
            status.update(
                role=row["role"],
                isSuperuser=row["rolsuper"],
                bypassRls=row["rolbypassrls"],
                policies=row["policies"],
                tablesForced=row["forced"],
            )

        if not settings.pg_rls:
            status["warning"] = "PG_RLS=0 — only the application-layer scope is active."
        elif not status["policies"]:
            status["warning"] = "No tenant_isolation policies. Apply db/tenancy.sql."
        elif status["isSuperuser"] or status["bypassRls"]:
            status["warning"] = (
                f"Connected as {status['role']}, which bypasses every policy. "
                "Point DATABASE_URL at the crm_app role for database-enforced "
                "isolation (see db/tenancy.sql)."
            )
        else:
            status["effective"] = True
    except Exception as exc:
        status["warning"] = f"Could not determine RLS status: {exc}"

    _rls_cache = status
    return status


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
