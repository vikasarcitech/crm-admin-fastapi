"""Media derivative generation, off the request path.

Building six responsive derivatives from a 12 MP photo is roughly 1.7
seconds of CPU. Doing that inside the upload handler blocked the whole
event loop — not just that request — so one client uploading a photo
gallery stalled every other request on the worker. At a hundred sites
that is an availability problem, not a latency one.

The split:

  upload request   validate the bytes, store the original under a
                   `src/` key, insert the media row as `pending`,
                   queue a job. Returns in the time it takes to write
                   one object.
  worker           read the source back, encode the derivatives in a
                   thread, upload them, swap the row to the optimized
                   primary, delete the source.

Source objects are deliberately not servable: `state != 'ready'` means
no URL, and the local file route refuses `src/` keys. The uploaded
original still carries its EXIF — GPS included — until the worker
strips it, and a few seconds of that being publicly fetchable is not
worth the convenience.
"""

from __future__ import annotations

import asyncio
import logging
import time

from . import db, events, imaging, storage
from .config import settings

log = logging.getLogger("crm.media_jobs")

MAX_ATTEMPTS = 3
BACKOFF_MINUTES = [1, 5]
BATCH_SIZE = 4          # CPU-bound: a big batch just queues behind itself

SOURCE_PREFIX = "src/"


def source_key(tenant_id: int, filename: str, data: bytes, extension: str) -> str:
    """Where the untouched upload is parked until it is processed."""
    return SOURCE_PREFIX + imaging.storage_key(tenant_id, filename, data, extension)


def is_source_key(key: str) -> bool:
    return (key or "").startswith(SOURCE_PREFIX)


async def queue(
    tenant_id: int, media_id: int, *, source: str, mime: str, filename: str,
    kind: str = "derivatives",
) -> int | None:
    try:
        row = await db.fetch_one(
            """INSERT INTO media_jobs (tenant_id, media_id, kind, source_key,
                                       mime_type, filename)
               VALUES ($1, $2, $3, $4, $5, $6) RETURNING id""",
            tenant_id, media_id, kind, source, mime, filename,
        )
        return row["id"] if row else None
    except Exception as exc:
        log.error("could not queue media job for %s: %s", media_id, exc)
        return None


async def run_batch(batch_size: int = BATCH_SIZE) -> int:
    """Process due jobs once. Returns how many were attempted."""
    try:
        due = await db.fetch(
            """SELECT j.id, j.tenant_id, j.media_id, j.kind, j.attempts,
                      j.source_key, j.mime_type, j.filename
                 FROM media_jobs j
                WHERE j.status = 'pending' AND j.next_attempt_at <= now()
                ORDER BY j.next_attempt_at
                LIMIT $1
                FOR UPDATE SKIP LOCKED""",
            batch_size,
        )
    except Exception as exc:
        log.error("media job poll failed: %s", exc)
        return 0

    for job in due:
        await _process(job)
    return len(due)


async def _process(job: dict) -> None:
    started = time.monotonic()
    await db.execute(
        "UPDATE media_jobs SET status = 'running', started_at = now() WHERE id = $1",
        job["id"],
    )
    await db.execute(
        "UPDATE media SET state = 'processing' WHERE id = $1", job["media_id"]
    )

    try:
        data = await asyncio.to_thread(storage.get, job["source_key"])
    except storage.StorageError as exc:
        await _fail(job, f"Source file could not be read: {exc}", retryable=False)
        return

    try:
        # to_thread: this is the CPU-bound part, and the whole point of
        # the exercise is keeping it off the event loop.
        primary, variants = await asyncio.to_thread(
            imaging.build_variants,
            job["tenant_id"], job["filename"], data, job["mime_type"],
        )
    except imaging.UploadRejected as exc:
        await _fail(job, str(exc), retryable=False)
        return
    except Exception as exc:
        log.exception("media job %s failed to encode", job["id"])
        await _fail(job, f"{type(exc).__name__}: {exc}"[:400], retryable=True)
        return

    written: list[str] = []
    try:
        await asyncio.to_thread(
            storage.put, primary["key"], primary["data"], primary["mime"]
        )
        written.append(primary["key"])
        for variant in variants:
            await asyncio.to_thread(
                storage.put, variant["key"], variant["data"], variant["mime"]
            )
            written.append(variant["key"])
    except storage.StorageError as exc:
        # Roll back the partial set; a half-written derivative set with
        # nothing pointing at it is just paid-for garbage.
        await asyncio.to_thread(storage.delete_many, written)
        await _fail(job, str(exc), retryable=True)
        return

    elapsed = int((time.monotonic() - started) * 1000)
    await db.execute(
        """UPDATE media
              SET storage_key = $2, filename = $3, mime_type = $4, byte_size = $5,
                  width = $6, height = $7, variants = $8::jsonb,
                  state = 'ready', processing_error = NULL, updated_at = now()
            WHERE id = $1""",
        job["media_id"], primary["key"], primary["key"].rsplit("/", 1)[-1],
        primary["mime"], primary["bytes"], primary["width"], primary["height"],
        [{k: v for k, v in variant.items() if k != "data"} for variant in variants],
    )
    await db.execute(
        """UPDATE media_jobs SET status = 'complete', attempts = attempts + 1,
                  finished_at = now(), duration_ms = $2, error = NULL
            WHERE id = $1""",
        job["id"], elapsed,
    )

    # The untouched original — EXIF and all — is no longer needed.
    await asyncio.to_thread(storage.delete, job["source_key"])
    log.info(
        "media %s processed: %d variants in %d ms", job["media_id"], len(variants), elapsed
    )


async def _fail(job: dict, error: str, *, retryable: bool) -> None:
    attempts = job["attempts"] + 1
    dead = not retryable or attempts >= MAX_ATTEMPTS
    delay = BACKOFF_MINUTES[min(attempts - 1, len(BACKOFF_MINUTES) - 1)]

    await db.execute(
        """UPDATE media_jobs
              SET status = $2::job_status, attempts = $3, error = $4,
                  next_attempt_at = now() + make_interval(mins => $5),
                  finished_at = CASE WHEN $2 = 'failed' THEN now() END
            WHERE id = $1""",
        job["id"], "failed" if dead else "pending", attempts, error[:500], delay,
    )
    if dead:
        await db.execute(
            "UPDATE media SET state = 'failed', processing_error = $2 WHERE id = $1",
            job["media_id"], error[:500],
        )
        await events.notify(
            job["tenant_id"], "media.failed",
            f"“{job['filename']}” could not be processed",
            body=error[:400], level="error", link="#/media",
        )
    log.error("media job %s failed (attempt %d): %s", job["id"], attempts, error)


async def worker() -> None:
    """Background loop. Cancelled on shutdown by the lifespan handler."""
    while True:
        try:
            processed = await run_batch()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("media worker error: %s", exc)
            processed = 0
        # Poll faster while there is a backlog: an editor waiting for a
        # thumbnail should not wait out an idle interval.
        await asyncio.sleep(0.5 if processed else settings.media_poll_seconds)


async def requeue_stuck(older_than_minutes: int = 15) -> int:
    """Reclaim jobs whose worker died mid-encode.

    `status = 'running'` with no heartbeat means the process holding it
    is gone; without this the media stays 'processing' forever.
    """
    rows = await db.fetch(
        """UPDATE media_jobs
              SET status = 'pending', next_attempt_at = now(),
                  error = 'Worker stopped mid-job; requeued.'
            WHERE status = 'running'
              AND started_at < now() - make_interval(mins => $1)
            RETURNING id, media_id""",
        older_than_minutes,
    )
    for row in rows:
        await db.execute(
            "UPDATE media SET state = 'pending' WHERE id = $1", row["media_id"]
        )
    if rows:
        log.warning("requeued %d stuck media job(s)", len(rows))
    return len(rows)
