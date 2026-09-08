"""Media management (2.3).

Upload flow, in order:

1. read the body with a hard byte cap;
2. validate by magic bytes, not the declared Content-Type
   (app/imaging.py);
3. build responsive WebP/AVIF derivatives and an optimized fallback;
4. write every object to storage (app/storage.py);
5. insert one ``media`` row describing the set.

Deletion is a two-step trash, and a file that is still referenced by
content cannot be deleted without an explicit force — the usage table
(rebuilt on every content save) is what makes that check trustworthy
rather than a guess.
"""

from __future__ import annotations

import logging

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.responses import Response

from .. import db, events, imaging, storage
from ..content import slugify
from ..permissions import require_perm
from ..schemas import (
    FolderCreate,
    FolderUpdate,
    MediaBulkAction,
    MediaBulkRequest,
    MediaUpdate,
    collapse,
    keep_lines,
)
from ..security import CurrentUser, client_ip, tenant_db

log = logging.getLogger("crm.media")

router = APIRouter(prefix="/api/media", tags=["media"])
public_router = APIRouter(tags=["media-public"])

MEDIA_COLUMNS = """m.id, m.folder_id, m.storage_key, m.filename, m.original_filename,
                   m.mime_type, m.byte_size, m.width, m.height, m.checksum,
                   m.alt_text, m.title, m.caption, m.tags, m.variants,
                   m.uploaded_by, m.deleted_at, m.created_at, m.updated_at"""

MAX_TAGS = 25


def _decorate(row: dict) -> dict:
    """Add the URLs and the srcset the frontend actually consumes."""
    row["url"] = storage.public_url(row["storage_key"])
    variants = row.get("variants") or []
    for variant in variants:
        variant["url"] = storage.public_url(variant["key"])

    # Grouped by format so a <picture> can offer AVIF, then WebP, then
    # the fallback — cheapest format first is the whole point.
    srcset: dict[str, str] = {}
    for variant in variants:
        mime = variant.get("mime", "")
        if not variant.get("width"):
            continue
        srcset.setdefault(mime, "")
        srcset[mime] += f"{', ' if srcset[mime] else ''}{variant['url']} {variant['width']}w"
    row["srcset"] = srcset
    row["isImage"] = row["mime_type"] in imaging.RASTER_TYPES
    return row


async def _get(scoped: db.TenantDB, media_id: int, *, include_deleted: bool = False) -> dict:
    row = await scoped.fetch_one(
        f"""SELECT {MEDIA_COLUMNS} FROM media m
             WHERE m.tenant_id = $1 AND m.id = $2
               AND ($3::boolean OR m.deleted_at IS NULL)""",
        media_id, include_deleted,
    )
    if not row:
        raise HTTPException(404, "That file no longer exists.")
    return row


# ================================================================ listing
@router.get("")
async def list_media(
    q: str | None = Query(default=None, max_length=120),
    folder_id: int | None = None,
    mime: str | None = Query(default=None, max_length=60),
    tag: str | None = Query(default=None, max_length=40),
    trashed: bool = False,
    unused: bool = False,
    page: int = Query(default=1, ge=1, le=500),
    per_page: int = Query(default=40, ge=10, le=200),
    scoped: db.TenantDB = Depends(tenant_db),
) -> dict:
    where = ["m.tenant_id = $1"]
    args: list = []

    def add(clause: str, value) -> None:
        args.append(value)
        where.append(clause.replace("?", f"${len(args) + 1}"))

    where.append("m.deleted_at IS NOT NULL" if trashed else "m.deleted_at IS NULL")
    if q:
        add(
            "to_tsvector('simple', coalesce(m.original_filename,'') || ' ' ||"
            " coalesce(m.alt_text,'') || ' ' || coalesce(m.title,'') || ' ' ||"
            " coalesce(m.caption,'')) @@ plainto_tsquery('simple', ?)",
            q.strip(),
        )
    if folder_id is not None:
        # folder_id=0 means "files not in any folder".
        if folder_id == 0:
            where.append("m.folder_id IS NULL")
        else:
            add("m.folder_id = ?", folder_id)
    if mime:
        # 'image' matches image/*; a full type matches exactly.
        if "/" in mime:
            add("m.mime_type = ?", mime)
        else:
            add("m.mime_type LIKE ? || '/%'", mime)
    if tag:
        add("? = ANY(m.tags)", tag.strip().lower())
    if unused:
        where.append(
            "NOT EXISTS (SELECT 1 FROM media_usage u WHERE u.media_id = m.id)"
        )

    clause = " AND ".join(where)
    offset = (page - 1) * per_page

    rows = await scoped.fetch(
        f"""SELECT {MEDIA_COLUMNS}, u.display_name AS uploaded_by_name,
                   f.name AS folder_name,
                   (SELECT count(*) FROM media_usage mu WHERE mu.media_id = m.id)::int AS usage_count
              FROM media m
              LEFT JOIN users u ON u.id = m.uploaded_by
              LEFT JOIN media_folders f ON f.id = m.folder_id
             WHERE {clause}
             ORDER BY m.created_at DESC
             LIMIT {per_page} OFFSET {offset}""",
        *args,
    )
    total = await scoped.fetch_one(
        f"SELECT count(*)::int AS n FROM media m WHERE {clause}", *args
    )
    stats = await scoped.fetch_one(
        """SELECT count(*)::int AS files,
                  coalesce(sum(byte_size), 0)::bigint AS bytes
             FROM media WHERE tenant_id = $1 AND deleted_at IS NULL"""
    )

    return {
        "media": [_decorate(row) for row in rows],
        "page": page,
        "perPage": per_page,
        "total": total["n"],
        "pages": max(1, -(-total["n"] // per_page)),
        "library": {"files": stats["files"], "bytes": int(stats["bytes"])},
        "storage": storage.describe(),
    }


@router.get("/tags")
async def list_tags(scoped: db.TenantDB = Depends(tenant_db)) -> dict:
    rows = await scoped.fetch(
        """SELECT tag, count(*)::int AS n
             FROM media, unnest(tags) AS tag
            WHERE tenant_id = $1 AND deleted_at IS NULL
            GROUP BY tag ORDER BY n DESC, tag LIMIT 200"""
    )
    return {"tags": rows}


# ================================================================ upload
@router.post("", status_code=201)
async def upload(
    request: Request,
    response: Response,
    file: UploadFile = File(...),
    folder_id: int | None = Form(default=None),
    alt_text: str | None = Form(default=None),
    title: str | None = Form(default=None),
    caption: str | None = Form(default=None),
    tags: str | None = Form(default=None),
    user: CurrentUser = Depends(require_perm("media.upload")),
) -> dict:
    scoped = db.TenantDB(user.tenant_id)

    # Read with a cap: UploadFile spools to disk, so an unbounded read
    # is a disk-fill vector even before it is a memory one.
    data = await file.read(imaging.MAX_UPLOAD_BYTES + 1)
    if len(data) > imaging.MAX_UPLOAD_BYTES:
        raise HTTPException(
            413,
            f"That file is larger than {imaging.MAX_UPLOAD_BYTES // 1048576} MB.",
        )

    original_name = (file.filename or "upload").strip()[:200] or "upload"
    try:
        mime = imaging.validate_upload(original_name, file.content_type, data)
    except imaging.UploadRejected as exc:
        raise HTTPException(400, str(exc)) from exc

    if folder_id:
        folder = await scoped.fetch_one(
            "SELECT id FROM media_folders WHERE tenant_id = $1 AND id = $2", folder_id
        )
        if not folder:
            raise HTTPException(400, "That folder no longer exists.")

    digest = imaging.checksum(data)
    duplicate = await scoped.fetch_one(
        f"""SELECT {MEDIA_COLUMNS} FROM media m
             WHERE m.tenant_id = $1 AND m.checksum = $2 AND m.deleted_at IS NULL
             LIMIT 1""",
        digest,
    )
    if duplicate:
        # Re-uploading the same bytes returns the existing row instead of
        # paying for a second copy in S3 and a second CDN cache entry.
        # 200, not 201: nothing was created.
        response.status_code = 200
        return {"media": _decorate(duplicate), "duplicate": True}

    try:
        primary, variants = imaging.build_variants(user.tenant_id, original_name, data, mime)
    except imaging.UploadRejected as exc:
        raise HTTPException(400, str(exc)) from exc

    written: list[str] = []
    try:
        storage.put(primary["key"], primary["data"], primary["mime"])
        written.append(primary["key"])
        for variant in variants:
            storage.put(variant["key"], variant["data"], variant["mime"])
            written.append(variant["key"])
    except storage.StorageError as exc:
        # Roll back the objects already written; a half-uploaded set
        # would otherwise be orphaned with nothing pointing at it.
        storage.delete_many(written)
        raise HTTPException(502, str(exc)) from exc

    row = await scoped.fetch_one(
        f"""INSERT INTO media (tenant_id, folder_id, storage_key, filename, original_filename,
                               mime_type, byte_size, checksum, width, height,
                               alt_text, title, caption, tags, variants, uploaded_by)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15::jsonb, $16)
            RETURNING {MEDIA_COLUMNS.replace("m.", "")}""",
        folder_id,
        primary["key"],
        primary["key"].rsplit("/", 1)[-1],
        original_name,
        primary["mime"],
        primary["bytes"],
        digest,
        primary["width"],
        primary["height"],
        collapse(alt_text, 300),
        collapse(title, 200) or original_name.rsplit(".", 1)[0][:200],
        keep_lines(caption, 600),
        _parse_tags(tags),
        # Strip the bytes: only the metadata belongs in the row.
        [{k: v for k, v in variant.items() if k != "data"} for variant in variants],
        user.id,
    )

    await events.log_activity(
        user.tenant_id, "media.uploaded", user_id=user.id,
        object_type="media", object_id=row["id"],
        meta={"filename": original_name, "mime": primary["mime"],
              "bytes": primary["bytes"], "variants": len(variants)},
        ip=db.to_inet(client_ip(request)),
    )
    return {"media": _decorate(row), "savedBytes": max(0, len(data) - primary["bytes"])}


def _parse_tags(raw: str | list[str] | None) -> list[str]:
    if not raw:
        return []
    parts = raw.split(",") if isinstance(raw, str) else raw
    cleaned = [collapse(str(p), 40) for p in parts]
    seen: dict[str, None] = {}
    for tag in cleaned:
        if tag:
            seen.setdefault(slugify(tag, "tag"), None)
    return list(seen)[:MAX_TAGS]


# ================================================================ detail
@router.get("/{media_id}")
async def media_detail(media_id: int, scoped: db.TenantDB = Depends(tenant_db)) -> dict:
    row = await _get(scoped, media_id, include_deleted=True)
    row["usage"] = await _usage(scoped, media_id)
    return {"media": _decorate(row)}


async def _usage(scoped: db.TenantDB, media_id: int) -> list[dict]:
    """Where this file is referenced, resolved to something clickable."""
    rows = await scoped.fetch(
        """SELECT u.object_type, u.object_id, u.field,
                  coalesce(i.title, b.name, c.name) AS label,
                  t.slug::text AS type_slug
             FROM media_usage u
             LEFT JOIN content_items i
                    ON u.object_type = 'content_item' AND i.id = u.object_id
             LEFT JOIN content_types t ON t.id = i.type_id
             LEFT JOIN reusable_blocks b
                    ON u.object_type = 'block' AND b.id = u.object_id
             LEFT JOIN campaigns c
                    ON u.object_type = 'campaign' AND c.id = u.object_id
            WHERE u.tenant_id = $1 AND u.media_id = $2
            ORDER BY u.object_type, u.object_id""",
        media_id,
    )
    return [row for row in rows if row["label"]]


@router.patch("/{media_id}")
async def update_media(
    media_id: int,
    payload: MediaUpdate,
    user: CurrentUser = Depends(require_perm("media.edit")),
) -> dict:
    scoped = db.TenantDB(user.tenant_id)
    await _get(scoped, media_id)

    sent = payload.model_dump(exclude_unset=True)
    if not sent:
        raise HTTPException(400, "Nothing to save.")

    if payload.folder_id:
        folder = await scoped.fetch_one(
            "SELECT id FROM media_folders WHERE tenant_id = $1 AND id = $2", payload.folder_id
        )
        if not folder:
            raise HTTPException(400, "That folder no longer exists.")

    row = await scoped.fetch_one(
        f"""UPDATE media
               SET alt_text  = CASE WHEN $3 THEN $4 ELSE alt_text END,
                   title     = CASE WHEN $5 THEN $6 ELSE title END,
                   caption   = CASE WHEN $7 THEN $8 ELSE caption END,
                   folder_id = CASE WHEN $9 THEN $10 ELSE folder_id END,
                   tags      = CASE WHEN $11 THEN $12::text[] ELSE tags END
             WHERE tenant_id = $1 AND id = $2
             RETURNING {MEDIA_COLUMNS.replace("m.", "")}""",
        media_id,
        "alt_text" in sent, collapse(payload.alt_text, 300),
        "title" in sent, collapse(payload.title, 200),
        "caption" in sent, keep_lines(payload.caption, 600),
        "folder_id" in sent, payload.folder_id,
        "tags" in sent, _parse_tags(payload.tags),
    )
    return {"media": _decorate(row)}


# ================================================================ delete
@router.delete("/{media_id}")
async def trash_media(
    media_id: int,
    request: Request,
    force: bool = Query(default=False),
    user: CurrentUser = Depends(require_perm("media.delete")),
) -> dict:
    """Soft delete. A file still used by content needs ?force=true."""
    scoped = db.TenantDB(user.tenant_id)
    row = await _get(scoped, media_id)

    usage = await _usage(scoped, media_id)
    if usage and not force:
        where = ", ".join(f"“{u['label']}”" for u in usage[:5])
        raise HTTPException(
            409,
            f"That file is still used by {len(usage)} item(s): {where}. "
            "Remove it there first, or delete anyway to leave broken references.",
        )

    await scoped.execute(
        "UPDATE media SET deleted_at = now() WHERE tenant_id = $1 AND id = $2", media_id
    )
    await events.log_activity(
        user.tenant_id, "media.trashed", user_id=user.id,
        object_type="media", object_id=media_id,
        meta={"filename": row["original_filename"], "forced": bool(usage)},
        ip=db.to_inet(client_ip(request)),
    )
    return {"ok": True, "usage": len(usage)}


@router.post("/{media_id}/restore")
async def restore_media(
    media_id: int, user: CurrentUser = Depends(require_perm("media.delete"))
) -> dict:
    scoped = db.TenantDB(user.tenant_id)
    row = await scoped.fetch_one(
        "UPDATE media SET deleted_at = NULL WHERE tenant_id = $1 AND id = $2 RETURNING id",
        media_id,
    )
    if not row:
        raise HTTPException(404, "That file no longer exists.")
    return {"ok": True}


@router.delete("/{media_id}/purge")
async def purge_media(
    media_id: int,
    request: Request,
    confirm: bool = Query(default=False),
    user: CurrentUser = Depends(require_perm("media.delete")),
) -> dict:
    """Permanent: removes every stored object as well as the row."""
    scoped = db.TenantDB(user.tenant_id)
    row = await _get(scoped, media_id, include_deleted=True)
    if row["deleted_at"] is None:
        raise HTTPException(400, "Move the file to the trash first.")
    if not confirm:
        raise HTTPException(400, "Permanent deletion needs confirm=true.")

    keys = [row["storage_key"]] + [
        variant["key"] for variant in (row["variants"] or []) if variant.get("key")
    ]
    # Row first: an orphaned object costs pennies, a row pointing at a
    # deleted object breaks every page that renders it.
    await scoped.execute("DELETE FROM media WHERE tenant_id = $1 AND id = $2", media_id)
    storage.delete_many(keys)

    await events.log_activity(
        user.tenant_id, "media.purged", user_id=user.id,
        object_type="media", object_id=media_id,
        meta={"filename": row["original_filename"], "objects": len(keys)},
        ip=db.to_inet(client_ip(request)),
    )
    return {"ok": True, "objectsDeleted": len(keys)}


@router.post("/bulk")
async def bulk_media(
    payload: MediaBulkRequest,
    user: CurrentUser = Depends(require_perm("media.edit")),
) -> dict:
    scoped = db.TenantDB(user.tenant_id)
    ids = sorted(set(payload.ids))[:200]

    if payload.action is MediaBulkAction.move:
        if payload.folder_id:
            folder = await scoped.fetch_one(
                "SELECT id FROM media_folders WHERE tenant_id = $1 AND id = $2",
                payload.folder_id,
            )
            if not folder:
                raise HTTPException(400, "That folder no longer exists.")
        rows = await scoped.fetch(
            "UPDATE media SET folder_id = $3 WHERE tenant_id = $1 AND id = ANY($2::bigint[]) RETURNING id",
            ids, payload.folder_id,
        )
    elif payload.action is MediaBulkAction.tag:
        tags = _parse_tags(payload.tags)
        if not tags:
            raise HTTPException(400, "Provide at least one tag.")
        rows = await scoped.fetch(
            """UPDATE media
                  SET tags = (SELECT array_agg(DISTINCT t)
                                FROM unnest(tags || $3::text[]) AS t)
                WHERE tenant_id = $1 AND id = ANY($2::bigint[]) RETURNING id""",
            ids, tags,
        )
    elif payload.action is MediaBulkAction.untag:
        tags = _parse_tags(payload.tags)
        rows = await scoped.fetch(
            """UPDATE media
                  SET tags = coalesce((SELECT array_agg(t) FROM unnest(tags) AS t
                                        WHERE NOT (t = ANY($3::text[]))), '{}')
                WHERE tenant_id = $1 AND id = ANY($2::bigint[]) RETURNING id""",
            ids, tags,
        )
    elif payload.action is MediaBulkAction.trash:
        rows = await scoped.fetch(
            "UPDATE media SET deleted_at = now() WHERE tenant_id = $1 AND id = ANY($2::bigint[]) RETURNING id",
            ids,
        )
    else:  # restore
        rows = await scoped.fetch(
            "UPDATE media SET deleted_at = NULL WHERE tenant_id = $1 AND id = ANY($2::bigint[]) RETURNING id",
            ids,
        )

    await events.log_activity(
        user.tenant_id, f"media.bulk_{payload.action.value}", user_id=user.id,
        meta={"count": len(rows)},
    )
    return {"ok": True, "affected": len(rows)}


# =============================================================== folders
@router.get("/folders/list")
async def list_folders(scoped: db.TenantDB = Depends(tenant_db)) -> dict:
    rows = await scoped.fetch(
        """SELECT f.id, f.name, f.slug::text AS slug, f.parent_id,
                  count(m.id) FILTER (WHERE m.deleted_at IS NULL)::int AS file_count
             FROM media_folders f
             LEFT JOIN media m ON m.folder_id = f.id
            WHERE f.tenant_id = $1
            GROUP BY f.id ORDER BY f.name"""
    )
    unfiled = await scoped.fetch_one(
        """SELECT count(*)::int AS n FROM media
            WHERE tenant_id = $1 AND folder_id IS NULL AND deleted_at IS NULL"""
    )
    return {"folders": rows, "unfiled": unfiled["n"]}


@router.post("/folders", status_code=201)
async def create_folder(
    payload: FolderCreate, user: CurrentUser = Depends(require_perm("media.edit"))
) -> dict:
    scoped = db.TenantDB(user.tenant_id)
    if payload.parent_id:
        parent = await scoped.fetch_one(
            "SELECT id FROM media_folders WHERE tenant_id = $1 AND id = $2", payload.parent_id
        )
        if not parent:
            raise HTTPException(400, "That parent folder no longer exists.")

    slug = slugify(payload.name, "folder")
    existing = await scoped.fetch_one(
        """SELECT id FROM media_folders
            WHERE tenant_id = $1 AND coalesce(parent_id, 0) = coalesce($2::bigint, 0)
              AND slug = $3""",
        payload.parent_id, slug,
    )
    if existing:
        raise HTTPException(400, "A folder with that name already exists here.")

    row = await scoped.fetch_one(
        """INSERT INTO media_folders (tenant_id, parent_id, name, slug)
           VALUES ($1, $2, $3, $4)
           RETURNING id, name, slug::text AS slug, parent_id""",
        payload.parent_id, collapse(payload.name, 80), slug,
    )
    return {"folder": {**row, "file_count": 0}}


@router.patch("/folders/{folder_id}")
async def update_folder(
    folder_id: int,
    payload: FolderUpdate,
    user: CurrentUser = Depends(require_perm("media.edit")),
) -> dict:
    scoped = db.TenantDB(user.tenant_id)
    if payload.parent_id == folder_id:
        raise HTTPException(400, "A folder cannot be inside itself.")

    row = await scoped.fetch_one(
        """UPDATE media_folders
              SET name = coalesce($3, name),
                  slug = coalesce($4, slug),
                  parent_id = CASE WHEN $5 THEN $6 ELSE parent_id END
            WHERE tenant_id = $1 AND id = $2
            RETURNING id, name, slug::text AS slug, parent_id""",
        folder_id,
        collapse(payload.name, 80),
        slugify(payload.name, "folder") if payload.name else None,
        "parent_id" in payload.model_dump(exclude_unset=True),
        payload.parent_id,
    )
    if not row:
        raise HTTPException(404, "That folder no longer exists.")
    return {"folder": row}


@router.delete("/folders/{folder_id}")
async def delete_folder(
    folder_id: int, user: CurrentUser = Depends(require_perm("media.edit"))
) -> dict:
    """Files are unfiled, never deleted with the folder."""
    scoped = db.TenantDB(user.tenant_id)
    moved = await scoped.fetch(
        "UPDATE media SET folder_id = NULL WHERE tenant_id = $1 AND folder_id = $2 RETURNING id",
        folder_id,
    )
    removed = await scoped.fetch(
        "DELETE FROM media_folders WHERE tenant_id = $1 AND id = $2 RETURNING id", folder_id
    )
    if not removed:
        raise HTTPException(404, "That folder no longer exists.")
    return {"ok": True, "filesUnfiled": len(moved)}


# ======================================================== local file serving
@public_router.get("/media/{key:path}", include_in_schema=False)
async def serve_media(key: str) -> Response:
    """Serve a locally stored file.

    Only used when MEDIA_STORAGE=local. With S3 + CloudFront the browser
    never reaches this app for media, which is the point.
    """
    from ..config import settings  # noqa: PLC0415

    if settings.media_storage != "local":
        raise HTTPException(404, "Not found.")

    row = await db.fetch_one(
        """SELECT mime_type FROM media WHERE storage_key = $1 AND deleted_at IS NULL
            UNION ALL
           SELECT (v->>'mime') FROM media, jsonb_array_elements(variants) AS v
            WHERE v->>'key' = $1 AND deleted_at IS NULL
            LIMIT 1""",
        key,
    )
    if not row:
        # Serving only keys the database knows about means a stray file
        # dropped into MEDIA_ROOT is not web-reachable.
        raise HTTPException(404, "Not found.")

    try:
        data = storage.get(key)
    except storage.StorageError:
        raise HTTPException(404, "Not found.") from None

    return Response(
        data,
        media_type=row["mime_type"] or "application/octet-stream",
        headers={
            # The key contains a content hash, so the bytes never change.
            "cache-control": "public, max-age=31536000, immutable",
            "x-content-type-options": "nosniff",
        },
    )
