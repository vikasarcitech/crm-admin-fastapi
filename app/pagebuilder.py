"""Page builder: block validation and server-side HTML rendering.

Pages are stored as a list of typed JSON blocks, never as HTML. Input is
cleaned against a per-type field spec on write, and every value is
escaped again at render time, so a page author cannot become a stored-XSS
vector for visitors. A raw-HTML block is deliberately not a feature.
"""

from __future__ import annotations

import html
import re
from typing import Any, Callable

from fastapi import HTTPException

from .schemas import collapse, keep_lines

MAX_BLOCKS = 80
MAX_FEATURE_ITEMS = 12
# Repeating items inside one block — pricing plans, gallery images, FAQ
# entries. Enough for any real section; a page that needs more needs
# another section.
MAX_ITEMS = 24

HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{3,8}$")
# javascript:, data: and friends never survive validation.
SAFE_HREF = re.compile(r"^(https://|http://|mailto:|tel:|/|#)", re.IGNORECASE)
# http images would be mixed content on an https page.
SAFE_IMAGE = re.compile(r"^(https://|/)", re.IGNORECASE)
FIELD_NAME = re.compile(r"^[a-z0-9_]{1,40}$")

# System font stacks only: the page CSP names no stylesheet host, so a
# web font cannot load, and these render instantly everywhere. The last
# three exist to give headings a voice of their own.
FONT_STACKS = {
    "system": "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif",
    "serif": "Georgia, 'Iowan Old Style', 'Times New Roman', serif",
    "mono": "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace",
    "humanist": "Seravek, 'Gill Sans Nova', Ubuntu, Calibri, 'DejaVu Sans', 'Trebuchet MS', sans-serif",
    "rounded": "ui-rounded, 'Hiragino Maru Gothic ProN', Quicksand, Comfortaa, 'Arial Rounded MT Bold', Calibri, sans-serif",
    "display": "'Iowan Old Style', 'Palatino Linotype', 'Book Antiqua', Palatino, 'URW Palladio L', P052, serif",
}
# Content column widths, and the side gutter every one of them keeps.
# The gutter is what stops text touching the screen edge on a phone; it
# is the only horizontal padding a page has, so it is small and shared.
MAX_WIDTHS = {"narrow": "900px", "normal": "1320px", "wide": "1560px"}
PAGE_GUTTER = "8px"
SPACER_SIZES = {"small": "20px", "medium": "48px", "large": "96px"}
RADII = {"sharp": "0px", "soft": "6px", "round": "14px"}

DEFAULT_THEME = {
    "primary": "#0b6e5a",
    "secondary": "#d9822b",
    "background": "#ffffff",
    "text": "#1c2422",
    "font": "system",
    "heading_font": "same",
    "radius": "soft",
    "max_width": "normal",
    # Whether the site-wide header and footer wrap this page. "site" is
    # the default every page gets; "none" is a landing page that wants
    # nothing above its hero.
    "chrome": "site",
}

# Per-block design: the layer that turns a list of content blocks into
# a designed page. Every block can carry one; none has to.
DESIGN_BG = ("none", "tint", "primary", "secondary", "dark", "custom", "image")
DESIGN_PADDING = ("none", "small", "medium", "large")
DESIGN_WIDTH = ("content", "wide", "full")
DESIGN_ANIMATE = ("none", "fade", "rise")
DESIGN_HIDE = ("none", "mobile", "desktop")
ANCHOR_ID = re.compile(r"^[a-zA-Z][\w-]{0,60}$")
CSS_CLASSES = re.compile(r"^[\w\s-]{1,200}$")

# Rendered when a form block points at a form with no field definition.
DEFAULT_FORM_FIELDS = [
    {"name": "full_name", "label": "Name", "type": "text", "required": True},
    {"name": "email", "label": "Email", "type": "email", "required": True},
    {"name": "message", "label": "Message", "type": "textarea"},
]


# ------------------------------------------------------------ validation
def _line(block: dict, key: str, limit: int) -> str | None:
    value = block.get(key)
    return collapse(value, limit) if isinstance(value, str) else None


def _text(block: dict, key: str, limit: int) -> str | None:
    value = block.get(key)
    return keep_lines(value, limit) if isinstance(value, str) else None


def _href(block: dict, key: str) -> str | None:
    cleaned = _line(block, key, 500)
    if not cleaned:
        return None
    if not SAFE_HREF.match(cleaned):
        raise HTTPException(400, "Links must be https, mailto, tel, an anchor or a path.")
    return cleaned


def _image_src(block: dict, key: str) -> str | None:
    cleaned = _line(block, key, 500)
    if not cleaned:
        return None
    if not SAFE_IMAGE.match(cleaned):
        raise HTTPException(400, "Image URLs must start with https:// or /.")
    return cleaned


def _choice(block: dict, key: str, allowed: tuple[str, ...], default: str) -> str:
    value = block.get(key)
    return value if value in allowed else default


def _flag(block: dict, key: str, default: bool = False) -> bool:
    value = block.get(key)
    return bool(value) if isinstance(value, bool) else default


def _int(block: dict, key: str, lo: int, hi: int, default: int) -> int:
    value = block.get(key)
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number if lo <= number <= hi else default


def _hex(block: dict, key: str) -> str | None:
    value = block.get(key)
    if isinstance(value, str) and HEX_COLOR.match(value.strip()):
        return value.strip()
    return None


def _items(block: dict, key: str, clean_item: Callable[[dict], dict | None],
           limit: int = MAX_ITEMS) -> list[dict]:
    """Clean a block's repeating items. An item the cleaner rejects (no
    title, no image) is dropped rather than failing the whole block: the
    author sees it missing in the preview, which is the message."""
    raw = block.get(key)
    out: list[dict] = []
    for item in (raw if isinstance(raw, list) else [])[:limit]:
        if isinstance(item, dict):
            cleaned = clean_item(item)
            if cleaned:
                out.append(cleaned)
    return out


def _lines(block: dict, key: str, limit: int, max_lines: int = 40) -> list[str]:
    """One entry per non-empty line of a text field."""
    text = _text(block, key, limit) or ""
    return [line.strip() for line in text.split("\n") if line.strip()][:max_lines]


def _link_lines(block: dict, key: str) -> list[dict]:
    """`Label | /path` per line -> [{label, href}]; a bad href drops the line."""
    links = []
    for line in _lines(block, key, 4000):
        label, _, href = line.partition("|")
        label, href = label.strip(), href.strip()
        if label and href and SAFE_HREF.match(href):
            links.append({"label": label[:80], "href": href[:500]})
    return links


def clean_design(block: dict) -> dict | None:
    """The block's design settings, or None when all of them are default.

    None rather than a dict of defaults so a page that never touched the
    design panel stores exactly what it stored before, and renders to
    exactly the markup it rendered before.
    """
    raw = block.get("design")
    if not isinstance(raw, dict):
        return None
    bg = _choice(raw, "bg", DESIGN_BG, "none")
    anchor = _line(raw, "anchor", 60)
    css_class = _line(raw, "css_class", 200)
    design = {
        "bg": bg,
        "bg_color": _hex(raw, "bg_color") if bg == "custom" else None,
        "bg_image": _image_src(raw, "bg_image") if bg == "image" else None,
        "text_color": _hex(raw, "text_color"),
        "padding": _choice(raw, "padding", DESIGN_PADDING, "none"),
        "width": _choice(raw, "width", DESIGN_WIDTH, "content"),
        "anchor": anchor if anchor and ANCHOR_ID.match(anchor) else None,
        "css_class": css_class if css_class and CSS_CLASSES.match(css_class) else None,
        "animate": _choice(raw, "animate", DESIGN_ANIMATE, "none"),
        "hide_on": _choice(raw, "hide_on", DESIGN_HIDE, "none"),
    }
    if design["bg"] == "custom" and not design["bg_color"]:
        design["bg"] = "none"
    if design["bg"] == "image" and not design["bg_image"]:
        design["bg"] = "none"
    untouched = (
        design["bg"] == "none" and not design["text_color"]
        and design["padding"] == "none" and design["width"] == "content"
        and not design["anchor"] and not design["css_class"]
        and design["animate"] == "none" and design["hide_on"] == "none"
    )
    return None if untouched else design


def _clean_hero(b: dict) -> dict:
    return {
        "eyebrow": _line(b, "eyebrow", 80),
        "heading": _line(b, "heading", 160) or "Untitled",
        "sub": _text(b, "sub", 400),
        "button_label": _line(b, "button_label", 60),
        "button_href": _href(b, "button_href"),
        "button2_label": _line(b, "button2_label", 60),
        "button2_href": _href(b, "button2_href"),
        # A background image makes it a cover hero: full bleed, white
        # text over a darkened photo.
        "image": _image_src(b, "image"),
        "size": _choice(b, "size", ("normal", "tall", "screen"), "normal"),
        "align": _choice(b, "align", ("left", "center"), "center"),
    }


def _clean_heading(b: dict) -> dict:
    level = b.get("level")
    return {
        "eyebrow": _line(b, "eyebrow", 80),
        "text": _line(b, "text", 160) or "Heading",
        "sub": _text(b, "sub", 300),
        "level": level if level in (2, 3, 4) else 2,
        "align": _choice(b, "align", ("left", "center"), "left"),
    }


def _clean_text(b: dict) -> dict:
    return {"body": _text(b, "body", 40000) or ""}  # room for blog-length posts


def _clean_image(b: dict) -> dict:
    src = _image_src(b, "src")
    if not src:
        raise HTTPException(400, "Image blocks need an image URL.")
    return {
        "src": src,
        "alt": _line(b, "alt", 200) or "",
        "caption": _line(b, "caption", 200),
        "href": _href(b, "href"),
        "width": _choice(b, "width", ("full", "medium", "small"), "full"),
        "align": _choice(b, "align", ("center", "left", "right"), "center"),
    }


def _clean_button(b: dict) -> dict:
    return {
        "label": _line(b, "label", 60) or "Learn more",
        "href": _href(b, "href") or "#",
        "align": _choice(b, "align", ("left", "center"), "center"),
        "variant": _choice(b, "variant", ("solid", "outline", "secondary", "link"), "solid"),
        "size": _choice(b, "size", ("normal", "large"), "normal"),
        "new_tab": _flag(b, "new_tab"),
    }


def _clean_features(b: dict) -> dict:
    def item(i: dict) -> dict | None:
        title = _line(i, "title", 120)
        if not title:
            return None
        return {
            "title": title,
            "body": _text(i, "body", 500),
            # An emoji or a short glyph; there is no icon font to load.
            "icon": _line(i, "icon", 8),
            "href": _href(i, "href"),
        }

    return {
        "heading": _line(b, "heading", 160),
        "sub": _text(b, "sub", 300),
        "columns": _int(b, "columns", 2, 4, 3),
        "style": _choice(b, "style", ("cards", "plain", "icons"), "cards"),
        "items": _items(b, "items", item, MAX_FEATURE_ITEMS),
    }


def _clean_quote(b: dict) -> dict:
    return {"body": _text(b, "body", 1000) or "", "attribution": _line(b, "attribution", 120)}


def _clean_form(b: dict) -> dict:
    slug = _line(b, "form_slug", 60)
    if not slug or not re.match(r"^[a-z0-9-]{1,60}$", slug, re.IGNORECASE):
        raise HTTPException(400, "Form blocks need the slug of one of your forms.")
    return {
        "form_slug": slug.lower(),
        "heading": _line(b, "heading", 160),
        "button_label": _line(b, "button_label", 60) or "Send",
    }


# The one block that stores markup rather than data. Every other block
# is escaped at render time, which is what makes stored XSS impossible
# here — so this one is sanitized at *write* time instead, through the
# same allow-list cleaner content_items.body uses (app/sanitize.py).
# Unlike the other callers it keeps <style> blocks and inline styles:
# a page written as HTML has to be able to look like something.
# Sanitizing on write means a later change to this renderer cannot
# start emitting something unsafe that was stored earlier.
# Raised from 60k with the allow-list: a page written entirely as HTML
# now legitimately carries its own <style> block, a form, and inline
# data: images, and 60k was small enough that a real page hit it.
MAX_HTML_CHARS = 150_000
# A whole document carries its own CSS and scripts inline, so it is a
# different size of thing from a fragment.
MAX_DOCUMENT_CHARS = 500_000

# A document, not a fragment: it *starts* with the doctype or <html>.
# Anchored on purpose — a fragment that merely mentions "<html" in its
# text is a fragment, and the difference decides whether the markup is
# served byte-for-byte or run through the allow-list.
_DOCUMENT_START = re.compile(r"(?is)\A\s*(?:<!doctype\s+html|<html[\s>])")


def is_document(markup: str | None) -> bool:
    """True when this markup is a whole HTML document."""
    return bool(markup) and bool(_DOCUMENT_START.match(str(markup)))


# A pasted page brings its <head> with it. Everything in there is
# dropped by the allow-list except <title>, which is legal in a body and
# would quietly become a second document title — so the head's title
# goes before the cleaner runs. Scoped to a real <head>, so the <title>
# that gives an inline SVG its accessible name is untouched.
_DOC_HEAD = re.compile(r"(?is)<head\b[^>]*>.*?</head>")
_DOC_TITLE = re.compile(r"(?is)<title\b[^>]*>.*?</title>")


def prepare_block_html(raw: str | None, *, allow_document: bool = False) -> tuple[str, bool]:
    """Markup for an HTML block, and whether it is a whole document.

    A document is stored **verbatim** — `<html>` to `</html>`, head,
    scripts and all — because that is the point of it: the page is that
    file, and a sanitizer that rewrote it would be answering a question
    nobody asked. Nothing else on the platform does this, and it takes
    the `pages.raw_html` permission, which no role below owner has by
    default: a page is served from the same origin as this API, so a
    script on one runs with the session of whoever opens it.

    Everything else — every fragment, every block on a normal page —
    goes through the allow-list exactly as before.
    """
    text = str(raw or "")
    if allow_document and is_document(text):
        if len(text) > MAX_DOCUMENT_CHARS:
            raise HTTPException(
                400, f"This document is too large (limit {MAX_DOCUMENT_CHARS:,} characters)."
            )
        return text.strip(), True
    return clean_block_html(text), False


def clean_block_html(raw: str | None) -> str:
    """Clean markup for an HTML block — the only way page markup is cleaned.

    The block validator, the editor's live dry run and the blocks → HTML
    conversion all come through here, so what the editor promises, what
    the switch stores and what the save writes cannot disagree. They did:
    the dry run ran without ``allow_stylesheet`` and told authors their
    <style> block was about to be deleted when the save was keeping it.
    """
    from .sanitize import clean_html  # noqa: PLC0415 — sanitize imports config

    text = _DOC_HEAD.sub(lambda m: _DOC_TITLE.sub("", m.group(0)), str(raw or ""))
    try:
        # A page's HTML block may carry a <style> block: it is the one
        # place where the fragment is meant to be the whole page.
        cleaned = clean_html(text, limit=MAX_HTML_CHARS, allow_stylesheet=True)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return (cleaned or "").strip()


def _clean_html(b: dict, *, allow_document: bool = False) -> dict:
    markup, document = prepare_block_html(b.get("html"), allow_document=allow_document)
    return {
        "html": markup,
        # Sticky, and checked at render time: a block that says `doc` is
        # served as the page rather than wrapped in it.
        "doc": document,
        # Whether to apply the page's own typography to the markup.
        # Off is the right default for a pasted embed or a third-party
        # widget that ships its own styling.
        "styled": bool(b.get("styled", True)),
        "width": _choice(b, "width", ("normal", "wide", "full"), "normal"),
    }


def _clean_spacer(b: dict) -> dict:
    return {"size": _choice(b, "size", tuple(SPACER_SIZES), "medium")}


def _clean_divider(b: dict) -> dict:
    return {}



# ------------------------------------------------------ layout blocks
def _clean_columns(b: dict) -> dict:
    def item(i: dict) -> dict | None:
        cleaned = {
            "image": _image_src(i, "image"),
            "heading": _line(i, "heading", 120),
            "body": _text(i, "body", 2000),
            "button_label": _line(i, "button_label", 60),
            "button_href": _href(i, "button_href"),
        }
        return cleaned if any(cleaned.values()) else None

    return {
        "count": _int(b, "count", 2, 4, 2),
        "align": _choice(b, "align", ("left", "center"), "left"),
        "gap": _choice(b, "gap", ("small", "medium", "large"), "medium"),
        "items": _items(b, "items", item, 4),
    }


def _clean_media_text(b: dict) -> dict:
    src = _image_src(b, "src")
    if not src:
        raise HTTPException(400, "Image + text blocks need an image URL.")
    return {
        "src": src,
        "alt": _line(b, "alt", 200) or "",
        "eyebrow": _line(b, "eyebrow", 80),
        "heading": _line(b, "heading", 160),
        "body": _text(b, "body", 4000),
        "button_label": _line(b, "button_label", 60),
        "button_href": _href(b, "button_href"),
        "side": _choice(b, "side", ("left", "right"), "left"),
        "ratio": _choice(b, "ratio", ("auto", "square", "wide", "portrait"), "auto"),
        "split": _choice(b, "split", ("half", "third"), "half"),
    }


def _clean_gallery(b: dict) -> dict:
    def item(i: dict) -> dict | None:
        src = _image_src(i, "src")
        if not src:
            return None
        return {
            "src": src,
            "alt": _line(i, "alt", 200) or "",
            "caption": _line(i, "caption", 200),
            "href": _href(i, "href"),
        }

    return {
        "heading": _line(b, "heading", 160),
        "columns": _int(b, "columns", 2, 5, 3),
        "ratio": _choice(b, "ratio", ("square", "landscape", "portrait", "natural"), "square"),
        "gap": _choice(b, "gap", ("none", "small", "medium"), "small"),
        # Open the full image in a new tab when nothing else is linked.
        "lightbox": _flag(b, "lightbox", True),
        "items": _items(b, "items", item),
    }


# ---------------------------------------------------- marketing blocks
def _clean_pricing(b: dict) -> dict:
    def item(i: dict) -> dict | None:
        name = _line(i, "name", 80)
        if not name:
            return None
        return {
            "name": name,
            "price": _line(i, "price", 40) or "",
            "period": _line(i, "period", 40),
            "description": _text(i, "description", 300),
            "features": _lines(i, "features", 2000, 20),
            "button_label": _line(i, "button_label", 60),
            "button_href": _href(i, "button_href"),
            "featured": _flag(i, "featured"),
            "badge": _line(i, "badge", 30),
        }

    return {
        "heading": _line(b, "heading", 160),
        "sub": _text(b, "sub", 300),
        "items": _items(b, "items", item, 6),
    }


def _clean_testimonials(b: dict) -> dict:
    def item(i: dict) -> dict | None:
        quote = _text(i, "quote", 1000)
        if not quote:
            return None
        return {
            "quote": quote,
            "name": _line(i, "name", 80),
            "role": _line(i, "role", 120),
            "avatar": _image_src(i, "avatar"),
            "rating": _int(i, "rating", 0, 5, 0),
        }

    return {
        "heading": _line(b, "heading", 160),
        "layout": _choice(b, "layout", ("grid", "carousel", "single"), "grid"),
        "items": _items(b, "items", item, 12),
    }


def _clean_team(b: dict) -> dict:
    def item(i: dict) -> dict | None:
        name = _line(i, "name", 80)
        if not name:
            return None
        return {
            "name": name,
            "role": _line(i, "role", 120),
            "photo": _image_src(i, "photo"),
            "bio": _text(i, "bio", 400),
            "href": _href(i, "href"),
        }

    return {
        "heading": _line(b, "heading", 160),
        "sub": _text(b, "sub", 300),
        "columns": _int(b, "columns", 2, 4, 3),
        "items": _items(b, "items", item, 16),
    }


def _clean_faq(b: dict) -> dict:
    def item(i: dict) -> dict | None:
        question = _line(i, "question", 200)
        if not question:
            return None
        return {"question": question, "answer": _text(i, "answer", 2000) or ""}

    return {
        "heading": _line(b, "heading", 160),
        "open_first": _flag(b, "open_first"),
        "items": _items(b, "items", item),
    }


def _clean_cta(b: dict) -> dict:
    return {
        "heading": _line(b, "heading", 160) or "Ready to get started?",
        "body": _text(b, "body", 400),
        "button_label": _line(b, "button_label", 60),
        "button_href": _href(b, "button_href"),
        "button2_label": _line(b, "button2_label", 60),
        "button2_href": _href(b, "button2_href"),
        "style": _choice(b, "style", ("tint", "primary", "dark", "outline"), "primary"),
        "align": _choice(b, "align", ("center", "split"), "center"),
    }


def _clean_stats(b: dict) -> dict:
    def item(i: dict) -> dict | None:
        value = _line(i, "value", 24)
        if not value:
            return None
        return {"value": value, "label": _line(i, "label", 80) or ""}

    return {
        "heading": _line(b, "heading", 160),
        "columns": _int(b, "columns", 2, 4, 4),
        "items": _items(b, "items", item, 8),
    }


def _clean_logos(b: dict) -> dict:
    def item(i: dict) -> dict | None:
        src = _image_src(i, "src")
        if not src:
            return None
        return {"src": src, "alt": _line(i, "alt", 120) or "", "href": _href(i, "href")}

    return {
        "heading": _line(b, "heading", 160),
        "grayscale": _flag(b, "grayscale", True),
        "items": _items(b, "items", item, 12),
    }


def _clean_steps(b: dict) -> dict:
    def item(i: dict) -> dict | None:
        title = _line(i, "title", 120)
        if not title:
            return None
        return {"title": title, "body": _text(i, "body", 600)}

    return {
        "heading": _line(b, "heading", 160),
        "sub": _text(b, "sub", 300),
        "layout": _choice(b, "layout", ("row", "list"), "row"),
        "items": _items(b, "items", item, 8),
    }


def _clean_notice(b: dict) -> dict:
    return {
        "text": _line(b, "text", 200) or "Announcement",
        "link_label": _line(b, "link_label", 40),
        "href": _href(b, "href"),
        "style": _choice(b, "style", ("primary", "secondary", "dark", "tint"), "primary"),
    }


# -------------------------------------------------------- media blocks
_YOUTUBE_ID = re.compile(
    r"(?:youtube(?:-nocookie)?\.com/(?:watch\?(?:.*&)?v=|embed/|shorts/|live/)|youtu\.be/)"
    r"([A-Za-z0-9_-]{6,20})"
)
_VIMEO_ID = re.compile(r"vimeo\.com/(?:video/)?(\d{6,12})")


def _video_embed(url: str) -> str | None:
    """A YouTube or Vimeo page URL -> the embed URL we are willing to frame.

    Built, not passed through: the author gives the address they copied
    from the browser and the renderer emits the one player URL for it,
    so the frame allow-list never sees anything but those two hosts.
    """
    match = _YOUTUBE_ID.search(url)
    if match:
        return f"https://www.youtube-nocookie.com/embed/{match.group(1)}"
    match = _VIMEO_ID.search(url)
    if match:
        return f"https://player.vimeo.com/video/{match.group(1)}"
    return None


def _clean_video(b: dict) -> dict:
    url = _line(b, "url", 500) or ""
    embed = _video_embed(url)
    if not embed:
        raise HTTPException(400, "Video blocks take a YouTube or Vimeo link.")
    return {
        "url": url,
        "embed": embed,
        "caption": _line(b, "caption", 200),
        "ratio": _choice(b, "ratio", ("16:9", "4:3", "1:1", "9:16"), "16:9"),
        "width": _choice(b, "width", ("full", "medium"), "full"),
    }


def _clean_map(b: dict) -> dict:
    address = _line(b, "address", 300)
    if not address:
        raise HTTPException(400, "Map blocks need an address or place name.")
    return {
        "address": address,
        "zoom": _int(b, "zoom", 3, 20, 15),
        "height": _choice(b, "height", ("short", "medium", "tall"), "medium"),
        "caption": _line(b, "caption", 200),
    }


# --------------------------------------------------------- site blocks
def _clean_header(b: dict) -> dict:
    def link(i: dict) -> dict | None:
        label, href = _line(i, "label", 60), _href(i, "href")
        return {"label": label, "href": href} if label and href else None

    return {
        "logo_src": _image_src(b, "logo_src"),
        "logo_text": _line(b, "logo_text", 60),
        "logo_href": _href(b, "logo_href") or "/",
        "links": _items(b, "links", link, 10),
        "button_label": _line(b, "button_label", 40),
        "button_href": _href(b, "button_href"),
        "sticky": _flag(b, "sticky", True),
        "style": _choice(b, "style", ("plain", "line", "filled"), "line"),
    }


SOCIAL_NETWORKS = (
    "facebook", "instagram", "x", "linkedin", "youtube", "tiktok",
    "github", "whatsapp", "email", "website",
)
SOCIAL_LABELS = {
    "facebook": "Facebook", "instagram": "Instagram", "x": "X", "linkedin": "LinkedIn",
    "youtube": "YouTube", "tiktok": "TikTok", "github": "GitHub", "whatsapp": "WhatsApp",
    "email": "Email", "website": "Website",
}


def _clean_social_items(b: dict, key: str = "items") -> list[dict]:
    def item(i: dict) -> dict | None:
        network = _choice(i, "network", SOCIAL_NETWORKS, "")
        href = _href(i, "href")
        return {"network": network, "href": href} if network and href else None

    return _items(b, key, item, len(SOCIAL_NETWORKS))


def _clean_footer(b: dict) -> dict:
    def column(i: dict) -> dict | None:
        heading = _line(i, "heading", 60)
        links = _link_lines(i, "links")
        return {"heading": heading, "links": links} if heading or links else None

    return {
        "brand": _line(b, "brand", 80),
        "tagline": _text(b, "tagline", 300),
        "columns": _items(b, "columns", column, 4),
        "social": _clean_social_items(b, "social"),
        "copyright": _line(b, "copyright", 200),
        "style": _choice(b, "style", ("plain", "tint", "dark"), "tint"),
    }


def _clean_contact(b: dict) -> dict:
    email = _line(b, "email", 254)
    if email and "@" not in email:
        raise HTTPException(400, "That does not look like an email address.")
    return {
        "heading": _line(b, "heading", 160),
        "address": _text(b, "address", 300),
        "phone": _line(b, "phone", 40),
        "email": email,
        "hours": _text(b, "hours", 300),
        "show_map": _flag(b, "show_map"),
        "map_zoom": _int(b, "map_zoom", 3, 20, 15),
    }


def _clean_social(b: dict) -> dict:
    return {
        "heading": _line(b, "heading", 120),
        "align": _choice(b, "align", ("left", "center"), "center"),
        "style": _choice(b, "style", ("pills", "text"), "pills"),
        "items": _clean_social_items(b),
    }


def _clean_table(b: dict) -> dict:
    rows: list[list[str]] = []
    for line in _lines(b, "rows", 20000, 60):
        cells = [cell.strip()[:200] for cell in line.split("|")]
        if any(cells):
            rows.append(cells[:12])
    return {
        "caption": _line(b, "caption", 160),
        # Kept as the author typed it, so the drawer can show it back.
        "rows": _text(b, "rows", 20000) or "",
        "header": _flag(b, "header", True),
        "striped": _flag(b, "striped", True),
        "compact": _flag(b, "compact"),
        "table": rows,
    }

CLEANERS: dict[str, Callable[[dict], dict]] = {
    "hero": _clean_hero,
    "heading": _clean_heading,
    "text": _clean_text,
    "image": _clean_image,
    "button": _clean_button,
    "features": _clean_features,
    "quote": _clean_quote,
    "form": _clean_form,
    "html": _clean_html,
    "spacer": _clean_spacer,
    "divider": _clean_divider,
    # Layout
    "columns": _clean_columns,
    "media_text": _clean_media_text,
    "gallery": _clean_gallery,
    # Marketing
    "pricing": _clean_pricing,
    "testimonials": _clean_testimonials,
    "team": _clean_team,
    "faq": _clean_faq,
    "cta": _clean_cta,
    "stats": _clean_stats,
    "logos": _clean_logos,
    "steps": _clean_steps,
    "notice": _clean_notice,
    # Media
    "video": _clean_video,
    "map": _clean_map,
    # Site
    "header": _clean_header,
    "footer": _clean_footer,
    "contact": _clean_contact,
    "social": _clean_social,
    "table": _clean_table,
}


def clean_blocks(raw: Any, *, allow_document: bool = False) -> list[dict]:
    """Validate a client-supplied block list into canonical stored form.

    ``allow_document`` lets a *single* HTML block be a whole document
    (see :func:`prepare_block_html`). Single on purpose: a document is
    the page, so there is nothing for a second block to be.
    """
    if not isinstance(raw, list):
        raise HTTPException(400, "Blocks must be a list.")
    if len(raw) > MAX_BLOCKS:
        raise HTTPException(400, f"A page can hold at most {MAX_BLOCKS} blocks.")

    cleaned: list[dict] = []
    for block in raw:
        if not isinstance(block, dict):
            raise HTTPException(400, "Each block must be an object.")
        # The html cleaner is the only one that takes an argument, and
        # it is spelled out here rather than threaded through every
        # other cleaner's signature for one caller.
        if block.get("type") == "html":
            entry = {
                "type": "html",
                **_clean_html(block, allow_document=allow_document and len(raw) == 1),
            }
        else:
            cleaner = CLEANERS.get(block.get("type"))
            if cleaner is None:
                raise HTTPException(400, "That block type is not supported.")
            entry = {"type": block["type"], **cleaner(block)}
        # The design layer is the same for every block, so it is applied
        # here rather than by thirty cleaners.
        design = clean_design(block)
        if design:
            entry["design"] = design
        cleaned.append(entry)
    return cleaned


def clean_theme(raw: Any) -> dict:
    """Whitelisted theme keys only; colours must be hex literals."""
    source = raw if isinstance(raw, dict) else {}
    theme = dict(DEFAULT_THEME)
    for key in ("primary", "secondary", "background", "text"):
        value = source.get(key)
        if isinstance(value, str) and HEX_COLOR.match(value.strip()):
            theme[key] = value.strip()
    if source.get("font") in FONT_STACKS:
        theme["font"] = source["font"]
    if source.get("heading_font") in FONT_STACKS or source.get("heading_font") == "same":
        theme["heading_font"] = source["heading_font"]
    if source.get("radius") in RADII:
        theme["radius"] = source["radius"]
    if source.get("max_width") in MAX_WIDTHS:
        theme["max_width"] = source["max_width"]
    if source.get("chrome") in ("site", "none"):
        theme["chrome"] = source["chrome"]
    return theme


# ------------------------------------------------------------ site chrome
def clean_site_chrome(raw: Any) -> dict:
    """The site-wide header and footer, validated as the blocks they are.

    Stored once per site (settings key ``site_chrome``) and put around
    every page at render time. They are ordinary header/footer blocks —
    same cleaner, same renderer, same drawer in the editor — so there is
    exactly one definition of what a header is.
    """
    source = raw if isinstance(raw, dict) else {}
    chrome: dict[str, Any] = {"enabled": bool(source.get("enabled", True)), "header": None, "footer": None}
    for part in ("header", "footer"):
        block = source.get(part)
        if isinstance(block, dict) and block.get("type", part) == part:
            chrome[part] = clean_blocks([{**block, "type": part}])[0]
    return chrome


def with_site_chrome(blocks: list[dict], chrome: dict | None, theme: dict | None) -> list[dict]:
    """The page's blocks with the site header first and footer last.

    Skipped when the site has none, when the page's theme says "none",
    or when the page already carries a block of that kind — an author
    who put their own header on a page meant that one.
    """
    if not chrome or not chrome.get("enabled"):
        return blocks
    if (theme or {}).get("chrome", "site") == "none":
        return blocks
    kinds = {b.get("type") for b in blocks if isinstance(b, dict)}
    out = list(blocks)
    if chrome.get("header") and "header" not in kinds:
        out.insert(0, chrome["header"])
    if chrome.get("footer") and "footer" not in kinds:
        out.append(chrome["footer"])
    return out


def clean_page_seo(raw: Any) -> dict:
    """Validate a page's SEO block.

    Reuses the content module's cleaner so a page's meta behaves exactly
    like a content item's — same keys, same limits, same canonical-URL
    and og_type checks. `meta_description` is dropped on the way through:
    a page's description is its own column (published alongside the
    blocks), and a second copy in here could disagree with it.
    """
    from .content import clean_seo  # noqa: PLC0415 — content imports db

    seo = clean_seo(raw)
    seo.pop("meta_description", None)
    return seo


# -------------------------------------------------------------- rendering
def _esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


# ---- markdown subset -----------------------------------------------------
# The source is escaped BEFORE these run, so the only tags that can appear
# in the output are the ones these substitutions emit. Authors write
# **bold**, *italic*, ~~strike~~, `code` and [label](https://link).
_MD_CODE = re.compile(r"`([^`\n]+)`")
_MD_BOLD = re.compile(r"\*\*(.+?)\*\*")
_MD_EM = re.compile(r"(?<![*\w])\*([^*\n]+)\*(?![*\w])")
_MD_STRIKE = re.compile(r"~~(.+?)~~")
_MD_LINK = re.compile(r"\[([^\]\n]+)\]\(([^()\s]+)\)")


def _md_link(match: re.Match) -> str:
    label, href = match.group(1), match.group(2)
    # The href was escaped with the rest of the text; undo that before the
    # scheme check, then re-escape for the attribute.
    raw = html.unescape(href)
    if not SAFE_HREF.match(raw):
        return label  # bad scheme: keep the words, drop the link
    external = raw.lower().startswith(("http://", "https://"))
    rel = ' target="_blank" rel="noopener"' if external else ""
    return f'<a href="{_esc(raw)}"{rel}>{label}</a>'


def _inline(text: str) -> str:
    """Escaped text -> inline HTML.

    Code spans are stashed behind placeholders first so `**x**` inside
    backticks stays literal instead of being re-formatted by later passes.
    """
    out = _esc(text)

    code_spans: list[str] = []

    def _stash(match: re.Match) -> str:
        code_spans.append(f"<code>{match.group(1)}</code>")
        return f"\x00{len(code_spans) - 1}\x00"

    out = _MD_CODE.sub(_stash, out)
    out = _MD_LINK.sub(_md_link, out)
    out = _MD_BOLD.sub(r"<strong>\1</strong>", out)
    out = _MD_EM.sub(r"<em>\1</em>", out)
    out = _MD_STRIKE.sub(r"<s>\1</s>", out)

    for index, span in enumerate(code_spans):
        out = out.replace(f"\x00{index}\x00", span)
    return out


def _paragraphs(body: str) -> str:
    parts = [p.strip() for p in re.split(r"\n{2,}", body) if p.strip()]
    return "".join(
        "<p>" + _inline(p).replace("\n", "<br>") + "</p>" for p in parts
    )


_HEADING_LINE = re.compile(r"^(#{1,3})\s+(.*)$")
_BULLET_LINE = re.compile(r"^[-*]\s+(.*)$")
_NUMBER_LINE = re.compile(r"^\d+\.\s+(.*)$")


def _rich(body: str) -> str:
    """Block-level markdown for text blocks: headings, lists, quotes, rules.

    The page's h1 belongs to the hero, so # / ## / ### map to h2/h3/h4.
    """
    out: list[str] = []
    paragraph: list[str] = []
    list_items: list[str] = []
    list_tag = ""
    quote_lines: list[str] = []

    def flush_paragraph() -> None:
        if paragraph:
            out.append("<p>" + "<br>".join(_inline(line) for line in paragraph) + "</p>")
            paragraph.clear()

    def flush_list() -> None:
        nonlocal list_tag
        if list_items:
            out.append(f"<{list_tag}>" + "".join(list_items) + f"</{list_tag}>")
            list_items.clear()
        list_tag = ""

    def flush_quote() -> None:
        if quote_lines:
            out.append("<blockquote><p>" + "<br>".join(quote_lines) + "</p></blockquote>")
            quote_lines.clear()

    def flush_all() -> None:
        flush_paragraph()
        flush_list()
        flush_quote()

    for line in body.split("\n"):
        stripped = line.strip()
        heading = _HEADING_LINE.match(stripped)
        bullet = _BULLET_LINE.match(stripped)
        number = _NUMBER_LINE.match(stripped)

        if not stripped:
            flush_all()
        elif heading:
            flush_all()
            level = len(heading.group(1)) + 1  # -> h2/h3/h4
            out.append(f"<h{level}>{_inline(heading.group(2))}</h{level}>")
        elif stripped in ("---", "***"):
            flush_all()
            out.append("<hr>")
        elif bullet or number:
            flush_paragraph()
            flush_quote()
            tag = "ul" if bullet else "ol"
            if list_tag != tag:
                flush_list()
                list_tag = tag
            list_items.append(f"<li>{_inline((bullet or number).group(1))}</li>")
        elif stripped.startswith(">"):
            flush_paragraph()
            flush_list()
            quote_lines.append(_inline(stripped.lstrip("> ")))
        else:
            flush_list()
            flush_quote()
            paragraph.append(stripped)

    flush_all()
    return "".join(out)


def _btn(label: str | None, href: str | None, variant: str = "", extra: str = "") -> str:
    """A button link, or nothing when either half is missing."""
    if not label or not href:
        return ""
    classes = "btn" + (f" btn-{variant}" if variant else "") + (f" {extra}" if extra else "")
    return f'<a class="{classes}" href="{_esc(href)}">{_esc(label)}</a>'


def _section_head(b: dict, *, level: int = 2, center: bool = True) -> str:
    """The heading + subheading most section blocks open with."""
    if not b.get("heading") and not b.get("sub"):
        return ""
    heading = f"<h{level}>{_esc(b['heading'])}</h{level}>" if b.get("heading") else ""
    sub = f'<p class="sec-sub">{_inline(b["sub"])}</p>' if b.get("sub") else ""
    return f'<div class="sec-head{" align-center" if center else ""}">{heading}{sub}</div>'


def _img(src: str | None, alt: str = "", extra: str = "") -> str:
    if not src:
        return ""
    return f'<img src="{_esc(src)}" alt="{_esc(alt)}" loading="lazy"{extra}>'


def _render_hero(b: dict, ctx: dict) -> str:
    buttons = _btn(b.get("button_label"), b.get("button_href")) + _btn(
        b.get("button2_label"), b.get("button2_href"), "outline"
    )
    buttons = f'<div class="hero-actions">{buttons}</div>' if buttons else ""
    eyebrow = f'<p class="eyebrow">{_esc(b["eyebrow"])}</p>' if b.get("eyebrow") else ""
    sub = f"<p class=\"hero-sub\">{_inline(b['sub'])}</p>" if b.get("sub") else ""
    classes = f'blk hero align-{b["align"]} hero-{b.get("size", "normal")}'
    style = ""
    if b.get("image"):
        classes += " hero-cover"
        style = f' style="--hero-img:url(&quot;{_esc(b["image"])}&quot;)"'
    return (
        f'<header class="{classes}"{style}><div class="hero-inner">'
        f"{eyebrow}<h1>{_esc(b['heading'])}</h1>{sub}{buttons}</div></header>"
    )


def _render_heading(b: dict, ctx: dict) -> str:
    level = b["level"]
    eyebrow = f'<p class="eyebrow">{_esc(b["eyebrow"])}</p>' if b.get("eyebrow") else ""
    sub = f'<p class="sec-sub">{_inline(b["sub"])}</p>' if b.get("sub") else ""
    return (
        f'<section class="blk sec-head align-{b.get("align", "left")}">'
        f"{eyebrow}<h{level}>{_esc(b['text'])}</h{level}>{sub}</section>"
    )


def _render_text(b: dict, ctx: dict) -> str:
    return f'<section class="blk prose">{_rich(b["body"])}</section>'


def _render_image(b: dict, ctx: dict) -> str:
    caption = f"<figcaption>{_inline(b['caption'])}</figcaption>" if b.get("caption") else ""
    image = _img(b["src"], b["alt"])
    if b.get("href"):
        image = f'<a href="{_esc(b["href"])}">{image}</a>'
    return (
        f'<section class="blk img-{b.get("width", "full")} img-align-{b.get("align", "center")}">'
        f"<figure>{image}{caption}</figure></section>"
    )


def _render_button(b: dict, ctx: dict) -> str:
    variant = b["variant"] if b["variant"] != "solid" else ""
    size = " btn-large" if b.get("size") == "large" else ""
    target = ' target="_blank" rel="noopener"' if b.get("new_tab") else ""
    classes = "btn" + (f" btn-{variant}" if variant else "") + size
    return (
        f'<section class="blk align-{b["align"]}">'
        f'<a class="{classes}" href="{_esc(b["href"])}"{target}>{_esc(b["label"])}</a></section>'
    )


def _render_features(b: dict, ctx: dict) -> str:
    style = b.get("style", "cards")
    cards = []
    for item in b["items"]:
        icon = f'<div class="feat-icon">{_esc(item["icon"])}</div>' if item.get("icon") else ""
        title = _esc(item["title"])
        if item.get("href"):
            title = f'<a href="{_esc(item["href"])}">{title}</a>'
        body = f"<p>{_inline(item['body'])}</p>" if item.get("body") else ""
        cards.append(f'<div class="card feat">{icon}<h3>{title}</h3>{body}</div>')
    return (
        f'<section class="blk">{_section_head(b)}'
        f'<div class="features features-{style} grid-{b.get("columns", 3)}">{"".join(cards)}</div></section>'
    )


# ---------------------------------------------------------- layout blocks
def _render_columns(b: dict, ctx: dict) -> str:
    cols = []
    for col in b["items"]:
        parts = [
            _img(col.get("image"), col.get("heading") or ""),
            f"<h3>{_esc(col['heading'])}</h3>" if col.get("heading") else "",
            _rich(col["body"]) if col.get("body") else "",
            _btn(col.get("button_label"), col.get("button_href"), "outline"),
        ]
        cols.append(f'<div class="col">{"".join(parts)}</div>')
    return (
        f'<section class="blk cols cols-{b["count"]} gap-{b["gap"]} align-{b["align"]}">'
        f'{"".join(cols)}</section>'
    )


def _render_media_text(b: dict, ctx: dict) -> str:
    eyebrow = f'<p class="eyebrow">{_esc(b["eyebrow"])}</p>' if b.get("eyebrow") else ""
    heading = f"<h2>{_esc(b['heading'])}</h2>" if b.get("heading") else ""
    body = _rich(b["body"]) if b.get("body") else ""
    return (
        f'<section class="blk media-text side-{b["side"]} ratio-{b["ratio"]} split-{b["split"]}">'
        f'<figure class="mt-media">{_img(b["src"], b["alt"])}</figure>'
        f'<div class="mt-body">{eyebrow}{heading}{body}'
        f'{_btn(b.get("button_label"), b.get("button_href"))}</div></section>'
    )


def _render_gallery(b: dict, ctx: dict) -> str:
    figures = []
    for item in b["items"]:
        image = _img(item["src"], item["alt"])
        href = item.get("href") or (item["src"] if b.get("lightbox") else None)
        if href:
            target = ' target="_blank" rel="noopener"' if href == item["src"] else ""
            image = f'<a href="{_esc(href)}"{target}>{image}</a>'
        caption = f"<figcaption>{_inline(item['caption'])}</figcaption>" if item.get("caption") else ""
        figures.append(f"<figure>{image}{caption}</figure>")
    return (
        f'<section class="blk">{_section_head(b)}'
        f'<div class="gallery grid-{b["columns"]} ratio-{b["ratio"]} gap-{b["gap"]}">'
        f'{"".join(figures)}</div></section>'
    )


# ------------------------------------------------------- marketing blocks
def _render_pricing(b: dict, ctx: dict) -> str:
    plans = []
    for plan in b["items"]:
        badge = f'<span class="plan-badge">{_esc(plan["badge"])}</span>' if plan.get("badge") else ""
        period = f'<span class="plan-period">{_esc(plan["period"])}</span>' if plan.get("period") else ""
        desc = f"<p class=\"plan-desc\">{_inline(plan['description'])}</p>" if plan.get("description") else ""
        feats = "".join(f"<li>{_inline(f)}</li>" for f in plan["features"])
        feats = f'<ul class="plan-features">{feats}</ul>' if feats else ""
        button = _btn(plan.get("button_label"), plan.get("button_href"),
                      "" if plan.get("featured") else "outline")
        plans.append(
            f'<div class="plan{" plan-featured" if plan.get("featured") else ""}">{badge}'
            f'<h3>{_esc(plan["name"])}</h3>'
            f'<div class="plan-price">{_esc(plan["price"])}{period}</div>'
            f"{desc}{feats}{button}</div>"
        )
    return (
        f'<section class="blk">{_section_head(b)}'
        f'<div class="pricing grid-{min(len(plans), 4) or 1}">{"".join(plans)}</div></section>'
    )


def _render_testimonials(b: dict, ctx: dict) -> str:
    cards = []
    for t in b["items"]:
        stars = ""
        if t.get("rating"):
            stars = f'<div class="stars" aria-label="{t["rating"]} out of 5">' + "★" * t["rating"] + "</div>"
        avatar = _img(t.get("avatar"), "", ' class="tm-avatar"')
        who = ""
        if t.get("name") or t.get("role"):
            who = (
                f'<figcaption>{avatar}<div>'
                + (f'<strong>{_esc(t["name"])}</strong>' if t.get("name") else "")
                + (f'<span>{_esc(t["role"])}</span>' if t.get("role") else "")
                + "</div></figcaption>"
            )
        cards.append(f'<figure class="tm">{stars}<blockquote>{_paragraphs(t["quote"])}</blockquote>{who}</figure>')
    return (
        f'<section class="blk">{_section_head(b)}'
        f'<div class="testimonials tm-{b["layout"]}">{"".join(cards)}</div></section>'
    )


def _render_team(b: dict, ctx: dict) -> str:
    people = []
    for m in b["items"]:
        name = _esc(m["name"])
        if m.get("href"):
            name = f'<a href="{_esc(m["href"])}">{name}</a>'
        people.append(
            f'<div class="member">{_img(m.get("photo"), m["name"], " class=\"member-photo\"")}'
            f"<h3>{name}</h3>"
            + (f'<p class="member-role">{_esc(m["role"])}</p>' if m.get("role") else "")
            + (f"<p>{_inline(m['bio'])}</p>" if m.get("bio") else "")
            + "</div>"
        )
    return (
        f'<section class="blk">{_section_head(b)}'
        f'<div class="team grid-{b["columns"]}">{"".join(people)}</div></section>'
    )


def _render_faq(b: dict, ctx: dict) -> str:
    entries = "".join(
        f'<details class="faq-item"{" open" if (i == 0 and b.get("open_first")) else ""}>'
        f"<summary>{_esc(item['question'])}</summary>"
        f'<div class="faq-answer">{_rich(item["answer"])}</div></details>'
        for i, item in enumerate(b["items"])
    )
    return f'<section class="blk">{_section_head(b, center=False)}<div class="faq">{entries}</div></section>'


def _render_cta(b: dict, ctx: dict) -> str:
    body = f"<p>{_inline(b['body'])}</p>" if b.get("body") else ""
    buttons = _btn(b.get("button_label"), b.get("button_href"), "cta-main") + _btn(
        b.get("button2_label"), b.get("button2_href"), "outline"
    )
    return (
        f'<section class="blk cta cta-{b["style"]} cta-{b["align"]}">'
        f'<div class="cta-text"><h2>{_esc(b["heading"])}</h2>{body}</div>'
        f'<div class="cta-actions">{buttons}</div></section>'
    )


def _render_stats(b: dict, ctx: dict) -> str:
    items = "".join(
        f'<div class="stat"><div class="stat-value">{_esc(i["value"])}</div>'
        f'<div class="stat-label">{_esc(i["label"])}</div></div>'
        for i in b["items"]
    )
    return f'<section class="blk">{_section_head(b)}<div class="stats grid-{b["columns"]}">{items}</div></section>'


def _render_logos(b: dict, ctx: dict) -> str:
    logos = []
    for logo in b["items"]:
        image = _img(logo["src"], logo["alt"])
        if logo.get("href"):
            image = f'<a href="{_esc(logo["href"])}" target="_blank" rel="noopener">{image}</a>'
        logos.append(f'<div class="logo">{image}</div>')
    return (
        f'<section class="blk">{_section_head(b)}'
        f'<div class="logos{" logos-gray" if b.get("grayscale") else ""}">{"".join(logos)}</div></section>'
    )


def _render_steps(b: dict, ctx: dict) -> str:
    items = "".join(
        f"<li><h3>{_esc(i['title'])}</h3>" + (f"<p>{_inline(i['body'])}</p>" if i.get("body") else "") + "</li>"
        for i in b["items"]
    )
    return f'<section class="blk">{_section_head(b)}<ol class="steps steps-{b["layout"]}">{items}</ol></section>'


def _render_notice(b: dict, ctx: dict) -> str:
    link = ""
    if b.get("link_label") and b.get("href"):
        link = f' <a href="{_esc(b["href"])}">{_esc(b["link_label"])} →</a>'
    return f'<div class="blk notice-bar notice-{b["style"]}">{_esc(b["text"])}{link}</div>'


# ------------------------------------------------------------ media blocks
def _render_video(b: dict, ctx: dict) -> str:
    caption = f"<figcaption>{_inline(b['caption'])}</figcaption>" if b.get("caption") else ""
    ratio = b["ratio"].replace(":", "-")
    return (
        f'<section class="blk video video-{b["width"]}"><figure>'
        f'<div class="video-frame ratio-{ratio}">'
        f'<iframe src="{_esc(b["embed"])}" loading="lazy" allowfullscreen'
        f' allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture"'
        f' referrerpolicy="strict-origin-when-cross-origin" title="Video"></iframe></div>'
        f"{caption}</figure></section>"
    )


def _map_src(address: str, zoom: int) -> str:
    from urllib.parse import quote_plus  # noqa: PLC0415 — only used here
    return f"https://www.google.com/maps?q={quote_plus(address)}&z={zoom}&output=embed"


def _render_map(b: dict, ctx: dict) -> str:
    caption = f"<figcaption>{_inline(b['caption'])}</figcaption>" if b.get("caption") else ""
    return (
        f'<section class="blk map map-{b["height"]}"><figure>'
        f'<iframe src="{_esc(_map_src(b["address"], b["zoom"]))}" loading="lazy"'
        f' referrerpolicy="no-referrer-when-downgrade" title="Map: {_esc(b["address"])}"></iframe>'
        f"{caption}</figure></section>"
    )


# ------------------------------------------------------------- site blocks
def _render_header(b: dict, ctx: dict) -> str:
    ctx["nav_count"] = ctx.get("nav_count", 0) + 1
    toggle_id = f"nav-toggle-{ctx['nav_count']}"
    brand = _img(b.get("logo_src"), b.get("logo_text") or "Home", ' class="brand-logo"')
    if b.get("logo_text"):
        brand += f'<span class="brand-text">{_esc(b["logo_text"])}</span>'
    links = "".join(
        f'<li><a href="{_esc(link["href"])}">{_esc(link["label"])}</a></li>' for link in b["links"]
    )
    button = _btn(b.get("button_label"), b.get("button_href"), "", "nav-cta")
    # The menu toggle is a checkbox + label: it needs no script, and the
    # page CSP allows none inline.
    return (
        f'<header class="blk site-header header-{b["style"]}{" header-sticky" if b.get("sticky") else ""}">'
        f'<nav class="site-nav" aria-label="Main">'
        f'<a class="brand" href="{_esc(b["logo_href"])}">{brand}</a>'
        f'<input type="checkbox" id="{toggle_id}" class="nav-toggle" hidden>'
        f'<label for="{toggle_id}" class="nav-burger" aria-label="Menu"><span></span><span></span><span></span></label>'
        f'<div class="nav-links"><ul>{links}</ul>{button}</div>'
        f"</nav></header>"
    )


def _render_social_links(items: list[dict], style: str = "pills") -> str:
    links = "".join(
        f'<a class="social-link social-{i["network"]}" href="{_esc(i["href"])}" target="_blank" rel="noopener">'
        f'{SOCIAL_LABELS[i["network"]]}</a>'
        for i in items
    )
    return f'<div class="social social-{style}">{links}</div>' if links else ""


def _render_footer(b: dict, ctx: dict) -> str:
    brand = ""
    if b.get("brand") or b.get("tagline"):
        brand = (
            '<div class="footer-brand">'
            + (f'<div class="brand-text">{_esc(b["brand"])}</div>' if b.get("brand") else "")
            + (f"<p>{_inline(b['tagline'])}</p>" if b.get("tagline") else "")
            + _render_social_links(b["social"], "text")
            + "</div>"
        )
    columns = "".join(
        '<div class="footer-col">'
        + (f"<h3>{_esc(col['heading'])}</h3>" if col.get("heading") else "")
        + "<ul>" + "".join(f'<li><a href="{_esc(link["href"])}">{_esc(link["label"])}</a></li>' for link in col["links"]) + "</ul>"
        + "</div>"
        for col in b["columns"]
    )
    bottom = f'<div class="footer-bottom">{_esc(b["copyright"])}</div>' if b.get("copyright") else ""
    return (
        f'<footer class="blk site-footer footer-{b["style"]}">'
        f'<div class="footer-grid">{brand}{columns}</div>{bottom}</footer>'
    )


def _render_contact(b: dict, ctx: dict) -> str:
    rows = []
    if b.get("address"):
        rows.append(f"<div><dt>Address</dt><dd>{_inline(b['address']).replace(chr(10), '<br>')}</dd></div>")
    if b.get("phone"):
        tel = re.sub(r"[^\d+]", "", b["phone"])
        rows.append(f'<div><dt>Phone</dt><dd><a href="tel:{_esc(tel)}">{_esc(b["phone"])}</a></dd></div>')
    if b.get("email"):
        rows.append(f'<div><dt>Email</dt><dd><a href="mailto:{_esc(b["email"])}">{_esc(b["email"])}</a></dd></div>')
    if b.get("hours"):
        rows.append(f"<div><dt>Hours</dt><dd>{_inline(b['hours']).replace(chr(10), '<br>')}</dd></div>")
    details = f'<dl class="contact-list">{"".join(rows)}</dl>'
    map_frame = ""
    if b.get("show_map") and b.get("address"):
        map_frame = (
            f'<div class="contact-map"><iframe src="{_esc(_map_src(b["address"], b["map_zoom"]))}"'
            f' loading="lazy" referrerpolicy="no-referrer-when-downgrade" title="Map"></iframe></div>'
        )
    return (
        f'<section class="blk contact{" contact-with-map" if map_frame else ""}">'
        f"{_section_head(b, center=False)}<div class=\"contact-grid\">{details}{map_frame}</div></section>"
    )


def _render_social(b: dict, ctx: dict) -> str:
    return (
        f'<section class="blk align-{b["align"]}">{_section_head(b, level=3, center=b["align"] == "center")}'
        f'{_render_social_links(b["items"], b["style"])}</section>'
    )


def _render_table(b: dict, ctx: dict) -> str:
    rows = b.get("table") or []
    if not rows:
        return ""
    head = ""
    body_rows = rows
    if b.get("header"):
        head = "<thead><tr>" + "".join(f"<th>{_inline(c)}</th>" for c in rows[0]) + "</tr></thead>"
        body_rows = rows[1:]
    body = "<tbody>" + "".join(
        "<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in r) + "</tr>" for r in body_rows
    ) + "</tbody>"
    caption = f"<caption>{_esc(b['caption'])}</caption>" if b.get("caption") else ""
    classes = "tbl" + (" tbl-striped" if b.get("striped") else "") + (" tbl-compact" if b.get("compact") else "")
    return f'<section class="blk tbl-wrap"><table class="{classes}">{caption}{head}{body}</table></section>'


# ------------------------------------------------------------ design layer
def _apply_design(block: dict, markup: str, ctx: dict) -> str:
    """Wrap a rendered block in its design settings, when it has any.

    Backgrounds, padding, width, anchors and animation are one wrapper
    around whatever the block renderer produced, so the renderers stay
    about content and this stays about presentation. A block with no
    design settings renders exactly as it did before the layer existed.
    """
    d = block.get("design")
    if not d or not markup:
        return markup
    classes = ["sec"]
    style: list[str] = []
    bg = d.get("bg", "none")
    if bg != "none":
        classes.append(f"sec-bg-{bg}")
    if bg == "custom" and d.get("bg_color"):
        style.append(f"--sec-bg:{d['bg_color']}")
    if bg == "image" and d.get("bg_image"):
        style.append(f"--sec-img:url(&quot;{_esc(d['bg_image'])}&quot;)")
    if d.get("text_color"):
        classes.append("sec-text")
        style.append(f"--sec-text:{d['text_color']}")
    if d.get("padding", "none") != "none":
        classes.append(f"sec-pad-{d['padding']}")
    # A site header or footer is edge-to-edge by nature, so its design
    # background is too — a content-wide colour band behind a full-width
    # bar is never what anyone meant.
    width = "full" if block.get("type") in ("header", "footer") else d.get("width", "content")
    if width != "content":
        classes.append(f"sec-{width}")
    if d.get("animate", "none") != "none":
        classes.append(f"sec-anim sec-anim-{d['animate']}")
        ctx["needs_motion_js"] = True
    if d.get("hide_on", "none") != "none":
        classes.append(f"sec-hide-{d['hide_on']}")
    if d.get("css_class"):
        classes.append(_esc(d["css_class"]))
    anchor = f' id="{_esc(d["anchor"])}"' if d.get("anchor") else ""
    style_attr = f' style="{";".join(style)}"' if style else ""
    return f'<div class="{" ".join(classes)}"{anchor}{style_attr}><div class="sec-inner">{markup}</div></div>'


def _render_quote(b: dict, ctx: dict) -> str:
    attribution = f"<cite>{_esc(b['attribution'])}</cite>" if b.get("attribution") else ""
    return (
        f'<section class="blk"><blockquote>{_paragraphs(b["body"])}'
        f"{attribution}</blockquote></section>"
    )


def _render_form_field(field: dict) -> str:
    name = str(field.get("name") or "")
    if not FIELD_NAME.match(name):
        return ""
    label = _esc(field.get("label") or name.replace("_", " ").title())
    required = " required" if field.get("required") else ""
    ftype = field.get("type")

    if ftype == "textarea":
        control = f'<textarea name="{_esc(name)}" rows="4"{required}></textarea>'
    else:
        input_type = {"email": "email", "phone": "tel", "tel": "tel"}.get(ftype, "text")
        control = f'<input type="{input_type}" name="{_esc(name)}"{required}>'
    return f"<label><span>{label}</span>{control}</label>"


def _render_form(b: dict, ctx: dict) -> str:
    ctx["needs_form_js"] = True
    fields = ctx["forms"].get(b["form_slug"]) or DEFAULT_FORM_FIELDS
    controls = "".join(_render_form_field(f) for f in fields)
    heading = f"<h2>{_esc(b['heading'])}</h2>" if b.get("heading") else ""
    return (
        f'<section class="blk">{heading}'
        f'<form class="lead-form" data-crm-form data-tenant="{_esc(ctx["tenant_slug"])}"'
        f' data-form-slug="{_esc(b["form_slug"])}">'
        # Honeypot: hidden from people, tempting to bots.
        f'<input type="text" name="_hp" class="hp" tabindex="-1" autocomplete="off" aria-hidden="true">'
        f"{controls}"
        f'<div class="form-error" hidden></div>'
        f'<button type="submit">{_esc(b["button_label"])}</button>'
        f"</form>"
        f'<div class="form-done" hidden>Thanks — we will be in touch shortly.</div>'
        f"</section>"
    )


def _render_html(b: dict, ctx: dict) -> str:
    """Emit the stored markup verbatim.

    Deliberately NOT passed through _esc: it was sanitized by
    app/sanitize.py when it was saved, and escaping it again would
    render the tags as visible text. This is the only renderer in the
    file that does not escape, which is why the cleaner above is the
    security boundary.
    """
    markup = b.get("html") or ""
    if not markup:
        return ""
    classes = ["pb-html"]
    if b.get("styled", True):
        classes.append("pb-html-styled")
    width = b.get("width", "normal")
    if width != "normal":
        classes.append(f"pb-html-{width}")
    return f'<div class="{" ".join(classes)}">{markup}</div>'


def _render_spacer(b: dict, ctx: dict) -> str:
    # A class rather than an inline style: `style` is the one attribute
    # the sanitizer always drops, so a spacer written this way is the
    # only kind that survives a page being converted to HTML mode.
    return f'<div class="pb-space-{b["size"]}"></div>'


def _render_divider(b: dict, ctx: dict) -> str:
    return "<hr>"


RENDERERS: dict[str, Callable[[dict, dict], str]] = {
    "hero": _render_hero,
    "heading": _render_heading,
    "text": _render_text,
    "image": _render_image,
    "button": _render_button,
    "features": _render_features,
    "quote": _render_quote,
    "form": _render_form,
    "html": _render_html,
    "spacer": _render_spacer,
    "divider": _render_divider,
    "columns": _render_columns,
    "media_text": _render_media_text,
    "gallery": _render_gallery,
    "pricing": _render_pricing,
    "testimonials": _render_testimonials,
    "team": _render_team,
    "faq": _render_faq,
    "cta": _render_cta,
    "stats": _render_stats,
    "logos": _render_logos,
    "steps": _render_steps,
    "notice": _render_notice,
    "video": _render_video,
    "map": _render_map,
    "header": _render_header,
    "footer": _render_footer,
    "contact": _render_contact,
    "social": _render_social,
    "table": _render_table,
}

# Blocks that cannot survive the trip to HTML mode. Only the form block
# qualifies, and no longer because of the sanitizer — <form> and its
# controls are on the allow-list now, so the markup would come through
# intact. What would not come through is the JavaScript that posts it:
# the page only ships the lead-form script when a form *block* rendered
# it. Converting would leave a form that looks right and submits
# nowhere, which is worse than dropping it and saying so.
UNCONVERTIBLE_BLOCKS = {"form"}


def page_document(blocks: Any) -> str | None:
    """The whole document this page *is*, or None if it is a normal page.

    Only ever a lone HTML block that was stored as a document: that is
    the shape clean_blocks allows one to be saved in, and checking it
    again here means a block list assembled some other way cannot make
    the renderer hand back raw markup.
    """
    if not isinstance(blocks, list) or len(blocks) != 1:
        return None
    block = blocks[0]
    if not isinstance(block, dict) or block.get("type") != "html" or not block.get("doc"):
        return None
    markup = block.get("html") or ""
    return markup if is_document(markup) else None


def blocks_to_html(blocks: list[dict], *, tenant_slug: str = "") -> tuple[str, list[str]]:
    """Render a block list to a markup fragment, and say what was left out.

    This is how a page switches from the builder to HTML mode without
    the author losing what they already laid out: the same renderers
    produce the same markup, and the page's own CSS classes come with
    it, so a converted page looks like it did before.
    """
    ctx = {"tenant_slug": tenant_slug, "forms": {}, "needs_form_js": False}
    parts: list[str] = []
    skipped: list[str] = []

    for block in blocks if isinstance(blocks, list) else []:
        kind = block.get("type")
        if kind in UNCONVERTIBLE_BLOCKS:
            skipped.append(kind)
            continue
        if kind == "html":
            # Already markup — taking the renderer's wrapper too would
            # nest a .pb-html inside the one the page is about to be.
            parts.append(block.get("html") or "")
            continue
        renderer = RENDERERS.get(kind)
        if renderer is None:
            skipped.append(str(kind))
            continue
        parts.append(_apply_design(block, renderer(block, ctx), ctx))

    return "\n".join(part for part in parts if part), sorted(set(skipped))


_PAGE_CSS = """
* { box-sizing: border-box; }
[hidden] { display: none !important; }
body {
  margin: 0; background: var(--bg); color: var(--text);
  font-family: var(--font); font-size: 17px; line-height: 1.65;
  -webkit-font-smoothing: antialiased;
  /* The content column fills the window even when its content does
     not, so a footer after it lands on the bottom edge instead of
     floating halfway up a white screen. */
  display: flex; flex-direction: column; min-height: 100vh;
}
body > .page { flex: 1 0 auto; }
/* The footer brings the gap above itself; the column's own bottom
   padding would only add to it. */
body.has-footer > .page { padding-bottom: 0; }
/* Out of the column the footer is already full width: the 100vw
   breakout it uses inside the column would overflow by a scrollbar. */
body > .site-footer, body > .sec {
  flex: 0 0 auto; width: auto; margin-left: 0; margin-right: 0;
}
.page { max-width: var(--maxw); margin: 0 auto; padding: 0 var(--gutter) 80px; }
.blk { margin: 28px 0; }
.align-center { text-align: center; }
h1, h2, h3, h4 { font-family: var(--hfont); }
h1 { font-size: clamp(2rem, 5vw, 3rem); line-height: 1.1; margin: 0 0 14px; letter-spacing: -.01em; }
h2 { font-size: clamp(1.5rem, 3vw, 2rem); line-height: 1.2; margin: 0 0 10px; }
h3 { font-size: 1.15rem; margin: 0 0 6px; }
p { margin: 0 0 14px; }
img { max-width: 100%; height: auto; border-radius: var(--radius); display: block; margin: 0 auto; }
code {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: .88em; padding: 2px 6px; border-radius: 4px;
  background: color-mix(in srgb, var(--text) 8%, transparent);
}
.prose ul, .prose ol { margin: 0 0 14px; padding-left: 26px; }
.prose li { margin: 4px 0; }
.prose h2, .prose h3, .prose h4 { margin-top: 26px; }
.prose blockquote {
  font-size: 1.05rem; margin-bottom: 14px;
}
a { color: var(--primary); }
figure { margin: 0; }
figcaption { font-size: .85rem; opacity: .7; text-align: center; margin-top: 8px; }
hr { border: 0; border-top: 1px solid color-mix(in srgb, var(--text) 15%, transparent); margin: 32px 0; }
.hero { padding: 56px 0 32px; }
.hero-tall { padding: 110px 0 90px; }
.hero-screen { min-height: 88vh; display: flex; align-items: center; }
.hero-inner { width: 100%; }
.hero-sub { font-size: 1.2rem; opacity: .8; max-width: 640px; }
.hero.align-center .hero-sub { margin-left: auto; margin-right: auto; }
.hero-actions { display: flex; gap: 10px; flex-wrap: wrap; margin-top: 8px; }
.hero.align-center .hero-actions { justify-content: center; }
/* A cover hero bleeds past the content column and darkens its photo
   so white type reads on it. */
.hero-cover {
  width: 100vw; margin-left: calc(50% - 50vw); padding-left: var(--gutter); padding-right: var(--gutter);
  background: var(--hero-img) center / cover no-repeat; color: #fff; position: relative;
}
.hero-cover::before { content: ""; position: absolute; inset: 0; background: rgba(8, 12, 14, .5); }
.hero-cover .hero-inner { position: relative; max-width: var(--maxw); margin: 0 auto; }
.hero-cover .btn-outline { color: #fff; border-color: #fff; }
.eyebrow {
  font-size: .8rem; font-weight: 700; letter-spacing: .12em; text-transform: uppercase;
  color: var(--secondary); margin: 0 0 10px;
}
.sec-head { margin-bottom: 26px; }
.sec-head h2, .sec-head h3 { margin-bottom: 8px; }
.sec-sub { font-size: 1.1rem; opacity: .78; max-width: 640px; margin: 0; }
.sec-head.align-center .sec-sub { margin-left: auto; margin-right: auto; }
.btn {
  display: inline-block; background: var(--primary); color: #fff;
  padding: 11px 26px; border-radius: var(--radius); text-decoration: none;
  font-weight: 600; border: 2px solid var(--primary); line-height: 1.3;
}
.btn:hover { filter: brightness(1.08); }
.btn-outline { background: transparent; color: var(--primary); }
.btn-secondary { background: var(--secondary); border-color: var(--secondary); }
.btn-link { background: none; border-color: transparent; color: var(--primary); padding-left: 0; padding-right: 0; text-decoration: underline; }
.btn-large { padding: 15px 34px; font-size: 1.1rem; }
/* Grids: columns on wide screens, fewer as the screen narrows. */
.grid-2, .grid-3, .grid-4, .grid-5 { display: grid; gap: 18px; }
.grid-2 { grid-template-columns: repeat(2, minmax(0, 1fr)); }
.grid-3 { grid-template-columns: repeat(3, minmax(0, 1fr)); }
.grid-4 { grid-template-columns: repeat(4, minmax(0, 1fr)); }
.grid-5 { grid-template-columns: repeat(5, minmax(0, 1fr)); }
@media (max-width: 900px) { .grid-4, .grid-5 { grid-template-columns: repeat(2, minmax(0, 1fr)); } .grid-3 { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
@media (max-width: 560px) { .grid-2, .grid-3, .grid-4, .grid-5 { grid-template-columns: 1fr; } }
.features { display: grid; gap: 16px; }
.card {
  border: 1px solid color-mix(in srgb, var(--text) 14%, transparent);
  border-radius: var(--radius); padding: 18px; background: color-mix(in srgb, var(--bg) 92%, var(--text) 0%);
}
.card p { margin: 0; font-size: .95rem; opacity: .85; }
.features-plain .card { border: 0; padding: 0; background: none; }
.feat-icon { font-size: 1.8rem; line-height: 1; margin-bottom: 12px; }
.features-icons .card { text-align: center; }
.features-icons .feat-icon {
  width: 56px; height: 56px; margin: 0 auto 12px; display: grid; place-items: center;
  border-radius: 50%; background: color-mix(in srgb, var(--primary) 12%, transparent); font-size: 1.5rem;
}
.card h3 a { color: inherit; text-decoration: none; }
.card h3 a:hover { color: var(--primary); }

/* Columns */
.cols { display: grid; gap: 24px; }
.cols.gap-small { gap: 12px; } .cols.gap-large { gap: 40px; }
.cols-2 { grid-template-columns: repeat(2, minmax(0, 1fr)); }
.cols-3 { grid-template-columns: repeat(3, minmax(0, 1fr)); }
.cols-4 { grid-template-columns: repeat(4, minmax(0, 1fr)); }
.cols.align-center { text-align: center; }
.cols .col img { margin: 0 0 14px; }
.cols.align-center .col img { margin-inline: auto; }
.cols .col > :last-child { margin-bottom: 0; }
@media (max-width: 900px) { .cols-3, .cols-4 { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
@media (max-width: 560px) { .cols { grid-template-columns: 1fr; } }

/* Image + text */
.media-text { display: grid; grid-template-columns: 1fr 1fr; gap: 36px; align-items: center; }
.media-text.split-third { grid-template-columns: 1fr 2fr; }
.media-text.side-right .mt-media { order: 2; }
.media-text.side-right.split-third { grid-template-columns: 2fr 1fr; }
.mt-media img { width: 100%; object-fit: cover; }
.ratio-square .mt-media img { aspect-ratio: 1; }
.ratio-wide .mt-media img { aspect-ratio: 16 / 10; }
.ratio-portrait .mt-media img { aspect-ratio: 4 / 5; }
.mt-body > :last-child { margin-bottom: 0; }
.mt-body .btn { margin-top: 6px; }
@media (max-width: 700px) { .media-text, .media-text.split-third, .media-text.side-right.split-third { grid-template-columns: 1fr; } .media-text.side-right .mt-media { order: 0; } }

/* Gallery */
.gallery { display: grid; gap: 12px; }
.gallery.gap-none { gap: 0; } .gallery.gap-medium { gap: 20px; }
.gallery img { width: 100%; object-fit: cover; margin: 0; }
.gallery.ratio-square img { aspect-ratio: 1; }
.gallery.ratio-landscape img { aspect-ratio: 4 / 3; }
.gallery.ratio-portrait img { aspect-ratio: 3 / 4; }
.gallery a { display: block; }
.gallery figcaption { text-align: left; }

/* Image block sizing */
.img-medium figure { max-width: 60%; } .img-small figure { max-width: 36%; }
.img-align-center figure { margin-inline: auto; } .img-align-right figure { margin-left: auto; }
.img-align-left img, .img-align-right img { margin: 0; }
@media (max-width: 560px) { .img-medium figure, .img-small figure { max-width: 100%; } }

/* Pricing */
.pricing { display: grid; gap: 18px; align-items: stretch; }
.plan {
  position: relative; display: flex; flex-direction: column; gap: 10px; padding: 26px 22px;
  border: 1px solid color-mix(in srgb, var(--text) 14%, transparent); border-radius: var(--radius);
}
.plan-featured { border-color: var(--primary); box-shadow: 0 10px 30px -18px color-mix(in srgb, var(--primary) 60%, transparent); }
.plan-badge {
  position: absolute; top: -12px; left: 22px; background: var(--secondary); color: #fff;
  font-size: .72rem; font-weight: 700; letter-spacing: .06em; text-transform: uppercase;
  padding: 4px 10px; border-radius: 999px;
}
.plan h3 { margin: 0; }
.plan-price { font-family: var(--hfont); font-size: 2.2rem; font-weight: 700; line-height: 1; }
.plan-period { font-size: .95rem; font-weight: 400; opacity: .7; margin-left: 4px; }
.plan-desc { margin: 0; opacity: .8; font-size: .95rem; }
.plan-features { list-style: none; margin: 6px 0 0; padding: 0; display: grid; gap: 7px; flex: 1; }
.plan-features li { padding-left: 24px; position: relative; font-size: .95rem; }
.plan-features li::before { content: "✓"; position: absolute; left: 0; color: var(--primary); font-weight: 700; }
.plan .btn { text-align: center; margin-top: 8px; }

/* Testimonials */
.testimonials { display: grid; gap: 18px; }
.tm-grid { grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); }
.tm-single { max-width: 720px; margin-inline: auto; }
.tm-carousel {
  grid-auto-flow: column; grid-auto-columns: min(100%, 360px); overflow-x: auto;
  scroll-snap-type: x mandatory; padding-bottom: 8px; scrollbar-width: thin;
}
.tm-carousel .tm { scroll-snap-align: start; }
.tm {
  margin: 0; padding: 22px; border-radius: var(--radius);
  background: color-mix(in srgb, var(--text) 5%, transparent); display: flex; flex-direction: column; gap: 12px;
}
.tm blockquote { border: 0; padding: 0; font-size: 1.05rem; }
.tm blockquote p:last-child { margin-bottom: 0; }
.tm figcaption { display: flex; align-items: center; gap: 12px; text-align: left; margin: 0; font-size: .95rem; opacity: 1; }
.tm figcaption span { display: block; font-size: .85rem; opacity: .7; }
.tm-avatar { width: 44px; height: 44px; border-radius: 50%; object-fit: cover; margin: 0; }
.stars { color: var(--secondary); letter-spacing: 2px; }

/* Team */
.team { display: grid; gap: 22px; text-align: center; }
.member-photo { width: 100%; aspect-ratio: 1; object-fit: cover; border-radius: var(--radius); margin: 0 0 12px; }
.member h3 { margin-bottom: 2px; }
.member h3 a { color: inherit; text-decoration: none; }
.member-role { color: var(--primary); font-size: .9rem; font-weight: 600; margin: 0 0 8px; }
.member p { font-size: .95rem; opacity: .85; margin: 0; }

/* FAQ */
.faq { display: grid; gap: 8px; }
.faq-item { border: 1px solid color-mix(in srgb, var(--text) 14%, transparent); border-radius: var(--radius); padding: 0 18px; }
.faq-item summary { cursor: pointer; font-weight: 600; padding: 14px 0; list-style: none; position: relative; padding-right: 28px; }
.faq-item summary::-webkit-details-marker { display: none; }
.faq-item summary::after { content: "+"; position: absolute; right: 0; top: 12px; font-size: 1.3rem; color: var(--primary); }
.faq-item[open] summary::after { content: "–"; }
.faq-answer { padding: 0 0 16px; opacity: .9; }
.faq-answer > :last-child { margin-bottom: 0; }

/* Call to action */
.cta { padding: 44px 36px; border-radius: var(--radius); display: grid; gap: 20px; text-align: center; }
.cta-primary { background: var(--primary); color: #fff; }
.cta-primary .btn-cta-main { background: #fff; color: var(--primary); border-color: #fff; }
.cta-primary .btn-outline, .cta-dark .btn-outline { color: #fff; border-color: #fff; }
.cta-dark { background: #14181b; color: #f2f4f3; }
.cta-tint { background: color-mix(in srgb, var(--primary) 9%, var(--bg)); }
.cta-outline { border: 2px solid var(--primary); }
.cta h2 { margin-bottom: 8px; }
.cta p { margin: 0; opacity: .9; }
.cta-actions { display: flex; gap: 10px; flex-wrap: wrap; justify-content: center; }
.cta-split { grid-template-columns: 1fr auto; text-align: left; align-items: center; }
.cta-split .cta-actions { justify-content: flex-end; }
@media (max-width: 700px) { .cta-split { grid-template-columns: 1fr; text-align: center; } .cta-split .cta-actions { justify-content: center; } }

/* Stats */
.stats { display: grid; gap: 18px; text-align: center; }
.stat-value { font-family: var(--hfont); font-size: clamp(2rem, 4vw, 2.8rem); font-weight: 700; color: var(--primary); line-height: 1; }
.stat-label { margin-top: 8px; opacity: .78; font-size: .95rem; }

/* Logos */
.logos { display: flex; flex-wrap: wrap; gap: 28px 40px; justify-content: center; align-items: center; }
.logo img { max-height: 44px; width: auto; margin: 0; border-radius: 0; }
.logos-gray img { filter: grayscale(1); opacity: .65; transition: .2s; }
.logos-gray a:hover img { filter: none; opacity: 1; }

/* Steps */
.steps { list-style: none; margin: 0; padding: 0; counter-reset: step; display: grid; gap: 22px; }
.steps-row { grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); }
.steps li { counter-increment: step; position: relative; padding-left: 52px; }
.steps li::before {
  content: counter(step); position: absolute; left: 0; top: 0; width: 38px; height: 38px;
  display: grid; place-items: center; border-radius: 50%; background: var(--primary); color: #fff; font-weight: 700;
}
.steps li p { margin: 0; font-size: .95rem; opacity: .85; }

/* Notice bar */
.notice-bar { padding: 10px 16px; border-radius: var(--radius); text-align: center; font-size: .95rem; font-weight: 500; }
.notice-bar a { color: inherit; font-weight: 700; }
.notice-primary { background: var(--primary); color: #fff; }
.notice-secondary { background: var(--secondary); color: #fff; }
.notice-dark { background: #14181b; color: #fff; }
.notice-tint { background: color-mix(in srgb, var(--primary) 10%, var(--bg)); }

/* Video and map */
.video-medium figure { max-width: 70%; margin-inline: auto; }
.video-frame { position: relative; border-radius: var(--radius); overflow: hidden; background: #000; }
.video-frame iframe { position: absolute; inset: 0; width: 100%; height: 100%; border: 0; }
.video-frame.ratio-16-9 { aspect-ratio: 16 / 9; } .video-frame.ratio-4-3 { aspect-ratio: 4 / 3; }
.video-frame.ratio-1-1 { aspect-ratio: 1; } .video-frame.ratio-9-16 { aspect-ratio: 9 / 16; max-width: 420px; margin-inline: auto; }
.map iframe, .contact-map iframe { width: 100%; border: 0; border-radius: var(--radius); display: block; }
.map-short iframe { height: 240px; } .map-medium iframe { height: 380px; } .map-tall iframe { height: 520px; }
.contact-map iframe { height: 100%; min-height: 260px; }

/* Header */
.site-header { margin: 0 0 28px; width: 100vw; margin-left: calc(50% - 50vw); padding: 0 var(--gutter); }
.header-line { border-bottom: 1px solid color-mix(in srgb, var(--text) 12%, transparent); }
.header-filled { background: color-mix(in srgb, var(--text) 5%, var(--bg)); }
.header-sticky { position: sticky; top: 0; z-index: 20; background: var(--bg); }
.site-nav { max-width: var(--maxw); margin: 0 auto; display: flex; align-items: center; gap: 20px; min-height: 64px; position: relative; }
.brand { display: flex; align-items: center; gap: 10px; color: inherit; text-decoration: none; font-weight: 700; font-size: 1.15rem; font-family: var(--hfont); }
.brand-logo { height: 36px; width: auto; margin: 0; border-radius: 0; }
.nav-links { display: flex; align-items: center; gap: 22px; margin-left: auto; }
.nav-links ul { list-style: none; margin: 0; padding: 0; display: flex; gap: 22px; }
.nav-links a { color: inherit; text-decoration: none; font-weight: 500; }
.nav-links a:hover { color: var(--primary); }
.nav-cta { padding: 8px 18px; }
.nav-burger { display: none; margin-left: auto; width: 40px; height: 40px; cursor: pointer; flex-direction: column; justify-content: center; gap: 5px; align-items: center; }
.nav-burger span { display: block; width: 22px; height: 2px; background: currentColor; }
@media (max-width: 760px) {
  .nav-burger { display: flex; }
  .nav-links {
    display: none; position: absolute; top: 100%; left: calc(-1 * var(--gutter)); right: calc(-1 * var(--gutter)); padding: 10px var(--gutter) 18px;
    background: var(--bg); border-bottom: 1px solid color-mix(in srgb, var(--text) 12%, transparent);
    flex-direction: column; align-items: stretch; gap: 8px; margin: 0;
  }
  .nav-links ul { flex-direction: column; gap: 4px; }
  .nav-links a:not(.btn) { display: block; padding: 8px 0; }
  .nav-toggle:checked ~ .nav-links { display: flex; }
}

/* Footer */
.site-footer { margin: 60px 0 0; width: 100vw; margin-left: calc(50% - 50vw); padding: 44px var(--gutter) 28px; font-size: .95rem; }
.footer-tint { background: color-mix(in srgb, var(--text) 5%, var(--bg)); }
.footer-dark { background: #14181b; color: #e6e9e8; }
.footer-dark a { color: #fff; }
.footer-grid { max-width: var(--maxw); margin: 0 auto; display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 30px; }
.footer-brand { grid-column: span 1; }
.footer-brand .brand-text { font-weight: 700; font-size: 1.15rem; font-family: var(--hfont); margin-bottom: 8px; }
.footer-brand p { opacity: .8; }
.footer-col h3 { font-size: .85rem; letter-spacing: .06em; text-transform: uppercase; opacity: .7; margin-bottom: 10px; }
.footer-col ul { list-style: none; margin: 0; padding: 0; display: grid; gap: 7px; }
.footer-col a { color: inherit; text-decoration: none; }
.footer-col a:hover { text-decoration: underline; }
.footer-bottom { max-width: var(--maxw); margin: 30px auto 0; padding-top: 18px; border-top: 1px solid color-mix(in srgb, currentColor 15%, transparent); font-size: .85rem; opacity: .75; }

/* Social links */
.social { display: flex; flex-wrap: wrap; gap: 8px; }
.align-center .social { justify-content: center; }
.social-link { text-decoration: none; font-weight: 600; font-size: .9rem; }
.social-pills .social-link {
  padding: 7px 14px; border-radius: 999px; color: inherit;
  border: 1px solid color-mix(in srgb, currentColor 22%, transparent);
}
.social-pills .social-link:hover { background: var(--primary); color: #fff; border-color: var(--primary); }
.social-text .social-link { color: inherit; opacity: .8; }
.social-text .social-link:hover { opacity: 1; text-decoration: underline; }

/* Contact */
.contact-grid { display: grid; gap: 28px; }
.contact-with-map .contact-grid { grid-template-columns: 1fr 1.3fr; }
.contact-list { margin: 0; display: grid; gap: 16px; }
.contact-list dt { font-size: .8rem; font-weight: 700; letter-spacing: .08em; text-transform: uppercase; opacity: .65; margin-bottom: 3px; }
.contact-list dd { margin: 0; }
@media (max-width: 700px) { .contact-with-map .contact-grid { grid-template-columns: 1fr; } }

/* Table */
.tbl-wrap { overflow-x: auto; }
.tbl { width: 100%; border-collapse: collapse; }
.tbl caption { text-align: left; font-weight: 600; padding-bottom: 10px; }
.tbl th, .tbl td { padding: 12px 14px; text-align: left; border-bottom: 1px solid color-mix(in srgb, var(--text) 12%, transparent); vertical-align: top; }
.tbl th { font-weight: 600; background: color-mix(in srgb, var(--text) 5%, transparent); }
.tbl-striped tbody tr:nth-child(even) { background: color-mix(in srgb, var(--text) 3%, transparent); }
.tbl-compact th, .tbl-compact td { padding: 7px 10px; font-size: .92rem; }

/* ---- Design layer: one wrapper around any block ---- */
.sec { position: relative; }
.sec-bg-tint { background: color-mix(in srgb, var(--primary) 8%, var(--bg)); }
.sec-bg-primary { background: var(--primary); color: #fff; }
.sec-bg-secondary { background: var(--secondary); color: #fff; }
.sec-bg-dark { background: #14181b; color: #f2f4f3; }
.sec-bg-custom { background: var(--sec-bg); }
.sec-bg-image { background: var(--sec-img) center / cover no-repeat; color: #fff; }
.sec-bg-image::before { content: ""; position: absolute; inset: 0; background: rgba(8, 12, 14, .5); }
.sec-bg-image > .sec-inner { position: relative; }
.sec-text { color: var(--sec-text); }
.sec-bg-primary a:not(.btn), .sec-bg-secondary a:not(.btn), .sec-bg-dark a:not(.btn), .sec-bg-image a:not(.btn) { color: inherit; }
.sec-bg-primary .btn:not(.btn-outline), .sec-bg-secondary .btn:not(.btn-outline) { background: #fff; color: var(--primary); border-color: #fff; }
.sec-bg-primary .btn-outline, .sec-bg-secondary .btn-outline, .sec-bg-dark .btn-outline, .sec-bg-image .btn-outline { color: #fff; border-color: #fff; }
.sec-bg-primary .eyebrow, .sec-bg-secondary .eyebrow, .sec-bg-image .eyebrow { color: inherit; opacity: .85; }
.sec-bg-primary .stat-value, .sec-bg-dark .stat-value, .sec-bg-image .stat-value { color: inherit; }
/* Any background gets breathing room; the padding setting adds more. */
.sec[class*="sec-bg-"] > .sec-inner { padding-top: 32px; padding-bottom: 32px; }
.sec-pad-small > .sec-inner { padding-top: 20px; padding-bottom: 20px; }
.sec-pad-medium > .sec-inner { padding-top: 56px; padding-bottom: 56px; }
.sec-pad-large > .sec-inner { padding-top: 104px; padding-bottom: 104px; }
.sec[class*="sec-bg-"]:not(.sec-full):not(.sec-wide) { border-radius: var(--radius); }
.sec[class*="sec-bg-"]:not(.sec-full):not(.sec-wide) > .sec-inner { padding-left: 11px; padding-right: 11px; }
.sec > .sec-inner > .blk:first-child { margin-top: 0; }
.sec > .sec-inner > .blk:last-child { margin-bottom: 0; }
.sec + .sec, .sec { margin: 28px 0; }
/* Breakouts: the background reaches the viewport edge while the
   content stays in the column. */
.sec-wide { width: min(1560px, 100vw); margin-left: calc(50% - min(780px, 50vw)); }
.sec-full { width: 100vw; margin-left: calc(50% - 50vw); }
.sec-wide > .sec-inner, .sec-full > .sec-inner { padding-left: var(--gutter); padding-right: var(--gutter); }
.sec-full > .sec-inner > .blk, .sec-full > .sec-inner > .pb-html { max-width: var(--maxw); margin-left: auto; margin-right: auto; }
.sec-full > .sec-inner > .blk.site-header, .sec-full > .sec-inner > .blk.site-footer, .sec-full > .sec-inner > .hero-cover { max-width: none; }
/* A header or footer inside a design wrapper: the wrapper is the band
   (full width, its colour), so the block gives up its own breakout,
   gutters and background, and the sticky behaviour moves to the wrapper. */
.sec > .sec-inner > .site-header, .sec > .sec-inner > .site-footer {
  width: auto; margin-left: 0; margin-right: 0; padding-left: 0; padding-right: 0;
}
.sec[class*="sec-bg-"] > .sec-inner > .site-header,
.sec[class*="sec-bg-"] > .sec-inner > .site-footer { background: transparent; border-bottom-color: transparent; }
.sec > .sec-inner > .site-header { margin-bottom: 0; }
.sec > .sec-inner > .site-footer { margin-top: 0; }
.sec:has(> .sec-inner > .site-header) { margin-top: 0; }
.sec:has(> .sec-inner > .site-footer) { margin-bottom: 0; }
.sec:has(> .sec-inner > .header-sticky) { position: sticky; top: 0; z-index: 20; }
/* The bar itself is the padding; a background alone should not make
   the header twice as tall. An explicit padding choice still applies. */
.sec[class*="sec-bg-"]:not([class*="sec-pad-"]):has(> .sec-inner > .site-header) > .sec-inner { padding-top: 0; padding-bottom: 0; }
.sec[class*="sec-bg-"]:not([class*="sec-pad-"]):has(> .sec-inner > .site-footer) > .sec-inner { padding-top: 0; padding-bottom: 0; }
/* Coloured bands: the nav and footer text read on the colour. */
.sec-bg-primary .nav-links a:hover, .sec-bg-secondary .nav-links a:hover, .sec-bg-dark .nav-links a:hover, .sec-bg-image .nav-links a:hover { opacity: .8; color: inherit; }
.sec-bg-primary .nav-cta, .sec-bg-secondary .nav-cta { background: #fff; color: var(--primary); border-color: #fff; }
/* Nothing above the first band on the page. */
.page > .sec:first-child, .page > .blk:first-child { margin-top: 0; }
/* ...and nothing below the last one. The page's 80px bottom padding is
   for a page that ends in content; a page that ends in a footer band
   would show it as a white strip under the footer. */
.page:has(> .site-footer:last-child),
.page:has(> .sec:last-child > .sec-inner > .site-footer) { padding-bottom: 0; }
.page > .site-footer:last-child, .page > .sec:last-child > .sec-inner > .site-footer { margin-bottom: 0; }
/* Hidden per device */
@media (max-width: 700px) { .sec-hide-mobile { display: none; } }
@media (min-width: 701px) { .sec-hide-desktop { display: none; } }
/* Scroll-in motion, only with the script that reveals it. */
html.motion .sec-anim { opacity: 0; transition: opacity .7s ease, transform .7s ease; }
html.motion .sec-anim-rise { transform: translateY(22px); }
html.motion .sec-anim.in { opacity: 1; transform: none; }
@media (prefers-reduced-motion: reduce) { html.motion .sec-anim { opacity: 1; transform: none; transition: none; } }
blockquote {
  margin: 0; padding: 8px 0 8px 22px; font-size: 1.25rem;
  border-left: 4px solid var(--primary);
}
blockquote cite { display: block; font-size: .9rem; font-style: normal; opacity: .7; margin-top: 8px; }
.lead-form { display: grid; gap: 14px; max-width: 560px; }
.lead-form label { display: grid; gap: 5px; font-size: .9rem; font-weight: 600; }
.lead-form input, .lead-form textarea {
  font: inherit; padding: 10px 12px; border-radius: var(--radius);
  border: 1px solid color-mix(in srgb, var(--text) 25%, transparent);
  background: var(--bg); color: var(--text); width: 100%;
}
.lead-form button {
  font: inherit; font-weight: 600; color: #fff; background: var(--primary);
  border: 0; border-radius: var(--radius); padding: 12px 26px; cursor: pointer; justify-self: start;
}
.lead-form button:disabled { opacity: .6; cursor: default; }
.lead-form .hp { position: absolute; left: -9999px; width: 1px; height: 1px; opacity: 0; }
.form-error { color: #b3261e; font-size: .95rem; }
.form-done { padding: 18px; border-radius: 8px; background: color-mix(in srgb, var(--primary) 12%, transparent); font-weight: 600; }
.pb-space-small { height: 20px; }
.pb-space-medium { height: 48px; }
.pb-space-large { height: 96px; }
.preview-banner {
  position: sticky; top: 0; z-index: 10; text-align: center;
  background: #a8791f; color: #fff; font-size: 13px; padding: 6px 10px;
}

/* HTML block. `pb-html-styled` opts the markup into the page's own
   typography; leaving it off is for a pasted widget that brings its
   own. Every rule is scoped under .pb-html so pasted markup cannot
   restyle the rest of the page. */
.pb-html { margin: 0 0 26px; }
.pb-html-wide { max-width: min(1560px, 94vw); margin-inline: auto; }
.pb-html-full { max-width: none; }

/* HTML mode: the author's markup IS the page, so the column moves off
   the wrapper and onto the block. That is also what makes "full bleed"
   actually reach the edges — nested inside .page's max-width it never
   could. */
.page-html { max-width: none; margin: 0; padding: 0 0 80px; }
.page-html .pb-html { margin: 0; }
.page-html .pb-html:not(.pb-html-wide):not(.pb-html-full) {
  max-width: var(--maxw); margin-inline: auto; padding: 0 var(--gutter);
}
.page-html .pb-html-wide { padding: 0 var(--gutter); }
.page-html .pb-html-full { max-width: none; padding: 0; }
.pb-html > *:last-child { margin-bottom: 0; }
/* Embeds keep a 16:9 box and never overflow the column. */
.pb-html iframe { width: 100%; max-width: 100%; aspect-ratio: 16 / 9; height: auto; border: 0; }
.pb-html img { max-width: 100%; height: auto; }
/* Anything that carries its own intrinsic size is held to the column,
   so a pasted <marquee>, <canvas> or <textarea> cannot make the page
   scroll sideways on a phone. */
.pb-html marquee { display: block; max-width: 100%; }
.pb-html :is(canvas, svg, video, textarea, table, pre, math) { max-width: 100%; }
.pb-html marquee img { max-width: none; }
.pb-html-styled h1, .pb-html-styled h2, .pb-html-styled h3,
.pb-html-styled h4, .pb-html-styled h5, .pb-html-styled h6 {
  line-height: 1.25; margin: 1.6em 0 .5em; font-weight: 600;
}
.pb-html-styled h2 { font-size: 1.55rem; }
.pb-html-styled h3 { font-size: 1.25rem; }
.pb-html-styled h4 { font-size: 1.08rem; }
.pb-html-styled p { margin: 0 0 1em; line-height: 1.65; }
.pb-html-styled ul, .pb-html-styled ol { margin: 0 0 1em; padding-left: 1.4em; line-height: 1.65; }
.pb-html-styled li { margin: .3em 0; }
.pb-html-styled a { color: var(--primary); text-decoration: underline; }
.pb-html-styled blockquote {
  margin: 1.2em 0; padding: .2em 0 .2em 1.1em;
  border-left: 3px solid var(--primary);
  color: color-mix(in srgb, var(--text) 72%, transparent);
}
.pb-html-styled code {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: .9em; padding: .15em .35em; border-radius: 3px;
  background: color-mix(in srgb, var(--text) 8%, transparent);
}
.pb-html-styled pre {
  padding: 14px 16px; border-radius: 4px; overflow-x: auto; margin: 0 0 1em;
  background: color-mix(in srgb, var(--text) 8%, transparent);
}
.pb-html-styled pre code { background: none; padding: 0; }
/* A wide table scrolls itself rather than making the page scroll —
   the difference between a readable price table and a broken layout. */
.pb-html-styled table {
  width: 100%; border-collapse: collapse; margin: 0 0 1em;
  display: block; overflow-x: auto;
}
.pb-html-styled th, .pb-html-styled td {
  padding: 8px 10px; text-align: left;
  border: 1px solid color-mix(in srgb, var(--text) 15%, transparent);
}
.pb-html-styled th { font-weight: 600; background: color-mix(in srgb, var(--text) 5%, transparent); }
.pb-html-styled figure { margin: 0 0 1em; }
.pb-html-styled figcaption {
  font-size: .86rem; margin-top: .4em;
  color: color-mix(in srgb, var(--text) 65%, transparent);
}
.pb-html-styled hr {
  border: 0; margin: 1.6em 0;
  border-top: 1px solid color-mix(in srgb, var(--text) 15%, transparent);
}
/* Form controls. A browser's defaults are a system font at 13px, which
   next to the page's 17px prose reads as a bug rather than a choice —
   so the controls inherit type, colour and radius like every other
   block does. Hand-written HTML is still free to override all of it:
   these are single-class selectors, and an author's own rule or style
   attribute outranks them. */
.pb-html-styled fieldset {
  margin: 0 0 1em; padding: 14px 16px; border-radius: 6px;
  border: 1px solid color-mix(in srgb, var(--text) 15%, transparent);
}
.pb-html-styled legend { padding: 0 6px; font-weight: 600; }
.pb-html-styled label { display: inline-block; margin-bottom: .35em; }
.pb-html-styled :is(input, select, textarea, button) { font: inherit; color: inherit; }
.pb-html-styled :is(input, select, textarea) {
  display: block; width: 100%; max-width: 440px; margin: 0 0 1em;
  padding: 10px 12px; border-radius: 6px; background: var(--bg);
  border: 1px solid color-mix(in srgb, var(--text) 22%, transparent);
}
.pb-html-styled textarea { min-height: 7em; resize: vertical; }
.pb-html-styled select[multiple], .pb-html-styled select[size] { padding: 6px; }
/* Checkboxes and radios sit *in* their label line, so the block
   treatment above would break every consent line ever written. */
.pb-html-styled input:is([type="checkbox"], [type="radio"]) {
  display: inline-block; width: auto; margin: 0 .5em 0 0; padding: 0;
  vertical-align: baseline; accent-color: var(--primary);
}
.pb-html-styled input:is([type="submit"], [type="button"], [type="reset"], [type="color"]),
.pb-html-styled button {
  display: inline-block; width: auto; margin: 0 .4em 1em 0;
  padding: 11px 20px; border: 0; border-radius: 6px; cursor: pointer;
  background: var(--primary); color: #fff; font-weight: 600;
}
.pb-html-styled input:is([type="checkbox"], [type="radio"], [type="file"], [type="range"]) {
  background: none;
}
.pb-html-styled :is(input, select, textarea, button):disabled { opacity: .6; cursor: not-allowed; }
.pb-html-styled :is(input, select, textarea, button):focus-visible {
  outline: 2px solid var(--primary); outline-offset: 2px;
}
"""


# ------------------------------------------------------------------- head
def _meta(name: str, content: str | None, *, prop: bool = False) -> str:
    """One meta tag, or nothing when there is no value to put in it."""
    if not content:
        return ""
    attribute = "property" if prop else "name"
    return f'<meta {attribute}="{name}" content="{_esc(content)}">\n'


def _render_head(
    *,
    title: str,
    description: str | None,
    seo: dict,
    page_url: str | None,
    image_url: str | None,
    preview: bool,
) -> str:
    """The document head's meta tags.

    Values fall back the way an author expects rather than repeating
    themselves: an empty og:title uses the SEO title, then the page
    title; an empty og:description uses the meta description. A tag with
    nothing behind it is left out entirely — an empty `content=""` is
    worse than no tag, because a crawler reads it as an answer.
    """
    meta_title = seo.get("meta_title") or title
    og_title = seo.get("og_title") or meta_title
    og_description = seo.get("og_description") or description
    twitter_title = seo.get("twitter_title") or og_title
    twitter_description = seo.get("twitter_description") or og_description
    # A card with an image but no card type would render as a thumbnail
    # strip; large_image is what a page with an og:image wants.
    twitter_card = seo.get("twitter_card") or ("summary_large_image" if image_url else "summary")

    # A draft preview is never indexable, whatever the page's own flags
    # say. Otherwise the flags are only worth a tag when one is set:
    # "index, follow" is the default and states nothing.
    if preview:
        robots = "noindex, nofollow"
    else:
        robots = ", ".join(
            ("noindex" if seo.get("noindex") else "index",
             "nofollow" if seo.get("nofollow") else "follow"),
        ) if (seo.get("noindex") or seo.get("nofollow")) else ""

    canonical = seo.get("canonical") or (page_url if not preview else None)

    parts = [
        f"<title>{_esc(meta_title)}</title>\n",
        _meta("description", description),
        _meta("robots", robots),
        f'<link rel="canonical" href="{_esc(canonical)}">\n' if canonical else "",
        _meta("og:title", og_title, prop=True),
        _meta("og:description", og_description, prop=True),
        _meta("og:type", seo.get("og_type") or "website", prop=True),
        _meta("og:url", page_url, prop=True),
        _meta("og:image", image_url, prop=True),
        _meta("og:image:alt", seo.get("og_image_alt") if image_url else None, prop=True),
        _meta("twitter:card", twitter_card),
        _meta("twitter:title", twitter_title),
        _meta("twitter:description", twitter_description),
        _meta("twitter:image", image_url),
    ]
    return "".join(parts)


def render_page(
    *,
    title: str,
    description: str | None,
    blocks: list[dict],
    theme: dict,
    tenant_slug: str,
    forms: dict[str, list] | None = None,
    seo: dict | None = None,
    page_url: str | None = None,
    image_url: str | None = None,
    mode: str = "blocks",
    preview: bool = False,
) -> str:
    """Render a full HTML document.

    `forms` maps form slug -> field list. `seo` is the cleaned SEO block;
    `page_url` and `image_url` are absolute URLs the caller resolved
    (the site's origin and a media id are both outside this module), and
    the og:url / og:image tags are simply omitted without them rather
    than emitted as paths a crawler cannot follow.

    `mode` only changes the wrapper. An HTML page is still a block list
    (one html block), so publish, revisions and sanitize-on-write are
    the same code either way — it just is not wrapped in the builder's
    fixed content column, because the author's markup is the page.
    """
    theme = clean_theme(theme)
    ctx = {"tenant_slug": tenant_slug, "forms": forms or {}, "needs_form_js": False}

    body_parts = []
    rendered: list[dict] = []
    for block in blocks if isinstance(blocks, list) else []:
        renderer = RENDERERS.get(block.get("type"))
        if renderer:
            body_parts.append(_apply_design(block, renderer(block, ctx), ctx))
            rendered.append(block)

    # A footer at the end of the page is emitted *after* the content
    # column rather than inside it, and the column is the flex child
    # that takes the leftover height: that is what puts the footer on
    # the bottom edge of the window on a page with one paragraph on it.
    # Only <body>'s own children become flex items, so margins between
    # the blocks inside the column keep collapsing exactly as before.
    footer_html = ""
    if rendered and rendered[-1].get("type") == "footer":
        footer_html = body_parts.pop()

    body_class = ' class="has-footer"' if footer_html else ""
    banner = '<div class="preview-banner">Draft preview — this is not the live page</div>' if preview else ""
    form_script = '<script src="/js/page-form.js" defer></script>' if ctx["needs_form_js"] else ""
    if ctx.get("needs_motion_js"):
        form_script += '<script src="/js/page-motion.js" defer></script>'
    head_meta = _render_head(
        title=title,
        description=description,
        seo=seo or {},
        page_url=page_url,
        image_url=image_url,
        preview=preview,
    )

    heading_font = theme["heading_font"] if theme["heading_font"] != "same" else theme["font"]
    css_vars = (
        f"--primary:{theme['primary']};--secondary:{theme['secondary']};"
        f"--bg:{theme['background']};--text:{theme['text']};"
        f"--font:{FONT_STACKS[theme['font']]};--hfont:{FONT_STACKS[heading_font]};"
        f"--radius:{RADII[theme['radius']]};--maxw:{MAX_WIDTHS[theme['max_width']]};"
        f"--gutter:{PAGE_GUTTER}"
    )

    return (
        "<!doctype html>\n"
        '<html lang="en">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"{head_meta}"
        f"<style>:root{{{css_vars}}}{_PAGE_CSS}</style>\n"
        f"{form_script}"
        f'</head>\n<body{body_class}>\n'
        f"{banner}"
        f'<main class="{"page page-html" if mode == "html" else "page"}">'
        f'{"".join(body_parts)}</main>\n'
        f"{footer_html}"
        "</body>\n</html>"
    )
