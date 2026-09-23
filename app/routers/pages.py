"""Pages — a WordPress-style builder for public marketing pages.

Editing writes the draft columns; the live page at /p/{tenant}/{slug}
serves only the published_* snapshot, so an in-progress edit can never
leak. Every publish snapshots a revision that can be restored later.
"""

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import db, events, permissions, storage
from ..config import settings
from ..pagebuilder import (
    blocks_to_html,
    clean_block_html,
    clean_blocks,
    clean_page_seo,
    clean_site_chrome,
    clean_theme,
    is_document,
    page_document,
    with_site_chrome,
    render_page,
)
from ..sanitize import describe_changes
from ..schemas import PageCreate, PageHtmlCheck, PageModeChange, PageUpdate, collapse
from ..security import CurrentUser, client_ip, require_role, require_user, tenant_db
from .seo import match_redirect

router = APIRouter(prefix="/api/pages", tags=["pages"])
public_router = APIRouter(tags=["pages-public"])

PAGE_COLUMNS = """id, slug, title, description, blocks, theme, seo, mode,
                  status::text AS status, published_at, created_at, updated_at"""

def _page_csp(*, preview: bool = False) -> str:
    """CSP for a rendered page.

    `default-src 'none'` means an unlisted directive blocks outright —
    which is why `frame-src` has to be stated explicitly now that an
    HTML block can hold an embed. Without it the sanitizer would happily
    store a YouTube iframe and the browser would silently refuse to load
    it, which is a confusing way to find out.

    The frame allow-list is the *same* list the sanitizer enforces
    (EMBED_ALLOWED_HOSTS), so the two cannot drift: a host the sanitizer
    strips is a host the CSP would have blocked anyway, and vice versa.

    `script-src 'self'` is what makes the HTML block's tag allow-list
    belt-and-braces rather than load-bearing on its own: even if some
    markup ever got past the cleaner, there is no origin the page is
    allowed to execute from but ours, and inline script is not allowed
    at all.
    """
    # Both forms per host: CSP matches hosts exactly, so
    # `https://youtube.com` does not cover `www.youtube.com` — which is
    # precisely what the sanitizer allows (it matches on a dot suffix).
    # Emitting only the bare host would store an embed the browser then
    # refuses to load.
    frame_src = " ".join(
        source
        for host in settings.embed_allowed_hosts
        for source in (f"https://{host}", f"https://*.{host}")
    )
    directives = [
        "default-src 'none'",
        # Images may come from anywhere over https so an image block can
        # point at a CDN we do not control. `'self'` is what makes a
        # same-origin media path work on a deployment served over plain
        # http — without it a relative /media/... image is blocked, which
        # is a confusing failure to hit behind a TLS-terminating proxy.
        "img-src 'self' https: data:",
        # Inline styles and <style> blocks are the author's own, cleaned
        # on write by app/sanitize.py. No host is listed, so a remote
        # stylesheet stays impossible even if one were ever stored.
        "style-src 'unsafe-inline'",
        "script-src 'self'",
        "connect-src 'self'",
        # https: so an @font-face in a hand-written page can load a real
        # font; without it the CSS is allowed but the file is refused.
        "font-src 'self' https: data:",
        "media-src 'self' https:",
        f"frame-src {frame_src}" if frame_src else "frame-src 'none'",
        # An HTML block can hold a real <form> now, and authors point
        # those at the endpoint that already collects for them (a
        # newsletter provider, their own CRM). Holding form-action to
        # 'self' would store that form, render it, and then have the
        # browser refuse the submit with nothing on the page to say why.
        # https: (never http:) is the same bar the sanitizer applies to
        # `action`; a form still cannot be *created* by anyone but a
        # signed-in author of this tenant.
        "form-action 'self' https:",
        "base-uri 'none'",
        # The editor shows the draft in a same-origin iframe; the live
        # page must not be framable at all.
        "frame-ancestors 'self'" if preview else "frame-ancestors 'none'",
    ]
    return "; ".join(directives)


def _document_csp(*, preview: bool = False) -> str:
    """CSP for a page stored as a whole HTML document.

    A document is somebody's own file: it loads the CDN it was written
    against and runs the script that makes it work, so the tight policy
    the block renderer earns — `default-src 'none'`, no inline script —
    would only break it. What stays is the shape of the thing: https
    sources (no mixed content), no plugins, and a page that still cannot
    be framed.

    The preview is the exception, and the important one. The editor
    frames the draft on the admin's own origin, so `sandbox` without
    `allow-same-origin` drops the author's scripts into an opaque
    origin: they can run, but they cannot read the session cookie or
    call this API as whoever is editing the page. The live page carries
    no sandbox — it is the page — which is why storing one takes the
    `pages.raw_html` permission.
    """
    directives = [
        "default-src 'self' https: data: blob:",
        "script-src 'self' https: 'unsafe-inline' 'unsafe-eval'",
        "style-src 'self' https: 'unsafe-inline'",
        "img-src 'self' https: data: blob:",
        "font-src 'self' https: data:",
        "connect-src 'self' https:",
        "frame-src 'self' https:",
        "media-src 'self' https: blob:",
        "form-action 'self' https:",
        "object-src 'none'",
        "base-uri 'self'",
        "frame-ancestors 'self'" if preview else "frame-ancestors 'none'",
    ]
    if preview:
        directives.append(
            "sandbox allow-scripts allow-forms allow-popups allow-modals "
            "allow-downloads allow-presentation"
        )
    return "; ".join(directives)


PUBLIC_PAGE_CSP = _page_csp()
PREVIEW_CSP = _page_csp(preview=True)
PUBLIC_DOCUMENT_CSP = _document_csp()
PREVIEW_DOCUMENT_CSP = _document_csp(preview=True)


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


async def _site_chrome(tenant_id: int) -> dict | None:
    """The site's header and footer (settings key site_chrome), cleaned."""
    row = await db.fetch_one(
        "SELECT value FROM settings WHERE tenant_id = $1 AND key = 'site_chrome'", tenant_id
    )
    if not row or not isinstance(row["value"], dict):
        return None
    return clean_site_chrome(row["value"])


def _absolute(url: str | None) -> str | None:
    """Make a path absolute, or give up.

    og:url and og:image have to be absolute to mean anything to a
    crawler, and this app only knows its own origin when APP_BASE_URL is
    set. Returning None (and so omitting the tag) beats emitting a path
    that resolves against whatever host the crawler happens to be on.
    """
    if not url:
        return None
    if url.startswith(("https://", "http://")):
        return url
    base = settings.app_base_url
    return f"{base}{url}" if base else None


async def _head_context(*, tenant_id: int, tenant_slug: str, slug: str, seo) -> dict:
    """The head values this request has to resolve for the renderer.

    `pagebuilder` renders meta tags but cannot know the site's origin or
    what an og_image_id points at, so both are looked up here. The media
    query only runs for a page that actually names an image.
    """
    seo = dict(seo) if isinstance(seo, dict) else {}
    image_url = None

    image_id = seo.get("og_image_id") or seo.get("twitter_image_id")
    if image_id:
        row = await db.fetch_one(
            "SELECT storage_key, alt_text FROM media WHERE tenant_id = $1 AND id = $2",
            tenant_id,
            int(image_id),
        )
        if row:
            try:
                image_url = _absolute(storage.public_url(row["storage_key"]))
            except storage.StorageError:
                image_url = None          # unusable key: no tag beats a broken one
            if image_url and row["alt_text"] and not seo.get("og_image_alt"):
                seo["og_image_alt"] = row["alt_text"]

    return {
        "seo": seo,
        "page_url": _absolute(f"/p/{tenant_slug}/{slug}"),
        "image_url": image_url,
    }


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
        """SELECT p.id, p.slug, p.title, p.status::text AS status, p.mode,
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

    # Passed through the block validator rather than trusted as written,
    # so a starter can never be a shape the editor would later reject.
    if payload.starter == "html":
        starter_blocks = clean_blocks([
            {"type": "html",
             "html": f"<h1>{payload.title}</h1>\n<p>Write or paste your HTML here.</p>",
             "styled": True, "width": "normal"},
        ])
    else:
        starter_blocks = clean_blocks([
            {"type": "hero", "heading": payload.title, "align": "center"},
            {"type": "text", "body": "Write your first paragraph here."},
        ])
    page = await scoped.fetch_one(
        f"""INSERT INTO pages
              (tenant_id, slug, title, description, blocks, theme, seo, mode, updated_by)
            VALUES ($1, $2, $3, $4, $5::jsonb, $6::jsonb, $7::jsonb, $8, $9)
            RETURNING {PAGE_COLUMNS}""",
        payload.slug,
        payload.title,
        # Meta is accepted at creation so a page that gets published
        # before anyone opens its settings still has a description.
        collapse(payload.description, 300),
        # The pool's jsonb codec runs json.dumps itself; pass objects, not
        # pre-dumped strings, or the value lands double-encoded.
        starter_blocks,
        clean_theme({}),
        clean_page_seo(payload.seo),
        "html" if payload.starter == "html" else "blocks",
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
    existing = await _get_page(scoped, page_id)

    # Only an HTML page can be a document — it is the one shape where
    # the markup *is* the page — and only with the permission for it.
    allow_document = existing["mode"] == "html" and await permissions.has(user, "pages.raw_html")

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
        assign("blocks", clean_blocks(payload.blocks, allow_document=allow_document), "::jsonb")
    if "theme" in sent:
        assign("theme", clean_theme(payload.theme), "::jsonb")
    if "seo" in sent:
        assign("seo", clean_page_seo(payload.seo), "::jsonb")
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
                   published_seo = seo,
                   published_mode = mode,
                   published_at = now(),
                   updated_by = $3
             WHERE tenant_id = $1 AND id = $2 RETURNING {PAGE_COLUMNS}""",
        page_id,
        user.id,
    )

    # Snapshot the published content; keep only the newest 20 revisions.
    await scoped.execute(
        """INSERT INTO page_revisions
              (tenant_id, page_id, title, blocks, theme, description, seo, mode, created_by)
           SELECT tenant_id, id, title, blocks, theme, description, seo, mode, $3 FROM pages
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
        """SELECT title, blocks, theme, description, seo, mode FROM page_revisions
            WHERE tenant_id = $1 AND page_id = $2 AND id = $3""",
        page_id,
        revision_id,
    )
    if not revision:
        raise HTTPException(404, "That revision no longer exists.")

    # Restore into the draft only; the live page changes on the next publish.
    page = await scoped.fetch_one(
        f"""UPDATE pages SET title = $3, blocks = $4::jsonb, theme = $5::jsonb,
                              description = $6, seo = $7::jsonb, mode = $8, updated_by = $9
             WHERE tenant_id = $1 AND id = $2 RETURNING {PAGE_COLUMNS}""",
        page_id,
        revision["title"],
        revision["blocks"],
        revision["theme"],
        # The meta the page had at that publish, not today's.
        revision["description"],
        revision["seo"] or {},
        revision["mode"] or "blocks",
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


# ============================================================= page mode
@router.post("/{page_id}/mode")
async def set_page_mode(
    page_id: int,
    payload: PageModeChange,
    request: Request,
    user: CurrentUser = Depends(require_role("admin")),
) -> dict:
    """Switch a page between the block builder and hand-written HTML.

    Going to HTML renders what is already there through the same block
    renderers and keeps it as the starting markup, so an author converts
    a page instead of starting over — the page's own CSS classes come
    with it, so it still looks like it did. A form block is the one
    thing that cannot come along: `<form>` is off the sanitizer's
    allow-list on purpose, and a form that posts nowhere is worse than
    an honest gap, so it is dropped and named in the response.

    Coming back to blocks keeps the markup as a single HTML block; there
    is no way to parse a document back into typed blocks, and inventing
    one would lose more than it recovered.
    """
    scoped = db.TenantDB(user.tenant_id)
    page = await _get_page(scoped, page_id)
    skipped: list[str] = []

    if page["mode"] == payload.mode:
        return {"page": page, "skipped": skipped}

    blocks = page["blocks"] if isinstance(page["blocks"], list) else []

    if payload.mode == "html":
        if payload.html is not None:
            # Sent by the builder's whole-page source view: the author
            # has already edited the markup we would otherwise generate.
            markup = payload.html
        else:
            markup, skipped = blocks_to_html(blocks, tenant_slug=user.tenant_slug)
        allow_document = await permissions.has(user, "pages.raw_html")
        if not (allow_document and is_document(markup)):
            # Same cleaner as the save, which matters here: an HTML block
            # already on the page brings its <style> with it, and the
            # switch used to drop it on the way through.
            markup = clean_block_html(markup)
        if not markup.strip():
            markup = f"<h1>{page['title']}</h1>\n<p>Write your HTML here.</p>"
        blocks = clean_blocks(
            [{"type": "html", "html": markup, "styled": True, "width": "normal"}],
            allow_document=allow_document,
        )
    else:
        # Coming back to blocks, a document cannot stay one: nothing
        # renders <html> inside a page, so it becomes the fragment the
        # block list can actually hold.
        document = page_document(blocks)
        if document:
            blocks = clean_blocks([
                {"type": "html", "html": document, "styled": True, "width": "normal"},
            ])
            skipped = ["the document's <head> and scripts"]

    page = await scoped.fetch_one(
        f"""UPDATE pages SET mode = $3, blocks = $4::jsonb, updated_by = $5
             WHERE tenant_id = $1 AND id = $2 RETURNING {PAGE_COLUMNS}""",
        page_id,
        payload.mode,
        blocks,
        user.id,
    )
    await events.log_activity(
        user.tenant_id,
        "page.mode_changed",
        user_id=user.id,
        object_type="page",
        object_id=page_id,
        meta={"mode": payload.mode, "skipped": skipped},
        ip=db.to_inet(client_ip(request)),
    )
    return {"page": page, "skipped": skipped}


# ====================================================== HTML block editor
@router.get("/{page_id}/html", include_in_schema=False)
async def page_html(
    page_id: int,
    user: CurrentUser = Depends(require_role("admin")),
    scoped: db.TenantDB = Depends(tenant_db),
) -> dict:
    """The whole draft page, as the document a browser would receive.

    `<!doctype html>` to `</html>`: the head with its title, meta and
    theme CSS, then every block rendered through the same renderers the
    preview and the live page use. It is the page's real source, not a
    description of it — an author can read it, copy it, or edit it and
    save it back, at which point it is stored as a document and served
    byte-for-byte (POST /mode with the edited text, then the
    `pages.raw_html` gate). A page that already *is* a document answers
    with itself.
    """
    page = await _get_page(scoped, page_id)
    blocks = page["blocks"] if isinstance(page["blocks"], list) else []

    document = page_document(blocks) if page["mode"] == "html" else None
    if document:
        return {"html": document, "document": True, "mode": page["mode"]}

    blocks = with_site_chrome(blocks, await _site_chrome(user.tenant_id), page["theme"])
    head = await _head_context(
        tenant_id=user.tenant_id,
        tenant_slug=user.tenant_slug,
        slug=page["slug"],
        seo=page["seo"],
    )
    html = render_page(
        title=page["title"],
        description=page["description"],
        blocks=blocks,
        theme=page["theme"] or {},
        tenant_slug=user.tenant_slug,
        forms=await _form_fields(user.tenant_id, blocks),
        seo=head["seo"],
        page_url=head["page_url"],
        image_url=head["image_url"],
        mode=page["mode"],
    )
    return {"html": html, "document": False, "mode": page["mode"]}


@router.post("/html/check", include_in_schema=False)
async def check_html(
    payload: PageHtmlCheck,
    user: CurrentUser = Depends(require_role("admin")),
) -> dict:
    """Dry-run the sanitizer for the HTML block editor.

    The editor calls this as someone edits, so markup that will not
    survive is removed in front of the author instead of silently on
    save. It is the block validator's own function, so `html` here is
    byte-for-byte what would be stored — there is no second cleaning
    path that could drift from it.

    Not a page-scoped route: it validates a fragment that has not been
    attached to anything yet. Admin-only all the same, because it is the
    sanitizer's behaviour it exposes.
    """
    # A whole document, on a page that may hold one: nothing is cleaned,
    # so the honest answer is "this saves exactly as written".
    if payload.whole_page and is_document(payload.html):
        if await permissions.has(user, "pages.raw_html"):
            return {
                "html": payload.html,
                "changed": False,
                "document": True,
                "removed_tags": [],
                "removed_attributes": [],
            }
        cleaned = clean_block_html(payload.html)
        return {
            "html": cleaned,
            "changed": True,
            "document_blocked": True,
            **describe_changes(payload.html, cleaned),
        }

    cleaned = clean_block_html(payload.html)
    return {
        "html": cleaned,
        "changed": cleaned.strip() != payload.html.strip(),
        **describe_changes(payload.html, cleaned),
    }


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

    # A document page is its own file: no wrapper, no injected head.
    # Sandboxed here and only here — see _document_csp.
    document = page_document(blocks) if page["mode"] == "html" else None
    if document:
        return HTMLResponse(
            document,
            headers={
                "content-security-policy": PREVIEW_DOCUMENT_CSP,
                "x-frame-options": "SAMEORIGIN",
                "cache-control": "no-store",
            },
        )

    # The site header and footer go on in the preview exactly as they
    # will on the live page; the block list itself stays the page's own.
    blocks = with_site_chrome(blocks, await _site_chrome(user.tenant_id), page["theme"])
    head = await _head_context(
        tenant_id=user.tenant_id,
        tenant_slug=user.tenant_slug,
        slug=page["slug"],
        seo=page["seo"],
    )
    html = render_page(
        title=page["title"],
        description=page["description"],
        blocks=blocks,
        theme=page["theme"] or {},
        tenant_slug=user.tenant_slug,
        forms=await _form_fields(user.tenant_id, blocks),
        seo=head["seo"],
        page_url=head["page_url"],
        image_url=head["image_url"],
        mode=page["mode"],
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


# ============================================================== hosted form
@public_router.get("/f/{tenant_slug}/{form_slug}", include_in_schema=False)
async def hosted_form(tenant_slug: str, form_slug: str) -> HTMLResponse:
    """A form on a page of its own, at a URL that can be shared or linked.

    The same renderer and the same intake script as a Lead form block on
    a built page — this is that block with nothing around it — so what
    an author sees here is exactly what a visitor gets, and a form can
    be used before (or without) a page being built for it.
    """
    row = await db.fetch_one(
        """SELECT f.tenant_id, f.slug::text AS slug, f.name, f.settings,
                  t.slug::text AS tenant_slug
             FROM forms f JOIN tenants t ON t.id = f.tenant_id
            WHERE t.slug = $1 AND t.is_active AND f.slug = $2 AND f.is_active""",
        collapse(tenant_slug, 60),
        collapse(form_slug, 60),
    )
    if not row:
        raise HTTPException(404, "This form is not available.")

    settings_ = row["settings"] if isinstance(row["settings"], dict) else {}
    blocks = clean_blocks([{
        "type": "form",
        "form_slug": row["slug"],
        "heading": row["name"],
        "button_label": settings_.get("button_label") or "Send",
    }])
    blocks = with_site_chrome(blocks, await _site_chrome(row["tenant_id"]), None)
    html = render_page(
        title=row["name"],
        description=None,
        blocks=blocks,
        theme={},
        tenant_slug=row["tenant_slug"],
        forms=await _form_fields(row["tenant_id"], blocks),
        seo={"noindex": True},
    )
    return HTMLResponse(
        html,
        headers={
            "content-security-policy": PUBLIC_PAGE_CSP,
            "cache-control": "public, max-age=60",
        },
    )


# ============================================================== public page
@public_router.get("/p/{tenant_slug}/{page_path:path}", include_in_schema=False)
async def public_page(tenant_slug: str, page_path: str, request: Request) -> HTMLResponse:
    """Serve a published page — after the site's redirects have had their say.

    Redirects are checked first and win over a page at the same address:
    the SEO screen is where an author says "this URL now means that one",
    and a redirect that only fires once the page is deleted is not what
    anyone means by it. The path segment is `:path` so a from-path with
    slashes in it (/p/demo/old/section/page) can match too; only a
    single-segment path can then be a page.
    """
    tenant = await db.fetch_one(
        "SELECT id FROM tenants WHERE slug = $1 AND is_active",
        collapse(tenant_slug, 60),
    )
    if not tenant:
        raise HTTPException(404, "This page is not available.")

    redirect = await match_redirect(tenant["id"], request.url.path)
    if redirect:
        target = redirect["to_path"]
        # The visitor's query string travels with them, as on any web server.
        if request.url.query and "?" not in target:
            target = f"{target}?{request.url.query}"
        return RedirectResponse(target, status_code=redirect["status_code"])

    page_slug = page_path.strip("/")
    if not page_slug or "/" in page_slug:
        raise HTTPException(404, "This page is not available.")

    row = await db.fetch_one(
        """SELECT p.tenant_id, p.slug::text AS slug, p.published_title,
                  p.published_description, p.published_blocks, p.published_theme,
                  p.published_seo, p.published_mode, t.slug::text AS tenant_slug
             FROM pages p JOIN tenants t ON t.id = p.tenant_id
            WHERE t.slug = $1 AND t.is_active
              AND p.slug = $2 AND p.status = 'published'""",
        collapse(tenant_slug, 60),
        collapse(page_slug, 80),
    )
    if not row or row["published_blocks"] is None:
        raise HTTPException(404, "This page is not available.")

    blocks = row["published_blocks"] if isinstance(row["published_blocks"], list) else []

    # Stored as a whole document, served as one — the author's <head>,
    # their scripts, their file. The permission to save one is what
    # gates this; by the time it is published there is nothing left to
    # decide.
    document = page_document(blocks) if (row["published_mode"] or "blocks") == "html" else None
    if document:
        return HTMLResponse(
            document,
            headers={
                "content-security-policy": PUBLIC_DOCUMENT_CSP,
                "cache-control": "public, max-age=60",
            },
        )

    blocks = with_site_chrome(blocks, await _site_chrome(row["tenant_id"]), row["published_theme"])
    head = await _head_context(
        tenant_id=row["tenant_id"],
        tenant_slug=row["tenant_slug"],
        slug=row["slug"],
        # The meta published with the page, not the draft's.
        seo=row["published_seo"],
    )
    html = render_page(
        title=row["published_title"] or "Untitled",
        description=row["published_description"],
        blocks=blocks,
        theme=row["published_theme"] or {},
        tenant_slug=row["tenant_slug"],
        forms=await _form_fields(row["tenant_id"], blocks),
        seo=head["seo"],
        page_url=head["page_url"],
        image_url=head["image_url"],
        # The mode published with the page; a draft switched to HTML
        # since must not change how the live page is wrapped.
        mode=row["published_mode"] or "blocks",
    )
    return HTMLResponse(
        html,
        headers={
            "content-security-policy": PUBLIC_PAGE_CSP,
            # Short TTL: publishes show up within a minute even behind a CDN.
            "cache-control": "public, max-age=60",
        },
    )
