"""Placeholder rendering for email templates and campaigns.

Templates hold ``{{lead.full_name}}``-style placeholders. This renders
them without a template engine on purpose: there is no expression
evaluation, no filters and no attribute traversal beyond a flat
dot-path, so a template stored by an Editor cannot execute anything.

Two entry points, because the escaping rules differ:

  render_text()  no escaping — plain-text bodies
  render_html()  every substituted value is HTML-escaped, so a lead
                 named ``<script>`` cannot inject into an HTML email
"""

from __future__ import annotations

import html
import re
from typing import Any

# {{ name }} or {{ scope.name }} — letters, digits, underscore, one dot.
PLACEHOLDER = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*(?:\.[a-zA-Z_][a-zA-Z0-9_]*)?)\s*\}\}")

MAX_VALUE_CHARS = 2000


def flatten(context: dict[str, Any], prefix: str = "") -> dict[str, str]:
    """{'lead': {'email': 'a@b.c'}} → {'lead.email': 'a@b.c'}.

    One level of nesting is enough for every template this platform
    sends, and refusing to go deeper keeps the path grammar trivial.
    """
    flat: dict[str, str] = {}
    for key, value in context.items():
        name = f"{prefix}{key}"
        if isinstance(value, dict) and not prefix:
            flat.update(flatten(value, f"{name}."))
        elif value is None or isinstance(value, (dict, list)):
            flat[name] = ""
        else:
            flat[name] = str(value)[:MAX_VALUE_CHARS]
    return flat


def _substitute(template: str, values: dict[str, str], escape: bool) -> str:
    def replace(match: re.Match) -> str:
        value = values.get(match.group(1), "")
        return html.escape(value, quote=True) if escape else value

    return PLACEHOLDER.sub(replace, template)


def render_text(template: str | None, context: dict[str, Any]) -> str:
    if not template:
        return ""
    return _substitute(template, flatten(context), escape=False)


def render_html(template: str | None, context: dict[str, Any]) -> str | None:
    """Substitute into an HTML body. The template itself must already be
    sanitized (see app/sanitize.py); this escapes only the values."""
    if not template:
        return None
    return _substitute(template, flatten(context), escape=True) or None


def placeholders_in(*templates: str | None) -> list[str]:
    """Every placeholder a template uses — shown in the editor as hints."""
    found: dict[str, None] = {}
    for template in templates:
        for match in PLACEHOLDER.finditer(template or ""):
            found.setdefault(match.group(1), None)
    return sorted(found)


def missing_placeholders(context: dict[str, Any], *templates: str | None) -> list[str]:
    """Placeholders the context cannot fill — surfaced as a save warning
    rather than an error, since a template may be reused elsewhere."""
    available = flatten(context)
    return [name for name in placeholders_in(*templates) if name not in available]
