"""Content model helpers shared by the admin and public content APIs.

The router handles HTTP; this module owns the rules:

* what a content type's ``field_schema`` accepts, and how a submitted
  ``fields`` object is coerced to it;
* what the public API is allowed to see (the published snapshot, never
  the draft columns);
* the URL a published item lives at, which the sitemap, the automatic
  redirects and menu resolution all need to agree on.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timezone
from typing import Any

from fastapi import HTTPException

from . import db
from .sanitize import clean_html, excerpt as html_excerpt, referenced_urls, to_text
from .schemas import collapse, keep_lines

SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
FIELD_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")

MAX_FIELDS = 40
MAX_REVISIONS = 30

# Field types a content type may declare. 'media' stores a media id;
# 'reference' stores another item's id.
FIELD_TYPES = frozenset(
    {
        "text", "textarea", "richtext", "number", "boolean", "date", "datetime",
        "select", "multiselect", "url", "email", "media", "reference", "json",
    }
)

# The five built-in types every new workspace gets, matching the content
# the marketing site actually needs on day one.
BUILTIN_TYPES: tuple[dict, ...] = (
    {
        "slug": "page", "name": "Page", "plural_name": "Pages", "kind": "collection",
        "route_prefix": "/", "icon": "page",
        "supports": {"seo": True, "revisions": True, "body": True, "excerpt": True},
        "field_schema": [
            {"name": "hero_subtitle", "label": "Hero subtitle", "type": "text", "max": 200},
        ],
    },
    {
        "slug": "post", "name": "Post", "plural_name": "Posts", "kind": "collection",
        "route_prefix": "/blog", "icon": "post",
        "supports": {"seo": True, "revisions": True, "body": True, "excerpt": True,
                     "taxonomies": ["category", "tag"], "author": True},
        "field_schema": [
            {"name": "reading_minutes", "label": "Reading time (min)", "type": "number"},
        ],
    },
    {
        "slug": "product", "name": "Product", "plural_name": "Products",
        "kind": "collection", "route_prefix": "/products", "icon": "product",
        "supports": {"seo": True, "revisions": True, "body": True, "excerpt": True,
                     "taxonomies": ["category"]},
        "field_schema": [
            {"name": "sku", "label": "SKU", "type": "text", "max": 60},
            {"name": "price", "label": "Price", "type": "number"},
            {"name": "currency", "label": "Currency", "type": "select",
             "options": ["USD", "EUR", "GBP", "AED", "INR"], "default": "USD"},
            {"name": "in_stock", "label": "In stock", "type": "boolean", "default": True},
            {"name": "gallery", "label": "Gallery", "type": "multiselect"},
        ],
    },
    {
        "slug": "case-study", "name": "Case study", "plural_name": "Case studies",
        "kind": "collection", "route_prefix": "/work", "icon": "case",
        "supports": {"seo": True, "revisions": True, "body": True, "excerpt": True,
                     "taxonomies": ["category", "tag"]},
        "field_schema": [
            {"name": "client", "label": "Client", "type": "text", "max": 120,
             "required": True},
            {"name": "industry", "label": "Industry", "type": "text", "max": 80},
            {"name": "outcome", "label": "Headline outcome", "type": "text", "max": 200},
            {"name": "project_url", "label": "Live URL", "type": "url"},
        ],
    },
    {
        "slug": "testimonial", "name": "Testimonial", "plural_name": "Testimonials",
        "kind": "collection", "route_prefix": None, "icon": "quote",
        "supports": {"seo": False, "revisions": True, "body": False, "excerpt": False},
        "field_schema": [
            {"name": "quote", "label": "Quote", "type": "textarea", "max": 800,
             "required": True},
            {"name": "author_name", "label": "Author", "type": "text", "max": 120,
             "required": True},
            {"name": "author_role", "label": "Role / company", "type": "text", "max": 160},
            {"name": "rating", "label": "Rating (1–5)", "type": "number"},
        ],
    },
    {
        "slug": "faq", "name": "FAQ", "plural_name": "FAQs", "kind": "collection",
        "route_prefix": None, "icon": "faq",
        "supports": {"seo": False, "revisions": True, "body": True, "excerpt": False,
                     "taxonomies": ["category"]},
        "field_schema": [
            {"name": "question", "label": "Question", "type": "text", "max": 300,
             "required": True},
        ],
    },
)

BUILTIN_TAXONOMIES: tuple[dict, ...] = (
    {"slug": "category", "name": "Category", "plural_name": "Categories",
     "is_hierarchical": True},
    {"slug": "tag", "name": "Tag", "plural_name": "Tags", "is_hierarchical": False},
)


# ------------------------------------------------------------------- slugs
def slugify(value: str, fallback: str = "item") -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")
    return (cleaned or fallback)[:80].strip("-") or fallback


def check_slug(value: str) -> str:
    cleaned = (value or "").strip().lower()
    if not SLUG_RE.match(cleaned):
        raise HTTPException(400, "Slugs use lowercase letters, numbers and dashes only.")
    return cleaned


async def unique_slug(scoped: db.TenantDB, type_id: int, wanted: str, exclude_id: int | None = None) -> str:
    """Append -2, -3 … until the slug is free within the type."""
    base = check_slug(wanted)
    candidate = base
    for suffix in range(2, 200):
        taken = await scoped.fetch_one(
            """SELECT 1 FROM content_items
                WHERE tenant_id = $1 AND type_id = $2 AND slug = $3
                  AND ($4::bigint IS NULL OR id <> $4)""",
            type_id, candidate, exclude_id,
        )
        if not taken:
            return candidate
        candidate = f"{base[:76]}-{suffix}"
    raise HTTPException(400, "Could not find a free slug. Choose a different title.")


def public_path(route_prefix: str | None, slug: str) -> str | None:
    """The path the published item lives at on the website.

    A type with no route_prefix is not addressable on its own — FAQs and
    testimonials are pulled into other pages — so it has no path, no
    sitemap entry and no redirect on rename.
    """
    if route_prefix is None:
        return None
    prefix = "/" + (route_prefix or "").strip("/")
    return f"/{slug}" if prefix == "/" else f"{prefix}/{slug}"


# ----------------------------------------------------------- field schema
def clean_field_schema(raw: Any) -> list[dict]:
    """Validate a content type's field definitions."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise HTTPException(400, "Field schema must be a list of field definitions.")
    if len(raw) > MAX_FIELDS:
        raise HTTPException(400, f"A content type can define at most {MAX_FIELDS} fields.")

    cleaned: list[dict] = []
    seen: set[str] = set()

    for entry in raw:
        if not isinstance(entry, dict):
            raise HTTPException(400, "Each field must be an object.")
        name = str(entry.get("name") or "").strip().lower()
        if not FIELD_NAME_RE.match(name):
            raise HTTPException(
                400, f"Field name {name!r} must be lowercase letters, digits and underscores."
            )
        if name in seen:
            raise HTTPException(400, f"Field {name!r} is defined twice.")
        seen.add(name)

        field_type = str(entry.get("type") or "text").strip().lower()
        if field_type not in FIELD_TYPES:
            raise HTTPException(400, f"Unknown field type {field_type!r}.")

        field: dict[str, Any] = {
            "name": name,
            "label": collapse(entry.get("label"), 80) or name.replace("_", " ").title(),
            "type": field_type,
            "required": bool(entry.get("required")),
        }
        if entry.get("help"):
            field["help"] = collapse(entry.get("help"), 200)
        if field_type in {"text", "textarea", "richtext"}:
            field["max"] = min(max(int(entry.get("max") or 200), 1), 100_000)
        if field_type in {"select", "multiselect"}:
            options = entry.get("options")
            if not isinstance(options, list) or not options:
                raise HTTPException(400, f"Field {name!r} needs a non-empty options list.")
            field["options"] = [collapse(str(o), 80) for o in options[:60] if collapse(str(o), 80)]
        if "default" in entry:
            field["default"] = entry["default"]
        cleaned.append(field)

    return cleaned


def clean_supports(raw: Any) -> dict:
    """Which optional capabilities a type turns on."""
    source = raw if isinstance(raw, dict) else {}
    supports = {
        key: bool(source.get(key, key in {"seo", "revisions", "body", "excerpt"}))
        for key in ("seo", "revisions", "body", "excerpt", "author", "featured_media", "menu_order")
    }
    taxonomies = source.get("taxonomies")
    if isinstance(taxonomies, list):
        supports["taxonomies"] = [
            check_slug(str(t)) for t in taxonomies[:10] if str(t).strip()
        ]
    return supports


def coerce_fields(schema: list[dict], submitted: Any) -> dict:
    """Coerce a submitted ``fields`` object to the type's schema.

    Unknown keys are dropped rather than rejected: a type whose schema
    loses a field should not make every existing item unsaveable.
    """
    source = submitted if isinstance(submitted, dict) else {}
    out: dict[str, Any] = {}
    missing: list[str] = []

    for field in schema:
        name, kind = field["name"], field["type"]
        raw = source.get(name, field.get("default"))
        value = _coerce_one(kind, raw, field)

        if field.get("required") and value in (None, "", [], {}):
            missing.append(field.get("label") or name)
        if value is not None:
            out[name] = value

    if missing:
        raise HTTPException(400, f"Please fill in: {', '.join(missing)}")
    return out


def _coerce_one(kind: str, raw: Any, field: dict) -> Any:
    if raw is None or raw == "":
        return None
    limit = int(field.get("max") or 200)

    if kind == "text":
        return collapse(raw, limit)
    if kind == "textarea":
        return keep_lines(raw, limit)
    if kind == "richtext":
        try:
            return clean_html(str(raw), limit=limit)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
    if kind == "boolean":
        return bool(raw) if not isinstance(raw, str) else raw.strip().lower() in {"1", "true", "yes", "on"}
    if kind == "number":
        try:
            number = float(raw)
        except (TypeError, ValueError) as exc:
            raise HTTPException(400, f"{field.get('label') or field['name']} must be a number.") from exc
        return int(number) if number.is_integer() else number
    if kind in {"date", "datetime"}:
        return _coerce_date(raw, kind, field)
    if kind == "select":
        value = collapse(raw, 80)
        if value and value not in field.get("options", []):
            raise HTTPException(400, f"{field.get('label')} must be one of the listed options.")
        return value
    if kind == "multiselect":
        if not isinstance(raw, list):
            raw = [raw]
        allowed = field.get("options")
        values = [collapse(str(v), 80) for v in raw[:60]]
        values = [v for v in values if v and (allowed is None or v in allowed)]
        return values or None
    if kind in {"url", "email"}:
        value = collapse(raw, 500 if kind == "url" else 254)
        if kind == "url" and value and not re.match(r"^(https?://|/)", value, re.IGNORECASE):
            raise HTTPException(400, f"{field.get('label')} must be an https:// URL or a path.")
        return value
    if kind in {"media", "reference"}:
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None
    if kind == "json":
        return raw if isinstance(raw, (dict, list)) else None
    return None


def _coerce_date(raw: Any, kind: str, field: dict) -> str | None:
    text = str(raw).strip()
    if not text:
        return None
    try:
        if kind == "date":
            return date.fromisoformat(text[:10]).isoformat()
        return datetime.fromisoformat(text.replace("Z", "+00:00")).isoformat()
    except ValueError as exc:
        raise HTTPException(
            400, f"{field.get('label') or field['name']} must be an ISO-8601 {kind}."
        ) from exc


# -------------------------------------------------------------------- SEO
SEO_TEXT_LIMITS = {
    "meta_title": 80,
    "meta_description": 320,
    "og_title": 120,
    "og_description": 320,
    "twitter_title": 120,
    "twitter_description": 320,
    "focus_keyword": 80,
    "canonical": 500,
    "og_image_alt": 200,
}

# Google truncates around these; the editor shows a counter against them.
SEO_TARGETS = {"meta_title": (30, 60), "meta_description": (70, 160)}

TWITTER_CARDS = frozenset({"summary", "summary_large_image", "app", "player"})
OG_TYPES = frozenset({"website", "article", "product", "profile", "video.other"})


def clean_seo(raw: Any) -> dict:
    """Validate the per-item SEO block."""
    source = raw if isinstance(raw, dict) else {}
    seo: dict[str, Any] = {}

    for key, limit in SEO_TEXT_LIMITS.items():
        value = collapse(source.get(key), limit)
        if value:
            seo[key] = value

    if seo.get("canonical") and not re.match(r"^(https?://|/)", seo["canonical"], re.IGNORECASE):
        raise HTTPException(400, "The canonical URL must be an https:// URL or a path.")

    for key in ("noindex", "nofollow"):
        if source.get(key):
            seo[key] = True

    og_type = collapse(source.get("og_type"), 40)
    if og_type:
        if og_type not in OG_TYPES:
            raise HTTPException(400, f"og_type must be one of: {', '.join(sorted(OG_TYPES))}")
        seo["og_type"] = og_type

    card = collapse(source.get("twitter_card"), 40)
    if card:
        if card not in TWITTER_CARDS:
            raise HTTPException(400, f"twitter_card must be one of: {', '.join(sorted(TWITTER_CARDS))}")
        seo["twitter_card"] = card

    for key in ("og_image_id", "twitter_image_id"):
        if source.get(key) is not None:
            try:
                seo[key] = int(source[key])
            except (TypeError, ValueError):
                pass

    # Schema.org JSON-LD, stored as given and emitted verbatim by the
    # frontend. Cap the size so it cannot be used as a blob store.
    schema_org = source.get("schema_org")
    if isinstance(schema_org, (dict, list)):
        import json  # noqa: PLC0415

        if len(json.dumps(schema_org)) > 20_000:
            raise HTTPException(400, "The Schema.org block is too large (20 KB limit).")
        seo["schema_org"] = schema_org

    return seo


def seo_report(item: dict) -> dict:
    """Character counts and checks the editor renders as indicators."""
    seo = item.get("seo") or {}
    title = seo.get("meta_title") or item.get("title") or ""
    description = seo.get("meta_description") or item.get("excerpt") or ""
    body_text = to_text(item.get("body"))
    keyword = (seo.get("focus_keyword") or "").strip().lower()

    checks = []
    for key, value in (("meta_title", title), ("meta_description", description)):
        low, high = SEO_TARGETS[key]
        checks.append(
            {
                "field": key,
                "length": len(value),
                "min": low,
                "max": high,
                "state": "ok" if low <= len(value) <= high else ("short" if len(value) < low else "long"),
            }
        )

    haystack = f"{title} {description} {body_text}".lower()
    return {
        "checks": checks,
        "wordCount": len(body_text.split()),
        "hasFocusKeyword": bool(keyword),
        "keywordInTitle": bool(keyword) and keyword in title.lower(),
        "keywordInDescription": bool(keyword) and keyword in description.lower(),
        "keywordInBody": bool(keyword) and keyword in haystack,
        "noindex": bool(seo.get("noindex")),
        "hasOpenGraph": bool(seo.get("og_title") or seo.get("og_description") or seo.get("og_image_id")),
        "hasSchema": bool(seo.get("schema_org")),
    }


# -------------------------------------------------------------- snapshots
def build_snapshot(item: dict, type_row: dict, terms: list[dict]) -> dict:
    """The frozen payload the public content API serves.

    Snapshotting rather than reading the live row is what lets an editor
    keep working on a published page without the change going live.
    """
    return {
        "id": item["id"],
        "type": str(type_row["slug"]),
        "slug": str(item["slug"]),
        "path": public_path(type_row.get("route_prefix"), str(item["slug"])),
        "title": item["title"],
        "excerpt": item.get("excerpt") or html_excerpt(item.get("body")),
        "body": item.get("body"),
        "fields": item.get("fields") or {},
        "seo": item.get("seo") or {},
        "featuredMediaId": item.get("featured_media_id"),
        "authorId": item.get("author_id"),
        "menuOrder": item.get("menu_order", 0),
        "terms": [
            {"id": t["id"], "taxonomy": str(t["taxonomy"]), "slug": str(t["slug"]), "name": t["name"]}
            for t in terms
        ],
        "publishedAt": _iso(item.get("published_at")),
        "updatedAt": _iso(item.get("updated_at")),
    }


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    return str(value)


# ----------------------------------------------------------- housekeeping
async def save_revision(
    scoped: db.TenantDB, item: dict, *, reason: str, user_id: int | None
) -> None:
    """Snapshot the item and prune to MAX_REVISIONS."""
    await scoped.execute(
        """INSERT INTO content_revisions
               (tenant_id, item_id, title, excerpt, body, fields, seo, reason, created_by)
           VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7::jsonb, $8, $9)""",
        item["id"], item["title"], item.get("excerpt"), item.get("body"),
        item.get("fields") or {}, item.get("seo") or {}, reason, user_id,
    )
    await scoped.execute(
        """DELETE FROM content_revisions
            WHERE tenant_id = $1 AND item_id = $2 AND id NOT IN (
              SELECT id FROM content_revisions WHERE item_id = $2
               ORDER BY created_at DESC, id DESC LIMIT $3)""",
        item["id"], MAX_REVISIONS,
    )


async def sync_media_usage(scoped: db.TenantDB, item: dict) -> None:
    """Re-derive which media this item references.

    Usage rows are what makes "this image is used on 3 pages" true, so
    they are rebuilt from the content on every save rather than tracked
    incrementally — an incremental count drifts, and a wrong count means
    a delete either blocks wrongly or breaks a live page.
    """
    ids: set[int] = set()
    if item.get("featured_media_id"):
        ids.add(int(item["featured_media_id"]))

    seo = item.get("seo") or {}
    for key in ("og_image_id", "twitter_image_id"):
        if seo.get(key):
            ids.add(int(seo[key]))

    for value in (item.get("fields") or {}).values():
        if isinstance(value, int) and 0 < value < 2**31:
            ids.add(value)

    # Images inside the HTML body, matched back by their storage key.
    keys = [
        url.split("/media/", 1)[1].split("?")[0]
        for url in referenced_urls(item.get("body"))
        if "/media/" in url
    ]
    if keys:
        rows = await scoped.fetch(
            "SELECT id FROM media WHERE tenant_id = $1 AND storage_key = ANY($2::text[])",
            keys,
        )
        ids.update(row["id"] for row in rows)

    await scoped.execute(
        "DELETE FROM media_usage WHERE tenant_id = $1 AND object_type = 'content_item' AND object_id = $2",
        item["id"],
    )
    if not ids:
        return
    # Only ids that exist for this tenant — a stale id in `fields` must
    # not violate the FK and fail the save.
    await scoped.execute(
        """INSERT INTO media_usage (tenant_id, media_id, object_type, object_id, field)
           SELECT $1, m.id, 'content_item', $2, 'content'
             FROM media m WHERE m.tenant_id = $1 AND m.id = ANY($3::bigint[])
           ON CONFLICT DO NOTHING""",
        item["id"], sorted(ids),
    )
