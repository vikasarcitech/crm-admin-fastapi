"""The multi-site control plane (portfolio administration).

Everything here is cross-tenant by definition, so it is gated on the
`sites.manage` permission — the one permission only Super Admin holds
by default and which `_SITE_ADMIN` deliberately excludes, so a site's
own Owner cannot reach the portfolio.

The site-switching and membership endpoints that used to live in
users.py moved here, because "which sites can I see" and "create a
site" are one concern.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from .. import db, events, tenancy
from ..permissions import DEFAULTS, permissions_for, require_perm
from ..schemas import (
    DomainCreate,
    MembershipCreate,
    SiteCreate,
    SiteLimitsUpdate,
    SiteStatusUpdate,
    SiteUpdate,
    collapse,
    valid_email,
)
from ..security import CurrentUser, client_ip, require_user

log = logging.getLogger("crm.sites")

router = APIRouter(prefix="/api/platform", tags=["platform"])

# Infra keys a site may override. An allow-list, so a compromised
# super-admin session cannot invent configuration the app then trusts.
INFRA_KEYS = frozenset(
    {
        "media_s3_bucket", "media_s3_prefix", "media_public_base_url",
        "cloudfront_distribution_id", "build_target", "region",
    }
)


# ===================================================================
# Portfolio
# ===================================================================
@router.get("/sites")
async def list_sites(
    q: str | None = Query(default=None, max_length=120),
    include_archived: bool = False,
    user: CurrentUser = Depends(require_perm("sites.manage")),
) -> dict:
    """Every site, with cached usage and any over-limit flags."""
    sites = await tenancy.list_sites(include_archived=include_archived, q=q)
    for site in sites:
        site["limits"] = tenancy.effective_limits(site)
        site["overLimit"] = _over_limit(site)

    return {
        "sites": sites,
        "totals": {
            "sites": len(sites),
            "active": sum(1 for s in sites if s["status"] == "active"),
            "suspended": sum(1 for s in sites if s["status"] == "suspended"),
            "leads30d": sum(int(s.get("leads_30d") or 0) for s in sites),
            "mediaBytes": sum(int(s.get("media_bytes") or 0) for s in sites),
            "pageViews30d": sum(int(s.get("page_views_30d") or 0) for s in sites),
        },
        "limitKeys": sorted(tenancy.DEFAULT_LIMITS),
        "defaultLimits": tenancy.DEFAULT_LIMITS,
        "isolation": await db.rls_status(),
    }


def _over_limit(site: dict) -> list[str]:
    limits = tenancy.effective_limits(site)
    breached = []
    for key, ceiling in limits.items():
        if not ceiling:
            continue
        counter = tenancy.LIMIT_SOURCES.get(key, (key, key))[0]
        used = site.get(counter)
        if used is not None and int(used) >= ceiling:
            breached.append(key)
    return breached


@router.post("/sites", status_code=201)
async def create_site(
    payload: SiteCreate,
    request: Request,
    user: CurrentUser = Depends(require_perm("sites.manage")),
) -> dict:
    """Create and provision a new client site from the admin.

    Returns the tenant, its owner and what was provisioned. The owner
    password is set here rather than emailed as an invite — there is no
    invite flow yet, so handing it over is the operator's job.
    """
    result = await tenancy.create_site(
        name=payload.name,
        slug=payload.slug,
        owner_email=payload.owner_email,
        owner_name=payload.owner_name,
        owner_password=payload.owner_password,
        domain=payload.domain,
        plan=payload.plan,
        limits=payload.limits,
        notes=payload.notes,
        actor_id=user.id,
        actor_email=user.email,
        ip=db.to_inet(client_ip(request)),
    )
    return result


@router.get("/sites/{slug}")
async def site_detail(
    slug: str, user: CurrentUser = Depends(require_perm("sites.manage"))
) -> dict:
    tenant = await tenancy.require_tenant(slug)
    report = await tenancy.usage_report(tenant)

    owners = await db.fetch(
        """SELECT id, email::text AS email, display_name, role::text AS role,
                  is_active, last_login_at
             FROM users WHERE tenant_id = $1 ORDER BY role, display_name LIMIT 50""",
        tenant["id"],
    )
    members = await db.fetch(
        """SELECT m.id, m.role::text AS role, m.created_at,
                  u.id AS user_id, u.email::text AS email, u.display_name
             FROM tenant_memberships m JOIN users u ON u.id = m.user_id
            WHERE m.tenant_id = $1 ORDER BY u.display_name""",
        tenant["id"],
    )
    timeline = await db.fetch(
        """SELECT action, actor_email, detail, created_at
             FROM tenant_events WHERE tenant_id = $1
            ORDER BY created_at DESC LIMIT 30""",
        tenant["id"],
    )
    return {
        "site": tenant,
        "domains": await tenancy.domains_for(tenant["id"]),
        "usage": report["usage"],
        "limits": report["limits"],
        "anyOverLimit": report["anyOverLimit"],
        "users": owners,
        "members": members,
        "events": timeline,
        "frontend": _frontend_hints(tenant),
    }


def _frontend_hints(tenant: dict) -> dict:
    """The exact URLs this site's static frontend should call.

    Every one of these works with the slug in the path, or without it
    on a verified domain — which is what "per-site frontend connection"
    comes down to in practice.
    """
    slug = tenant["slug"]
    return {
        "config": f"/api/v1/{slug}/config",
        "allContent": f"/api/v1/{slug}/all",
        "sitemap": f"/api/v1/{slug}/sitemap.xml",
        "robots": f"/api/v1/{slug}/robots.txt",
        "redirectLookup": f"/api/v1/{slug}/redirect?path=/old",
        "formIntake": f"/api/public/{slug}/forms/contact",
        "analyticsBeacon": f"/api/public/{slug}/collect",
        "conversions": f"/api/public/{slug}/conversions",
        "consent": f"/api/public/{slug}/consent",
        "hostBased": bool(tenant.get("primary_domain")),
    }


@router.patch("/sites/{slug}")
async def update_site(
    slug: str,
    payload: SiteUpdate,
    request: Request,
    user: CurrentUser = Depends(require_perm("sites.manage")),
) -> dict:
    tenant = await tenancy.require_tenant(slug)
    sent = payload.model_dump(exclude_unset=True)
    if not sent:
        raise HTTPException(400, "Nothing to save.")

    infra = None
    if "infra" in sent:
        source = payload.infra or {}
        unknown = set(source) - INFRA_KEYS
        if unknown:
            raise HTTPException(
                400,
                f"Unknown infra key(s): {', '.join(sorted(unknown))}. "
                f"Valid: {', '.join(sorted(INFRA_KEYS))}",
            )
        infra = {k: collapse(str(v), 300) for k, v in source.items() if v not in (None, "")}

    row = await db.fetch_one(
        f"""UPDATE tenants
               SET name  = coalesce($2, name),
                   plan  = coalesce($3, plan),
                   notes = CASE WHEN $4 THEN $5 ELSE notes END,
                   infra = CASE WHEN $6 THEN $7::jsonb ELSE infra END
             WHERE id = $1
             RETURNING {tenancy.TENANT_COLUMNS.replace("t.", "")}""",
        tenant["id"], collapse(payload.name, 120), collapse(payload.plan, 40),
        "notes" in sent, collapse(payload.notes, 1000),
        "infra" in sent, infra,
    )
    tenancy.invalidate(tenant["slug"])
    await tenancy.log_event(
        tenant_id=tenant["id"], tenant_slug=tenant["slug"], action="updated",
        actor_id=user.id, actor_email=user.email, detail={"fields": list(sent)},
        ip=db.to_inet(client_ip(request)),
    )
    return {"site": row}


@router.put("/sites/{slug}/status")
async def set_site_status(
    slug: str,
    payload: SiteStatusUpdate,
    request: Request,
    user: CurrentUser = Depends(require_perm("sites.manage")),
) -> dict:
    """Suspend, resume or archive. Suspending ends the site's sessions."""
    tenant = await tenancy.require_tenant(slug)

    # Suspending the site you are signed in to would end your own
    # session mid-request and lock you out of the portfolio.
    if tenant["id"] == user.tenant_id and payload.status.value != "active":
        raise HTTPException(
            400,
            "You are signed in to that site. Switch to another one first, or you "
            "will end your own session.",
        )

    result = await tenancy.set_status(
        tenant["id"], payload.status.value, reason=payload.reason,
        actor_id=user.id, actor_email=user.email, ip=db.to_inet(client_ip(request)),
    )
    return result


@router.put("/sites/{slug}/limits")
async def set_site_limits(
    slug: str,
    payload: SiteLimitsUpdate,
    request: Request,
    user: CurrentUser = Depends(require_perm("sites.manage")),
) -> dict:
    """Raise or lower one site's ceilings without touching the platform."""
    tenant = await tenancy.require_tenant(slug)
    limits = await tenancy.set_limits(tenant["id"], payload.limits)
    await tenancy.log_event(
        tenant_id=tenant["id"], tenant_slug=tenant["slug"], action="limits_changed",
        actor_id=user.id, actor_email=user.email, detail={"limits": limits},
        ip=db.to_inet(client_ip(request)),
    )
    fresh = await tenancy.require_tenant(slug)
    return {"limits": limits, "effective": tenancy.effective_limits(fresh)}


@router.delete("/sites/{slug}")
async def delete_site(
    slug: str,
    request: Request,
    confirm_slug: str = Query(default="", max_length=60),
    user: CurrentUser = Depends(require_perm("sites.manage")),
) -> dict:
    """Permanently delete an archived site and everything in it.

    Needs the slug typed back in `confirm_slug`. A boolean confirm is
    too easy to send at the wrong row; typing the name is the standard
    guard for an action with no undo.
    """
    tenant = await tenancy.require_tenant(slug)
    if confirm_slug.strip().lower() != tenant["slug"].lower():
        raise HTTPException(
            400,
            f"To delete this site permanently, pass confirm_slug={tenant['slug']}.",
        )
    if tenant["id"] == user.tenant_id:
        raise HTTPException(400, "You cannot delete the site you are signed in to.")

    return await tenancy.delete_site(
        tenant["id"], actor_id=user.id, actor_email=user.email,
        ip=db.to_inet(client_ip(request)),
    )


@router.post("/sites/{slug}/refresh-usage")
async def refresh_usage(
    slug: str, user: CurrentUser = Depends(require_perm("sites.manage"))
) -> dict:
    tenant = await tenancy.require_tenant(slug)
    return {"usage": await tenancy.compute_usage(tenant["id"])}


# ===================================================================
# Domains
# ===================================================================
@router.get("/sites/{slug}/domains")
async def list_domains(
    slug: str, user: CurrentUser = Depends(require_perm("sites.manage"))
) -> dict:
    tenant = await tenancy.require_tenant(slug)
    domains = await tenancy.domains_for(tenant["id"])
    return {
        "domains": domains,
        "origins": sorted(await tenancy.allowed_origins(tenant["id"])),
        "verification": {
            "instructions": (
                "Add a DNS TXT record at _crm-verify.<domain> with the token as its "
                "value, or serve it at /.well-known/crm-verify, then mark it verified."
            ),
        },
    }


@router.post("/sites/{slug}/domains", status_code=201)
async def add_domain(
    slug: str,
    payload: DomainCreate,
    request: Request,
    user: CurrentUser = Depends(require_perm("sites.manage")),
) -> dict:
    tenant = await tenancy.require_tenant(slug)
    domain = await tenancy.add_domain(
        tenant["id"], payload.domain,
        make_primary=payload.make_primary, actor_id=user.id,
    )
    await tenancy.log_event(
        tenant_id=tenant["id"], tenant_slug=tenant["slug"], action="domain_added",
        actor_id=user.id, actor_email=user.email, detail={"domain": domain["domain"]},
        ip=db.to_inet(client_ip(request)),
    )
    return {"domain": domain}


@router.post("/sites/{slug}/domains/{domain_id}/verify")
async def verify_domain(
    slug: str,
    domain_id: int,
    request: Request,
    user: CurrentUser = Depends(require_perm("sites.manage")),
) -> dict:
    tenant = await tenancy.require_tenant(slug)
    domain = await tenancy.verify_domain(tenant["id"], domain_id)
    await tenancy.log_event(
        tenant_id=tenant["id"], tenant_slug=tenant["slug"], action="domain_verified",
        actor_id=user.id, actor_email=user.email, detail={"domain": domain["domain"]},
        ip=db.to_inet(client_ip(request)),
    )
    return {"domain": domain}


@router.post("/sites/{slug}/domains/{domain_id}/primary")
async def make_primary(
    slug: str, domain_id: int, user: CurrentUser = Depends(require_perm("sites.manage"))
) -> dict:
    tenant = await tenancy.require_tenant(slug)
    await tenancy.set_primary_domain(tenant["id"], domain_id)
    return {"domains": await tenancy.domains_for(tenant["id"])}


@router.delete("/sites/{slug}/domains/{domain_id}")
async def remove_domain(
    slug: str,
    domain_id: int,
    request: Request,
    user: CurrentUser = Depends(require_perm("sites.manage")),
) -> dict:
    tenant = await tenancy.require_tenant(slug)
    domain = await tenancy.remove_domain(tenant["id"], domain_id)
    await tenancy.log_event(
        tenant_id=tenant["id"], tenant_slug=tenant["slug"], action="domain_removed",
        actor_id=user.id, actor_email=user.email, detail={"domain": domain},
        ip=db.to_inet(client_ip(request)),
    )
    return {"ok": True, "domain": domain}


# ===================================================================
# Cross-site people and audit
# ===================================================================
@router.get("/users")
async def directory(
    q: str | None = Query(default=None, max_length=120),
    user: CurrentUser = Depends(require_perm("sites.manage")),
) -> dict:
    """Every account on the install, with the sites it can reach.

    The one screen that answers "who has access to what" — otherwise
    that question needs a query per site.
    """
    rows = await db.fetch(
        """SELECT u.id, u.email::text AS email, u.display_name,
                  u.role::text AS home_role, u.is_active, u.last_login_at,
                  t.slug::text AS home_site, t.name AS home_site_name,
                  (SELECT count(*)::int FROM tenant_memberships m WHERE m.user_id = u.id)
                    AS extra_sites,
                  (SELECT count(*)::int FROM sessions s
                    WHERE s.user_id = u.id AND s.expires_at > now()) AS live_sessions,
                  (SELECT tt.confirmed_at IS NOT NULL FROM user_totp tt WHERE tt.user_id = u.id)
                    AS has_2fa
             FROM users u JOIN tenants t ON t.id = u.tenant_id
            WHERE ($1::text IS NULL
                   OR u.email::text ILIKE '%' || $1 || '%'
                   OR u.display_name ILIKE '%' || $1 || '%')
            ORDER BY t.name, u.display_name LIMIT 500""",
        collapse(q, 120),
    )
    return {"users": rows}


@router.get("/events")
async def platform_events(
    limit: int = Query(default=100, ge=10, le=500),
    user: CurrentUser = Depends(require_perm("sites.manage")),
) -> dict:
    """Site lifecycle audit — creations, suspensions, deletions.

    Survives a hard delete: tenant_slug is stored as text next to the
    nullable tenant_id, so the record of a deletion outlives the site.
    """
    rows = await db.fetch(
        """SELECT e.id, e.tenant_id, e.tenant_slug, e.action, e.actor_email,
                  e.detail, e.ip::text AS ip, e.created_at,
                  (e.tenant_id IS NULL) AS site_gone
             FROM tenant_events e ORDER BY e.created_at DESC LIMIT $1""",
        limit,
    )
    return {"events": rows}


@router.get("/isolation")
async def isolation_report(
    user: CurrentUser = Depends(require_perm("sites.manage")),
) -> dict:
    """Is tenant isolation actually enforced, layer by layer?

    Honest about which layers are on. An install reading
    `database.effective = false` has application-layer scoping only —
    which is the original design and still safe, but it is not the
    defence-in-depth the schema is set up for.
    """
    rls = await db.rls_status(refresh=True)
    unscoped = await db.fetch_one(
        """SELECT count(*)::int AS n FROM information_schema.tables t
            WHERE t.table_schema = 'public' AND t.table_type = 'BASE TABLE'
              AND t.table_name <> 'tenants'
              AND NOT EXISTS (SELECT 1 FROM information_schema.columns c
                               WHERE c.table_schema = 'public'
                                 AND c.table_name = t.table_name
                                 AND c.column_name = 'tenant_id')"""
    )
    return {
        "authentication": {
            "sessionCarriesTenant": True,
            "suspendedSiteEndsSessions": True,
            "note": "A session row carries tenant_id; suspending a site deletes its sessions.",
        },
        "authorization": {
            "permissions": len(await permissions_for(user.tenant_id, user.role)),
            "portfolioGatedOn": "sites.manage",
            "rolesWithPortfolioAccess": sorted(
                role for role, perms in DEFAULTS.items() if "sites.manage" in perms
            ),
        },
        "api": {
            "publicRoutesResolveTenant": True,
            "corsPerSite": True,
            "note": "Public origins come from each site's verified domains, "
                    "not one shared allow-list.",
        },
        "data": {
            **rls,
            "tablesWithoutTenantId": unscoped["n"],
            "note": "TenantDB binds $1 unconditionally; RLS is the backstop and "
                    "needs a non-superuser role to bite.",
        },
    }


# ===================================================================
# Site switching and membership (moved from users.py)
# ===================================================================
@router.get("/my-sites")
async def my_sites(user: CurrentUser = Depends(require_user)) -> dict:
    """Sites this account can switch into. Not gated on sites.manage —
    seeing where you have access is part of owning the account."""
    granted = await permissions_for(user.tenant_id, user.role)
    can_manage = "sites.manage" in granted

    sites = await db.fetch(
        """SELECT t.id, t.slug::text AS slug, t.name, t.status::text AS status,
                  coalesce(m.role::text, u.role::text) AS role,
                  (t.id = $2) AS is_current,
                  (u.id IS NOT NULL) AS is_home
             FROM tenants t
             LEFT JOIN tenant_memberships m ON m.tenant_id = t.id AND m.user_id = $1
             LEFT JOIN users u ON u.id = $1 AND u.tenant_id = t.id
            WHERE t.status <> 'archived'
              AND (m.user_id IS NOT NULL OR u.id IS NOT NULL
                   OR $3::boolean)
            ORDER BY is_current DESC, is_home DESC, t.name""",
        user.id, user.tenant_id, can_manage,
    )
    return {"sites": sites, "canManage": can_manage}


@router.post("/switch")
async def switch_site(
    request: Request,
    tenant_slug: str = Query(max_length=60),
    user: CurrentUser = Depends(require_user),
) -> dict:
    """Re-point the current session at another site.

    The session row carries tenant_id, so this is one UPDATE and every
    TenantDB built afterwards is scoped to the new site — including its
    RLS scope. Audited on both sides, because a support engineer
    switching into a client's site is exactly the event a client will
    later ask about.
    """
    target = await tenancy.by_slug(tenant_slug)
    if not target:
        raise HTTPException(404, "Unknown site.")
    if target["status"] != "active":
        raise HTTPException(400, f"“{target['name']}” is {target['status']}.")

    if target["id"] != user.tenant_id:
        granted = await permissions_for(user.tenant_id, user.role)
        membership = await db.fetch_one(
            "SELECT role::text AS role FROM tenant_memberships WHERE tenant_id = $1 AND user_id = $2",
            target["id"], user.id,
        )
        home = await db.fetch_one(
            "SELECT 1 FROM users WHERE id = $1 AND tenant_id = $2", user.id, target["id"]
        )
        if not membership and not home and "sites.manage" not in granted:
            raise HTTPException(403, "You do not have access to that site.")

    await db.execute(
        "UPDATE sessions SET tenant_id = $2 WHERE id = $1::uuid", user.session_id, target["id"]
    )
    ip = db.to_inet(client_ip(request))
    # Logged in the site being entered, so its own audit trail shows it.
    await events.log_activity(
        target["id"], "site.entered", user_id=user.id,
        meta={"from": user.tenant_slug, "actor": user.email}, ip=ip,
    )
    await tenancy.log_event(
        tenant_id=target["id"], tenant_slug=target["slug"], action="session_switched",
        actor_id=user.id, actor_email=user.email,
        detail={"from": user.tenant_slug}, ip=ip,
    )
    return {"ok": True, "site": {"id": target["id"], "slug": target["slug"],
                                 "name": target["name"]}}


@router.get("/sites/{slug}/members")
async def list_members(
    slug: str, user: CurrentUser = Depends(require_perm("users.view"))
) -> dict:
    tenant = await tenancy.require_tenant(slug)
    granted = await permissions_for(user.tenant_id, user.role)
    if tenant["id"] != user.tenant_id and "sites.manage" not in granted:
        raise HTTPException(403, "You do not have access to that site.")

    rows = await db.fetch(
        """SELECT m.id, m.role::text AS role, m.created_at,
                  u.id AS user_id, u.email::text AS email, u.display_name,
                  g.display_name AS granted_by_name
             FROM tenant_memberships m
             JOIN users u ON u.id = m.user_id
             LEFT JOIN users g ON g.id = m.granted_by
            WHERE m.tenant_id = $1 ORDER BY u.display_name""",
        tenant["id"],
    )
    return {"members": rows}


@router.post("/members", status_code=201)
async def grant_membership(
    payload: MembershipCreate,
    request: Request,
    user: CurrentUser = Depends(require_perm("sites.manage")),
) -> dict:
    """Give an existing account access to another site."""
    email = valid_email(payload.user_email)
    if not email:
        raise HTTPException(400, "That email address is not valid.")

    target = await tenancy.require_tenant(payload.tenant_slug)
    account = await db.fetch_one(
        "SELECT id, display_name FROM users WHERE email = $1 AND is_active ORDER BY id LIMIT 1",
        email,
    )
    if not account:
        raise HTTPException(404, "No active account uses that email address.")

    role = payload.role if isinstance(payload.role, str) else payload.role.value
    if role not in DEFAULTS:
        raise HTTPException(400, "Unknown role.")
    if role == "super_admin":
        raise HTTPException(
            400,
            "Super Admin is an install-wide role, not a per-site one. Set it on the "
            "account itself.",
        )

    row = await db.fetch_one(
        """INSERT INTO tenant_memberships (tenant_id, user_id, role, granted_by)
           VALUES ($1, $2, $3::user_role, $4)
           ON CONFLICT (tenant_id, user_id) DO UPDATE SET role = EXCLUDED.role
           RETURNING id, role::text AS role, created_at""",
        target["id"], account["id"], role, user.id,
    )
    ip = db.to_inet(client_ip(request))
    await tenancy.log_event(
        tenant_id=target["id"], tenant_slug=target["slug"], action="member_added",
        actor_id=user.id, actor_email=user.email,
        detail={"user": email, "role": role}, ip=ip,
    )
    return {"member": {**row, "user_id": account["id"], "email": email,
                       "display_name": account["display_name"]}}


@router.delete("/members/{membership_id}")
async def revoke_membership(
    membership_id: int,
    request: Request,
    user: CurrentUser = Depends(require_perm("sites.manage")),
) -> dict:
    removed = await db.fetch(
        """DELETE FROM tenant_memberships WHERE id = $1
           RETURNING tenant_id, user_id""",
        membership_id,
    )
    if not removed:
        raise HTTPException(404, "That membership no longer exists.")

    row = removed[0]
    # Any session that had switched into the site loses it immediately,
    # or revoking access would only take effect at next sign-in.
    ended = await db.fetch(
        "DELETE FROM sessions WHERE user_id = $1 AND tenant_id = $2 RETURNING id",
        row["user_id"], row["tenant_id"],
    )
    tenant = await db.fetch_one(
        "SELECT slug::text AS slug FROM tenants WHERE id = $1", row["tenant_id"]
    )
    await tenancy.log_event(
        tenant_id=row["tenant_id"], tenant_slug=(tenant or {}).get("slug", "?"),
        action="member_removed", actor_id=user.id, actor_email=user.email,
        detail={"user_id": row["user_id"], "sessions_ended": len(ended)},
        ip=db.to_inet(client_ip(request)),
    )
    return {"ok": True, "sessionsEnded": len(ended)}
