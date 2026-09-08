"""Content management (2.1) — schema-driven types, items and taxonomies.

Two content systems coexist deliberately:

  * ``pages`` (app/routers/pages.py) is the visual block builder that
    renders its own HTML at /p/{tenant}/{slug};
  * ``content_items`` — here — is the schema-driven, API-first model a
    static frontend reads at build time. Rich text is stored as
    sanitized HTML, everything else as typed JSON against the content
    type's own ``field_schema``.

Editing writes the live columns; the public API serves only
``published_snapshot``, so an in-progress draft can never leak. Every
save snapshots a revision, and a slug change on published content
leaves a 301 behind unless the editor opts out.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from .. import content as C
from .. import db, events, publishing
from ..permissions import permissions_for, require_perm
from ..sanitize import clean_html, excerpt as html_excerpt
from ..schemas import (
    ContentBulkAction,
    ContentBulkRequest,
    ContentCreate,
    ContentStatus,
    ContentTypeCreate,
    ContentTypeUpdate,
    ContentUpdate,
    PublishRequest,
    RevisionRestore,
    TaxonomyCreate,
    TaxonomyUpdate,
    TermCreate,
    TermUpdate,
    collapse,
    keep_lines,
)
from ..security import CurrentUser, client_ip, require_user, sha256, tenant_db

router = APIRouter(prefix="/api/content", tags=["content"])

ITEM_COLUMNS = """i.id, i.slug::text AS slug, i.title, i.excerpt, i.body, i.fields, i.seo,
                  i.status::text AS status, i.author_id, i.featured_media_id, i.menu_order,
                  i.scheduled_for, i.published_at, i.trashed_at,
                  i.created_at, i.updated_at, i.type_id"""

LIST_SORTS = {
    "updated": "i.updated_at DESC",
    "created": "i.created_at DESC",
    "published": "i.published_at DESC NULLS LAST",
    "title": "i.title ASC",
    "order": "i.menu_order ASC, i.title ASC",
}

PREVIEW_TOKEN_HOURS = 48


# ================================================================ helpers
async def _type_by_slug(scoped: db.TenantDB, slug: str) -> dict:
    row = await scoped.fetch_one(
        """SELECT id, slug::text AS slug, name, plural_name, kind, route_prefix,
                  field_schema, supports, is_builtin, is_active
             FROM content_types WHERE tenant_id = $1 AND slug = $2""",
        C.check_slug(slug),
    )
    if not row:
        raise HTTPException(404, f"There is no “{slug}” content type.")
    return row


async def _type_by_id(scoped: db.TenantDB, type_id: int) -> dict:
    row = await scoped.fetch_one(
        """SELECT id, slug::text AS slug, name, plural_name, kind, route_prefix,
                  field_schema, supports, is_builtin, is_active
             FROM content_types WHERE tenant_id = $1 AND id = $2""",
        type_id,
    )
    if not row:
        raise HTTPException(404, "That content type no longer exists.")
    return row


async def _item(scoped: db.TenantDB, item_id: int) -> dict:
    row = await scoped.fetch_one(
        f"SELECT {ITEM_COLUMNS} FROM content_items i WHERE i.tenant_id = $1 AND i.id = $2",
        item_id,
    )
    if not row:
        raise HTTPException(404, "That content no longer exists.")
    return row


async def _terms_for(scoped: db.TenantDB, item_id: int) -> list[dict]:
    return await scoped.fetch(
        """SELECT tm.id, tm.name, tm.slug::text AS slug, tx.slug::text AS taxonomy
             FROM content_terms ct
             JOIN terms tm ON tm.id = ct.term_id
             JOIN taxonomies tx ON tx.id = tm.taxonomy_id
            WHERE ct.tenant_id = $1 AND ct.item_id = $2
            ORDER BY tx.slug, tm.name""",
        item_id,
    )


async def _assert_can_edit(user: CurrentUser, item: dict) -> None:
    """content.edit_any edits anything; content.edit_own only own drafts.

    An author who could edit a colleague's published page would make the
    author/editor split meaningless, so ownership is checked here rather
    than left to the UI hiding a button.
    """
    granted = await permissions_for(user.tenant_id, user.role)
    if "content.edit_any" in granted:
        return
    if "content.edit_own" not in granted:
        raise HTTPException(403, "Your role cannot edit content.")
    if item.get("author_id") != user.id and item.get("created_by") != user.id:
        raise HTTPException(403, "You can only edit content you authored.")


async def _sync_terms(
    scoped: db.TenantDB, item_id: int, type_id: int, term_ids: list[int]
) -> None:
    """Replace an item's terms, keeping only terms whose taxonomy applies
    to this content type."""
    await scoped.execute(
        "DELETE FROM content_terms WHERE tenant_id = $1 AND item_id = $2", item_id
    )
    if not term_ids:
        return
    # Terms whose taxonomy is not attached to this type are dropped
    # silently: the picker may offer everything, and a 400 here would be
    # a worse experience than quietly ignoring an inapplicable tag.
    await scoped.execute(
        """INSERT INTO content_terms (tenant_id, item_id, term_id)
           SELECT $1, $2, tm.id FROM terms tm
             JOIN taxonomy_types tt ON tt.taxonomy_id = tm.taxonomy_id AND tt.type_id = $4
            WHERE tm.tenant_id = $1 AND tm.id = ANY($3::bigint[])
           ON CONFLICT DO NOTHING""",
        item_id, sorted(set(term_ids))[:60], type_id,
    )


async def _maybe_redirect(
    scoped: db.TenantDB,
    *,
    type_row: dict,
    old_slug: str,
    new_slug: str,
    was_published: bool,
    user_id: int,
) -> str | None:
    """Leave a 301 behind when a published item's URL changes."""
    if old_slug == new_slug or not was_published:
        return None
    old_path = C.public_path(type_row.get("route_prefix"), old_slug)
    new_path = C.public_path(type_row.get("route_prefix"), new_slug)
    if not old_path or not new_path:
        return None

    await scoped.execute(
        """INSERT INTO redirects (tenant_id, from_path, to_path, status_code,
                                  is_automatic, note, created_by)
           VALUES ($1, $2, $3, 301, TRUE, $4, $5)
           ON CONFLICT (tenant_id, from_path)
           DO UPDATE SET to_path = EXCLUDED.to_path, is_active = TRUE,
                         is_automatic = TRUE, note = EXCLUDED.note""",
        old_path, new_path, f"Slug changed on “{type_row['name']}”", user_id,
    )
    # A chain (a → b, then b → c) makes crawlers follow two hops, so
    # point anything that aimed at the old path straight at the new one.
    await scoped.execute(
        """UPDATE redirects SET to_path = $3
            WHERE tenant_id = $1 AND to_path = $2 AND from_path <> $3""",
        old_path, new_path,
    )
    return old_path


# ========================================================== content types
@router.get("/types")
async def list_types(scoped: db.TenantDB = Depends(tenant_db)) -> dict:
    rows = await scoped.fetch(
        """SELECT t.id, t.slug::text AS slug, t.name, t.plural_name, t.description,
                  t.kind, t.route_prefix, t.field_schema, t.supports, t.icon,
                  t.sort_order, t.is_builtin, t.is_active,
                  count(i.id) FILTER (WHERE i.status <> 'trashed')::int AS item_count,
                  count(i.id) FILTER (WHERE i.status = 'draft')::int AS draft_count,
                  count(i.id) FILTER (WHERE i.status = 'published')::int AS published_count,
                  count(i.id) FILTER (WHERE i.status = 'scheduled')::int AS scheduled_count,
                  count(i.id) FILTER (WHERE i.status = 'trashed')::int AS trashed_count
             FROM content_types t
             LEFT JOIN content_items i ON i.type_id = t.id
            WHERE t.tenant_id = $1
            GROUP BY t.id
            ORDER BY t.sort_order, t.name"""
    )
    return {"types": rows, "fieldTypes": sorted(C.FIELD_TYPES)}


@router.post("/types", status_code=201)
async def create_type(
    payload: ContentTypeCreate,
    request: Request,
    user: CurrentUser = Depends(require_perm("types.manage")),
) -> dict:
    scoped = db.TenantDB(user.tenant_id)
    slug = C.check_slug(payload.slug)

    if await scoped.fetch_one(
        "SELECT 1 FROM content_types WHERE tenant_id = $1 AND slug = $2", slug
    ):
        raise HTTPException(400, "A content type already uses that slug.")

    row = await scoped.fetch_one(
        """INSERT INTO content_types (tenant_id, slug, name, plural_name, description,
                                      kind, route_prefix, field_schema, supports, icon)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9::jsonb, $10)
           RETURNING id, slug::text AS slug, name, plural_name, kind, route_prefix,
                     field_schema, supports, icon, sort_order, is_builtin, is_active""",
        slug,
        payload.name,
        payload.plural_name or f"{payload.name}s",
        collapse(payload.description, 300),
        payload.kind.value,
        _route_prefix(payload.route_prefix),
        C.clean_field_schema(payload.field_schema),
        C.clean_supports(payload.supports),
        collapse(payload.icon, 40),
    )

    # Attach the taxonomies the type says it supports.
    await _attach_taxonomies(scoped, row["id"], (row["supports"] or {}).get("taxonomies") or [])

    await events.log_activity(
        user.tenant_id, "content_type.created", user_id=user.id,
        object_type="content_type", object_id=row["id"], meta={"slug": slug},
        ip=db.to_inet(client_ip(request)),
    )
    return {"type": {**row, "item_count": 0}}


def _route_prefix(raw: str | None) -> str | None:
    """Normalise to '/blog' form, or None for a non-addressable type."""
    if raw is None:
        return None
    cleaned = collapse(raw, 80)
    if not cleaned:
        return None
    return "/" + cleaned.strip("/").lower() if cleaned.strip("/") else "/"


async def _attach_taxonomies(scoped: db.TenantDB, type_id: int, slugs: list[str]) -> None:
    if not slugs:
        return
    await scoped.execute(
        """INSERT INTO taxonomy_types (tenant_id, taxonomy_id, type_id)
           SELECT $1, tx.id, $2 FROM taxonomies tx
            WHERE tx.tenant_id = $1 AND tx.slug = ANY($3::citext[])
           ON CONFLICT DO NOTHING""",
        type_id, slugs,
    )


@router.patch("/types/{type_id}")
async def update_type(
    type_id: int,
    payload: ContentTypeUpdate,
    request: Request,
    user: CurrentUser = Depends(require_perm("types.manage")),
) -> dict:
    scoped = db.TenantDB(user.tenant_id)
    existing = await _type_by_id(scoped, type_id)

    sent = payload.model_dump(exclude_unset=True)
    if not sent:
        raise HTTPException(400, "Nothing to save.")

    args: list = [user.tenant_id, type_id]
    assignments: list[str] = []

    def assign(column: str, value, cast: str = "") -> None:
        args.append(value)
        assignments.append(f"{column} = ${len(args)}{cast}")

    if "name" in sent:
        assign("name", payload.name)
    if "plural_name" in sent:
        assign("plural_name", payload.plural_name or payload.name or existing["name"])
    if "description" in sent:
        assign("description", collapse(payload.description, 300))
    if "route_prefix" in sent:
        assign("route_prefix", _route_prefix(payload.route_prefix))
    if "field_schema" in sent:
        assign("field_schema", C.clean_field_schema(payload.field_schema), "::jsonb")
    if "supports" in sent:
        assign("supports", C.clean_supports(payload.supports), "::jsonb")
    if "icon" in sent:
        assign("icon", collapse(payload.icon, 40))
    if "sort_order" in sent:
        assign("sort_order", payload.sort_order or 0)
    if "is_active" in sent:
        assign("is_active", bool(payload.is_active))

    row = await db.fetch_one(
        f"""UPDATE content_types SET {", ".join(assignments)}
             WHERE tenant_id = $1 AND id = $2
             RETURNING id, slug::text AS slug, name, plural_name, kind, route_prefix,
                       field_schema, supports, icon, sort_order, is_builtin, is_active""",
        *args,
    )
    if "supports" in sent:
        await _attach_taxonomies(scoped, type_id, (row["supports"] or {}).get("taxonomies") or [])

    await events.log_activity(
        user.tenant_id, "content_type.updated", user_id=user.id,
        object_type="content_type", object_id=type_id, meta={"fields": list(sent)},
        ip=db.to_inet(client_ip(request)),
    )
    return {"type": row}


@router.delete("/types/{type_id}")
async def delete_type(
    type_id: int,
    request: Request,
    user: CurrentUser = Depends(require_perm("types.manage")),
) -> dict:
    scoped = db.TenantDB(user.tenant_id)
    existing = await _type_by_id(scoped, type_id)
    if existing["is_builtin"]:
        raise HTTPException(400, "Built-in content types cannot be deleted. Deactivate it instead.")

    count = await scoped.fetch_one(
        "SELECT count(*)::int AS n FROM content_items WHERE tenant_id = $1 AND type_id = $2",
        type_id,
    )
    if count["n"]:
        raise HTTPException(
            400,
            f"That type still has {count['n']} item(s). Delete them first, "
            "or deactivate the type to hide it.",
        )

    await scoped.execute(
        "DELETE FROM content_types WHERE tenant_id = $1 AND id = $2", type_id
    )
    await events.log_activity(
        user.tenant_id, "content_type.deleted", user_id=user.id,
        object_type="content_type", object_id=type_id,
        meta={"slug": existing["slug"]}, ip=db.to_inet(client_ip(request)),
    )
    return {"ok": True}


# ========================================================== content items
@router.get("/items")
async def list_items(
    user: CurrentUser = Depends(require_perm("content.view")),
    type: str | None = Query(default=None, max_length=60),
    status: ContentStatus | None = None,
    q: str | None = Query(default=None, max_length=120),
    term_id: int | None = None,
    author_id: int | None = None,
    mine: bool = False,
    sort: str = Query(default="updated"),
    page: int = Query(default=1, ge=1, le=500),
    per_page: int = Query(default=25, ge=5, le=200),
) -> dict:
    scoped = db.TenantDB(user.tenant_id)
    where = ["i.tenant_id = $1"]
    args: list = []

    def add(clause: str, value) -> None:
        args.append(value)
        where.append(clause.replace("?", f"${len(args) + 1}"))

    if type:
        type_row = await _type_by_slug(scoped, type)
        add("i.type_id = ?", type_row["id"])
    if status:
        add("i.status = ?::content_status", status.value)
    else:
        # Trash is a separate view; it should not dilute the default list.
        where.append("i.status <> 'trashed'")
    if q:
        add(
            "to_tsvector('simple', coalesce(i.title,'') || ' ' || coalesce(i.excerpt,'')"
            " || ' ' || coalesce(i.body,'')) @@ plainto_tsquery('simple', ?)",
            q.strip(),
        )
    if term_id:
        add(
            "EXISTS (SELECT 1 FROM content_terms ct"
            " WHERE ct.item_id = i.id AND ct.term_id = ?)",
            term_id,
        )
    if mine:
        # Both ? become the same placeholder: one argument, two uses.
        add("(i.author_id = ? OR i.created_by = ?)", user.id)
    elif author_id:
        add("i.author_id = ?", author_id)

    clause = " AND ".join(where)
    order = LIST_SORTS.get(sort, LIST_SORTS["updated"])
    offset = (page - 1) * per_page

    rows = await scoped.fetch(
        f"""SELECT i.id, i.slug::text AS slug, i.title, i.excerpt, i.status::text AS status,
                   i.scheduled_for, i.published_at, i.updated_at, i.menu_order,
                   i.featured_media_id, i.seo,
                   t.slug::text AS type_slug, t.name AS type_name, t.route_prefix,
                   au.display_name AS author_name,
                   ed.display_name AS updated_by_name
              FROM content_items i
              JOIN content_types t ON t.id = i.type_id
              LEFT JOIN users au ON au.id = i.author_id
              LEFT JOIN users ed ON ed.id = i.updated_by
             WHERE {clause}
             ORDER BY {order}
             LIMIT {per_page} OFFSET {offset}""",
        *args,
    )
    total = await scoped.fetch_one(
        f"SELECT count(*)::int AS n FROM content_items i WHERE {clause}", *args
    )

    for row in rows:
        row["path"] = C.public_path(row.pop("route_prefix"), row["slug"])
        row["noindex"] = bool((row.get("seo") or {}).get("noindex"))
        row.pop("seo", None)

    return {
        "items": rows,
        "page": page,
        "perPage": per_page,
        "total": total["n"],
        "pages": max(1, -(-total["n"] // per_page)),
    }


@router.get("/items/counts")
async def item_counts(user: CurrentUser = Depends(require_perm("content.view"))) -> dict:
    """Status tallies per type, for the sidebar and the status tabs."""
    scoped = db.TenantDB(user.tenant_id)
    rows = await scoped.fetch(
        """SELECT t.slug::text AS type_slug, i.status::text AS status, count(*)::int AS n
             FROM content_items i JOIN content_types t ON t.id = i.type_id
            WHERE i.tenant_id = $1 GROUP BY 1, 2"""
    )
    by_type: dict[str, dict[str, int]] = {}
    totals: dict[str, int] = {}
    for row in rows:
        by_type.setdefault(row["type_slug"], {})[row["status"]] = row["n"]
        totals[row["status"]] = totals.get(row["status"], 0) + row["n"]
    return {"byType": by_type, "totals": totals}


@router.post("/items", status_code=201)
async def create_item(
    payload: ContentCreate,
    request: Request,
    user: CurrentUser = Depends(require_perm("content.create")),
) -> dict:
    scoped = db.TenantDB(user.tenant_id)
    type_row = await _type_by_slug(scoped, payload.type)
    if not type_row["is_active"]:
        raise HTTPException(400, f"The “{type_row['name']}” type is deactivated.")

    if type_row["kind"] == "single":
        existing = await scoped.fetch_one(
            "SELECT id FROM content_items WHERE tenant_id = $1 AND type_id = $2",
            type_row["id"],
        )
        if existing:
            raise HTTPException(
                400, f"“{type_row['name']}” is a single-item type. Edit the existing one."
            )

    granted = await permissions_for(user.tenant_id, user.role)
    status = payload.status
    if status is not ContentStatus.draft and "content.publish" not in granted:
        raise HTTPException(403, "Your role can create drafts but cannot publish.")

    slug = await C.unique_slug(
        scoped, type_row["id"], payload.slug or C.slugify(payload.title)
    )
    body = _clean_body(payload.body, type_row)
    fields = C.coerce_fields(type_row["field_schema"] or [], payload.fields)
    seo = C.clean_seo(payload.seo)

    scheduled_for, status = _resolve_schedule(status, payload.scheduled_for)

    row = await scoped.fetch_one(
        f"""INSERT INTO content_items
                (tenant_id, type_id, slug, title, excerpt, body, fields, seo, status,
                 author_id, featured_media_id, menu_order, scheduled_for, published_at,
                 created_by, updated_by)
            VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8::jsonb, $9::content_status,
                    $10, $11, $12, $13,
                    CASE WHEN $9 = 'published' THEN now() END, $14, $14)
            RETURNING {ITEM_COLUMNS.replace("i.", "")}""",
        type_row["id"],
        slug,
        collapse(payload.title, 200),
        collapse(payload.excerpt, 600) or html_excerpt(body),
        body,
        fields,
        seo,
        status.value,
        payload.author_id or user.id,
        payload.featured_media_id,
        payload.menu_order or 0,
        scheduled_for,
        user.id,
    )

    await _sync_terms(scoped, row["id"], type_row["id"], payload.term_ids or [])
    if (type_row["supports"] or {}).get("revisions", True):
        await C.save_revision(scoped, row, reason="create", user_id=user.id)
    await C.sync_media_usage(scoped, row)

    if status is ContentStatus.published:
        await _finalise_publish(scoped, row, type_row, user)

    await events.log_activity(
        user.tenant_id, "content.created", user_id=user.id,
        object_type="content_item", object_id=row["id"],
        meta={"type": type_row["slug"], "slug": slug, "status": status.value},
        ip=db.to_inet(client_ip(request)),
    )
    return {"item": await _detail(scoped, row["id"], user)}


def _clean_body(raw: str | None, type_row: dict) -> str | None:
    if raw is None:
        return None
    if not (type_row["supports"] or {}).get("body", True):
        return None
    try:
        return clean_html(raw)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


def _resolve_schedule(
    status: ContentStatus, scheduled_for: datetime | None
) -> tuple[datetime | None, ContentStatus]:
    """A future date means 'scheduled'; a past one means publish now."""
    if scheduled_for is None:
        return None, status
    when = scheduled_for if scheduled_for.tzinfo else scheduled_for.replace(tzinfo=timezone.utc)
    if when <= datetime.now(timezone.utc):
        return None, ContentStatus.published
    return when, ContentStatus.scheduled


@router.get("/items/{item_id}")
async def item_detail(
    item_id: int, user: CurrentUser = Depends(require_perm("content.view"))
) -> dict:
    scoped = db.TenantDB(user.tenant_id)
    await _item(scoped, item_id)
    return {"item": await _detail(scoped, item_id, user)}


async def _detail(scoped: db.TenantDB, item_id: int, user: CurrentUser) -> dict:
    row = await scoped.fetch_one(
        f"""SELECT {ITEM_COLUMNS}, i.created_by,
                   t.slug::text AS type_slug, t.name AS type_name, t.plural_name,
                   t.route_prefix, t.field_schema, t.supports,
                   au.display_name AS author_name,
                   ed.display_name AS updated_by_name
              FROM content_items i
              JOIN content_types t ON t.id = i.type_id
              LEFT JOIN users au ON au.id = i.author_id
              LEFT JOIN users ed ON ed.id = i.updated_by
             WHERE i.tenant_id = $1 AND i.id = $2""",
        item_id,
    )
    if not row:
        raise HTTPException(404, "That content no longer exists.")

    row["path"] = C.public_path(row["route_prefix"], row["slug"])
    row["terms"] = await _terms_for(scoped, item_id)
    row["seoReport"] = C.seo_report(row)
    revisions = await scoped.fetch_one(
        "SELECT count(*)::int AS n FROM content_revisions WHERE tenant_id = $1 AND item_id = $2",
        item_id,
    )
    row["revisionCount"] = revisions["n"]
    granted = await permissions_for(user.tenant_id, user.role)
    owns = row.get("author_id") == user.id or row.get("created_by") == user.id
    row["can"] = {
        "edit": "content.edit_any" in granted or ("content.edit_own" in granted and owns),
        "publish": "content.publish" in granted,
        "trash": "content.trash" in granted,
        "purge": "content.purge" in granted,
    }
    return row


@router.patch("/items/{item_id}")
async def update_item(
    item_id: int,
    payload: ContentUpdate,
    request: Request,
    user: CurrentUser = Depends(require_user),
) -> dict:
    scoped = db.TenantDB(user.tenant_id)
    item = await scoped.fetch_one(
        f"SELECT {ITEM_COLUMNS}, i.created_by FROM content_items i"
        " WHERE i.tenant_id = $1 AND i.id = $2",
        item_id,
    )
    if not item:
        raise HTTPException(404, "That content no longer exists.")
    await _assert_can_edit(user, item)

    type_row = await _type_by_id(scoped, item["type_id"])
    sent = payload.model_dump(exclude_unset=True)
    sent.pop("skip_redirect", None)
    if not sent:
        raise HTTPException(400, "Nothing to save.")

    # Revision before the write, so the pre-edit state is recoverable.
    if (type_row["supports"] or {}).get("revisions", True):
        await C.save_revision(scoped, item, reason="save", user_id=user.id)

    args: list = [user.tenant_id, item_id]
    assignments: list[str] = []

    def assign(column: str, value, cast: str = "") -> None:
        args.append(value)
        assignments.append(f"{column} = ${len(args)}{cast}")

    redirect_from = None
    if "slug" in sent and payload.slug:
        new_slug = await C.unique_slug(scoped, type_row["id"], payload.slug, exclude_id=item_id)
        if new_slug != item["slug"] and not payload.skip_redirect:
            redirect_from = await _maybe_redirect(
                scoped,
                type_row=type_row,
                old_slug=item["slug"],
                new_slug=new_slug,
                was_published=item["status"] == "published",
                user_id=user.id,
            )
        assign("slug", new_slug)

    if "title" in sent:
        assign("title", collapse(payload.title, 200))
    if "body" in sent:
        assign("body", _clean_body(payload.body, type_row))
    if "excerpt" in sent:
        assign(
            "excerpt",
            collapse(payload.excerpt, 600)
            or html_excerpt(_clean_body(payload.body, type_row) if "body" in sent else item["body"]),
        )
    if "fields" in sent:
        assign("fields", C.coerce_fields(type_row["field_schema"] or [], payload.fields), "::jsonb")
    if "seo" in sent:
        assign("seo", C.clean_seo(payload.seo), "::jsonb")
    if "author_id" in sent:
        assign("author_id", payload.author_id)
    if "featured_media_id" in sent:
        assign("featured_media_id", payload.featured_media_id)
    if "menu_order" in sent:
        assign("menu_order", payload.menu_order or 0)
    assign("updated_by", user.id)

    updated = await db.fetch_one(
        f"""UPDATE content_items SET {", ".join(assignments)}
             WHERE tenant_id = $1 AND id = $2 RETURNING {ITEM_COLUMNS.replace("i.", "")}""",
        *args,
    )

    if "term_ids" in sent:
        await _sync_terms(scoped, item_id, type_row["id"], payload.term_ids or [])
    await C.sync_media_usage(scoped, updated)

    # A published item keeps serving its old snapshot until re-published,
    # which is the whole point of the draft/published split.
    await events.log_activity(
        user.tenant_id, "content.updated", user_id=user.id,
        object_type="content_item", object_id=item_id,
        meta={"fields": list(sent), "redirect_from": redirect_from},
        ip=db.to_inet(client_ip(request)),
    )
    response: dict = {"item": await _detail(scoped, item_id, user)}
    if redirect_from:
        response["redirectCreated"] = redirect_from
    return response


# ============================================================= lifecycle
async def _finalise_publish(
    scoped: db.TenantDB, item: dict, type_row: dict, user: CurrentUser | None
) -> dict:
    """Freeze the snapshot the public API serves, then fan out.

    Snapshot first: if the build trigger or sitemap step fails, the item
    is still correctly published and the queue retries the rest.
    """
    terms = await _terms_for(scoped, item["id"])
    snapshot = C.build_snapshot(item, type_row, terms)
    await scoped.execute(
        "UPDATE content_items SET published_snapshot = $3::jsonb WHERE tenant_id = $1 AND id = $2",
        item["id"], snapshot,
    )

    path = C.public_path(type_row.get("route_prefix"), str(item["slug"]))
    # Publishing to a path a redirect still points away from would send
    # visitors straight back off it.
    if path:
        await scoped.execute(
            "UPDATE redirects SET is_active = FALSE WHERE tenant_id = $1 AND from_path = $2 AND is_automatic",
            path,
        )

    await events.emit(
        item["tenant_id"] if "tenant_id" in item else scoped.tenant_id,
        "content.published",
        {
            "id": item["id"],
            "type": type_row["slug"],
            "slug": str(item["slug"]),
            "title": item["title"],
            "path": path,
            "publishedAt": snapshot.get("publishedAt"),
        },
    )
    await publishing.on_content_published(
        scoped.tenant_id,
        item={"path": path, "slug": str(item["slug"]), "route_prefix": type_row.get("route_prefix")},
        reason="content.published",
    )
    return snapshot


@router.post("/items/{item_id}/publish")
async def publish_item(
    item_id: int,
    payload: PublishRequest,
    request: Request,
    user: CurrentUser = Depends(require_perm("content.publish")),
) -> dict:
    scoped = db.TenantDB(user.tenant_id)
    item = await _item(scoped, item_id)
    type_row = await _type_by_id(scoped, item["type_id"])

    scheduled_for, status = _resolve_schedule(ContentStatus.published, payload.scheduled_for)

    if status is ContentStatus.scheduled and item["status"] == "published":
        # 'scheduled' is not served publicly, so accepting this would
        # pull a live page off the site until the date arrives.
        raise HTTPException(
            400,
            "That page is already live. Publish now to push your edits, "
            "or unpublish it first if you want to schedule a relaunch.",
        )

    if status is ContentStatus.scheduled:
        row = await scoped.fetch_one(
            f"""UPDATE content_items
                   SET status = 'scheduled', scheduled_for = $3, updated_by = $4
                 WHERE tenant_id = $1 AND id = $2
                 RETURNING {ITEM_COLUMNS.replace("i.", "")}""",
            item_id, scheduled_for, user.id,
        )
        await events.log_activity(
            user.tenant_id, "content.scheduled", user_id=user.id,
            object_type="content_item", object_id=item_id,
            meta={"for": scheduled_for.isoformat() if scheduled_for else None},
            ip=db.to_inet(client_ip(request)),
        )
        await events.emit(
            user.tenant_id, "content.scheduled",
            {"id": item_id, "slug": str(row["slug"]),
             "scheduledFor": scheduled_for.isoformat() if scheduled_for else None},
        )
        return {"item": await _detail(scoped, item_id, user), "scheduled": True}

    row = await scoped.fetch_one(
        f"""UPDATE content_items
               SET status = 'published', scheduled_for = NULL, trashed_at = NULL,
                   published_at = coalesce(published_at, now()), updated_by = $3
             WHERE tenant_id = $1 AND id = $2
             RETURNING {ITEM_COLUMNS.replace("i.", "")}""",
        item_id, user.id,
    )
    if (type_row["supports"] or {}).get("revisions", True):
        await C.save_revision(scoped, row, reason=payload.note or "publish", user_id=user.id)
    await _finalise_publish(scoped, row, type_row, user)

    await events.log_activity(
        user.tenant_id, "content.published", user_id=user.id,
        object_type="content_item", object_id=item_id,
        meta={"type": type_row["slug"], "slug": str(row["slug"])},
        ip=db.to_inet(client_ip(request)),
    )
    return {"item": await _detail(scoped, item_id, user)}


@router.post("/items/{item_id}/unpublish")
async def unpublish_item(
    item_id: int,
    request: Request,
    user: CurrentUser = Depends(require_perm("content.publish")),
) -> dict:
    scoped = db.TenantDB(user.tenant_id)
    item = await _item(scoped, item_id)
    type_row = await _type_by_id(scoped, item["type_id"])

    await scoped.execute(
        """UPDATE content_items
              SET status = 'draft', scheduled_for = NULL, published_snapshot = NULL,
                  updated_by = $3
            WHERE tenant_id = $1 AND id = $2""",
        item_id, user.id,
    )
    await events.log_activity(
        user.tenant_id, "content.unpublished", user_id=user.id,
        object_type="content_item", object_id=item_id,
        ip=db.to_inet(client_ip(request)),
    )
    path = C.public_path(type_row.get("route_prefix"), str(item["slug"]))
    await events.emit(
        user.tenant_id, "content.unpublished",
        {"id": item_id, "slug": str(item["slug"]), "path": path},
    )
    # The page is gone from the site, so the CDN copy has to go too.
    await publishing.on_content_published(
        user.tenant_id, paths=[path] if path else None, reason="content.unpublished"
    )
    return {"item": await _detail(scoped, item_id, user)}


@router.post("/items/{item_id}/trash")
async def trash_item(
    item_id: int,
    request: Request,
    user: CurrentUser = Depends(require_perm("content.trash")),
) -> dict:
    scoped = db.TenantDB(user.tenant_id)
    item = await _item(scoped, item_id)
    await _assert_can_edit(user, item)
    type_row = await _type_by_id(scoped, item["type_id"])

    await scoped.execute(
        """UPDATE content_items
              SET status = 'trashed', trashed_at = now(), published_snapshot = NULL,
                  scheduled_for = NULL, updated_by = $3
            WHERE tenant_id = $1 AND id = $2""",
        item_id, user.id,
    )
    await events.log_activity(
        user.tenant_id, "content.trashed", user_id=user.id,
        object_type="content_item", object_id=item_id,
        meta={"slug": str(item["slug"])}, ip=db.to_inet(client_ip(request)),
    )
    if item["status"] == "published":
        path = C.public_path(type_row.get("route_prefix"), str(item["slug"]))
        await publishing.on_content_published(
            user.tenant_id, paths=[path] if path else None, reason="content.unpublished"
        )
    return {"ok": True}


@router.post("/items/{item_id}/restore")
async def restore_item(
    item_id: int,
    request: Request,
    user: CurrentUser = Depends(require_perm("content.trash")),
) -> dict:
    scoped = db.TenantDB(user.tenant_id)
    item = await _item(scoped, item_id)
    if item["status"] != "trashed":
        raise HTTPException(400, "That item is not in the trash.")

    # Restores to draft, never straight back to live: a page removed for
    # a reason should not silently reappear on the website.
    await scoped.execute(
        """UPDATE content_items SET status = 'draft', trashed_at = NULL, updated_by = $3
            WHERE tenant_id = $1 AND id = $2""",
        item_id, user.id,
    )
    await events.log_activity(
        user.tenant_id, "content.restored", user_id=user.id,
        object_type="content_item", object_id=item_id,
        ip=db.to_inet(client_ip(request)),
    )
    return {"item": await _detail(scoped, item_id, user)}


@router.delete("/items/{item_id}")
async def purge_item(
    item_id: int,
    request: Request,
    confirm: bool = Query(default=False),
    user: CurrentUser = Depends(require_perm("content.purge")),
) -> dict:
    """Permanent delete. Requires ?confirm=true and a trashed item."""
    scoped = db.TenantDB(user.tenant_id)
    item = await _item(scoped, item_id)
    if item["status"] != "trashed":
        raise HTTPException(400, "Move the item to the trash before deleting it permanently.")
    if not confirm:
        raise HTTPException(400, "Permanent deletion needs confirm=true.")

    await scoped.execute(
        "DELETE FROM content_items WHERE tenant_id = $1 AND id = $2", item_id
    )
    await events.log_activity(
        user.tenant_id, "content.purged", user_id=user.id,
        object_type="content_item", object_id=item_id,
        meta={"slug": str(item["slug"]), "title": item["title"]},
        ip=db.to_inet(client_ip(request)),
    )
    return {"ok": True}


# ================================================================== bulk
@router.post("/items/bulk")
async def bulk_items(
    payload: ContentBulkRequest,
    request: Request,
    user: CurrentUser = Depends(require_user),
) -> dict:
    scoped = db.TenantDB(user.tenant_id)
    granted = await permissions_for(user.tenant_id, user.role)

    needed = {
        ContentBulkAction.publish: "content.publish",
        ContentBulkAction.unpublish: "content.publish",
        ContentBulkAction.trash: "content.trash",
        ContentBulkAction.restore: "content.trash",
        ContentBulkAction.delete: "content.purge",
        ContentBulkAction.add_terms: "content.edit_any",
        ContentBulkAction.remove_terms: "content.edit_any",
        ContentBulkAction.set_author: "content.edit_any",
    }[payload.action]
    if needed not in granted:
        raise HTTPException(403, "Your role cannot run that bulk action.")

    ids = sorted(set(payload.ids))[:200]
    rows = await scoped.fetch(
        """SELECT i.id, i.slug::text AS slug, i.status::text AS status, i.type_id,
                  t.route_prefix, t.slug::text AS type_slug
             FROM content_items i JOIN content_types t ON t.id = i.type_id
            WHERE i.tenant_id = $1 AND i.id = ANY($2::bigint[])""",
        ids,
    )
    if not rows:
        raise HTTPException(404, "None of those items exist.")

    found = [row["id"] for row in rows]
    affected = 0
    paths: list[str] = []

    if payload.action is ContentBulkAction.publish:
        await scoped.execute(
            """UPDATE content_items
                  SET status = 'published', trashed_at = NULL, scheduled_for = NULL,
                      published_at = coalesce(published_at, now()), updated_by = $3
                WHERE tenant_id = $1 AND id = ANY($2::bigint[])""",
            found, user.id,
        )
        # Snapshot each item; a shared UPDATE cannot build per-item JSON.
        for row in rows:
            fresh = await _item(scoped, row["id"])
            type_row = await _type_by_id(scoped, row["type_id"])
            terms = await _terms_for(scoped, row["id"])
            await scoped.execute(
                "UPDATE content_items SET published_snapshot = $3::jsonb"
                " WHERE tenant_id = $1 AND id = $2",
                row["id"], C.build_snapshot(fresh, type_row, terms),
            )
            path = C.public_path(row["route_prefix"], row["slug"])
            if path:
                paths.append(path)
        affected = len(found)

    elif payload.action is ContentBulkAction.unpublish:
        await scoped.execute(
            """UPDATE content_items
                  SET status = 'draft', published_snapshot = NULL, scheduled_for = NULL,
                      updated_by = $3
                WHERE tenant_id = $1 AND id = ANY($2::bigint[])""",
            found, user.id,
        )
        paths = [p for p in (C.public_path(r["route_prefix"], r["slug"]) for r in rows) if p]
        affected = len(found)

    elif payload.action is ContentBulkAction.trash:
        await scoped.execute(
            """UPDATE content_items
                  SET status = 'trashed', trashed_at = now(), published_snapshot = NULL,
                      scheduled_for = NULL, updated_by = $3
                WHERE tenant_id = $1 AND id = ANY($2::bigint[])""",
            found, user.id,
        )
        paths = [
            p for p in (C.public_path(r["route_prefix"], r["slug"]) for r in rows
                        if r["status"] == "published") if p
        ]
        affected = len(found)

    elif payload.action is ContentBulkAction.restore:
        await scoped.execute(
            """UPDATE content_items SET status = 'draft', trashed_at = NULL, updated_by = $3
                WHERE tenant_id = $1 AND id = ANY($2::bigint[]) AND status = 'trashed'""",
            found, user.id,
        )
        affected = len([r for r in rows if r["status"] == "trashed"])

    elif payload.action is ContentBulkAction.delete:
        trashed = [r["id"] for r in rows if r["status"] == "trashed"]
        if not trashed:
            raise HTTPException(400, "Move items to the trash before deleting them permanently.")
        await scoped.execute(
            "DELETE FROM content_items WHERE tenant_id = $1 AND id = ANY($2::bigint[])",
            trashed,
        )
        affected = len(trashed)

    elif payload.action in {ContentBulkAction.add_terms, ContentBulkAction.remove_terms}:
        term_ids = sorted(set(payload.term_ids or []))[:60]
        if not term_ids:
            raise HTTPException(400, "Choose at least one category or tag.")
        if payload.action is ContentBulkAction.add_terms:
            await scoped.execute(
                """INSERT INTO content_terms (tenant_id, item_id, term_id)
                   SELECT $1, i.id, tm.id
                     FROM content_items i
                     JOIN terms tm ON tm.tenant_id = $1 AND tm.id = ANY($3::bigint[])
                     JOIN taxonomy_types tt
                       ON tt.taxonomy_id = tm.taxonomy_id AND tt.type_id = i.type_id
                    WHERE i.tenant_id = $1 AND i.id = ANY($2::bigint[])
                   ON CONFLICT DO NOTHING""",
                found, term_ids,
            )
        else:
            await scoped.execute(
                """DELETE FROM content_terms
                    WHERE tenant_id = $1 AND item_id = ANY($2::bigint[])
                      AND term_id = ANY($3::bigint[])""",
                found, term_ids,
            )
        affected = len(found)

    elif payload.action is ContentBulkAction.set_author:
        if not payload.author_id:
            raise HTTPException(400, "Choose an author.")
        author = await scoped.fetch_one(
            "SELECT 1 FROM users WHERE tenant_id = $1 AND id = $2 AND is_active",
            payload.author_id,
        )
        if not author:
            raise HTTPException(400, "That user is not on this workspace.")
        await scoped.execute(
            """UPDATE content_items SET author_id = $3, updated_by = $4
                WHERE tenant_id = $1 AND id = ANY($2::bigint[])""",
            found, payload.author_id, user.id,
        )
        affected = len(found)

    await events.log_activity(
        user.tenant_id, f"content.bulk_{payload.action.value}", user_id=user.id,
        meta={"count": affected, "ids": found[:50]},
        ip=db.to_inet(client_ip(request)),
    )
    if paths or payload.action is ContentBulkAction.publish:
        # One rebuild for the whole batch, not one per item — that is
        # what build_hooks.debounce_seconds is for.
        await publishing.on_content_published(
            user.tenant_id, paths=paths or None, reason="content.published"
        )
    return {"ok": True, "affected": affected, "requested": len(ids), "missing": len(ids) - len(found)}


# ============================================================= revisions
@router.get("/items/{item_id}/revisions")
async def list_revisions(
    item_id: int, user: CurrentUser = Depends(require_perm("content.view"))
) -> dict:
    scoped = db.TenantDB(user.tenant_id)
    await _item(scoped, item_id)
    rows = await scoped.fetch(
        """SELECT r.id, r.title, r.reason, r.created_at, u.display_name AS author,
                  length(coalesce(r.body, '')) AS body_length
             FROM content_revisions r LEFT JOIN users u ON u.id = r.created_by
            WHERE r.tenant_id = $1 AND r.item_id = $2
            ORDER BY r.created_at DESC, r.id DESC""",
        item_id,
    )
    return {"revisions": rows, "keep": C.MAX_REVISIONS}


@router.get("/items/{item_id}/revisions/{revision_id}")
async def revision_detail(
    item_id: int, revision_id: int, user: CurrentUser = Depends(require_perm("content.view"))
) -> dict:
    scoped = db.TenantDB(user.tenant_id)
    row = await scoped.fetch_one(
        """SELECT r.id, r.title, r.excerpt, r.body, r.fields, r.seo, r.reason, r.created_at,
                  u.display_name AS author
             FROM content_revisions r LEFT JOIN users u ON u.id = r.created_by
            WHERE r.tenant_id = $1 AND r.item_id = $2 AND r.id = $3""",
        item_id, revision_id,
    )
    if not row:
        raise HTTPException(404, "That revision no longer exists.")
    return {"revision": row}


@router.post("/items/{item_id}/revisions/{revision_id}/restore")
async def restore_revision(
    item_id: int,
    revision_id: int,
    payload: RevisionRestore,
    request: Request,
    user: CurrentUser = Depends(require_user),
) -> dict:
    scoped = db.TenantDB(user.tenant_id)
    item = await _item(scoped, item_id)
    await _assert_can_edit(user, item)

    revision = await scoped.fetch_one(
        """SELECT title, excerpt, body, fields, seo FROM content_revisions
            WHERE tenant_id = $1 AND item_id = $2 AND id = $3""",
        item_id, revision_id,
    )
    if not revision:
        raise HTTPException(404, "That revision no longer exists.")

    # Snapshot the current state first, so a restore is itself undoable.
    await C.save_revision(scoped, item, reason="pre-restore", user_id=user.id)

    # Restores into the draft columns only; the live page changes on the
    # next publish, never as a side effect of a rollback.
    updated = await scoped.fetch_one(
        f"""UPDATE content_items
               SET title = $3, excerpt = $4, body = $5, fields = $6::jsonb,
                   seo = $7::jsonb, updated_by = $8
             WHERE tenant_id = $1 AND id = $2 RETURNING {ITEM_COLUMNS.replace("i.", "")}""",
        item_id, revision["title"], revision["excerpt"], revision["body"],
        revision["fields"] or {}, revision["seo"] or {}, user.id,
    )
    await C.sync_media_usage(scoped, updated)
    await events.log_activity(
        user.tenant_id, "content.revision_restored", user_id=user.id,
        object_type="content_item", object_id=item_id,
        meta={"revision": revision_id, "note": payload.note},
        ip=db.to_inet(client_ip(request)),
    )
    return {"item": await _detail(scoped, item_id, user)}


# =========================================================== draft preview
@router.post("/items/{item_id}/preview", status_code=201)
async def create_preview(
    item_id: int, user: CurrentUser = Depends(require_perm("content.view"))
) -> dict:
    """Mint a shareable draft-preview link.

    The token is single-purpose and expiring, so a client can review an
    unpublished page without being given an admin account. Only the
    hash is stored, as with sessions and reset tokens.
    """
    scoped = db.TenantDB(user.tenant_id)
    item = await _item(scoped, item_id)
    type_row = await _type_by_id(scoped, item["type_id"])

    token = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(hours=PREVIEW_TOKEN_HOURS)
    await scoped.execute(
        """INSERT INTO preview_tokens (tenant_id, item_id, token_hash, expires_at, created_by)
           VALUES ($1, $2, $3, $4, $5)""",
        item_id, sha256(token), expires, user.id,
    )

    site = await publishing.site_base_url(user.tenant_id)
    path = C.public_path(type_row.get("route_prefix"), str(item["slug"])) or "/"
    return {
        "token": token,
        "expiresAt": expires.isoformat(),
        # Both forms: the API URL a static frontend fetches, and the
        # site URL a reviewer opens if the frontend honours ?preview=.
        "apiUrl": f"/api/v1/preview/{token}",
        "siteUrl": f"{site}{path}?preview={token}" if site else None,
    }


# ============================================================ taxonomies
@router.get("/taxonomies")
async def list_taxonomies(scoped: db.TenantDB = Depends(tenant_db)) -> dict:
    rows = await scoped.fetch(
        """SELECT tx.id, tx.slug::text AS slug, tx.name, tx.plural_name,
                  tx.is_hierarchical, tx.is_builtin,
                  count(DISTINCT tm.id)::int AS term_count,
                  coalesce(array_agg(DISTINCT ct.slug::text)
                           FILTER (WHERE ct.slug IS NOT NULL), '{}') AS type_slugs
             FROM taxonomies tx
             LEFT JOIN terms tm ON tm.taxonomy_id = tx.id
             LEFT JOIN taxonomy_types tt ON tt.taxonomy_id = tx.id
             LEFT JOIN content_types ct ON ct.id = tt.type_id
            WHERE tx.tenant_id = $1
            GROUP BY tx.id ORDER BY tx.name"""
    )
    return {"taxonomies": rows}


@router.post("/taxonomies", status_code=201)
async def create_taxonomy(
    payload: TaxonomyCreate,
    request: Request,
    user: CurrentUser = Depends(require_perm("taxonomy.manage")),
) -> dict:
    scoped = db.TenantDB(user.tenant_id)
    slug = C.check_slug(payload.slug)
    if await scoped.fetch_one(
        "SELECT 1 FROM taxonomies WHERE tenant_id = $1 AND slug = $2", slug
    ):
        raise HTTPException(400, "A taxonomy already uses that slug.")

    row = await scoped.fetch_one(
        """INSERT INTO taxonomies (tenant_id, slug, name, plural_name, is_hierarchical)
           VALUES ($1, $2, $3, $4, $5)
           RETURNING id, slug::text AS slug, name, plural_name, is_hierarchical, is_builtin""",
        slug, payload.name, payload.plural_name or f"{payload.name}s",
        payload.is_hierarchical,
    )
    if payload.type_slugs:
        await scoped.execute(
            """INSERT INTO taxonomy_types (tenant_id, taxonomy_id, type_id)
               SELECT $1, $2, ct.id FROM content_types ct
                WHERE ct.tenant_id = $1 AND ct.slug = ANY($3::citext[])
               ON CONFLICT DO NOTHING""",
            row["id"], [C.check_slug(s) for s in payload.type_slugs],
        )
    await events.log_activity(
        user.tenant_id, "taxonomy.created", user_id=user.id,
        object_type="taxonomy", object_id=row["id"], meta={"slug": slug},
        ip=db.to_inet(client_ip(request)),
    )
    return {"taxonomy": {**row, "term_count": 0}}


@router.patch("/taxonomies/{taxonomy_id}")
async def update_taxonomy(
    taxonomy_id: int,
    payload: TaxonomyUpdate,
    user: CurrentUser = Depends(require_perm("taxonomy.manage")),
) -> dict:
    scoped = db.TenantDB(user.tenant_id)
    sent = payload.model_dump(exclude_unset=True)
    if not sent:
        raise HTTPException(400, "Nothing to save.")

    if "name" in sent or "plural_name" in sent:
        row = await scoped.fetch_one(
            """UPDATE taxonomies
                  SET name = coalesce($3, name), plural_name = coalesce($4, plural_name)
                WHERE tenant_id = $1 AND id = $2
                RETURNING id, slug::text AS slug, name, plural_name, is_hierarchical""",
            taxonomy_id, payload.name, payload.plural_name,
        )
        if not row:
            raise HTTPException(404, "That taxonomy no longer exists.")
    else:
        row = await scoped.fetch_one(
            """SELECT id, slug::text AS slug, name, plural_name, is_hierarchical
                 FROM taxonomies WHERE tenant_id = $1 AND id = $2""",
            taxonomy_id,
        )
        if not row:
            raise HTTPException(404, "That taxonomy no longer exists.")

    if "type_slugs" in sent:
        await scoped.execute(
            "DELETE FROM taxonomy_types WHERE tenant_id = $1 AND taxonomy_id = $2",
            taxonomy_id,
        )
        if payload.type_slugs:
            await scoped.execute(
                """INSERT INTO taxonomy_types (tenant_id, taxonomy_id, type_id)
                   SELECT $1, $2, ct.id FROM content_types ct
                    WHERE ct.tenant_id = $1 AND ct.slug = ANY($3::citext[])
                   ON CONFLICT DO NOTHING""",
                taxonomy_id, [C.check_slug(s) for s in payload.type_slugs],
            )
    return {"taxonomy": row}


@router.delete("/taxonomies/{taxonomy_id}")
async def delete_taxonomy(
    taxonomy_id: int, user: CurrentUser = Depends(require_perm("taxonomy.manage"))
) -> dict:
    scoped = db.TenantDB(user.tenant_id)
    row = await scoped.fetch_one(
        "SELECT is_builtin FROM taxonomies WHERE tenant_id = $1 AND id = $2", taxonomy_id
    )
    if not row:
        raise HTTPException(404, "That taxonomy no longer exists.")
    if row["is_builtin"]:
        raise HTTPException(400, "Categories and tags are built in and cannot be deleted.")

    await scoped.execute(
        "DELETE FROM taxonomies WHERE tenant_id = $1 AND id = $2", taxonomy_id
    )
    return {"ok": True}


# ================================================================= terms
@router.get("/terms")
async def list_terms(
    taxonomy: str | None = Query(default=None, max_length=60),
    scoped: db.TenantDB = Depends(tenant_db),
) -> dict:
    rows = await scoped.fetch(
        """SELECT tm.id, tm.slug::text AS slug, tm.name, tm.description, tm.parent_id,
                  tm.sort_order, tm.seo, tx.slug::text AS taxonomy, tx.is_hierarchical,
                  count(ct.item_id)::int AS item_count
             FROM terms tm
             JOIN taxonomies tx ON tx.id = tm.taxonomy_id
             LEFT JOIN content_terms ct ON ct.term_id = tm.id
            WHERE tm.tenant_id = $1 AND ($2::citext IS NULL OR tx.slug = $2)
            GROUP BY tm.id, tx.slug, tx.is_hierarchical
            ORDER BY tx.slug, tm.sort_order, tm.name""",
        C.check_slug(taxonomy) if taxonomy else None,
    )
    return {"terms": rows}


@router.post("/terms", status_code=201)
async def create_term(
    payload: TermCreate,
    user: CurrentUser = Depends(require_perm("taxonomy.manage")),
) -> dict:
    scoped = db.TenantDB(user.tenant_id)
    taxonomy = await scoped.fetch_one(
        "SELECT id, is_hierarchical FROM taxonomies WHERE tenant_id = $1 AND slug = $2",
        C.check_slug(payload.taxonomy),
    )
    if not taxonomy:
        raise HTTPException(404, f"There is no “{payload.taxonomy}” taxonomy.")
    if payload.parent_id and not taxonomy["is_hierarchical"]:
        raise HTTPException(400, "That taxonomy is flat — terms cannot nest.")

    slug = await _unique_term_slug(
        scoped, taxonomy["id"], payload.slug or C.slugify(payload.name)
    )
    row = await scoped.fetch_one(
        """INSERT INTO terms (tenant_id, taxonomy_id, parent_id, slug, name,
                              description, seo, sort_order)
           VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8)
           RETURNING id, slug::text AS slug, name, description, parent_id, sort_order, seo""",
        taxonomy["id"], payload.parent_id, slug, collapse(payload.name, 120),
        keep_lines(payload.description, 600), C.clean_seo(payload.seo),
        payload.sort_order or 0,
    )
    return {"term": {**row, "taxonomy": payload.taxonomy, "item_count": 0}}


async def _unique_term_slug(
    scoped: db.TenantDB, taxonomy_id: int, wanted: str, exclude_id: int | None = None
) -> str:
    base = C.check_slug(wanted)
    candidate = base
    for suffix in range(2, 200):
        taken = await scoped.fetch_one(
            """SELECT 1 FROM terms
                WHERE tenant_id = $1 AND taxonomy_id = $2 AND slug = $3
                  AND ($4::bigint IS NULL OR id <> $4)""",
            taxonomy_id, candidate, exclude_id,
        )
        if not taken:
            return candidate
        candidate = f"{base[:76]}-{suffix}"
    raise HTTPException(400, "Could not find a free slug for that term.")


@router.patch("/terms/{term_id}")
async def update_term(
    term_id: int,
    payload: TermUpdate,
    user: CurrentUser = Depends(require_perm("taxonomy.manage")),
) -> dict:
    scoped = db.TenantDB(user.tenant_id)
    term = await scoped.fetch_one(
        "SELECT id, taxonomy_id, slug::text AS slug FROM terms WHERE tenant_id = $1 AND id = $2",
        term_id,
    )
    if not term:
        raise HTTPException(404, "That term no longer exists.")

    sent = payload.model_dump(exclude_unset=True)
    if not sent:
        raise HTTPException(400, "Nothing to save.")

    args: list = [user.tenant_id, term_id]
    assignments: list[str] = []

    def assign(column: str, value, cast: str = "") -> None:
        args.append(value)
        assignments.append(f"{column} = ${len(args)}{cast}")

    if "name" in sent:
        assign("name", collapse(payload.name, 120))
    if "slug" in sent and payload.slug:
        assign("slug", await _unique_term_slug(
            scoped, term["taxonomy_id"], payload.slug, exclude_id=term_id
        ))
    if "description" in sent:
        assign("description", keep_lines(payload.description, 600))
    if "parent_id" in sent:
        if payload.parent_id == term_id:
            raise HTTPException(400, "A term cannot be its own parent.")
        assign("parent_id", payload.parent_id)
    if "seo" in sent:
        assign("seo", C.clean_seo(payload.seo), "::jsonb")
    if "sort_order" in sent:
        assign("sort_order", payload.sort_order or 0)

    row = await db.fetch_one(
        f"""UPDATE terms SET {", ".join(assignments)}
             WHERE tenant_id = $1 AND id = $2
             RETURNING id, slug::text AS slug, name, description, parent_id, sort_order, seo""",
        *args,
    )
    return {"term": row}


@router.delete("/terms/{term_id}")
async def delete_term(
    term_id: int, user: CurrentUser = Depends(require_perm("taxonomy.manage"))
) -> dict:
    scoped = db.TenantDB(user.tenant_id)
    removed = await scoped.fetch(
        "DELETE FROM terms WHERE tenant_id = $1 AND id = $2 RETURNING slug::text AS slug",
        term_id,
    )
    if not removed:
        raise HTTPException(404, "That term no longer exists.")
    return {"ok": True}


# =============================================================== authors
@router.get("/authors")
async def list_authors(scoped: db.TenantDB = Depends(tenant_db)) -> dict:
    """Everyone who can be set as an author, with their published counts."""
    rows = await scoped.fetch(
        """SELECT u.id, u.display_name, u.email, u.role::text AS role,
                  p.job_title, p.bio, p.avatar_media_id, p.social,
                  count(i.id) FILTER (WHERE i.status = 'published')::int AS published_count,
                  count(i.id) FILTER (WHERE i.status = 'draft')::int AS draft_count
             FROM users u
             LEFT JOIN user_profiles p ON p.user_id = u.id
             LEFT JOIN content_items i ON i.author_id = u.id
            WHERE u.tenant_id = $1 AND u.is_active
            GROUP BY u.id, p.user_id ORDER BY u.display_name"""
    )
    return {"authors": rows}
