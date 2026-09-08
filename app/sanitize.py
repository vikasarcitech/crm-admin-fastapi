"""HTML sanitization for rich-text content.

Rich text is the one place this platform stores markup instead of data,
so it is also the one place an XSS could enter. Everything written into
``content_items.body``, a reusable block, an email template or a
campaign passes through :func:`clean_html` first — sanitizing on write
means a later template change cannot accidentally emit raw stored HTML.

Sanitization is delegated to nh3 (Rust ``ammonia``), an allow-list
cleaner, rather than a hand-rolled regex pass. On top of it:

* ``<iframe>`` survives only when its ``src`` host is on
  ``EMBED_ALLOWED_HOSTS`` — that is what makes "embed support" safe.
* ``style`` is dropped entirely; layout comes from the frontend's CSS.
* ``javascript:``/``data:`` URLs are rejected by the scheme allow-list.
* ``rel`` stays author-controlled (SEO needs ``nofollow``/``sponsored``)
  but is filtered to a token allow-list. Every browser since 2021
  implies ``noopener`` for ``target="_blank"``, so leaving ``rel`` to
  the author no longer reopens reverse tabnabbing.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from urllib.parse import urlparse

import nh3

from .config import settings

# Structural + inline tags a marketing site actually needs. Deliberately
# no <style>, <script>, <form>, <input> or <object>.
ALLOWED_TAGS: set[str] = {
    "p", "br", "hr", "span", "div", "section",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "strong", "b", "em", "i", "u", "s", "del", "ins", "mark", "small",
    "sub", "sup", "abbr", "cite", "q", "blockquote", "code", "pre", "kbd",
    "ul", "ol", "li", "dl", "dt", "dd",
    "a", "img", "figure", "figcaption", "picture", "source",
    "table", "thead", "tbody", "tfoot", "tr", "th", "td", "caption", "colgroup", "col",
    "iframe", "video", "audio", "track",
    "details", "summary", "time",
}

ALLOWED_ATTRIBUTES: dict[str, set[str]] = {
    "*": {"class", "id", "title", "dir", "lang"},
    "a": {"href", "target", "rel", "download", "hreflang"},
    "img": {"src", "srcset", "sizes", "alt", "width", "height", "loading", "decoding"},
    "source": {"src", "srcset", "sizes", "type", "media"},
    "iframe": {
        "src", "width", "height", "allow", "allowfullscreen",
        "loading", "referrerpolicy", "frameborder",
    },
    "video": {"src", "poster", "width", "height", "controls", "muted", "loop", "playsinline"},
    "audio": {"src", "controls", "loop"},
    "track": {"src", "kind", "srclang", "label", "default"},
    "th": {"colspan", "rowspan", "scope", "headers", "abbr"},
    "td": {"colspan", "rowspan", "headers"},
    "col": {"span"},
    "colgroup": {"span"},
    "time": {"datetime"},
    "blockquote": {"cite"},
    "q": {"cite"},
    "del": {"cite", "datetime"},
    "ins": {"cite", "datetime"},
    "details": {"open"},
    "ol": {"start", "reversed", "type"},
}

URL_SCHEMES: set[str] = {"http", "https", "mailto", "tel"}

# Tags whose *contents* are discarded too, not just the tag.
CLEAN_CONTENT_TAGS: set[str] = {"script", "style", "noscript", "template", "iframe"}

_SRCLESS_IFRAME = re.compile(
    r"<iframe(?![^>]*\ssrc=)[^>]*>.*?</iframe>", re.IGNORECASE | re.DOTALL
)

_EMBED_HOST_RE = re.compile(r"^[a-z0-9.-]+$")

# Link relationships worth keeping; anything else is noise or a tracker hint.
_REL_TOKENS = frozenset(
    {"nofollow", "noopener", "noreferrer", "sponsored", "ugc",
     "external", "author", "license", "alternate", "canonical", "me", "prev", "next"}
)


def _embed_hosts() -> tuple[str, ...]:
    return settings.embed_allowed_hosts


def _host_allowed(url: str) -> bool:
    """True when the URL is https and its host is on the embed allow-list.

    Matching is exact or a dot-suffix, so 'youtube.com' admits
    'www.youtube.com' but never 'notyoutube.com'.
    """
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return False
    if parsed.scheme != "https":
        return False
    host = (parsed.hostname or "").lower()
    if not host or not _EMBED_HOST_RE.match(host):
        return False
    return any(host == allowed or host.endswith(f".{allowed}") for allowed in _embed_hosts())


def _attribute_filter(tag: str, attribute: str, value: str) -> str | None:
    """Last word on individual attributes. None drops the attribute."""
    # An iframe with no allowed src is useless and its src is the risk.
    if tag == "iframe":
        if attribute == "src":
            return value if _host_allowed(value) else None
        if attribute == "allow":
            # Only capability tokens, never a wildcard origin grant.
            tokens = [t.strip() for t in value.split(";") if t.strip()]
            safe = [t for t in tokens if re.fullmatch(r"[a-z-]+(?: '(?:self|none)')?", t)]
            return "; ".join(safe) or None
        if attribute == "referrerpolicy":
            return value if value in {"no-referrer", "strict-origin-when-cross-origin"} else None

    # Numeric-only geometry; 'width="100%"' is fine, 'width="expression(…)"' is not.
    if attribute in {"width", "height"} and not re.fullmatch(r"\d{1,5}%?", value.strip()):
        return None

    if attribute == "target":
        return "_blank" if value.strip() == "_blank" else None

    if attribute == "rel":
        tokens = [t for t in value.lower().split() if t in _REL_TOKENS]
        return " ".join(dict.fromkeys(tokens)) or None

    # class/id are allowed but must not carry anything script-like.
    if attribute in {"class", "id"} and not re.fullmatch(r"[\w\s:-]{0,300}", value):
        return None

    return value


def clean_html(raw: str | None, *, limit: int = 400_000) -> str | None:
    """Sanitize a rich-text fragment. Returns None for empty input.

    ``limit`` caps the *input*: a multi-megabyte paste should be rejected
    before the cleaner walks it, not after.
    """
    if raw is None:
        return None
    text = str(raw)
    if len(text) > limit:
        raise ValueError(f"content is too large (limit {limit:,} characters)")
    if not text.strip():
        return None

    cleaned = nh3.clean(
        text,
        tags=ALLOWED_TAGS,
        attributes={k: set(v) for k, v in ALLOWED_ATTRIBUTES.items()},
        # iframe is in ALLOWED_TAGS, so it is not content-cleaned here;
        # the rest have their bodies removed with them.
        clean_content_tags=CLEAN_CONTENT_TAGS - {"iframe"},
        url_schemes=URL_SCHEMES,
        attribute_filter=_attribute_filter,
        strip_comments=True,
        # None: rel is filtered by _attribute_filter instead of forced,
        # so an author can mark a paid link 'sponsored'.
        link_rel=None,
        # Relative URLs (/about, /media/…) must survive; they are the
        # normal case for a site's own links and images.
        url_relative="pass_through",
    )
    # An iframe whose src the filter rejected is left behind as an empty
    # (inert, but visible) box. Drop the element rather than ship a frame
    # that renders as a blank rectangle on the live site.
    cleaned = _SRCLESS_IFRAME.sub("", cleaned)
    return cleaned or None


# --------------------------------------------------------------- extraction
class _Stripper(HTMLParser):
    """Collects text and the src/href targets of a sanitized fragment."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.text: list[str] = []
        self.urls: list[str] = []

    def handle_data(self, data: str) -> None:
        self.text.append(data)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"p", "br", "li", "h1", "h2", "h3", "h4", "div", "tr"}:
            self.text.append(" ")
        for name, value in attrs:
            if name in {"src", "href"} and value:
                self.urls.append(value)


def _parse(html: str) -> _Stripper:
    stripper = _Stripper()
    stripper.feed(html)
    stripper.close()
    return stripper


def to_text(html: str | None) -> str:
    """Plain text, for excerpts, search and SEO character counts."""
    if not html:
        return ""
    return re.sub(r"\s+", " ", "".join(_parse(html).text)).strip()


def excerpt(html: str | None, limit: int = 200) -> str | None:
    """First ``limit`` characters of plain text, cut on a word boundary."""
    text = to_text(html)
    if not text:
        return None
    if len(text) <= limit:
        return text
    head = text[: limit + 1]
    cut = head.rfind(" ")
    return f"{head[:cut] if cut > limit // 2 else text[:limit]}…"


def referenced_urls(html: str | None) -> list[str]:
    """Every src/href in the fragment — how media usage is tracked."""
    if not html:
        return []
    seen: dict[str, None] = {}
    for url in _parse(html).urls:
        seen.setdefault(url.strip(), None)
    return list(seen)
