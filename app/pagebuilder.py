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

MAX_BLOCKS = 60
MAX_FEATURE_ITEMS = 12

HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{3,8}$")
# javascript:, data: and friends never survive validation.
SAFE_HREF = re.compile(r"^(https://|http://|mailto:|tel:|/|#)", re.IGNORECASE)
# http images would be mixed content on an https page.
SAFE_IMAGE = re.compile(r"^(https://|/)", re.IGNORECASE)
FIELD_NAME = re.compile(r"^[a-z0-9_]{1,40}$")

FONT_STACKS = {
    "system": "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif",
    "serif": "Georgia, 'Iowan Old Style', 'Times New Roman', serif",
    "mono": "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace",
}
MAX_WIDTHS = {"narrow": "660px", "normal": "840px", "wide": "1080px"}
SPACER_SIZES = {"small": "20px", "medium": "48px", "large": "96px"}

DEFAULT_THEME = {
    "primary": "#0b6e5a",
    "background": "#ffffff",
    "text": "#1c2422",
    "font": "system",
    "max_width": "normal",
}

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


def _clean_hero(b: dict) -> dict:
    return {
        "heading": _line(b, "heading", 160) or "Untitled",
        "sub": _text(b, "sub", 400),
        "button_label": _line(b, "button_label", 60),
        "button_href": _href(b, "button_href"),
        "align": _choice(b, "align", ("left", "center"), "center"),
    }


def _clean_heading(b: dict) -> dict:
    level = b.get("level")
    return {
        "text": _line(b, "text", 160) or "Heading",
        "level": level if level in (2, 3, 4) else 2,
    }


def _clean_text(b: dict) -> dict:
    return {"body": _text(b, "body", 40000) or ""}  # room for blog-length posts


def _clean_image(b: dict) -> dict:
    src = _image_src(b, "src")
    if not src:
        raise HTTPException(400, "Image blocks need an image URL.")
    return {"src": src, "alt": _line(b, "alt", 200) or "", "caption": _line(b, "caption", 200)}


def _clean_button(b: dict) -> dict:
    return {
        "label": _line(b, "label", 60) or "Learn more",
        "href": _href(b, "href") or "#",
        "align": _choice(b, "align", ("left", "center"), "center"),
        "variant": _choice(b, "variant", ("solid", "outline"), "solid"),
    }


def _clean_features(b: dict) -> dict:
    raw_items = b.get("items")
    items = []
    for item in (raw_items if isinstance(raw_items, list) else [])[:MAX_FEATURE_ITEMS]:
        if not isinstance(item, dict):
            continue
        title = _line(item, "title", 120)
        if not title:
            continue
        items.append({"title": title, "body": _text(item, "body", 500)})
    return {"heading": _line(b, "heading", 160), "items": items}


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
# Sanitizing on write means a later change to this renderer cannot
# start emitting something unsafe that was stored earlier.
MAX_HTML_CHARS = 60_000


def _clean_html(b: dict) -> dict:
    from .sanitize import clean_html  # noqa: PLC0415 — sanitize imports config

    raw = b.get("html")
    try:
        cleaned = clean_html(raw, limit=MAX_HTML_CHARS)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    return {
        "html": cleaned or "",
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
}


def clean_blocks(raw: Any) -> list[dict]:
    """Validate a client-supplied block list into canonical stored form."""
    if not isinstance(raw, list):
        raise HTTPException(400, "Blocks must be a list.")
    if len(raw) > MAX_BLOCKS:
        raise HTTPException(400, f"A page can hold at most {MAX_BLOCKS} blocks.")

    cleaned: list[dict] = []
    for block in raw:
        if not isinstance(block, dict):
            raise HTTPException(400, "Each block must be an object.")
        cleaner = CLEANERS.get(block.get("type"))
        if cleaner is None:
            raise HTTPException(400, "That block type is not supported.")
        cleaned.append({"type": block["type"], **cleaner(block)})
    return cleaned


def clean_theme(raw: Any) -> dict:
    """Whitelisted theme keys only; colours must be hex literals."""
    source = raw if isinstance(raw, dict) else {}
    theme = dict(DEFAULT_THEME)
    for key in ("primary", "background", "text"):
        value = source.get(key)
        if isinstance(value, str) and HEX_COLOR.match(value.strip()):
            theme[key] = value.strip()
    if source.get("font") in FONT_STACKS:
        theme["font"] = source["font"]
    if source.get("max_width") in MAX_WIDTHS:
        theme["max_width"] = source["max_width"]
    return theme


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


def _render_hero(b: dict, ctx: dict) -> str:
    button = ""
    if b.get("button_label") and b.get("button_href"):
        button = f'<a class="btn" href="{_esc(b["button_href"])}">{_esc(b["button_label"])}</a>'
    sub = f"<p class=\"hero-sub\">{_inline(b['sub'])}</p>" if b.get("sub") else ""
    return (
        f'<header class="blk hero align-{b["align"]}">'
        f"<h1>{_esc(b['heading'])}</h1>{sub}{button}</header>"
    )


def _render_heading(b: dict, ctx: dict) -> str:
    level = b["level"]
    return f'<section class="blk"><h{level}>{_esc(b["text"])}</h{level}></section>'


def _render_text(b: dict, ctx: dict) -> str:
    return f'<section class="blk prose">{_rich(b["body"])}</section>'


def _render_image(b: dict, ctx: dict) -> str:
    caption = f"<figcaption>{_inline(b['caption'])}</figcaption>" if b.get("caption") else ""
    return (
        f'<section class="blk"><figure>'
        f'<img src="{_esc(b["src"])}" alt="{_esc(b["alt"])}" loading="lazy">'
        f"{caption}</figure></section>"
    )


def _render_button(b: dict, ctx: dict) -> str:
    variant = " btn-outline" if b["variant"] == "outline" else ""
    return (
        f'<section class="blk align-{b["align"]}">'
        f'<a class="btn{variant}" href="{_esc(b["href"])}">{_esc(b["label"])}</a></section>'
    )


def _render_features(b: dict, ctx: dict) -> str:
    heading = f"<h2>{_esc(b['heading'])}</h2>" if b.get("heading") else ""
    cards = "".join(
        f'<div class="card"><h3>{_esc(item["title"])}</h3>'
        + (f"<p>{_inline(item['body'])}</p>" if item.get("body") else "")
        + "</div>"
        for item in b["items"]
    )
    return f'<section class="blk">{heading}<div class="features">{cards}</div></section>'


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
    return f'<div style="height:{SPACER_SIZES[b["size"]]}"></div>'


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
}

_PAGE_CSS = """
* { box-sizing: border-box; }
[hidden] { display: none !important; }
body {
  margin: 0; background: var(--bg); color: var(--text);
  font-family: var(--font); font-size: 17px; line-height: 1.65;
  -webkit-font-smoothing: antialiased;
}
.page { max-width: var(--maxw); margin: 0 auto; padding: 0 20px 80px; }
.blk { margin: 28px 0; }
.align-center { text-align: center; }
h1 { font-size: 2.4rem; line-height: 1.15; margin: 0 0 12px; }
h2 { font-size: 1.6rem; margin: 0 0 10px; }
h3 { font-size: 1.15rem; margin: 0 0 6px; }
p { margin: 0 0 14px; }
img { max-width: 100%; height: auto; border-radius: 6px; display: block; margin: 0 auto; }
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
.hero-sub { font-size: 1.2rem; opacity: .8; max-width: 640px; }
.hero.align-center .hero-sub { margin-left: auto; margin-right: auto; }
.btn {
  display: inline-block; background: var(--primary); color: #fff;
  padding: 11px 26px; border-radius: 6px; text-decoration: none;
  font-weight: 600; border: 2px solid var(--primary);
}
.btn:hover { filter: brightness(1.08); }
.btn-outline { background: transparent; color: var(--primary); }
.features { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 16px; }
.card {
  border: 1px solid color-mix(in srgb, var(--text) 14%, transparent);
  border-radius: 8px; padding: 18px;
}
.card p { margin: 0; font-size: .95rem; opacity: .85; }
blockquote {
  margin: 0; padding: 8px 0 8px 22px; font-size: 1.25rem;
  border-left: 4px solid var(--primary);
}
blockquote cite { display: block; font-size: .9rem; font-style: normal; opacity: .7; margin-top: 8px; }
.lead-form { display: grid; gap: 14px; max-width: 560px; }
.lead-form label { display: grid; gap: 5px; font-size: .9rem; font-weight: 600; }
.lead-form input, .lead-form textarea {
  font: inherit; padding: 10px 12px; border-radius: 6px;
  border: 1px solid color-mix(in srgb, var(--text) 25%, transparent);
  background: var(--bg); color: var(--text); width: 100%;
}
.lead-form button {
  font: inherit; font-weight: 600; color: #fff; background: var(--primary);
  border: 0; border-radius: 6px; padding: 12px 26px; cursor: pointer; justify-self: start;
}
.lead-form button:disabled { opacity: .6; cursor: default; }
.lead-form .hp { position: absolute; left: -9999px; width: 1px; height: 1px; opacity: 0; }
.form-error { color: #b3261e; font-size: .95rem; }
.form-done { padding: 18px; border-radius: 8px; background: color-mix(in srgb, var(--primary) 12%, transparent); font-weight: 600; }
.preview-banner {
  position: sticky; top: 0; z-index: 10; text-align: center;
  background: #a8791f; color: #fff; font-size: 13px; padding: 6px 10px;
}

/* HTML block. `pb-html-styled` opts the markup into the page's own
   typography; leaving it off is for a pasted widget that brings its
   own. Every rule is scoped under .pb-html so pasted markup cannot
   restyle the rest of the page. */
.pb-html { margin: 0 0 26px; }
.pb-html-wide { max-width: min(1080px, 92vw); margin-inline: auto; }
.pb-html-full { max-width: none; }
.pb-html > *:last-child { margin-bottom: 0; }
/* Embeds keep a 16:9 box and never overflow the column. */
.pb-html iframe { width: 100%; max-width: 100%; aspect-ratio: 16 / 9; height: auto; border: 0; }
.pb-html img { max-width: 100%; height: auto; }
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
"""


def render_page(
    *,
    title: str,
    description: str | None,
    blocks: list[dict],
    theme: dict,
    tenant_slug: str,
    forms: dict[str, list] | None = None,
    preview: bool = False,
) -> str:
    """Render a full HTML document. `forms` maps form slug -> field list."""
    theme = clean_theme(theme)
    ctx = {"tenant_slug": tenant_slug, "forms": forms or {}, "needs_form_js": False}

    body_parts = []
    for block in blocks if isinstance(blocks, list) else []:
        renderer = RENDERERS.get(block.get("type"))
        if renderer:
            body_parts.append(renderer(block, ctx))

    meta_description = (
        f'<meta name="description" content="{_esc(description)}">' if description else ""
    )
    banner = '<div class="preview-banner">Draft preview — this is not the live page</div>' if preview else ""
    form_script = '<script src="/js/page-form.js" defer></script>' if ctx["needs_form_js"] else ""
    robots = '<meta name="robots" content="noindex, nofollow">' if preview else ""

    css_vars = (
        f"--primary:{theme['primary']};--bg:{theme['background']};--text:{theme['text']};"
        f"--font:{FONT_STACKS[theme['font']]};--maxw:{MAX_WIDTHS[theme['max_width']]}"
    )

    return (
        "<!doctype html>\n"
        '<html lang="en">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"{robots}{meta_description}"
        f"<title>{_esc(title)}</title>\n"
        f"<style>:root{{{css_vars}}}{_PAGE_CSS}</style>\n"
        f"{form_script}"
        "</head>\n<body>\n"
        f"{banner}"
        f'<main class="page">{"".join(body_parts)}</main>\n'
        "</body>\n</html>"
    )
