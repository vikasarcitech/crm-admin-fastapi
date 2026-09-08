"""Workspace provisioning.

Everything a workspace needs to be usable on first sign-in: the
built-in content types and taxonomies, the default menus, the
transactional email templates, sensible retention policies and the
site-identity settings row.

:func:`provision_tenant` is idempotent — it upserts by natural key — so
it is safe to call on sign-up, from ``db.seed`` and as a one-shot
backfill for workspaces that existed before these tables did.
"""

from __future__ import annotations

import logging

from . import db
from .content import BUILTIN_TAXONOMIES, BUILTIN_TYPES

log = logging.getLogger("crm.bootstrap")

# Retention defaults are conservative: long enough to be useful, short
# enough that a workspace is not accumulating personal data forever.
DEFAULT_RETENTION: tuple[tuple[str, int, str], ...] = (
    ("form_submissions", 365, "delete"),
    ("activity_log", 730, "delete"),
    ("conversion_events", 540, "delete"),
    ("not_found_log", 180, "delete"),
    ("error_log", 90, "delete"),
    ("visitor_days", 45, "delete"),
    ("notifications", 120, "delete"),
)

DEFAULT_MENUS: tuple[tuple[str, str, str], ...] = (
    ("primary", "Primary navigation", "header"),
    ("footer", "Footer", "footer"),
)

DEFAULT_TEMPLATES: tuple[dict, ...] = (
    {
        "slug": "lead-autoresponder",
        "name": "Enquiry received (autoresponder)",
        "kind": "autoresponder",
        "subject": "Thanks for getting in touch, {{lead.first_name}}",
        "body_text": (
            "Hi {{lead.first_name}},\n\n"
            "Thanks for contacting {{site.name}} — we have your enquiry and "
            "someone will reply within one business day.\n\n"
            "For reference, this is what you sent us:\n\n"
            "  {{lead.message}}\n\n"
            "— The {{site.name}} team"
        ),
    },
    {
        "slug": "lead-notification",
        "name": "New lead alert (internal)",
        "kind": "notification",
        "subject": "New lead: {{lead.full_name}} ({{form.name}})",
        "body_text": (
            "A new lead came in from {{form.name}}.\n\n"
            "  Name:    {{lead.full_name}}\n"
            "  Email:   {{lead.email}}\n"
            "  Phone:   {{lead.phone}}\n"
            "  Company: {{lead.company}}\n"
            "  Page:    {{lead.source_page}}\n"
            "  Source:  {{lead.utm_source}} / {{lead.utm_medium}}\n\n"
            "  Message:\n  {{lead.message}}\n\n"
            "Open it: {{app.lead_url}}"
        ),
    },
    {
        "slug": "subscriber-confirm",
        "name": "Confirm your newsletter subscription",
        "kind": "transactional",
        "subject": "Confirm your subscription to {{site.name}}",
        "body_text": (
            "Hi{{subscriber.name_suffix}},\n\n"
            "Please confirm you want to receive email from {{site.name}}:\n\n"
            "  {{subscriber.confirm_url}}\n\n"
            "If you did not sign up, ignore this email — nothing happens.\n"
        ),
    },
    {
        "slug": "subscriber-welcome",
        "name": "Newsletter welcome",
        "kind": "transactional",
        "subject": "You’re subscribed to {{site.name}}",
        "body_text": (
            "Thanks for subscribing to {{site.name}}.\n\n"
            "You can unsubscribe at any time:\n  {{subscriber.unsubscribe_url}}\n"
        ),
    },
)

DEFAULT_SETTINGS: dict[str, dict] = {
    "site_identity": {
        "site_name": None,
        "tagline": None,
        "site_url": None,
        "logo_media_id": None,
        "favicon_media_id": None,
        "contact_email": None,
        "contact_phone": None,
        "whatsapp": None,
        "address": None,
        "social": {},
    },
    "locale": {"timezone": "UTC", "language": "en", "date_format": "d MMM yyyy"},
    "maintenance": {"enabled": False, "message": None, "allow_ips": []},
    "seo_defaults": {
        "title_template": "{{title}} — {{site_name}}",
        "meta_description": None,
        "og_image_id": None,
        "twitter_card": "summary_large_image",
        "twitter_site": None,
    },
    "breadcrumbs": {"enabled": True, "home_label": "Home", "separator": "/"},
    "analytics": {
        "ga4_measurement_id": None,
        "gtm_container_id": None,
        "search_console_verification": None,
        "first_party_beacon": True,
    },
    "cookie_consent": {
        "enabled": True,
        "policy_version": "1",
        "categories": ["necessary", "analytics", "marketing"],
        "banner_text": "We use cookies to improve your experience.",
        "policy_url": "/privacy",
    },
    "smtp": {"provider": "inherit", "from_name": None, "from_email": None, "reply_to": None},
    "spam": {"honeypot": True, "captcha": "turnstile", "min_fill_ms": 2500},
}


async def provision_tenant(tenant_id: int, *, created_by: int | None = None) -> dict:
    """Create or refresh every default a workspace needs.

    Returns a small summary so the caller (or the seed script) can say
    what happened. Never raises on an individual step: a workspace that
    provisions 90% of the way is usable, one that fails sign-up is not.
    """
    summary: dict[str, int] = {}

    async def step(name: str, coro) -> None:
        try:
            summary[name] = await coro
        except Exception as exc:
            log.error("provision step %s failed for tenant %s: %s", name, tenant_id, exc)
            summary[name] = -1

    await step("taxonomies", _taxonomies(tenant_id))
    await step("types", _types(tenant_id))
    await step("taxonomy_links", _link_taxonomies(tenant_id))
    await step("menus", _menus(tenant_id))
    await step("templates", _templates(tenant_id, created_by))
    await step("settings", _settings(tenant_id))
    await step("retention", _retention(tenant_id))
    return summary


async def _taxonomies(tenant_id: int) -> int:
    count = 0
    for taxonomy in BUILTIN_TAXONOMIES:
        await db.execute(
            """INSERT INTO taxonomies (tenant_id, slug, name, plural_name,
                                       is_hierarchical, is_builtin)
               VALUES ($1, $2, $3, $4, $5, TRUE)
               ON CONFLICT (tenant_id, slug) DO UPDATE SET is_builtin = TRUE""",
            tenant_id, taxonomy["slug"], taxonomy["name"],
            taxonomy["plural_name"], taxonomy["is_hierarchical"],
        )
        count += 1
    return count


async def _types(tenant_id: int) -> int:
    count = 0
    for index, type_def in enumerate(BUILTIN_TYPES):
        await db.execute(
            """INSERT INTO content_types
                   (tenant_id, slug, name, plural_name, kind, route_prefix,
                    field_schema, supports, icon, sort_order, is_builtin)
               VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8::jsonb, $9, $10, TRUE)
               ON CONFLICT (tenant_id, slug) DO UPDATE
                  SET is_builtin = TRUE,
                      -- Refresh the shape, but never clobber a route
                      -- prefix or field schema an admin has customised.
                      name = EXCLUDED.name,
                      plural_name = EXCLUDED.plural_name""",
            tenant_id, type_def["slug"], type_def["name"], type_def["plural_name"],
            type_def["kind"], type_def["route_prefix"], type_def["field_schema"],
            type_def["supports"], type_def.get("icon"), index,
        )
        count += 1
    return count


async def _link_taxonomies(tenant_id: int) -> int:
    """Attach each type to the taxonomies its `supports` block names."""
    rows = await db.fetch(
        "SELECT id, supports FROM content_types WHERE tenant_id = $1", tenant_id
    )
    linked = 0
    for row in rows:
        slugs = (row["supports"] or {}).get("taxonomies") or []
        if not slugs:
            continue
        await db.execute(
            """INSERT INTO taxonomy_types (tenant_id, taxonomy_id, type_id)
               SELECT $1, tx.id, $2 FROM taxonomies tx
                WHERE tx.tenant_id = $1 AND tx.slug = ANY($3::citext[])
               ON CONFLICT DO NOTHING""",
            tenant_id, row["id"], slugs,
        )
        linked += 1
    return linked


async def _menus(tenant_id: int) -> int:
    for slug, name, location in DEFAULT_MENUS:
        await db.execute(
            """INSERT INTO menus (tenant_id, slug, name, location)
               VALUES ($1, $2, $3, $4) ON CONFLICT (tenant_id, slug) DO NOTHING""",
            tenant_id, slug, name, location,
        )
    return len(DEFAULT_MENUS)


async def _templates(tenant_id: int, created_by: int | None) -> int:
    for template in DEFAULT_TEMPLATES:
        await db.execute(
            """INSERT INTO email_templates
                   (tenant_id, slug, name, subject, body_text, kind, updated_by)
               VALUES ($1, $2, $3, $4, $5, $6, $7)
               ON CONFLICT (tenant_id, slug) DO NOTHING""",
            tenant_id, template["slug"], template["name"], template["subject"],
            template["body_text"], template["kind"], created_by,
        )
    return len(DEFAULT_TEMPLATES)


async def _settings(tenant_id: int) -> int:
    """Seed defaults without overwriting anything already configured.

    jsonb_strip_nulls + ``||`` merges the default under the stored value,
    so a new key appears on an existing workspace but a customised one
    is left alone.
    """
    for key, value in DEFAULT_SETTINGS.items():
        await db.execute(
            """INSERT INTO settings (tenant_id, key, value)
               VALUES ($1, $2, $3::jsonb)
               ON CONFLICT (tenant_id, key) DO UPDATE
                  SET value = EXCLUDED.value || settings.value""",
            tenant_id, key, value,
        )
    return len(DEFAULT_SETTINGS)


async def _retention(tenant_id: int) -> int:
    for scope, days, action in DEFAULT_RETENTION:
        await db.execute(
            """INSERT INTO retention_policies (tenant_id, scope, days, action, is_active)
               VALUES ($1, $2, $3, $4, FALSE)
               ON CONFLICT (tenant_id, scope) DO NOTHING""",
            tenant_id, scope, days, action,
        )
    return len(DEFAULT_RETENTION)


async def provision_all() -> dict:
    """Backfill every existing workspace. Run once after a deploy that
    adds new defaults; safe to repeat."""
    tenants = await db.fetch("SELECT id FROM tenants ORDER BY id")
    results = {}
    for tenant in tenants:
        results[tenant["id"]] = await provision_tenant(tenant["id"])
    return results
