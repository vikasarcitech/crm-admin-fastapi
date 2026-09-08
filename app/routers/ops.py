"""Backups, monitoring, logs & notifications (2.11).

Four separate concerns that all answer "is this install healthy?":

* **Backups** — pg_dump to off-server storage. The dump runs in a
  thread (it is a blocking subprocess) and streams straight to S3 when
  a bucket is configured, because a multi-gigabyte dump buffered in
  memory is how a backup job takes down the app it is protecting.
* **Error log** — the folded log written by ``events.log_error``, with
  a viewer and a resolve action.
* **Notifications** — the central feed: new leads, failed publishes,
  system events.
* **Health checks** — outbound probes of the published sites, run by a
  worker and surfaced with latency and failure streaks.

Plus the cross-cutting trash view: content and media that are
soft-deleted, with permanent-delete behind an explicit confirmation.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request

from .. import db, events, storage
from ..config import settings
from ..permissions import require_perm
from ..schemas import BackupRequest, HealthCheckCreate, HealthCheckUpdate, collapse
from ..security import CurrentUser, client_ip, tenant_db

log = logging.getLogger("crm.ops")

router = APIRouter(prefix="/api/ops", tags=["operations"])

BACKUP_TIMEOUT_SECONDS = 1800
HEALTH_TIMEOUT = 10.0


# ============================================================== overview
@router.get("/overview")
async def overview(user: CurrentUser = Depends(require_perm("ops.view"))) -> dict:
    """One screen answering "is anything broken right now?"."""
    scoped = db.TenantDB(user.tenant_id)

    queues = await db.fetch_one(
        """SELECT
             (SELECT count(*) FROM email_outbox WHERE status = 'pending')::int AS email_pending,
             (SELECT count(*) FROM email_outbox WHERE status = 'dead')::int AS email_dead,
             (SELECT count(*) FROM webhook_deliveries WHERE status = 'pending')::int AS hooks_pending,
             (SELECT count(*) FROM webhook_deliveries WHERE status = 'dead')::int AS hooks_dead,
             (SELECT count(*) FROM build_runs WHERE status = 'pending')::int AS builds_pending,
             (SELECT count(*) FROM build_runs WHERE status = 'failed'
               AND created_at > now() - interval '7 days')::int AS builds_failed,
             (SELECT count(*) FROM cdn_invalidations WHERE status = 'pending')::int AS cdn_pending"""
    )
    errors = await scoped.fetch_one(
        """SELECT count(*)::int AS open,
                  coalesce(sum(count), 0)::bigint AS occurrences,
                  max(last_seen_at) AS last_seen_at
             FROM error_log
            WHERE coalesce(tenant_id, $1) = $1 AND NOT is_resolved
              AND last_seen_at > now() - interval '30 days'"""
    )
    health = await scoped.fetch(
        """SELECT id, name, url, last_status, last_latency_ms, last_error,
                  last_checked_at, consecutive_failures, is_active
             FROM health_checks WHERE tenant_id = $1 ORDER BY name"""
    )
    backups = await db.fetch(
        """SELECT id, kind, status::text AS status, byte_size, object_key, destination,
                  error, started_at, finished_at, trigger, created_at
             FROM backups
            WHERE coalesce(tenant_id, $1) = $1 OR tenant_id IS NULL
            ORDER BY created_at DESC LIMIT 10""",
        user.tenant_id,
    )
    trash = await scoped.fetch_one(
        """SELECT (SELECT count(*) FROM content_items
                    WHERE tenant_id = $1 AND status = 'trashed')::int AS content,
                  (SELECT count(*) FROM media
                    WHERE tenant_id = $1 AND deleted_at IS NOT NULL)::int AS media"""
    )
    return {
        "queues": queues,
        "errors": errors,
        "health": health,
        "backups": backups,
        "trash": trash,
        "storage": storage.describe(),
        "backupTarget": settings.backup_s3_bucket or None,
        "workersEnabled": settings.run_workers,
    }


# =========================================================== error log
@router.get("/errors")
async def list_errors(
    include_resolved: bool = False,
    level: str | None = Query(default=None, max_length=20),
    days: int = Query(default=30, ge=1, le=365),
    user: CurrentUser = Depends(require_perm("logs.view")),
) -> dict:
    rows = await db.fetch(
        """SELECT e.id, e.level, e.source, e.message, e.fingerprint, e.detail,
                  e.request_method, e.request_path, e.count, e.is_resolved,
                  e.first_seen_at, e.last_seen_at, u.display_name AS user_name
             FROM error_log e LEFT JOIN users u ON u.id = e.user_id
            WHERE (e.tenant_id = $1 OR e.tenant_id IS NULL)
              AND e.last_seen_at > now() - make_interval(days => $2)
              AND ($3::boolean OR NOT e.is_resolved)
              AND ($4::text IS NULL OR e.level = $4)
            ORDER BY e.is_resolved, e.last_seen_at DESC LIMIT 300""",
        user.tenant_id, days, include_resolved, level,
    )
    return {"errors": rows}


@router.post("/errors/{error_id}/resolve")
async def resolve_error(
    error_id: int, user: CurrentUser = Depends(require_perm("logs.view"))
) -> dict:
    """Marks it handled. A new occurrence reopens it automatically —
    log_error's upsert sets is_resolved back to false."""
    row = await db.fetch_one(
        """UPDATE error_log SET is_resolved = TRUE
            WHERE id = $1 AND (tenant_id = $2 OR tenant_id IS NULL) RETURNING id""",
        error_id, user.tenant_id,
    )
    if not row:
        raise HTTPException(404, "That entry no longer exists.")
    return {"ok": True}


@router.delete("/errors")
async def clear_errors(
    resolved_only: bool = True,
    user: CurrentUser = Depends(require_perm("logs.view")),
) -> dict:
    removed = await db.fetch(
        """DELETE FROM error_log
            WHERE (tenant_id = $1 OR tenant_id IS NULL)
              AND (NOT $2::boolean OR is_resolved) RETURNING id""",
        user.tenant_id, resolved_only,
    )
    return {"ok": True, "deleted": len(removed)}


# ========================================================= notifications
@router.get("/notifications")
async def list_notifications(
    unread_only: bool = False,
    limit: int = Query(default=50, ge=10, le=200),
    user: CurrentUser = Depends(require_perm("ops.view")),
) -> dict:
    scoped = db.TenantDB(user.tenant_id)
    rows = await scoped.fetch(
        """SELECT id, kind, level, title, body, link, read_at, created_at
             FROM notifications
            WHERE tenant_id = $1
              AND (user_id IS NULL OR user_id = $2)
              AND ($3::boolean IS NOT TRUE OR read_at IS NULL)
            ORDER BY created_at DESC LIMIT $4""",
        user.id, unread_only, limit,
    )
    unread = await scoped.fetch_one(
        """SELECT count(*)::int AS n FROM notifications
            WHERE tenant_id = $1 AND (user_id IS NULL OR user_id = $2) AND read_at IS NULL""",
        user.id,
    )
    return {"notifications": rows, "unread": unread["n"]}


@router.post("/notifications/{notification_id}/read")
async def mark_read(
    notification_id: int, user: CurrentUser = Depends(require_perm("ops.view"))
) -> dict:
    scoped = db.TenantDB(user.tenant_id)
    await scoped.execute(
        """UPDATE notifications SET read_at = now()
            WHERE tenant_id = $1 AND id = $2 AND read_at IS NULL""",
        notification_id,
    )
    return {"ok": True}


@router.post("/notifications/read-all")
async def mark_all_read(user: CurrentUser = Depends(require_perm("ops.view"))) -> dict:
    scoped = db.TenantDB(user.tenant_id)
    rows = await scoped.fetch(
        """UPDATE notifications SET read_at = now()
            WHERE tenant_id = $1 AND (user_id IS NULL OR user_id = $2) AND read_at IS NULL
            RETURNING id""",
        user.id,
    )
    return {"ok": True, "marked": len(rows)}


# ================================================================ trash
@router.get("/trash")
async def list_trash(scoped: db.TenantDB = Depends(tenant_db)) -> dict:
    """Everything soft-deleted, in one place."""
    content = await scoped.fetch(
        """SELECT i.id, i.title, i.slug::text AS slug, i.trashed_at,
                  t.name AS type_name, t.slug::text AS type_slug,
                  u.display_name AS author_name
             FROM content_items i
             JOIN content_types t ON t.id = i.type_id
             LEFT JOIN users u ON u.id = i.author_id
            WHERE i.tenant_id = $1 AND i.status = 'trashed'
            ORDER BY i.trashed_at DESC LIMIT 300"""
    )
    media = await scoped.fetch(
        """SELECT m.id, m.original_filename, m.mime_type, m.byte_size, m.storage_key,
                  m.deleted_at, u.display_name AS uploaded_by_name,
                  (SELECT count(*) FROM media_usage mu WHERE mu.media_id = m.id)::int AS usage_count
             FROM media m LEFT JOIN users u ON u.id = m.uploaded_by
            WHERE m.tenant_id = $1 AND m.deleted_at IS NOT NULL
            ORDER BY m.deleted_at DESC LIMIT 300"""
    )
    for row in media:
        row["url"] = storage.public_url(row["storage_key"])
    return {
        "content": content,
        "media": media,
        "reclaimableBytes": sum(row["byte_size"] for row in media),
    }


@router.post("/trash/empty")
async def empty_trash(
    request: Request,
    confirm: bool = Query(default=False),
    older_than_days: int = Query(default=0, ge=0, le=3650),
    user: CurrentUser = Depends(require_perm("content.purge")),
) -> dict:
    """Permanently delete everything in the trash.

    Two guards, because this is unrecoverable: confirm=true, and an
    optional age filter so "empty things older than 30 days" is
    possible without touching a page trashed this morning.
    """
    if not confirm:
        raise HTTPException(400, "Emptying the trash needs confirm=true.")

    scoped = db.TenantDB(user.tenant_id)
    content = await scoped.fetch(
        """DELETE FROM content_items
            WHERE tenant_id = $1 AND status = 'trashed'
              AND trashed_at < now() - make_interval(days => $2)
            RETURNING id, title""",
        older_than_days,
    )
    media_rows = await scoped.fetch(
        """SELECT id, storage_key, variants FROM media
            WHERE tenant_id = $1 AND deleted_at IS NOT NULL
              AND deleted_at < now() - make_interval(days => $2)""",
        older_than_days,
    )
    keys: list[str] = []
    for row in media_rows:
        keys.append(row["storage_key"])
        keys += [v["key"] for v in (row["variants"] or []) if v.get("key")]

    if media_rows:
        await scoped.execute(
            "DELETE FROM media WHERE tenant_id = $1 AND id = ANY($2::bigint[])",
            [row["id"] for row in media_rows],
        )
        # Objects after rows: an orphaned object is cheap, a row pointing
        # at a deleted object breaks every page that renders it.
        storage.delete_many(keys)

    await events.log_activity(
        user.tenant_id, "trash.emptied", user_id=user.id,
        meta={"content": len(content), "media": len(media_rows), "objects": len(keys)},
        ip=db.to_inet(client_ip(request)),
    )
    return {
        "ok": True,
        "contentDeleted": len(content),
        "mediaDeleted": len(media_rows),
        "objectsDeleted": len(keys),
    }


# ============================================================== backups
@router.get("/backups")
async def list_backups(user: CurrentUser = Depends(require_perm("backups.manage"))) -> dict:
    rows = await db.fetch(
        """SELECT b.id, b.tenant_id, b.kind, b.status::text AS status, b.destination,
                  b.object_key, b.byte_size, b.checksum, b.trigger, b.error,
                  b.started_at, b.finished_at, b.created_at,
                  u.display_name AS created_by_name
             FROM backups b LEFT JOIN users u ON u.id = b.created_by
            ORDER BY b.created_at DESC LIMIT 50"""
    )
    return {
        "backups": rows,
        "target": settings.backup_s3_bucket or None,
        "configured": bool(settings.backup_s3_bucket),
        "pgDump": shutil.which(settings.pg_dump_path) or None,
        # Off-server storage is the whole point: a dump left on the same
        # disk as the database is not a backup.
        "warning": (
            None if settings.backup_s3_bucket
            else "BACKUP_S3_BUCKET is not set, so dumps stay on this server's disk."
        ),
    }


@router.post("/backups", status_code=202)
async def run_backup(
    payload: BackupRequest,
    request: Request,
    user: CurrentUser = Depends(require_perm("backups.manage")),
) -> dict:
    """Start a backup. Returns immediately; the row tracks progress."""
    if payload.kind in {"database", "full"} and not shutil.which(settings.pg_dump_path):
        raise HTTPException(
            400,
            f"pg_dump was not found at “{settings.pg_dump_path}”. "
            "Install the PostgreSQL client tools or set PG_DUMP_PATH.",
        )

    row = await db.fetch_one(
        """INSERT INTO backups (tenant_id, kind, status, destination, trigger, created_by)
           VALUES ($1, $2, 'pending', $3, 'manual', $4) RETURNING id, kind, status::text AS status""",
        user.tenant_id, payload.kind,
        f"s3://{settings.backup_s3_bucket}/{settings.backup_s3_prefix}"
        if settings.backup_s3_bucket else "local",
        user.id,
    )
    await events.log_activity(
        user.tenant_id, "backup.started", user_id=user.id,
        object_type="backup", object_id=row["id"], meta={"kind": payload.kind},
        ip=db.to_inet(client_ip(request)),
    )
    # Fire and forget: a 30-minute dump must not hold the HTTP request.
    asyncio.create_task(perform_backup(row["id"], user.tenant_id, payload.kind))
    return {"ok": True, "backup": row, "message": "Backup started. Refresh to see progress."}


async def perform_backup(backup_id: int, tenant_id: int, kind: str) -> None:
    """Run one backup to completion. Also called by the scheduled worker."""
    await db.execute(
        "UPDATE backups SET status = 'running', started_at = now() WHERE id = $1", backup_id
    )
    try:
        if kind == "media":
            key, size, digest = await asyncio.to_thread(_archive_media, tenant_id)
        else:
            key, size, digest = await asyncio.to_thread(_dump_database)

        await db.execute(
            """UPDATE backups
                  SET status = 'complete', object_key = $2, byte_size = $3,
                      checksum = $4, finished_at = now(), error = NULL
                WHERE id = $1""",
            backup_id, key, size, digest,
        )
        await events.notify(
            tenant_id, "backup.complete", f"{kind.title()} backup complete",
            body=f"{size / 1048576:.1f} MB written to {key}", level="success", link="#/operations",
        )
    except Exception as exc:
        log.exception("backup %s failed", backup_id)
        await db.execute(
            "UPDATE backups SET status = 'failed', error = $2, finished_at = now() WHERE id = $1",
            backup_id, str(exc)[:1000],
        )
        await events.notify(
            tenant_id, "backup.failed", f"{kind.title()} backup failed",
            body=str(exc)[:500], level="error", link="#/operations",
        )
        await events.log_error(
            f"backup failed: {exc}", tenant_id=tenant_id, source="backup", level="critical"
        )


def _upload_or_keep(path: Path, key: str) -> tuple[str, int, str]:
    """Push to S3 when configured; otherwise leave it on disk and say so.

    Returns (location, bytes, sha256). The hash is computed streaming so
    a large dump is never fully resident in memory.
    """
    import hashlib  # noqa: PLC0415

    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)

    if not settings.backup_s3_bucket:
        target = Path(settings.media_root).parent / "backups"
        target.mkdir(parents=True, exist_ok=True)
        final = target / key.rsplit("/", 1)[-1]
        shutil.move(str(path), final)
        return str(final), size, digest.hexdigest()

    try:
        import boto3  # noqa: PLC0415
    except ImportError as exc:
        raise RuntimeError("BACKUP_S3_BUCKET is set but boto3 is not installed.") from exc

    client = boto3.client("s3", region_name=settings.aws_region or None)
    full_key = f"{settings.backup_s3_prefix.strip('/')}/{key}" if settings.backup_s3_prefix else key
    # upload_file streams in parts, so dump size is bounded by disk, not RAM.
    client.upload_file(
        str(path), settings.backup_s3_bucket, full_key,
        ExtraArgs={"ServerSideEncryption": "AES256"},
    )
    path.unlink(missing_ok=True)
    return f"s3://{settings.backup_s3_bucket}/{full_key}", size, digest.hexdigest()


def _dump_database() -> tuple[str, int, str]:
    """pg_dump the whole database, gzip-compressed, to off-server storage.

    Whole-database rather than per-tenant: a per-tenant dump cannot
    restore referential integrity on its own, and this is a disaster
    recovery artefact, not a data export (that is 2.12's job).
    """
    if not settings.database_url:
        raise RuntimeError("DATABASE_URL is not set.")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    key = f"db-{stamp}.dump"
    with tempfile.TemporaryDirectory() as work:
        path = Path(work) / key
        # -Fc is compressed and restorable selectively with pg_restore.
        # --no-owner/--no-acl so a restore into a differently-owned
        # database does not fail on every GRANT.
        command = [
            settings.pg_dump_path, "--format=custom", "--compress=6",
            "--no-owner", "--no-acl", f"--file={path}", settings.database_url,
        ]
        env = {**os.environ, "PGCONNECT_TIMEOUT": "10"}
        result = subprocess.run(  # noqa: S603 — fixed argv, no shell
            command, capture_output=True, text=True,
            timeout=BACKUP_TIMEOUT_SECONDS, env=env, check=False,
        )
        if result.returncode != 0:
            # stderr can echo the connection string; keep the password out.
            detail = (result.stderr or "")[-500:].replace(settings.database_url, "<dsn>")
            raise RuntimeError(f"pg_dump exited {result.returncode}: {detail}")
        if not path.is_file() or path.stat().st_size == 0:
            raise RuntimeError("pg_dump produced an empty file.")
        return _upload_or_keep(path, key)


def _archive_media(tenant_id: int) -> tuple[str, int, str]:
    """Tar+gzip one tenant's media tree (local storage only).

    With MEDIA_STORAGE=s3 this is deliberately a no-op that raises:
    re-uploading S3 objects through this app to copy them within S3
    would be slow and expensive. Use S3 versioning plus a replication
    rule instead — that is the AWS-native answer.
    """
    import tarfile  # noqa: PLC0415

    if settings.media_storage != "local":
        raise RuntimeError(
            "Media lives in S3. Enable bucket versioning and a replication rule "
            "for backups rather than copying objects through the app."
        )

    root = Path(settings.media_root).expanduser().resolve() / f"t{tenant_id}"
    if not root.is_dir():
        raise RuntimeError("This site has no locally stored media.")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    key = f"media-t{tenant_id}-{stamp}.tar.gz"
    with tempfile.TemporaryDirectory() as work:
        path = Path(work) / key
        with tarfile.open(path, "w:gz") as archive:
            archive.add(root, arcname=f"t{tenant_id}")
        return _upload_or_keep(path, key)


# ======================================================== health checks
@router.get("/health-checks")
async def list_health_checks(user: CurrentUser = Depends(require_perm("ops.view"))) -> dict:
    scoped = db.TenantDB(user.tenant_id)
    rows = await scoped.fetch(
        """SELECT id, name, url, expect_status, expect_text, interval_seconds, is_active,
                  last_status, last_latency_ms, last_error, last_checked_at,
                  consecutive_failures, created_at
             FROM health_checks WHERE tenant_id = $1 ORDER BY name"""
    )
    return {"checks": rows, "workersEnabled": settings.run_workers}


@router.post("/health-checks", status_code=201)
async def create_health_check(
    payload: HealthCheckCreate, user: CurrentUser = Depends(require_perm("ops.view"))
) -> dict:
    url = (payload.url or "").strip()
    if not url.lower().startswith(("https://", "http://")):
        raise HTTPException(400, "The URL must start with https:// or http://")

    scoped = db.TenantDB(user.tenant_id)
    row = await scoped.fetch_one(
        """INSERT INTO health_checks (tenant_id, name, url, expect_status, expect_text,
                                      interval_seconds)
           VALUES ($1, $2, $3, $4, $5, $6)
           RETURNING id, name, url, expect_status, expect_text, interval_seconds,
                     is_active, created_at""",
        collapse(payload.name, 80), url, payload.expect_status,
        collapse(payload.expect_text, 200), payload.interval_seconds,
    )
    return {"check": row}


@router.patch("/health-checks/{check_id}")
async def update_health_check(
    check_id: int,
    payload: HealthCheckUpdate,
    user: CurrentUser = Depends(require_perm("ops.view")),
) -> dict:
    scoped = db.TenantDB(user.tenant_id)
    sent = payload.model_dump(exclude_unset=True)
    if not sent:
        raise HTTPException(400, "Nothing to save.")

    row = await scoped.fetch_one(
        """UPDATE health_checks
              SET name = coalesce($3, name),
                  url = coalesce($4, url),
                  expect_status = coalesce($5, expect_status),
                  expect_text = CASE WHEN $6 THEN $7 ELSE expect_text END,
                  interval_seconds = coalesce($8, interval_seconds),
                  is_active = coalesce($9, is_active)
            WHERE tenant_id = $1 AND id = $2
            RETURNING id, name, url, expect_status, expect_text, interval_seconds,
                      is_active, last_status, last_latency_ms, last_checked_at""",
        check_id, collapse(payload.name, 80),
        (payload.url or "").strip() or None, payload.expect_status,
        "expect_text" in sent, collapse(payload.expect_text, 200),
        payload.interval_seconds, payload.is_active,
    )
    if not row:
        raise HTTPException(404, "That check no longer exists.")
    return {"check": row}


@router.delete("/health-checks/{check_id}")
async def delete_health_check(
    check_id: int, user: CurrentUser = Depends(require_perm("ops.view"))
) -> dict:
    scoped = db.TenantDB(user.tenant_id)
    removed = await scoped.fetch(
        "DELETE FROM health_checks WHERE tenant_id = $1 AND id = $2 RETURNING id", check_id
    )
    if not removed:
        raise HTTPException(404, "That check no longer exists.")
    return {"ok": True}


@router.post("/health-checks/{check_id}/run")
async def run_health_check(
    check_id: int, user: CurrentUser = Depends(require_perm("ops.view"))
) -> dict:
    scoped = db.TenantDB(user.tenant_id)
    row = await scoped.fetch_one(
        """SELECT id, tenant_id, name, url, expect_status, expect_text, consecutive_failures
             FROM health_checks WHERE tenant_id = $1 AND id = $2""",
        check_id,
    )
    if not row:
        raise HTTPException(404, "That check no longer exists.")

    async with httpx.AsyncClient(follow_redirects=True) as client:
        result = await _probe(client, row)
    return {"result": result}


async def _probe(client: httpx.AsyncClient, check: dict) -> dict:
    """Run one probe and record the outcome."""
    started = time.monotonic()
    status_code: int | None = None
    error: str | None = None

    try:
        response = await client.get(
            check["url"],
            timeout=HEALTH_TIMEOUT,
            headers={"user-agent": "crm-admin-healthcheck/1.0"},
        )
        status_code = response.status_code
        if status_code != check["expect_status"]:
            error = f"expected HTTP {check['expect_status']}, got {status_code}"
        elif check["expect_text"] and check["expect_text"] not in response.text[:200_000]:
            error = f"page did not contain “{check['expect_text']}”"
    except httpx.TimeoutException:
        error = f"timed out after {HEALTH_TIMEOUT:.0f}s"
    except httpx.HTTPError as exc:
        error = str(exc)[:300]

    latency = int((time.monotonic() - started) * 1000)
    failures = 0 if error is None else check["consecutive_failures"] + 1

    await db.execute(
        """UPDATE health_checks
              SET last_status = $2, last_latency_ms = $3, last_error = $4,
                  last_checked_at = now(), consecutive_failures = $5
            WHERE id = $1""",
        check["id"], status_code, latency, error, failures,
    )

    # Alert on the second consecutive failure, not the first: a single
    # blip would page someone every deploy.
    if failures == 2:
        await events.notify(
            check["tenant_id"], "health.down", f"“{check['name']}” is failing",
            body=f"{check['url']} — {error}", level="error", link="#/operations",
        )
        await events.emit(
            check["tenant_id"], "health.down",
            {"name": check["name"], "url": check["url"], "error": error},
        )
    elif error is None and check["consecutive_failures"] >= 2:
        await events.notify(
            check["tenant_id"], "health.up", f"“{check['name']}” is back up",
            body=f"Responded in {latency} ms.", level="success", link="#/operations",
        )

    return {"ok": error is None, "status": status_code, "latencyMs": latency, "error": error}
