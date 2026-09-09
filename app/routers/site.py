"""Site structure & global settings (2.5).

Menus, reusable blocks and the site-wide configuration a static
frontend needs at build time. The public ``/api/v1/{tenant}/config``
endpoint is the single place a frontend reads all of it from — site
identity, menus, live announcements, analytics ids, cookie-consent
settings and the locale — so a rebuild picks up an admin change without
a code deploy.

Menu items are stored flat with a ``parent_id`` and re-nested on read.
The whole tree is replaced in one PUT rather than patched node by node:
a partial reorder is how a drag-and-drop builder ends up with an
orphaned branch.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from .. import db, events, storage, tenancy
from ..content import public_path, slugify
from ..permissions import require_perm
from ..sanitize import clean_html
from ..schemas import (
    BlockCreate,
    BlockUpdate,
    MenuCreate,
    MenuItemInput,
    MenuTreeUpdate,
    collapse,
)
from ..security import CurrentUser, client_ip, require_user, tenant_db

router = APIRouter(prefix="/api/site", tags=["site"])
public_router = APIRouter(tags=["site-public"])

MENU_LOCATIONS = ("header", "footer", "mobile", "sidebar", "legal", "social")
MAX_MENU_DEPTH = 3

# Settings a tenant may write through the settings API. An allow-list,
# so a compromised admin session cannot invent arbitrary config keys.
SITE_SETTING_KEYS = frozenset(
    {
        "site_identity", "locale", "maintenance", "seo_defaults", "breadcrumbs",
        "analytics", "cookie_consent", "smtp", "spam", "robots_txt", "api",
        "notify_emails", "branding", "pipeline",
    }
)


# ================================================================= menus
@router.get("/menus")
async def list_menus(scoped: db.TenantDB = Depends(tenant_db)) -> dict:
    menus = await scoped.fetch(
        """SELECT m.id, m.slug::text AS slug, m.name, m.location, m.updated_at,
                  count(i.id)::int AS item_count
             FROM menus m LEFT JOIN menu_items i ON i.menu_id = m.id
            WHERE m.tenant_id = $1
            GROUP BY m.id ORDER BY m.name"""
    )
    return {"menus": menus, "locations": list(MENU_LOCATIONS)}


@router.post("/menus", status_code=201)
async def create_menu(
    payload: MenuCreate, user: CurrentUser = Depends(require_perm("menus.manage"))
) -> dict:
    scoped = db.TenantDB(user.tenant_id)
    slug = slugify(payload.slug or payload.name, "menu")
    if await scoped.fetch_one("SELECT 1 FROM menus WHERE tenant_id = $1 AND slug = $2", slug):
        raise HTTPException(400, "A menu already uses that name.")

    location = collapse(payload.location, 40)
    if location and location not in MENU_LOCATIONS:
        raise HTTPException(400, f"Location must be one of: {', '.join(MENU_LOCATIONS)}")

    row = await scoped.fetch_one(
        """INSERT INTO menus (tenant_id, slug, name, location) VALUES ($1, $2, $3, $4)
           RETURNING id, slug::text AS slug, name, location, updated_at""",
        slug, collapse(payload.name, 80), location,
    )
    return {"menu": {**row, "item_count": 0, "items": []}}


@router.get("/menus/{menu_id}")
async def menu_detail(menu_id: int, scoped: db.TenantDB = Depends(tenant_db)) -> dict:
    menu = await scoped.fetch_one(
        """SELECT id, slug::text AS slug, name, location, updated_at
             FROM menus WHERE tenant_id = $1 AND id = $2""",
        menu_id,
    )
    if not menu:
        raise HTTPException(404, "That menu no longer exists.")
    menu["items"] = await _menu_tree(scoped, menu_id, resolve=True)
    return {"menu": menu, "locations": list(MENU_LOCATIONS)}


async def _menu_tree(scoped: db.TenantDB, menu_id: int, *, resolve: bool = False) -> list[dict]:
    """Flat rows → nested tree, resolving content/term links to live URLs."""
    rows = await scoped.fetch(
        """SELECT i.id, i.parent_id, i.label, i.link_type, i.url, i.object_id,
                  i.target, i.rel, i.icon, i.sort_order, i.is_active
             FROM menu_items i
            WHERE i.tenant_id = $1 AND i.menu_id = $2
            ORDER BY i.sort_order, i.id""",
        menu_id,
    )
    if resolve:
        await _resolve_links(scoped, rows)

    by_parent: dict[int, list[dict]] = {}
    for row in rows:
        by_parent.setdefault(row["parent_id"] or 0, []).append({**row, "children": []})

    def attach(parent_key: int, depth: int) -> list[dict]:
        if depth > MAX_MENU_DEPTH:
            return []
        nodes = by_parent.get(parent_key, [])
        for node in nodes:
            node["children"] = attach(node["id"], depth + 1)
        return nodes

    return attach(0, 1)


async def _resolve_links(scoped: db.TenantDB, rows: list[dict]) -> None:
    """Fill `resolvedUrl` for content/term/page items.

    Storing the object id rather than the URL is what keeps a menu
    correct after a slug change — the URL is derived on read.
    """
    content_ids = [r["object_id"] for r in rows if r["link_type"] == "content" and r["object_id"]]
    term_ids = [r["object_id"] for r in rows if r["link_type"] == "term" and r["object_id"]]
    page_ids = [r["object_id"] for r in rows if r["link_type"] == "page" and r["object_id"]]

    content_map: dict[int, dict] = {}
    if content_ids:
        for row in await scoped.fetch(
            """SELECT i.id, i.slug::text AS slug, i.title, i.status::text AS status,
                      t.route_prefix
                 FROM content_items i JOIN content_types t ON t.id = i.type_id
                WHERE i.tenant_id = $1 AND i.id = ANY($2::bigint[])""",
            content_ids,
        ):
            content_map[row["id"]] = {
                "url": public_path(row["route_prefix"], row["slug"]),
                "title": row["title"],
                "published": row["status"] == "published",
            }

    term_map: dict[int, dict] = {}
    if term_ids:
        for row in await scoped.fetch(
            """SELECT tm.id, tm.slug::text AS slug, tm.name, tx.slug::text AS taxonomy
                 FROM terms tm JOIN taxonomies tx ON tx.id = tm.taxonomy_id
                WHERE tm.tenant_id = $1 AND tm.id = ANY($2::bigint[])""",
            term_ids,
        ):
            term_map[row["id"]] = {
                "url": f"/{row['taxonomy']}/{row['slug']}",
                "title": row["name"],
                "published": True,
            }

    page_map: dict[int, dict] = {}
    if page_ids:
        tenant = await db.fetch_one(
            "SELECT slug::text AS slug FROM tenants WHERE id = $1", scoped.tenant_id
        )
        for row in await scoped.fetch(
            """SELECT id, slug::text AS slug, title, status::text AS status
                 FROM pages WHERE tenant_id = $1 AND id = ANY($2::bigint[])""",
            page_ids,
        ):
            page_map[row["id"]] = {
                "url": f"/p/{tenant['slug']}/{row['slug']}" if tenant else None,
                "title": row["title"],
                "published": row["status"] == "published",
            }

    lookup = {"content": content_map, "term": term_map, "page": page_map}
    for row in rows:
        target = lookup.get(row["link_type"], {}).get(row["object_id"])
        row["resolvedUrl"] = target["url"] if target else row["url"]
        row["resolvedTitle"] = target["title"] if target else None
        # A menu pointing at an unpublished page is a dead link on the
        # live site; the UI flags it rather than silently shipping it.
        row["isBroken"] = bool(row["link_type"] != "custom" and not target)
        row["targetUnpublished"] = bool(target and not target["published"])


@router.put("/menus/{menu_id}")
async def replace_menu(
    menu_id: int,
    payload: MenuTreeUpdate,
    request: Request,
    user: CurrentUser = Depends(require_perm("menus.manage")),
) -> dict:
    """Replace the menu's whole tree. Order comes from list position."""
    scoped = db.TenantDB(user.tenant_id)
    menu = await scoped.fetch_one(
        "SELECT id FROM menus WHERE tenant_id = $1 AND id = $2", menu_id
    )
    if not menu:
        raise HTTPException(404, "That menu no longer exists.")

    if payload.location and payload.location not in MENU_LOCATIONS:
        raise HTTPException(400, f"Location must be one of: {', '.join(MENU_LOCATIONS)}")

    flat: list[tuple[MenuItemInput, int | None, int]] = []

    def walk(nodes: list[MenuItemInput], parent_index: int | None, depth: int) -> None:
        if depth > MAX_MENU_DEPTH:
            raise HTTPException(400, f"Menus can nest at most {MAX_MENU_DEPTH} levels deep.")
        for order, node in enumerate(nodes):
            index = len(flat)
            flat.append((node, parent_index, order))
            if node.children:
                walk(node.children, index, depth + 1)

    walk(payload.items, None, 1)
    if len(flat) > 200:
        raise HTTPException(400, "A menu can hold at most 200 items.")

    # Rebuild rather than diff: ids are reassigned, and nothing else
    # references a menu_item id, so there is nothing to preserve.
    async with db.pool().acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "DELETE FROM menu_items WHERE tenant_id = $1 AND menu_id = $2",
                user.tenant_id, menu_id,
            )
            new_ids: list[int] = []
            for node, parent_index, order in flat:
                row = await conn.fetchrow(
                    """INSERT INTO menu_items
                           (tenant_id, menu_id, parent_id, label, link_type, url,
                            object_id, target, rel, icon, sort_order, is_active)
                       VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
                       RETURNING id""",
                    user.tenant_id, menu_id,
                    new_ids[parent_index] if parent_index is not None else None,
                    collapse(node.label, 120) or "Untitled",
                    node.link_type,
                    _menu_url(node),
                    node.object_id if node.link_type != "custom" else None,
                    node.target, collapse(node.rel, 80), collapse(node.icon, 40),
                    order, node.is_active,
                )
                new_ids.append(row["id"])

            if payload.name or payload.location:
                await conn.execute(
                    """UPDATE menus SET name = coalesce($3, name),
                                        location = coalesce($4, location),
                                        updated_at = now()
                        WHERE tenant_id = $1 AND id = $2""",
                    user.tenant_id, menu_id,
                    collapse(payload.name, 80), collapse(payload.location, 40),
                )

    await events.log_activity(
        user.tenant_id, "menu.updated", user_id=user.id,
        object_type="menu", object_id=menu_id, meta={"items": len(flat)},
        ip=db.to_inet(client_ip(request)),
    )
    return await menu_detail(menu_id, scoped)


def _menu_url(node: MenuItemInput) -> str | None:
    if node.link_type != "custom":
        return None
    url = collapse(node.url, 500)
    if not url:
        raise HTTPException(400, f"“{node.label}” needs a URL.")
    if not url.startswith(("http://", "https://", "/", "#", "mailto:", "tel:")):
        raise HTTPException(400, f"“{node.label}” has a URL that is not allowed.")
    return url


@router.delete("/menus/{menu_id}")
async def delete_menu(
    menu_id: int, user: CurrentUser = Depends(require_perm("menus.manage"))
) -> dict:
    scoped = db.TenantDB(user.tenant_id)
    removed = await scoped.fetch(
        "DELETE FROM menus WHERE tenant_id = $1 AND id = $2 RETURNING slug::text AS slug",
        menu_id,
    )
    if not removed:
        raise HTTPException(404, "That menu no longer exists.")
    return {"ok": True}


@router.get("/menus/link-targets/list")
async def link_targets(scoped: db.TenantDB = Depends(tenant_db)) -> dict:
    """Everything a menu item can point at, for the builder's picker."""
    content = await scoped.fetch(
        """SELECT i.id, i.title, i.status::text AS status, t.name AS type_name,
                  t.route_prefix, i.slug::text AS slug
             FROM content_items i JOIN content_types t ON t.id = i.type_id
            WHERE i.tenant_id = $1 AND i.status <> 'trashed'
              AND t.route_prefix IS NOT NULL
            ORDER BY t.sort_order, i.title LIMIT 500"""
    )
    for row in content:
        row["path"] = public_path(row.pop("route_prefix"), row["slug"])

    terms = await scoped.fetch(
        """SELECT tm.id, tm.name, tx.name AS taxonomy_name, tx.slug::text AS taxonomy
             FROM terms tm JOIN taxonomies tx ON tx.id = tm.taxonomy_id
            WHERE tm.tenant_id = $1 ORDER BY tx.name, tm.name LIMIT 500"""
    )
    pages = await scoped.fetch(
        """SELECT id, title, slug::text AS slug, status::text AS status
             FROM pages WHERE tenant_id = $1 ORDER BY title LIMIT 200"""
    )
    return {"content": content, "terms": terms, "pages": pages}


# ======================================================= reusable blocks
@router.get("/blocks")
async def list_blocks(scoped: db.TenantDB = Depends(tenant_db)) -> dict:
    rows = await scoped.fetch(
        """SELECT b.id, b.slug::text AS slug, b.name, b.kind, b.content, b.is_active,
                  b.updated_at, u.display_name AS updated_by_name
             FROM reusable_blocks b LEFT JOIN users u ON u.id = b.updated_by
            WHERE b.tenant_id = $1 ORDER BY b.name"""
    )
    return {"blocks": rows}


@router.post("/blocks", status_code=201)
async def create_block(
    payload: BlockCreate, user: CurrentUser = Depends(require_perm("blocks.manage"))
) -> dict:
    scoped = db.TenantDB(user.tenant_id)
    slug = slugify(payload.slug or payload.name, "block")
    if await scoped.fetch_one(
        "SELECT 1 FROM reusable_blocks WHERE tenant_id = $1 AND slug = $2", slug
    ):
        raise HTTPException(400, "A block already uses that name.")

    row = await scoped.fetch_one(
        """INSERT INTO reusable_blocks (tenant_id, slug, name, kind, content, updated_by)
           VALUES ($1, $2, $3, $4, $5::jsonb, $6)
           RETURNING id, slug::text AS slug, name, kind, content, is_active, updated_at""",
        slug, collapse(payload.name, 80), payload.kind,
        _clean_block_content(payload.content), user.id,
    )
    await _sync_block_media(scoped, row["id"], row["content"])
    return {"block": row}


def _clean_block_content(raw: dict | None) -> dict:
    """Blocks hold small JSON. `html` and `body` are rich text and are
    sanitized; everything else is capped scalars."""
    source = raw if isinstance(raw, dict) else {}
    out: dict = {}
    for key, value in list(source.items())[:40]:
        name = collapse(key, 40)
        if not name:
            continue
        if name in {"html", "body"}:
            try:
                out[name] = clean_html(str(value), limit=100_000)
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
        elif isinstance(value, bool) or value is None:
            out[name] = value
        elif isinstance(value, (int, float)):
            out[name] = value
        elif isinstance(value, list):
            out[name] = [collapse(str(v), 300) for v in value[:50]]
        elif isinstance(value, dict):
            out[name] = {
                collapse(k, 40): collapse(str(v), 300) for k, v in list(value.items())[:20]
            }
        else:
            out[name] = collapse(str(value), 2000)
    return out


async def _sync_block_media(scoped: db.TenantDB, block_id: int, content: dict) -> None:
    """Blocks appear on live pages too, so their images count as used."""
    from ..sanitize import referenced_urls  # noqa: PLC0415

    ids: set[int] = set()
    for key, value in (content or {}).items():
        if key.endswith("_media_id") or key.endswith("_id"):
            try:
                ids.add(int(value))
            except (TypeError, ValueError):
                continue
    keys = [
        url.split("/media/", 1)[1].split("?")[0]
        for url in referenced_urls((content or {}).get("html") or (content or {}).get("body"))
        if "/media/" in url
    ]
    if keys:
        for row in await scoped.fetch(
            "SELECT id FROM media WHERE tenant_id = $1 AND storage_key = ANY($2::text[])", keys
        ):
            ids.add(row["id"])

    await scoped.execute(
        "DELETE FROM media_usage WHERE tenant_id = $1 AND object_type = 'block' AND object_id = $2",
        block_id,
    )
    if ids:
        await scoped.execute(
            """INSERT INTO media_usage (tenant_id, media_id, object_type, object_id, field)
               SELECT $1, m.id, 'block', $2, 'content' FROM media m
                WHERE m.tenant_id = $1 AND m.id = ANY($3::bigint[])
               ON CONFLICT DO NOTHING""",
            block_id, sorted(ids),
        )


@router.patch("/blocks/{block_id}")
async def update_block(
    block_id: int,
    payload: BlockUpdate,
    user: CurrentUser = Depends(require_perm("blocks.manage")),
) -> dict:
    scoped = db.TenantDB(user.tenant_id)
    sent = payload.model_dump(exclude_unset=True)
    if not sent:
        raise HTTPException(400, "Nothing to save.")

    row = await scoped.fetch_one(
        """UPDATE reusable_blocks
              SET name    = coalesce($3, name),
                  kind    = coalesce($4, kind),
                  content = CASE WHEN $5 THEN $6::jsonb ELSE content END,
                  is_active = coalesce($7, is_active),
                  updated_by = $8
            WHERE tenant_id = $1 AND id = $2
            RETURNING id, slug::text AS slug, name, kind, content, is_active, updated_at""",
        block_id, collapse(payload.name, 80), payload.kind,
        "content" in sent, _clean_block_content(payload.content),
        payload.is_active, user.id,
    )
    if not row:
        raise HTTPException(404, "That block no longer exists.")
    if "content" in sent:
        await _sync_block_media(scoped, block_id, row["content"])
    return {"block": row}


@router.delete("/blocks/{block_id}")
async def delete_block(
    block_id: int, user: CurrentUser = Depends(require_perm("blocks.manage"))
) -> dict:
    scoped = db.TenantDB(user.tenant_id)
    removed = await scoped.fetch(
        "DELETE FROM reusable_blocks WHERE tenant_id = $1 AND id = $2 RETURNING id", block_id
    )
    if not removed:
        raise HTTPException(404, "That block no longer exists.")
    return {"ok": True}


# ============================================================== settings
@router.get("/settings")
async def get_site_settings(user: CurrentUser = Depends(require_user)) -> dict:
    """Every configuration key, with media ids resolved to URLs."""
    scoped = db.TenantDB(user.tenant_id)
    rows = await scoped.fetch("SELECT key, value, updated_at FROM settings WHERE tenant_id = $1")
    settings_map = {row["key"]: row["value"] for row in rows}

    identity = settings_map.get("site_identity") or {}
    for field in ("logo_media_id", "favicon_media_id"):
        media_id = identity.get(field)
        if media_id:
            media = await scoped.fetch_one(
                "SELECT storage_key FROM media WHERE tenant_id = $1 AND id = $2", media_id
            )
            identity[f"{field.replace('_media_id', '')}_url"] = (
                storage.public_url(media["storage_key"]) if media else None
            )

    return {
        "settings": settings_map,
        "keys": sorted(SITE_SETTING_KEYS),
        "tenant": {"name": user.tenant_name, "slug": user.tenant_slug},
        "storage": storage.describe(),
    }


@router.put("/settings/{key}")
async def put_site_setting(
    key: str,
    payload: dict,
    request: Request,
    user: CurrentUser = Depends(require_perm("settings.manage")),
) -> dict:
    if key not in SITE_SETTING_KEYS:
        raise HTTPException(400, f"Unknown setting. Allowed: {', '.join(sorted(SITE_SETTING_KEYS))}")

    value = payload.get("value", payload)
    _check_setting_size(value)
    if key == "site_identity":
        value = _clean_identity(value)
    if key == "maintenance":
        value = _clean_maintenance(value)

    scoped = db.TenantDB(user.tenant_id)
    await scoped.execute(
        """INSERT INTO settings (tenant_id, key, value) VALUES ($1, $2, $3::jsonb)
           ON CONFLICT (tenant_id, key)
           DO UPDATE SET value = EXCLUDED.value, updated_at = now()""",
        key, value,
    )
    await events.log_activity(
        user.tenant_id, "settings.updated", user_id=user.id,
        meta={"key": key}, ip=db.to_inet(client_ip(request)),
    )
    return {"ok": True, "key": key, "value": value}


def _check_setting_size(value) -> None:
    import json  # noqa: PLC0415

    if len(json.dumps(value, default=str)) > 60_000:
        raise HTTPException(400, "That setting is too large (60 KB limit).")


def _clean_identity(raw) -> dict:
    """Site identity is rendered on the public site; URLs are validated."""
    source = raw if isinstance(raw, dict) else {}
    out: dict = {}
    for key in ("site_name", "tagline", "contact_email", "contact_phone", "whatsapp", "address"):
        out[key] = collapse(source.get(key), 300)
    site_url = collapse(source.get("site_url"), 300)
    if site_url:
        if not site_url.lower().startswith(("https://", "http://")):
            raise HTTPException(400, "The site URL must start with https://")
        out["site_url"] = site_url.rstrip("/")
    for key in ("logo_media_id", "favicon_media_id"):
        try:
            out[key] = int(source[key]) if source.get(key) else None
        except (TypeError, ValueError):
            out[key] = None
    social = source.get("social")
    out["social"] = (
        {
            collapse(k, 30): collapse(v, 300)
            for k, v in list(social.items())[:15]
            if collapse(v, 300) and str(v).lower().startswith("https://")
        }
        if isinstance(social, dict)
        else {}
    )
    return out


def _clean_maintenance(raw) -> dict:
    import ipaddress  # noqa: PLC0415

    source = raw if isinstance(raw, dict) else {}
    allow: list[str] = []
    for entry in (source.get("allow_ips") or [])[:20]:
        try:
            # Accept a single address or a CIDR block.
            allow.append(str(ipaddress.ip_network(str(entry).strip(), strict=False)))
        except ValueError as exc:
            raise HTTPException(400, f"“{entry}” is not a valid IP address or range.") from exc
    return {
        "enabled": bool(source.get("enabled")),
        "message": collapse(source.get("message"), 500),
        "allow_ips": allow,
    }


# ========================================================== public config
@public_router.get("/api/v1/{tenant_slug}/config")
async def public_config(tenant_slug: str, request: Request) -> dict:
    """Everything a static frontend needs, in one call.

    Deliberately excludes SMTP credentials and any setting that is not
    safe to ship to a browser — a static build embeds whatever this
    returns, so the filtering happens here rather than in the frontend.
    """
    tenant = await tenancy.resolve_public(tenant_slug, request.headers.get("host"), request)

    scoped = db.TenantDB(tenant["id"])
    rows = await scoped.fetch("SELECT key, value FROM settings WHERE tenant_id = $1")
    stored = {row["key"]: (row["value"] or {}) for row in rows}

    identity = dict(stored.get("site_identity") or {})
    for field in ("logo_media_id", "favicon_media_id"):
        if identity.get(field):
            media = await scoped.fetch_one(
                "SELECT storage_key FROM media WHERE tenant_id = $1 AND id = $2", identity[field]
            )
            identity[f"{field.replace('_media_id', '')}_url"] = (
                storage.public_url(media["storage_key"]) if media else None
            )

    menus: dict[str, dict] = {}
    for menu in await scoped.fetch(
        "SELECT id, slug::text AS slug, name, location FROM menus WHERE tenant_id = $1"
    ):
        tree = await _menu_tree(scoped, menu["id"], resolve=True)
        menus[menu["slug"]] = {
            "name": menu["name"],
            "location": menu["location"],
            "items": _public_menu(tree),
        }

    blocks = {
        row["slug"]: {"name": row["name"], "kind": row["kind"], "content": row["content"]}
        for row in await scoped.fetch(
            """SELECT slug::text AS slug, name, kind, content
                 FROM reusable_blocks WHERE tenant_id = $1 AND is_active"""
        )
    }

    announcements = await scoped.fetch(
        """SELECT id, name, kind, content, placement, priority, starts_at, ends_at
             FROM announcements
            WHERE tenant_id = $1 AND is_active
              AND (starts_at IS NULL OR starts_at <= now())
              AND (ends_at IS NULL OR ends_at > now())
            ORDER BY priority DESC, id LIMIT 20"""
    )

    types = await scoped.fetch(
        """SELECT slug::text AS slug, name, plural_name, route_prefix, field_schema, supports
             FROM content_types WHERE tenant_id = $1 AND is_active ORDER BY sort_order"""
    )

    maintenance = stored.get("maintenance") or {}
    return {
        "site": {
            "slug": tenant["slug"],
            "name": identity.get("site_name") or tenant["name"],
            "domain": tenant["primary_domain"],
            **{k: v for k, v in identity.items() if k != "site_name"},
        },
        "locale": stored.get("locale") or {},
        "seoDefaults": stored.get("seo_defaults") or {},
        "breadcrumbs": stored.get("breadcrumbs") or {},
        "analytics": stored.get("analytics") or {},
        "cookieConsent": stored.get("cookie_consent") or {},
        # Only the flag and the message — never the allow-list, which
        # would tell a visitor which IPs bypass maintenance mode.
        "maintenance": {
            "enabled": bool(maintenance.get("enabled")),
            "message": maintenance.get("message"),
        },
        "menus": menus,
        "blocks": blocks,
        "announcements": announcements,
        "contentTypes": types,
        "generatedAt": _now(),
    }


def _public_menu(nodes: list[dict]) -> list[dict]:
    """Trim menu nodes to what a frontend renders, dropping inactive and
    broken entries so the live nav never shows a dead link."""
    out = []
    for node in nodes:
        if not node["is_active"] or node.get("isBroken") or node.get("targetUnpublished"):
            continue
        out.append(
            {
                "label": node["label"],
                "url": node.get("resolvedUrl") or node.get("url"),
                "target": node.get("target"),
                "rel": node.get("rel"),
                "icon": node.get("icon"),
                "children": _public_menu(node.get("children") or []),
            }
        )
    return out


def _now() -> str:
    from datetime import datetime, timezone  # noqa: PLC0415

    return datetime.now(timezone.utc).isoformat()


@public_router.get("/api/v1/{tenant_slug}/menus/{menu_slug}")
async def public_menu(tenant_slug: str, menu_slug: str, request: Request) -> dict:
    tenant = await tenancy.resolve_public(tenant_slug, request.headers.get("host"), request)
    scoped = db.TenantDB(tenant["id"])
    menu = await scoped.fetch_one(
        "SELECT id, name, location FROM menus WHERE tenant_id = $1 AND slug = $2",
        collapse(menu_slug, 60),
    )
    if not menu:
        raise HTTPException(404, "Unknown menu.")
    tree = await _menu_tree(scoped, menu["id"], resolve=True)
    return {"name": menu["name"], "location": menu["location"], "items": _public_menu(tree)}
