"""Pages — a WordPress-style builder for public marketing pages.

Editing writes the draft columns; the live page at /p/{tenant}/{slug}
serves only the published_* snapshot, so an in-progress edit can never
leak. Every publish snapshots a revision that can be restored later.
"""

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse

from .. import db, events
from ..pagebuilder import clean_blocks, clean_theme, render_page
from ..schemas import PageCreate, PageUpdate, collapse
from ..security import CurrentUser, client_ip, require_role, require_user, tenant_db

router = APIRouter(prefix="/api/pages", tags=["pages"])
public_router = APIRouter(tags=["pages-public"])

PAGE_COLUMNS = """id, slug, title, description, blocks, theme, status::text AS status,
                  published_at, created_at, updated_at"""

# The public page loads images from anywhere over https and posts the
# lead form back to this origin; everything else stays locked down.
PUBLIC_PAGE_CSP = "; ".join(
    [
        "default-src 'none'",
        "img-src https: data:",
        "style-src 'unsafe-inline'",
        "script-src 'self'",
        "connect-src 'self'",
        "form-action 'self'",
        "base-uri 'none'",
        "frame-ancestors 'none'",
    ]
)
# The editor shows the draft in a same-origin iframe.
PREVIEW_CSP = PUBLIC_PAGE_CSP.replace("frame-ancestors 'none'", "frame-ancestors 'self'")


async def _form_fields(tenant_id: int, blocks: list[dict]) -> dict[str, list]:
    """Field definitions for every form block on the page, keyed by slug."""
    slugs = list({b["form_slug"] for b in blocks if b.get("type") == "form"})
    if not slugs:
        return {}
    rows = await db.fetch(
        """SELECT slug::text AS slug, fields FROM forms
            WHERE tenant_id = $1 AND slug = ANY($2::citext[]) AND is_active""",
        tenant_id,
        slugs,
    )
    return {row["slug"].lower(): row["fields"] for row in rows}


async def _get_page(scoped: db.TenantDB, page_id: int) -> dict:
    page = await scoped.fetch_one(
        f"SELECT {PAGE_COLUMNS} FROM pages WHERE tenant_id = $1 AND id = $2", page_id
    )
    if not page:
        raise HTTPException(404, "That page no longer exists.")
    return page


# ================================================================ admin API
@router.get("")
async def list_pages(scoped: db.TenantDB = Depends(tenant_db)) -> dict:
    rows = await scoped.fetch(
        """SELECT p.id, p.slug, p.title, p.status::text AS status,
                  p.published_at, p.updated_at, u.display_name AS updated_by_name
             FROM pages p LEFT JOIN users u ON u.id = p.updated_by
            WHERE p.tenant_id = $1 ORDER BY p.updated_at DESC"""
    )
    return {"pages": rows}


@router.post("", status_code=201)
async def create_page(
    payload: PageCreate,
    request: Request,
    user: CurrentUser = Depends(require_role("admin")),
) -> dict:
    scoped = db.TenantDB(user.tenant_id)
    existing = await scoped.fetch_one(
        "SELECT 1 FROM pages WHERE tenant_id = $1 AND slug = $2", payload.slug
    )
    if existing:
        raise HTTPException(400, "A page already uses that slug.")

    starter_blocks = [
        {"type": "hero", "heading": payload.title, "sub": None,
         "button_label": None, "button_href": None, "align": "center"},
        {"type": "text", "body": "Write your first paragraph here."},
    ]
    page = await scoped.fetch_one(
        f"""INSERT INTO pages (tenant_id, slug, title, blocks, theme, updated_by)
            VALUES ($1, $2, $3, $4::jsonb, $5::jsonb, $6)
            RETURNING {PAGE_COLUMNS}""",
        payload.slug,
        payload.title,
        # The pool's jsonb codec runs json.dumps itself; pass objects, not
        # pre-dumped strings, or the value lands double-encoded.
        starter_blocks,
        clean_theme({}),
        user.id,
    )
    await events.log_activity(
        user.tenant_id,
        "page.created",
        user_id=user.id,
        object_type="page",
        object_id=page["id"],
        meta={"slug": payload.slug},
        ip=db.to_inet(client_ip(request)),
    )
    return {"page": page}


@router.get("/{page_id}")
async def page_detail(page_id: int, scoped: db.TenantDB = Depends(tenant_db)) -> dict:
    return {"page": await _get_page(scoped, page_id)}


@router.patch("/{page_id}")
async def update_page(
    page_id: int,
    payload: PageUpdate,
    request: Request,
    user: CurrentUser = Depends(require_role("admin")),
) -> dict:
    scoped = db.TenantDB(user.tenant_id)
    await _get_page(scoped, page_id)

    sent = payload.model_dump(exclude_unset=True)
    if not sent:
        raise HTTPException(400, "Nothing to save.")

    args: list = [user.tenant_id, page_id]
    assignments: list[str] = []

    def assign(column: str, value, cast: str = "") -> None:
        args.append(value)
        assignments.append(f"{column} = ${len(args)}{cast}")

    if "title" in sent:
        assign("title", payload.title)
    if "slug" in sent:
        taken = await scoped.fetch_one(
            "SELECT 1 FROM pages WHERE tenant_id = $1 AND slug = $2 AND id <> $3",
            payload.slug,
            page_id,
        )
        if taken:
            raise HTTPException(400, "A page already uses that slug.")
        assign("slug", payload.slug)
    if "description" in sent:
        assign("description", collapse(payload.description, 300))
    if "blocks" in sent:
        assign("blocks", clean_blocks(payload.blocks), "::jsonb")
    if "theme" in sent:
        assign("theme", clean_theme(payload.theme), "::jsonb")
    assign("updated_by", user.id)

    page = await db.fetch_one(
        f"""UPDATE pages SET {', '.join(assignments)}
             WHERE tenant_id = $1 AND id = $2 RETURNING {PAGE_COLUMNS}""",
        *args,
    )
    await events.log_activity(
        user.tenant_id,
        "page.updated",
        user_id=user.id,
        object_type="page",
        object_id=page_id,
        meta={"fields": list(sent.keys())},
        ip=db.to_inet(client_ip(request)),
    )
    return {"page": page}


@router.post("/{page_id}/publish")
async def publish_page(
    page_id: int,
    request: Request,
    user: CurrentUser = Depends(require_role("admin")),
) -> dict:
    scoped = db.TenantDB(user.tenant_id)
    await _get_page(scoped, page_id)

    page = await scoped.fetch_one(
        f"""UPDATE pages
               SET status = 'published',
                   published_title = title,
                   published_description = description,
                   published_blocks = blocks,
                   published_theme = theme,
                   published_at = now(),
                   updated_by = $3
             WHERE tenant_id = $1 AND id = $2 RETURNING {PAGE_COLUMNS}""",
        page_id,
        user.id,
    )

    # Snapshot the published content; keep only the newest 20 revisions.
    await scoped.execute(
        """INSERT INTO page_revisions (tenant_id, page_id, title, blocks, theme, created_by)
           SELECT tenant_id, id, title, blocks, theme, $3 FROM pages
            WHERE tenant_id = $1 AND id = $2""",
        page_id,
        user.id,
    )
    await scoped.execute(
        """DELETE FROM page_revisions
            WHERE tenant_id = $1 AND page_id = $2 AND id NOT IN (
              SELECT id FROM page_revisions WHERE page_id = $2
               ORDER BY created_at DESC, id DESC LIMIT 20)""",
        page_id,
    )

    await events.log_activity(
        user.tenant_id,
        "page.published",
        user_id=user.id,
        object_type="page",
        object_id=page_id,
        meta={"slug": page["slug"]},
        ip=db.to_inet(client_ip(request)),
    )
    return {"page": page}


@router.post("/{page_id}/unpublish")
async def unpublish_page(
    page_id: int,
    request: Request,
    user: CurrentUser = Depends(require_role("admin")),
) -> dict:
    scoped = db.TenantDB(user.tenant_id)
    await _get_page(scoped, page_id)

    page = await scoped.fetch_one(
        f"""UPDATE pages SET status = 'draft', updated_by = $3
             WHERE tenant_id = $1 AND id = $2 RETURNING {PAGE_COLUMNS}""",
        page_id,
        user.id,
    )
    await events.log_activity(
        user.tenant_id,
        "page.unpublished",
        user_id=user.id,
        object_type="page",
        object_id=page_id,
        ip=db.to_inet(client_ip(request)),
    )
    return {"page": page}


@router.delete("/{page_id}")
async def delete_page(
    page_id: int,
    request: Request,
    user: CurrentUser = Depends(require_role("admin")),
) -> dict:
    scoped = db.TenantDB(user.tenant_id)
    removed = await scoped.fetch(
        "DELETE FROM pages WHERE tenant_id = $1 AND id = $2 RETURNING slug",
        page_id,
    )
    if not removed:
        raise HTTPException(404, "That page no longer exists.")
    await events.log_activity(
        user.tenant_id,
        "page.deleted",
        user_id=user.id,
        object_type="page",
        object_id=page_id,
        meta={"slug": str(removed[0]["slug"])},
        ip=db.to_inet(client_ip(request)),
    )
    return {"ok": True}


# =============================================================== revisions
@router.get("/{page_id}/revisions")
async def list_revisions(page_id: int, scoped: db.TenantDB = Depends(tenant_db)) -> dict:
    await _get_page(scoped, page_id)
    rows = await scoped.fetch(
        """SELECT r.id, r.title, r.created_at, u.display_name AS author
             FROM page_revisions r LEFT JOIN users u ON u.id = r.created_by
            WHERE r.tenant_id = $1 AND r.page_id = $2
            ORDER BY r.created_at DESC, r.id DESC""",
        page_id,
    )
    return {"revisions": rows}


@router.post("/{page_id}/revisions/{revision_id}/restore")
async def restore_revision(
    page_id: int,
    revision_id: int,
    request: Request,
    user: CurrentUser = Depends(require_role("admin")),
) -> dict:
    scoped = db.TenantDB(user.tenant_id)
    await _get_page(scoped, page_id)

    revision = await scoped.fetch_one(
        """SELECT title, blocks, theme FROM page_revisions
            WHERE tenant_id = $1 AND page_id = $2 AND id = $3""",
        page_id,
        revision_id,
    )
    if not revision:
        raise HTTPException(404, "That revision no longer exists.")

    # Restore into the draft only; the live page changes on the next publish.
    page = await scoped.fetch_one(
        f"""UPDATE pages SET title = $3, blocks = $4::jsonb, theme = $5::jsonb, updated_by = $6
             WHERE tenant_id = $1 AND id = $2 RETURNING {PAGE_COLUMNS}""",
        page_id,
        revision["title"],
        revision["blocks"],
        revision["theme"],
        user.id,
    )
    await events.log_activity(
        user.tenant_id,
        "page.revision_restored",
        user_id=user.id,
        object_type="page",
        object_id=page_id,
        meta={"revision": revision_id},
        ip=db.to_inet(client_ip(request)),
    )
    return {"page": page}


# ================================================================= preview
@router.get("/{page_id}/preview", include_in_schema=False)
async def preview_page(
    page_id: int,
    user: CurrentUser = Depends(require_user),
    scoped: db.TenantDB = Depends(tenant_db),
) -> HTMLResponse:
    """Render the current draft, iframed by the editor."""
    page = await _get_page(scoped, page_id)
    blocks = page["blocks"] if isinstance(page["blocks"], list) else []
    html = render_page(
        title=page["title"],
        description=page["description"],
        blocks=blocks,
        theme=page["theme"] or {},
        tenant_slug=user.tenant_slug,
        forms=await _form_fields(user.tenant_id, blocks),
        preview=True,
    )
    return HTMLResponse(
        html,
        headers={
            "content-security-policy": PREVIEW_CSP,
            "x-frame-options": "SAMEORIGIN",
            "cache-control": "no-store",
        },
    )


# ============================================================== public page
@public_router.get("/p/{tenant_slug}/{page_slug}", include_in_schema=False)
async def public_page(tenant_slug: str, page_slug: str) -> HTMLResponse:
    row = await db.fetch_one(
        """SELECT p.tenant_id, p.published_title, p.published_description,
                  p.published_blocks, p.published_theme, t.slug::text AS tenant_slug
             FROM pages p JOIN tenants t ON t.id = p.tenant_id
            WHERE t.slug = $1 AND t.is_active
              AND p.slug = $2 AND p.status = 'published'""",
        collapse(tenant_slug, 60),
        collapse(page_slug, 80),
    )
    if not row or row["published_blocks"] is None:
        raise HTTPException(404, "This page is not available.")

    blocks = row["published_blocks"] if isinstance(row["published_blocks"], list) else []
    html = render_page(
        title=row["published_title"] or "Untitled",
        description=row["published_description"],
        blocks=blocks,
        theme=row["published_theme"] or {},
        tenant_slug=row["tenant_slug"],
        forms=await _form_fields(row["tenant_id"], blocks),
    )
    return HTMLResponse(
        html,
        headers={
            "content-security-policy": PUBLIC_PAGE_CSP,
            # Short TTL: publishes show up within a minute even behind a CDN.
            "cache-control": "public, max-age=60",
        },
    )
