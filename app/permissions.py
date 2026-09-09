"""Roles and fine-grained permissions.

Two things are deliberately separate:

  * the **role** on a user (or on a tenant_memberships row) — a coarse
    label the UI shows;
  * the **permission** a handler actually requires — a verb like
    ``content.publish``.

DEFAULTS maps role → permissions. A tenant can override any single cell
in ``role_permissions``, which is what makes access fine-grained without
inventing a new role every time one client wants authors to publish.

Overrides are cached per tenant for CACHE_TTL seconds; a write through
``set_override`` invalidates the entry, so a permission change takes
effect on the next request rather than after a restart.
"""

from __future__ import annotations

import time
from typing import Iterable

from fastapi import Depends, HTTPException

from . import db
from .security import CurrentUser, require_user

# --------------------------------------------------------------- catalogue
# Grouped only for readability in the settings UI; the strings are the API.
PERMISSIONS: dict[str, tuple[str, ...]] = {
    "Content": (
        "content.view", "content.create", "content.edit_own", "content.edit_any",
        "content.publish", "content.trash", "content.purge",
        "types.manage", "taxonomy.manage",
    ),
    "Media": ("media.view", "media.upload", "media.edit", "media.delete"),
    "SEO": ("seo.manage", "redirects.manage"),
    "Leads": (
        "leads.view", "leads.edit", "leads.assign", "leads.delete", "leads.export",
        "forms.manage", "submissions.view",
    ),
    "Marketing": (
        "marketing.view", "marketing.manage", "campaigns.send", "subscribers.manage",
    ),
    "Site": ("menus.manage", "blocks.manage", "settings.manage"),
    "People": ("users.view", "users.manage", "roles.manage"),
    "Analytics": ("analytics.view", "analytics.manage"),
    "Publishing": ("deploy.trigger", "deploy.manage", "apikeys.manage", "webhooks.manage"),
    "Operations": ("ops.view", "backups.manage", "logs.view", "audit.view"),
    "Compliance": ("compliance.view", "compliance.manage"),
    "Platform": ("sites.manage",),
}

ALL_PERMISSIONS: frozenset[str] = frozenset(
    perm for group in PERMISSIONS.values() for perm in group
)

# Roles in ascending order of reach. ROLE_RANK in security.py still gates
# the original owner/admin/agent/viewer checks; this list is what the UI
# offers and what DEFAULTS is keyed by.
ROLES: tuple[str, ...] = (
    "contributor", "viewer", "author", "agent", "editor", "admin", "owner", "super_admin",
)

ROLE_LABELS = {
    "super_admin": "Super Admin",
    "owner": "Owner",
    "admin": "Admin",
    "editor": "Editor",
    "author": "Author",
    "contributor": "Contributor",
    "agent": "Agent (CRM)",
    "viewer": "Viewer",
}

_VIEW_ONLY = frozenset(
    {"content.view", "media.view", "leads.view", "submissions.view",
     "marketing.view", "users.view", "analytics.view", "ops.view", "compliance.view"}
)

_EDITOR = frozenset(
    {
        "content.view", "content.create", "content.edit_own", "content.edit_any",
        "content.publish", "content.trash", "taxonomy.manage",
        "media.view", "media.upload", "media.edit", "media.delete",
        "seo.manage", "redirects.manage",
        "leads.view", "leads.edit", "leads.assign", "leads.export",
        "forms.manage", "submissions.view",
        "marketing.view", "marketing.manage", "subscribers.manage",
        "menus.manage", "blocks.manage",
        "analytics.view", "deploy.trigger", "ops.view",
    }
)

# Everything a single site can be administered with — no cross-site reach.
_SITE_ADMIN = ALL_PERMISSIONS - {"sites.manage", "roles.manage"}

DEFAULTS: dict[str, frozenset[str]] = {
    # Cross-site operator: the only role that manages tenants.
    "super_admin": ALL_PERMISSIONS,
    "owner": frozenset(_SITE_ADMIN | {"roles.manage"}),
    "admin": _SITE_ADMIN,
    "editor": _EDITOR,
    "author": frozenset(
        {"content.view", "content.create", "content.edit_own", "content.publish",
         "content.trash", "media.view", "media.upload", "media.edit",
         "leads.view", "analytics.view"}
    ),
    # Writes but never publishes — the classic contributor workflow.
    "contributor": frozenset(
        {"content.view", "content.create", "content.edit_own",
         "media.view", "media.upload"}
    ),
    # Legacy CRM roles from the original schema, kept working as-is.
    "agent": frozenset(
        {"leads.view", "leads.edit", "leads.assign", "leads.export",
         "submissions.view", "content.view", "media.view", "analytics.view"}
    ),
    "viewer": _VIEW_ONLY,
}


# ----------------------------------------------------------------- overrides
CACHE_TTL = 30.0
_cache: dict[int, tuple[float, dict[tuple[str, str], bool]]] = {}


async def _overrides(tenant_id: int) -> dict[tuple[str, str], bool]:
    hit = _cache.get(tenant_id)
    now = time.monotonic()
    if hit and hit[0] > now:
        return hit[1]

    rows = await db.fetch(
        "SELECT role::text AS role, permission, allowed FROM role_permissions WHERE tenant_id = $1",
        tenant_id,
    )
    table = {(row["role"], row["permission"]): row["allowed"] for row in rows}
    _cache[tenant_id] = (now + CACHE_TTL, table)
    return table


def invalidate(tenant_id: int) -> None:
    """Drop this process's copy."""
    _cache.pop(tenant_id, None)


async def invalidate_everywhere(tenant_id: int) -> None:
    """Drop it here and on every other replica.

    A permission change that takes effect on one task and not the
    others for another 30 seconds is the kind of inconsistency that is
    very hard to reproduce from a bug report.
    """
    invalidate(tenant_id)
    from . import cache  # noqa: PLC0415 — avoids a cycle at import

    await cache.invalidate_permissions(tenant_id)


def _on_invalidate(message: dict) -> None:
    tenant_id = message.get("tenant_id")
    if tenant_id:
        _cache.pop(int(tenant_id), None)
    else:
        _cache.clear()


async def permissions_for(tenant_id: int, role: str) -> set[str]:
    """Effective permission set: role defaults with tenant overrides applied."""
    granted = set(DEFAULTS.get(role, frozenset()))
    for (row_role, permission), allowed in (await _overrides(tenant_id)).items():
        if row_role != role or permission not in ALL_PERMISSIONS:
            continue
        granted.add(permission) if allowed else granted.discard(permission)
    return granted


async def has(user: CurrentUser, permission: str) -> bool:
    return permission in await permissions_for(user.tenant_id, user.role)


async def set_override(
    tenant_id: int, role: str, permission: str, allowed: bool | None, actor_id: int
) -> None:
    """allowed=None clears the override and restores the role default."""
    if role not in DEFAULTS:
        raise HTTPException(400, "Unknown role.")
    if permission not in ALL_PERMISSIONS:
        raise HTTPException(400, "Unknown permission.")
    if role == "super_admin":
        raise HTTPException(400, "Super Admin permissions cannot be narrowed.")

    if allowed is None:
        await db.execute(
            """DELETE FROM role_permissions
                WHERE tenant_id = $1 AND role = $2::user_role AND permission = $3""",
            tenant_id, role, permission,
        )
    else:
        await db.execute(
            """INSERT INTO role_permissions (tenant_id, role, permission, allowed, updated_by)
               VALUES ($1, $2::user_role, $3, $4, $5)
               ON CONFLICT (tenant_id, role, permission)
               DO UPDATE SET allowed = EXCLUDED.allowed,
                             updated_by = EXCLUDED.updated_by,
                             updated_at = now()""",
            tenant_id, role, permission, allowed, actor_id,
        )
    invalidate(tenant_id)


# --------------------------------------------------------------- dependency
def require_perm(*needed: str):
    """Dependency factory. Several permissions means all of them.

        @router.post("", dependencies=[Depends(require_perm("content.create"))])

    Prefer taking the returned user when the handler needs it:

        user: CurrentUser = Depends(require_perm("content.create"))
    """
    if not needed:
        raise ValueError("require_perm needs at least one permission")
    unknown = set(needed) - ALL_PERMISSIONS
    if unknown:
        raise ValueError(f"unknown permission(s): {sorted(unknown)}")

    async def _guard(user: CurrentUser = Depends(require_user)) -> CurrentUser:
        granted = await permissions_for(user.tenant_id, user.role)
        missing = [perm for perm in needed if perm not in granted]
        if missing:
            raise HTTPException(403, f"Your role cannot {_phrase(missing[0])}.")
        return user

    return _guard


async def require_any(user: CurrentUser, options: Iterable[str]) -> str:
    """First permission the user holds, or 403. For "edit any OR edit own"."""
    granted = await permissions_for(user.tenant_id, user.role)
    for option in options:
        if option in granted:
            return option
    raise HTTPException(403, "Your role cannot do that.")


_VERBS = {
    "view": "view", "create": "create", "edit": "edit", "edit_own": "edit",
    "edit_any": "edit others’", "publish": "publish", "trash": "delete",
    "purge": "permanently delete", "manage": "manage", "delete": "delete",
    "upload": "upload", "export": "export", "assign": "assign",
    "send": "send", "trigger": "trigger",
}


def _phrase(permission: str) -> str:
    """'content.publish' → 'publish content' for a readable 403."""
    subject, _, verb = permission.partition(".")
    return f"{_VERBS.get(verb, verb.replace('_', ' '))} {subject.replace('_', ' ')}"


# Registered at import so every process drops its copy when any of them
# changes a permission.
def _register() -> None:
    from . import cache  # noqa: PLC0415

    cache.subscribe("permissions", _on_invalidate)


_register()
