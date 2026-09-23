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
* ``style`` is cleaned rather than dropped (:func:`clean_css`), and a
  ``<style>`` block is kept only where the caller asks for it. CSS
  cannot run script in any browser this platform supports, so what the
  CSS cleaner is for is remote resource loading and the handful of
  legacy constructs that once could execute.
* ``javascript:`` URLs are rejected by the scheme allow-list, and a
  ``data:`` URL survives only as a non-SVG inline image.
* Form controls (``<form>``, ``<select>``, ``<textarea>`` …) and the
  presentational tags HTML deprecated but browsers still render
  (``<marquee>``, ``<center>``, ``<font>`` …) are kept: pasted markup is
  full of them, and an element that vanishes is how "paste your HTML"
  ends up looking broken. What makes them safe is the same thing that
  makes a ``<div>`` safe — every ``on*`` handler is off the attribute
  allow-list, so nothing in the fragment can run.
* ``rel`` stays author-controlled (SEO needs ``nofollow``/``sponsored``)
  but is filtered to a token allow-list. Every browser since 2021
  implies ``noopener`` for ``target="_blank"``, so leaving ``rel`` to
  the author no longer reopens reverse tabnabbing.
"""

from __future__ import annotations

import re
from collections import Counter
from html.parser import HTMLParser
from urllib.parse import urlparse

import nh3

from .config import settings

# Inline SVG, drawing elements only. Everything SVG can use to reach
# outside itself is left out on purpose: no <script>, no <foreignObject>
# (which smuggles HTML back in), no <use>/<image> (which reference other
# documents), and none of the <animate> family (which can animate an
# attribute into a URL). What is left draws shapes.
SVG_TAGS: set[str] = {
    "svg", "g", "defs", "symbol", "title", "desc",
    "path", "rect", "circle", "ellipse", "line", "polyline", "polygon",
    "text", "tspan", "textpath",
    "lineargradient", "radialgradient", "stop", "clippath", "mask", "pattern",
}

# Form controls. There is no handler attribute and no <script> to give
# one meaning, so what an author gets is the markup: a <select> of
# plans, a newsletter <input>, the <label> that names it. `action` is
# filtered to https or one of our own paths (see _attribute_filter), and
# the page CSP holds form-action to the same set. Submissions that have
# to reach the CRM still belong in a Lead form block, which knows how to
# post them.
FORM_TAGS: set[str] = {
    "form", "label", "input", "select", "option", "optgroup", "datalist",
    "textarea", "fieldset", "legend", "output", "button",
}

# Presentational tags HTML dropped and browsers kept. Pasted markup is
# full of them; keeping the text while dropping the tag is the worst
# possible answer, because the page renders and looks wrong.
LEGACY_TAGS: set[str] = {
    "marquee", "center", "font", "big", "strike", "tt", "nobr", "acronym", "blink",
}

# MathML, minus the three elements that exist to change how the parser
# reads what follows them: <annotation-xml> (smuggles HTML back in, the
# way <foreignObject> does in SVG) and <mglyph>/<malignmark>.
MATHML_TAGS: set[str] = {
    "math", "semantics", "annotation", "merror", "mstyle", "mrow", "mfrac",
    "mi", "mn", "mo", "ms", "mtext", "mspace", "mpadded", "mphantom", "menclose",
    "msqrt", "mroot", "msub", "msup", "msubsup", "munder", "mover", "munderover",
    "mmultiscripts", "mprescripts", "none", "maction", "mfenced",
    "mtable", "mtr", "mtd", "mlabeledtr",
}

# Deliberately absent, and the only tags that are: <script>, <object>,
# <embed>, <applet>, <param> (they execute), <base>, <link>, <meta>
# (they re-point or re-fetch the document), <frame>/<frameset>,
# <template> and <noscript> (both smuggle unparsed markup past the
# cleaner), and <math>'s <annotation-xml>/<mglyph>/<malignmark> (parser
# confusion). <style> is separate: kept only where the caller asks for
# it (``allow_stylesheet``).
ALLOWED_TAGS: set[str] = {
    "p", "br", "wbr", "hr", "span", "div", "section",
    # Sectioning and layout wrappers. Inert containers — no scripting and
    # no styling reaches them (handlers are dropped, style is cleaned) —
    # and a page written as HTML is built out of exactly these.
    "header", "footer", "main", "article", "aside", "nav", "hgroup", "address",
    "search", "figure", "figcaption",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "strong", "b", "em", "i", "u", "s", "del", "ins", "mark", "small",
    "sub", "sup", "abbr", "cite", "q", "blockquote", "code", "pre", "kbd", "samp", "var",
    "bdi", "bdo", "ruby", "rt", "rp", "data",
    "ul", "ol", "li", "dl", "dt", "dd",
    "a", "img", "picture", "source",
    "table", "thead", "tbody", "tfoot", "tr", "th", "td", "caption", "colgroup", "col",
    "iframe", "video", "audio", "track",
    "details", "summary", "dialog", "time", "meter", "progress",
    "canvas", "map", "area", "menu", "rb", "rtc",
    *FORM_TAGS,
    *LEGACY_TAGS,
    *MATHML_TAGS,
    *SVG_TAGS,
}

# Attribute names any element may carry. `role` and the aria-*/data-*
# prefixes below are what make a hand-written page accessible; without
# them a <div role="navigation"> silently loses the half that mattered.
GENERIC_ATTRIBUTE_PREFIXES: set[str] = {"aria-", "data-"}

# One shared set for the form controls: an author who writes
# `required` on an <input> writes it on the <select> next to it too, and
# a per-tag split here only produces the kind of near-miss that makes
# one of the two silently lose the attribute.
_FORM_CONTROL_ATTRIBUTES: set[str] = {
    "name", "value", "type", "placeholder", "disabled", "readonly", "required",
    "checked", "selected", "multiple", "size", "rows", "cols", "wrap",
    "min", "max", "step", "minlength", "maxlength", "pattern", "inputmode",
    "autocomplete", "autofocus", "list", "label", "for", "form", "accept",
    "novalidate", "enctype", "method", "action", "target",
    "formaction", "formmethod", "formtarget", "formnovalidate",
    "src", "alt", "width", "height", "spellcheck", "accept-charset",
}

# Legacy presentation, per the tags that still honour it. Inert — they
# only ever move or colour something — and without them a pasted table
# or <marquee> renders as a stack of unstyled rows.
_LEGACY_PRESENTATION: set[str] = {
    "align", "valign", "bgcolor", "background", "border", "cellpadding",
    "cellspacing", "width", "height", "hspace", "vspace", "nowrap",
    "color", "face", "size", "frame", "rules", "summary", "char", "charoff",
}

ALLOWED_ATTRIBUTES: dict[str, set[str]] = {
    "*": {
        "class", "id", "title", "dir", "lang", "style", "role", "hidden",
        # Keyboard order and microdata: both are author intent that no
        # other attribute can express, and neither can reach script.
        "tabindex", "accesskey", "translate", "inert", "draggable",
        "itemprop", "itemscope", "itemtype", "itemid", "itemref",
    },
    "button": {"type", "disabled", "name", "value", "form", "autofocus",
               "formaction", "formmethod", "formtarget", "formnovalidate"},
    "li": {"value"},
    "dialog": {"open"},
    "meter": {"value", "min", "max", "low", "high", "optimum"},
    "progress": {"value", "max"},
    "data": {"value"},
    "bdo": {"dir"},
    "a": {"href", "target", "rel", "download", "hreflang", "name", "type", "ping"},
    "img": {"src", "srcset", "sizes", "alt", "width", "height", "loading",
            "decoding", "usemap", "ismap", "referrerpolicy", *_LEGACY_PRESENTATION},
    "map": {"name"},
    "area": {"shape", "coords", "href", "alt", "target", "rel", "download"},
    "canvas": {"width", "height"},
    "marquee": {"behavior", "direction", "scrollamount", "scrolldelay",
                "loop", "truespeed", *_LEGACY_PRESENTATION},
    "font": {"color", "face", "size"},
    "center": set(),
    "hr": {*_LEGACY_PRESENTATION},
    "div": {*_LEGACY_PRESENTATION},
    "p": {"align"},
    "h1": {"align"}, "h2": {"align"}, "h3": {"align"},
    "h4": {"align"}, "h5": {"align"}, "h6": {"align"},
    "caption": {"align"},
    "ul": {"type", *_LEGACY_PRESENTATION},
    "menu": {"type"},
    "source": {"src", "srcset", "sizes", "type", "media"},
    "iframe": {
        "src", "width", "height", "allow", "allowfullscreen",
        # `sandbox` can only take privileges away from the frame, so an
        # author is free to tighten their own embed with it.
        "sandbox", "loading", "referrerpolicy", "frameborder", "name",
    },
    "video": {"src", "poster", "width", "height", "controls", "muted", "loop", "playsinline"},
    "audio": {"src", "controls", "loop"},
    "track": {"src", "kind", "srclang", "label", "default"},
    "table": {*_LEGACY_PRESENTATION},
    "thead": {"align", "valign", "char", "charoff"},
    "tbody": {"align", "valign", "char", "charoff"},
    "tfoot": {"align", "valign", "char", "charoff"},
    "tr": {*_LEGACY_PRESENTATION},
    "th": {"colspan", "rowspan", "scope", "headers", "abbr", *_LEGACY_PRESENTATION},
    "td": {"colspan", "rowspan", "headers", *_LEGACY_PRESENTATION},
    "col": {"span"},
    "colgroup": {"span"},
    "time": {"datetime"},
    "blockquote": {"cite"},
    "q": {"cite"},
    "del": {"cite", "datetime"},
    "ins": {"cite", "datetime"},
    "details": {"open"},
    "ol": {"start", "reversed", "type", *_LEGACY_PRESENTATION},
}

for _form_tag in FORM_TAGS:
    ALLOWED_ATTRIBUTES.setdefault(_form_tag, set()).update(_FORM_CONTROL_ATTRIBUTES)

# Layout and notation only. No href/xlink:href: a formula does not link.
_MATHML_ATTRIBUTES: set[str] = {
    "display", "xmlns", "displaystyle", "scriptlevel", "mathvariant",
    "mathcolor", "mathbackground", "mathsize", "stretchy", "symmetric",
    "fence", "separator", "separators", "lspace", "rspace", "voffset",
    "linethickness", "numalign", "denomalign", "bevelled", "notation",
    "open", "close", "accent", "accentunder", "align", "columnalign",
    "rowalign", "columnlines", "rowlines", "frame", "framespacing",
    "columnspacing", "rowspacing", "columnspan", "rowspan", "width",
    "height", "depth", "largeop", "movablelimits", "form", "actiontype",
    "selection", "encoding", "definitionurl",
}
for _math_tag in MATHML_TAGS:
    ALLOWED_ATTRIBUTES[_math_tag] = set(_MATHML_ATTRIBUTES)

# Geometry and paint. No href/xlink:href anywhere: an inline graphic
# has no business pointing at another document.
_SVG_ATTRIBUTES: set[str] = {
    "viewbox", "viewBox", "xmlns", "width", "height", "preserveaspectratio",
    "d", "points", "x", "y", "x1", "y1", "x2", "y2", "cx", "cy", "r", "rx", "ry",
    "dx", "dy", "transform", "opacity", "offset",
    "fill", "fill-opacity", "fill-rule", "clip-rule", "clip-path", "mask",
    "stroke", "stroke-width", "stroke-linecap", "stroke-linejoin",
    "stroke-dasharray", "stroke-dashoffset", "stroke-opacity", "stroke-miterlimit",
    "stop-color", "stop-opacity", "gradientunits", "gradienttransform",
    "patternunits", "clippathunits", "maskunits", "spreadmethod",
    "text-anchor", "dominant-baseline", "font-family", "font-size", "font-weight",
    "letter-spacing", "vector-effect", "focusable", "overflow",
}
for _tag in SVG_TAGS:
    ALLOWED_ATTRIBUTES[_tag] = set(_SVG_ATTRIBUTES)

# `data` is here only so a pasted inline image survives; which data
# URLs actually pass is decided by _DATA_IMAGE below, not by this set.
URL_SCHEMES: set[str] = {"http", "https", "mailto", "tel", "data"}

# Attributes that carry a URL, whatever the tag. Keyed by name rather
# than by (tag, attribute) so a new tag cannot be added above with an
# unchecked href: nh3 validates the handful of URL attributes it knows
# about, and this covers the rest (`action`, `formaction`, `poster`, …).
URL_ATTRIBUTES: frozenset[str] = frozenset(
    {"href", "src", "srcset", "action", "formaction", "poster", "cite", "ping"}
)

# An inline image, but never image/svg+xml: an SVG document can carry
# script, and a data: URL is the one place it would arrive unparsed.
_DATA_IMAGE = re.compile(
    r"^data:image/(?:png|jpeg|jpg|gif|webp|avif|bmp|x-icon|vnd\.microsoft\.icon)"
    r"\s*;\s*base64\s*,[a-z0-9+/=\s]+$",
    re.IGNORECASE,
)

# Whitespace and control characters a scheme can be smeared with
# ("java\tscript:") before a browser folds it back together.
_URL_NOISE = re.compile(r"[\x00-\x20\x7f]+")

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


# ----------------------------------------------------------------- CSS
# What CSS can and cannot do decides how much of it has to be taken out.
# It cannot run script: `expression()` died with IE, `-moz-binding` with
# old Gecko, and a `javascript:` URL in a stylesheet has not executed in
# any engine for years. So the cleaners below are not an XSS boundary
# the way the tag allow-list is — they exist to stop a page quietly
# fetching from somewhere it should not, and to keep the dead-but-not-
# forgotten constructs out for the browsers nobody has upgraded.
_CSS_DANGEROUS = re.compile(
    r"expression\s*\(|javascript\s*:|vbscript\s*:|behaviou?r\s*:|-moz-binding|@import|@charset",
    re.IGNORECASE,
)
# Every url() a declaration points at, quoted or not.
_CSS_URL = re.compile(r"url\(\s*(['\"]?)([^'\")]*)\1\s*\)", re.IGNORECASE)
# Property names, including vendor prefixes and custom properties.
_CSS_PROPERTY = re.compile(r"^-{0,2}[a-zA-Z][\w-]*$")
# One declaration, for the targeted removals a stylesheet needs.
_CSS_AT_RULE = re.compile(r"@(?:import|charset)[^;{}]*;?", re.IGNORECASE)
# A '<' that could open a tag if the stylesheet were ever re-read as
# markup. See the note in clean_stylesheet.
_CSS_TAG_OPEN = re.compile(r"<(?=[/!?a-zA-Z])")

MAX_CSS_CHARS = 40_000


def _css_urls_ok(value: str) -> bool:
    """Every url() in a declaration points somewhere we are willing to load.

    Same rule the image blocks use — https or a path of our own — plus
    inline data images, which are the one case where the bytes are
    already in the page and fetch nothing.
    """
    for _quote, url in _CSS_URL.findall(value):
        target = url.strip().lower()
        if not (target.startswith(("https://", "/", "#"))
                or target.startswith("data:image/")):
            return False
    return True


def _css_value_ok(value: str) -> bool:
    return (
        bool(value.strip())
        # An inline SVG data URI is full of '<'; only '</' could close
        # the rawtext element a stylesheet lives in.
        and "</" not in value
        and not _CSS_DANGEROUS.search(value)
        and _css_urls_ok(value)
    )


def _split_declarations(value: str) -> list[str]:
    """Split on the semicolons that separate declarations — not the ones
    inside url() or a quoted string, where `data:image/png;base64,...`
    puts them."""
    parts: list[str] = []
    buf: list[str] = []
    depth = 0
    quote = ""

    for char in value:
        if quote:
            buf.append(char)
            if char == quote:
                quote = ""
            continue
        if char in "\"'":
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth = max(depth - 1, 0)
        elif char == ";" and depth == 0:
            parts.append("".join(buf))
            buf = []
            continue
        buf.append(char)

    parts.append("".join(buf))
    return parts


def clean_style_attribute(value: str) -> str | None:
    """Clean one element's `style` value, declaration by declaration.

    Rebuilt rather than pattern-patched: an inline style is a short list
    of `property: value` pairs, so keeping only the pairs that parse and
    pass is both simple and exact. Returns None when nothing survives,
    which drops the attribute instead of leaving `style=""`.
    """
    if not value or len(value) > 4_000:
        return None

    kept: list[str] = []
    for declaration in _split_declarations(value):
        if ":" not in declaration:
            continue
        prop, _, val = declaration.partition(":")
        prop, val = prop.strip(), val.strip()
        if _CSS_PROPERTY.match(prop) and _css_value_ok(val):
            kept.append(f"{prop}: {val}")
    return "; ".join(kept) or None


def clean_stylesheet(css: str) -> str:
    """Clean the contents of a `<style>` block.

    Scanned rather than parsed: the sheet is walked once, tracking
    braces, parens and quotes, and only the individual declarations
    inside a rule are judged. Everything structural — selectors,
    at-rule preludes, nesting, comments, whitespace — is copied through
    exactly as written, which is what a rebuild from a half-complete CSS
    parser would quietly destroy.
    """
    if not css:
        return ""
    if len(css) > MAX_CSS_CHARS:
        css = css[:MAX_CSS_CHARS]

    # @import would pull in a stylesheet we do not control; the page CSP
    # names no host in style-src, so the browser would refuse it anyway.
    css = _CSS_AT_RULE.sub("", css)

    out: list[str] = []
    buf: list[str] = []
    braces = 0
    parens = 0
    quote = ""

    def flush(terminator: str) -> None:
        """A declaration is judged; anything else is passed through."""
        segment = "".join(buf)
        buf.clear()
        if braces > 0 and ":" in segment:
            _prop, _, val = segment.partition(":")
            if not _css_value_ok(val):
                return          # its separator goes with it
        out.append(segment + terminator)

    for char in css:
        if quote:
            buf.append(char)
            if char == quote:
                quote = ""
            continue
        if char in "\"'":
            quote = char
            buf.append(char)
            continue
        if char == "(":
            parens += 1
        elif char == ")":
            parens = max(parens - 1, 0)

        if parens == 0 and char == ";":
            flush(";")
        elif parens == 0 and char == "{":
            out.append("".join(buf) + "{")
            buf.clear()
            braces += 1
        elif parens == 0 and char == "}":
            flush("")
            braces = max(braces - 1, 0)
            out.append("}")
        else:
            buf.append(char)

    flush("")
    cleaned = "".join(out)
    # A stylesheet is raw text: the browser reads everything up to
    # </style> as CSS, and nh3 hands the body back exactly as written.
    # That is safe while the <style> stays where it was parsed — but an
    # element the *receiving* parser relocates or ignores (a <style>
    # inside a <select>, an <option>, a <marquee>) can have its body
    # re-read as markup, and that is how a stylesheet holding the text
    # "<img src=x onerror=...>" becomes a live element. So every '<'
    # that could open a tag is written as its CSS escape: byte-for-byte
    # the same stylesheet to a CSS parser, with nothing left in it that
    # an HTML parser could turn into an element. (A range media query
    # therefore has to be spaced — `(width < 40em)`, not `(width<40em)`,
    # which is the form every spec example uses anyway.)
    cleaned = _CSS_TAG_OPEN.sub(r"\\3c ", cleaned)
    return cleaned.replace("</style", "")


def _scheme_ok(url: str) -> bool:
    """True when a URL is relative or carries an allowed scheme.

    nh3 already checks the URL attributes ammonia knows (``href``,
    ``src``); this is the same test applied by attribute *name*, so the
    ones it does not know — ``action``, ``formaction``, ``poster`` — are
    held to it too. Noise is stripped first, because "java\tscript:" is
    a scheme to a browser and a relative path to a naive split.
    """
    candidate = _URL_NOISE.sub("", url)
    head, colon, _rest = candidate.partition(":")
    if not colon:
        return True                     # no scheme at all: relative
    if any(char in head for char in "/?#"):
        return True                     # the colon is inside a path
    scheme = head.lower()
    if scheme == "data":
        return bool(_DATA_IMAGE.match(candidate))
    return scheme in URL_SCHEMES


def _url_attribute_ok(attribute: str, value: str) -> bool:
    """``srcset`` holds a list; everything else holds one URL."""
    if attribute == "srcset":
        return all(
            _scheme_ok(candidate.strip().split(" ")[0])
            for candidate in value.split(",")
            if candidate.strip()
        )
    return _scheme_ok(value)


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

    # Every URL, by attribute name rather than by tag.
    if attribute in URL_ATTRIBUTES and not _url_attribute_ok(attribute, value):
        return None

    # Numeric geometry; 'width="100%"' and 'width="640px"' are fine,
    # 'width="expression(…)"' is not. SVG and MathML are exempt: there
    # the two are coordinates and carry units ('1.5em', '24') that this
    # rule would throw away.
    if (attribute in {"width", "height"} and tag not in SVG_TAGS
            and tag not in MATHML_TAGS
            and not re.fullmatch(r"\d{1,5}(?:px|%)?", value.strip())):
        return None

    if attribute in {"target", "formtarget"}:
        # Keyword targets, plus a named one so a form can post into an
        # iframe on the same page. Anything else is a typo.
        target = value.strip()
        if target.lower() in {"_blank", "_self", "_parent", "_top"}:
            return target.lower()
        return target if re.fullmatch(r"[A-Za-z][\w.:-]{0,60}", target) else None

    if attribute in {"method", "formmethod"}:
        method = value.strip().lower()
        return method if method in {"get", "post", "dialog"} else None

    if attribute == "rel":
        tokens = [t for t in value.lower().split() if t in _REL_TOKENS]
        return " ".join(dict.fromkeys(tokens)) or None

    # class/id are capped, not pattern-matched: nh3 quotes and escapes
    # the value, so there is nothing to smuggle out of it, and a utility
    # framework's `md:w-1/2` or `bg-[#fff]` is a perfectly ordinary
    # class name that the old character class silently threw away.
    if attribute in {"class", "id", "name"} and len(value) > 500:
        return None

    if attribute == "style":
        return clean_style_attribute(value)

    return value


_STYLE_BLOCK = re.compile(r"(?is)(<style[^>]*>)(.*?)(</style>)")


def clean_html(
    raw: str | None,
    *,
    limit: int = 400_000,
    allow_stylesheet: bool = False,
) -> str | None:
    """Sanitize a rich-text fragment. Returns None for empty input.

    ``limit`` caps the *input*: a multi-megabyte paste should be rejected
    before the cleaner walks it, not after.

    ``allow_stylesheet`` keeps ``<style>`` blocks, with their CSS run
    through :func:`clean_stylesheet`. It is off by default because a
    stylesheet is page-wide by nature: in a blog post or an email
    template that reaches past the fragment it was pasted into, which is
    rarely what the author of *that* fragment meant. A page written as
    HTML is the case where it is exactly what they meant.
    """
    if raw is None:
        return None
    text = str(raw)
    if len(text) > limit:
        raise ValueError(f"content is too large (limit {limit:,} characters)")
    if not text.strip():
        return None

    tags = ALLOWED_TAGS | {"style"} if allow_stylesheet else ALLOWED_TAGS
    # nh3 refuses a tag that is both allowed and content-cleaned.
    content_tags = CLEAN_CONTENT_TAGS - {"iframe"} - ({"style"} if allow_stylesheet else set())

    cleaned = nh3.clean(
        text,
        tags=tags,
        attributes={k: set(v) for k, v in ALLOWED_ATTRIBUTES.items()},
        generic_attribute_prefixes=GENERIC_ATTRIBUTE_PREFIXES,
        # iframe is in ALLOWED_TAGS, so it is not content-cleaned here;
        # the rest have their bodies removed with them.
        clean_content_tags=content_tags,
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
    # nh3 keeps a <style> body verbatim (it is rawtext, not escaped
    # text), so the CSS is cleaned here, on the parsed output.
    if allow_stylesheet:
        cleaned = _STYLE_BLOCK.sub(
            lambda m: f"{m.group(1)}{clean_stylesheet(m.group(2))}{m.group(3)}", cleaned
        )
    return cleaned or None


# ----------------------------------------------------- change reporting
class _Inventory(HTMLParser):
    """Counts tag and attribute names in a fragment.

    Run over the author's input and over the cleaned output, the two
    counts say what the cleaner took out. Advisory only: the security
    boundary is :func:`clean_html`, never this.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: Counter[str] = Counter()
        self.attrs: Counter[str] = Counter()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags[tag.lower()] += 1
        for name, _value in attrs:
            self.attrs[name.lower()] += 1


def _inventory(html: str | None) -> _Inventory:
    inventory = _Inventory()
    if html:
        inventory.feed(html)
        inventory.close()
    return inventory


def describe_changes(raw: str | None, cleaned: str | None) -> dict[str, list[str]]:
    """Which tags and attributes the cleaner dropped, by name.

    Reported by name rather than by count: "iframe was removed" is what
    an author needs to read, and a partial drop (two of three iframes)
    is still worth surfacing, so any decrease counts.
    """
    before, after = _inventory(raw), _inventory(cleaned)
    return {
        "removed_tags": sorted(
            tag for tag, count in before.tags.items() if count > after.tags[tag]
        ),
        "removed_attributes": sorted(
            attr for attr, count in before.attrs.items() if count > after.attrs[attr]
        ),
    }


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
