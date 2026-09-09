# CRM Admin — FastAPI

A multi-tenant CMS and lead CRM with a WordPress-shaped admin panel. FastAPI +
asyncpg + PostgreSQL on the back, vanilla HTML/CSS/JS on the front — no build
step, no framework, no jQuery.

Built for the case where one control plane serves many client sites: every row
carries a `tenant_id`, every query is scoped by it, and a new client is a row in
`tenants` rather than another server to patch. A Super Admin creates and
provisions a site from the admin itself — tenant, owner, content types,
menus, templates and defaults — then manages the whole portfolio from one
screen.

It is **API-first**: the admin edits content here, and static frontends read it
from a versioned public API (`/api/v1/…`) at build time or runtime. Publishing
fires a build hook, regenerates the sitemap and queues a CDN purge, so a change
in the admin reaches the live site without anyone touching a deploy pipeline.

## Quick start

Three ways in, pick one.

**Fastest — one script** (needs Python 3.11+ and a running PostgreSQL):

```bash
./setup.sh          # Linux / macOS
.\setup.ps1         # Windows PowerShell
```

It creates the venv, installs, applies both schema files, seeds a workspace and
starts the server. If the database isn't reachable it prints the two `CREATE`
commands you need and stops. Re-run any time; `./setup.sh --start` skips setup.

The schema lives in three idempotent files applied in order — `db/schema.sql`
(the original core: tenants, users, sessions, leads, forms, pages),
`db/platform.sql` (content, media, SEO, marketing, analytics, publishing,
operations, compliance) and `db/tenancy.sql` (site status, domains, usage, and
the row-level-security policies). All three are safe to re-run, and re-running
`tenancy.sql` is how a newly added table picks up its isolation policy.

**Zero install — Docker:**

```bash
docker compose up --build
docker compose exec app python -m db.seed     # once
```

**Manual**, if you'd rather see every step:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env                     # set DATABASE_URL at minimum
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f db/schema.sql
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f db/platform.sql
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f db/tenancy.sql
OWNER_EMAIL=you@example.com OWNER_PASSWORD='at-least-12-chars' python -m db.seed

uvicorn app.main:app --reload            # http://localhost:8000
```

Sign in at `/login` with the workspace slug (`demo` by default), your email and
password. A working public form lives at `/form-example.html`. Interactive API
docs are at `/api/docs` in development and disabled in production.

## What's in the box

| Area | Covered |
| --- | --- |
| **Content** | Schema-driven content types (page, post, product, case study, testimonial, FAQ, plus your own), sanitized HTML/rich text with host-allow-listed embeds, draft → published → scheduled → trashed states, bulk publish/trash/taxonomy/author, manual slugs with automatic 301s on rename, revision history with rollback, shareable draft-preview links, managed categories, tags and authors |
| **SEO** | Per-item meta title and description with live character indicators, Open Graph and Twitter Card fields, `noindex`/`nofollow`, focus keyword with in-title/in-body checks, Schema.org JSON-LD, sitemap generated on publish (honouring `noindex`), robots.txt editor, folded 404 log with redirect suggestions, 301/302/307/308 redirect manager, breadcrumb config |
| **Media** | Central library with search, folders and tags, alt/title/caption, magic-byte validation, EXIF stripping, automatic WebP + AVIF derivatives at responsive widths, content-addressed keys with a one-year CDN TTL, usage tracking that blocks deleting a file a page still renders, local or S3 storage |
| **Access** | Super Admin / Admin / Editor / Author / Contributor plus the original Agent and Viewer CRM roles, 45 named permissions with per-workspace overrides, cross-site membership and session switching, profiles with avatar and social links, optional TOTP 2FA with single-use recovery codes, active-session list with revoke, searchable audit log |
| **Multi-site** | Create and provision a site from the admin; per-site users, content, media, leads, settings, integrations and API keys; multiple verified domains per site with host-based resolution; per-site CORS derived from those domains; per-site limits and infrastructure overrides; suspend/resume/archive/delete with a lifecycle audit that outlives a deletion; portfolio KPIs and a cross-site user directory; four-layer isolation with a report that says which layers are actually enforced |
| **Site** | Drag-free nested menu builder (keyboard- and touch-accessible) with links that resolve content by id, reusable blocks for CTAs and banners, site identity, logo, favicon, contact details, SMTP sender, maintenance mode, timezone and locale — all exposed to the frontend through one config endpoint |
| **Pipeline** | Lead records with status (New → Contacted → Qualified → Proposal → Won → Lost), owner, follow-up date, deal value, notes, merged activity timeline, search, filters, bulk actions and CSV export |
| **Forms** | Admin-managed field builder with validation, lead mapping, honeypot, fill-time and Turnstile checks, per-form success/redirect, conditional notification rules, autoresponders from templates, full submission log with formula-safe CSV export, conversion events for forms, CTAs, phone, email and WhatsApp |
| **Marketing** | Double opt-in subscribers with import/export/unsubscribe, campaigns with audience filters and scheduling through the shared outbox, announcement bars/popups/banners with path and frequency rules, UTM link builder with countable short links |
| **Analytics** | GA4, GTM and Search Console configuration with copy-paste snippets, plus an aggregate-only first-party beacon: visitors, page views, conversion rate, subscribers, blog views, top pages, traffic sources, per-page conversion rate, and a portfolio view across sites |
| **Publishing** | Build hooks for Vercel/Netlify/GitHub/Cloudflare with debouncing and retries, CloudFront invalidation queue, "what is live vs edited" status, versioned public content API with scoped API keys |
| **Operations** | Queue depths at a glance, folded error log, notification centre, unified trash with confirmed permanent delete, `pg_dump` backups to S3, outbound health checks that alert on the second failure |
| **Compliance** | Consent logging with policy version and evidence, cookie-consent config, data-subject export (JSON across six tables) and erasure (anonymize or delete), per-scope retention policies with a preview and a worker that enforces them |
| Attribution | Landing page, referrer and the full UTM set captured at submission, first-touch persisted per session |
| Outbound | Webhook endpoints with HMAC-SHA256 signing, idempotency keys, exponential backoff, delivery log — subscribable to publishes and build failures, not just leads |
| Page builder | The original block builder is still here: typed JSON blocks (hero, rich text, image, features, quote, lead form, …), markdown-subset formatting, per-page theme, draft → publish with revisions, served at `/p/{tenant}/{slug}` |
| Sign-up | Self-service registration at `/register` — creates a fresh workspace with the registrant as owner, a starter contact form, the built-in content types, default menus and email templates (`ALLOW_SIGNUPS=0` for invite-only installs) |
| Email | Queued outbound email (`email_outbox` + worker, retries with backoff) through a provider-agnostic sender — SES SMTP in production, log provider in dev. Powers lead notifications, autoresponders, campaigns, subscriber confirmations and password reset at `/forgot` → `/reset` |

### Two content systems, on purpose

`pages` is the **visual block builder**: it renders its own HTML and serves it
at `/p/{tenant}/{slug}`. Good for a one-off landing page nobody wants to code.

`content_items` is the **schema-driven, API-first model**. Rich text is stored
as sanitized HTML, everything else as typed JSON validated against the content
type's own `field_schema`. Nothing is rendered server-side — a static frontend
reads it from `/api/v1/{site}/…` and owns the markup.

They coexist because they answer different questions. Both appear in the
sitemap.

## Layout

```
app/
  main.py            app wiring, lifespan, middleware, error shape, static SPA
  config.py          settings from the environment
  db.py              asyncpg pool, type codecs, TenantDB scoping, RLS probe
  tenancy.py         the control plane: resolution, lifecycle, domains, limits
  security.py        sessions, bcrypt, CSRF, role dependencies
  permissions.py     role → permission matrix, overrides, require_perm()
  ratelimit.py       fixed-window limiter (in-process — see caveats)
  events.py          activity log, webhook queue, notifications, folded error log
  schemas.py         pydantic request models (one place for every shape)
  mail.py            outbound email queue, worker and provider abstraction
  templating.py      {{placeholder}} rendering for email, escaping-aware
  sanitize.py        rich-text HTML sanitization (nh3 + embed allow-list)
  content.py         content-type schemas, field coercion, SEO checks, snapshots
  bootstrap.py       provisioning a workspace with its defaults
  publishing.py      sitemaps, build hooks, CDN invalidation
  imaging.py         upload validation, EXIF stripping, WebP/AVIF derivatives
  storage.py         media object storage (local disk or S3/CloudFront)
  totp.py            RFC 6238 two-factor, on the standard library
  workers.py         scheduled publishing, campaigns, builds, health, retention
  pagebuilder.py     page block validation + escaped server-side rendering
  routers/
    auth.py          sign in / out / me / second factor
    users.py         profiles, 2FA, sessions, roles, site membership, audit
    leads.py         list, detail, update, bulk, notes, timeline, CSV
    content.py       content types, items, lifecycle, revisions, taxonomy
    media.py         library, upload, folders, usage, local file serving
    seo.py           redirects, 404 log, sitemap, robots.txt
    site.py          menus, reusable blocks, settings, public site config
    sites.py         multi-site control plane (/api/platform/*)
    forms.py         form builder, submissions, email templates, conversions
    marketing.py     subscribers, campaigns, announcements, UTM links
    analytics.py     beacon, KPI overview, per-page report, portfolio
    deploy.py        build hooks, CDN, API keys, public content API (/api/v1)
    ops.py           queues, errors, notifications, trash, backups, health
    compliance.py    consent, data-subject requests, retention
    admin.py         dashboard, users, webhooks, activity, lead settings
    intake.py        public form submission (unauthenticated)
    pages.py         page builder API + published pages (/p/{tenant}/{slug})
db/
  schema.sql         original core: tenants, users, sessions, leads, forms, pages
  platform.sql       content, media, SEO, marketing, analytics, ops, compliance
  tenancy.sql        site status, domains, usage, RLS policies, restricted role
  seed.py            tenant + owner + default form + samples, then provisioning
public/
  css/admin.css      one stylesheet, design tokens at the top
  js/
    api.js           fetch wrapper: CSRF, cookies, error shape
    ui.js            h(), mount(), toasts, the drawer stack
    kit.js           shared view parts: tabs, tables, form drawers, confirms
    app.js           bootstrap + hash router + permission-aware sidebar
    views.js         dashboard, leads, users, webhooks, activity, settings
    pages.js         the block builder
    content.js media.js seo.js site.js forms.js
    marketing.js insights.js publishing.js operations.js account.js
    platform.js      the portfolio: sites, domains, limits, isolation report
```

### Where the framework does the work

Pydantic replaced most of the hand-rolled validation: lengths, enums, types and
required fields are enforced before a handler runs. `LeadUpdate` and `UserUpdate`
use `extra="forbid"`, so a request smuggling `{"tenant_id": 99}` into a PATCH is
rejected at the boundary rather than being silently ignored by an allow-list.

`model_dump(exclude_unset=True)` distinguishes "field absent" from "field set to
null", which matters when clearing a follow-up date.

Dependencies carry the auth model: `require_user` checks the session and the
CSRF header, `require_role("admin")` layers on the rank check, and `tenant_db`
hands the route a `TenantDB` already bound to the caller's tenant.

Newer routes use `require_perm("content.publish")` instead of a role rank. The
role is a label; the permission is what a handler actually needs, and a
workspace can override any single cell of the matrix without inventing a role.
`ROLE_RANK` still backs the original `require_role` gates, with the new content
roles slotted below `admin` so an Editor cannot pass `require_role("admin")`.

### Multi-tenancy

The platform is a control plane, not a template you deploy per client. Every
table except `tenants` itself carries a `tenant_id`, and isolation is enforced
at four layers.

**1. Authentication.** A session row carries `tenant_id`, so what a session can
reach is decided at sign-in rather than per request. Suspending a site deletes
its sessions, and `optional_user` additionally refuses any session whose site
is not active — otherwise "suspended" would mean "suspended at next sign-in".

**2. Authorization.** The portfolio is gated on `sites.manage`, which
`_SITE_ADMIN` deliberately excludes. A site's own Owner has every permission
*for their site* and cannot list, create, enter or administer another one.

**3. API.** Every public route resolves its tenant through
`tenancy.resolve_public` — slug first, then the `Host` header — so a suspended
site answers 503 (it exists; a 404 would cost the client their rankings) and an
archived one answers 404. CORS is per-site: allowed origins come from that
site's *verified* domains, not from one install-wide list. The original single
`PUBLIC_FORM_ORIGINS` meant any client's frontend could post to any other
client's forms; it now survives only as a development escape hatch.

**4. Data access.** `TenantDB` binds `$1` to the tenant id, so a call site
cannot write a scoped query without the scope. Underneath it,
`db/tenancy.sql` puts a `tenant_isolation` policy on all 60 tenant tables with
`FORCE ROW LEVEL SECURITY`, and every `TenantDB` call declares its tenant with
`SET LOCAL app.tenant_id` inside a transaction. A query that forgets its WHERE
clause entirely still returns only its own rows, and a write that forges
another tenant's id is refused by the database.

Verified: signing in to site B and requesting site A's content by id returns
404 for read, patch and delete; a bulk action on A's ids from B's session
affects zero rows; a scoped query with no tenant filter at all returns only its
own tenant's rows; and forging another tenant's id on INSERT or moving a row
out with UPDATE both raise a policy violation.

#### Row-level security needs the right database role

This is the part that is easy to get wrong and expensive to believe:
**PostgreSQL exempts superusers and `BYPASSRLS` roles from every policy**, and
`FORCE ROW LEVEL SECURITY` only reaches the table *owner*. `setup.sh` creates
`crm` as a superuser (it needs to, for `CREATE EXTENSION`), so with the default
`DATABASE_URL` the policies are installed and completely inert.

`db/tenancy.sql` therefore also creates `crm_app` — no ownership, no superuser,
no way to opt out of a policy. Give it a password from your secret store and
point the app at it:

```bash
psql "$DATABASE_URL" -c "ALTER ROLE crm_app PASSWORD '<from Secrets Manager>'"
# then
DATABASE_URL=postgres://crm_app:<pw>@host:5432/crm
```

Nothing is committed with a password, and nothing silently assumes this was
done. `GET /healthz` reports it, the startup log says which layers are active,
and Platform → Isolation shows it layer by layer:

```json
{ "ok": true,
  "isolation": { "applicationScope": true, "rowLevelSecurity": false,
                 "warning": "Connected as crm, which bypasses every policy…" } }
```

Application-layer scoping is unconditional either way. RLS is defence in depth
against a bug in it, not a replacement for it.

The policy is *scoped-when-declared*: an undeclared connection is
unrestricted, which is what keeps the worker loops, portfolio reporting,
migrations and `pg_dump` working. `TenantDB` always declares, so every
tenant-scoped path is covered; the raw `db.fetch`/`db.execute` helpers
deliberately do not, and those call sites are the ones to review by hand.

### Creating and scaling a site

Creating a site is provisioning, in one call: the tenant row, its owner, a
starter contact form, six content types, two taxonomies, two menus, four email
templates, nine settings groups and seven retention policies. The tenant, owner
and form go in one transaction (a failure part-way must not leave an ownerless
workspace); the defaults follow after it commits, because a site missing a
default menu is usable and one missing its owner is not.

Each site carries its own **limits** (`tenants.limits`) and **infrastructure
overrides** (`tenants.infra`), which is what "scale one site without changing
the platform" comes down to:

| Lever | Effect |
| --- | --- |
| `limits.media_bytes`, `media_files` | Checked before derivatives are built, so a refusal does not burn the CPU first |
| `limits.content_items`, `users`, `api_keys`, `subscribers`, `domains` | Refused with 402 at the ceiling |
| `limits.leads_per_month`, `emails_per_month` | **Soft**: notified, never blocked — losing a client's enquiry to a quota costs them more than the overage costs you |
| `infra.media_s3_bucket`, `media_s3_prefix`, `media_public_base_url` | One busy site gets its own bucket and CDN |
| `infra.cloudfront_distribution_id`, `build_target`, `region` | Its own invalidation target and build pipeline |

Gates count live; the `tenant_usage` rollup that the portfolio screen reads is
refreshed hourly by a worker, so a stale number can only make a dashboard old,
never let a site past its ceiling.

### Per-site domains and frontend connection

`tenant_domains` holds several hostnames per site with a primary flag and a
verification token, unique across the install — two sites claiming one hostname
would be a tenant-confusion bug, not a validation nicety. Only **verified**
domains resolve or are trusted for CORS, so registering a hostname is a claim,
not an authorization.

The DNS/TXT check itself is the operator's to perform; this platform will not
pretend to prove a DNS record from inside a container that may have no egress.
What it guarantees is that an unverified domain is inert.

Each site's Platform detail screen lists the exact URLs its frontend should
call. Every one works with the slug in the path, and also without it when the
request arrives on a verified domain — which is the single-domain,
path-routed deployment (CloudFront sending `/api/*` to the CMS and everything
else to the static site).

## API

Admin routes need a session cookie; writes also need `X-CSRF-Token` (returned by
`/api/auth/me` and `/api/auth/login`). Errors are `{"error": "..."}` throughout —
FastAPI's `{"detail": ...}` shape is remapped in `main.py`, including for
validation failures, so the SPA reads one field.

```
POST   /api/auth/login          { tenant, email, password }
POST   /api/auth/register       { workspace, display_name, email, password }  new tenant + owner
POST   /api/auth/forgot         { tenant, email }        emails a 30-min reset link
POST   /api/auth/reset          { token, password }      single-use; signs out all sessions
POST   /api/auth/logout
GET    /api/auth/me

GET    /api/leads               ?status=&q=&assigned_to=&from=&to=&sort=&dir=&page=&per_page=
GET    /api/leads/counts
GET    /api/leads/{id}
PATCH  /api/leads/{id}          { status, assigned_to, follow_up_on, value_amount, ... }
POST   /api/leads/bulk          { ids[], action: status|assign|spam|delete, value }
POST   /api/leads/{id}/notes    { body }
GET    /api/leads/export/csv    same filters as the list

GET    /api/dashboard           ?days=30
GET    /api/activity            ?limit=100
GET    /api/users               POST /api/users        PATCH /api/users/{id}
GET    /api/webhooks            POST /api/webhooks     DELETE /api/webhooks/{id}
GET    /api/settings            PUT   /api/settings/{key}

GET    /api/pages               POST /api/pages         { title, slug }
GET    /api/pages/{id}          PATCH /api/pages/{id}   { title, slug, description, blocks, theme }
POST   /api/pages/{id}/publish  POST  /api/pages/{id}/unpublish
GET    /api/pages/{id}/revisions
POST   /api/pages/{id}/revisions/{rev}/restore
GET    /api/pages/{id}/preview  rendered draft HTML (iframed by the editor)
```

### Content (2.1)

```
GET    /api/content/types                POST /api/content/types
PATCH  /api/content/types/{id}           DELETE /api/content/types/{id}

GET    /api/content/items         ?type=&status=&q=&term_id=&author_id=&mine=&sort=&page=
GET    /api/content/items/counts         status tallies per type
POST   /api/content/items                { type, title, slug?, body?, fields, seo, term_ids }
GET    /api/content/items/{id}           includes field_schema, terms, seoReport, can{}
PATCH  /api/content/items/{id}           slug change on live content leaves a 301
POST   /api/content/items/{id}/publish   { scheduled_for? }  future date = scheduled
POST   /api/content/items/{id}/unpublish
POST   /api/content/items/{id}/trash     POST .../restore
DELETE /api/content/items/{id}?confirm=true      permanent, trashed items only
POST   /api/content/items/bulk           { ids[], action, term_ids?, author_id? }
GET    /api/content/items/{id}/revisions POST .../revisions/{rev}/restore
POST   /api/content/items/{id}/preview   mints a 48-hour draft-preview token

GET    /api/content/taxonomies    POST/PATCH/DELETE
GET    /api/content/terms         POST/PATCH/DELETE
GET    /api/content/authors
```

### SEO, media, site (2.2, 2.3, 2.5)

```
GET    /api/seo/redirects         POST/PATCH/DELETE     loop-checked on create
GET    /api/seo/not-found         POST /api/seo/not-found/{id}/resolve  → a redirect
POST   /api/seo/not-found/{id}/ignore    DELETE /api/seo/not-found
GET    /api/seo/sitemaps          POST /api/seo/sitemaps/regenerate
GET    /api/seo/robots            PUT  /api/seo/robots

GET    /api/media                 ?q=&folder_id=&mime=&tag=&unused=&trashed=&page=
POST   /api/media                 multipart: file, folder_id?, alt_text?, tags?
GET    /api/media/{id}            PATCH /api/media/{id}
DELETE /api/media/{id}[?force=true]      soft delete; 409 while still in use
POST   /api/media/{id}/restore    DELETE /api/media/{id}/purge?confirm=true
POST   /api/media/bulk            GET /api/media/tags
GET    /api/media/folders/list    POST/PATCH/DELETE /api/media/folders

GET    /api/site/menus            POST /api/site/menus
GET    /api/site/menus/{id}       PUT  /api/site/menus/{id}   replaces the whole tree
GET    /api/site/menus/link-targets/list
GET    /api/site/blocks           POST/PATCH/DELETE
GET    /api/site/settings         PUT  /api/site/settings/{key}
```

### People, forms, marketing (2.4, 2.7, 2.8)

```
GET    /api/profile               PATCH /api/profile     POST /api/profile/password
POST   /api/profile/2fa/start     POST /api/profile/2fa/confirm   → recovery codes
POST   /api/profile/2fa/disable   POST /api/profile/2fa/recovery-codes
GET    /api/sessions              DELETE /api/sessions/{id}
POST   /api/sessions/revoke-others
GET    /api/roles                 PUT  /api/roles/permissions   { role, permission, allowed }
POST   /api/roles/permissions/reset?role=editor
GET    /api/sites                 POST /api/sites/switch?tenant_slug=
GET    /api/sites/{slug}/members  POST /api/sites/members   DELETE /api/sites/members/{id}
GET    /api/audit                 ?action=&user_id=&object_type=&days=&page=

GET    /api/forms                 POST /api/forms      GET/PATCH/DELETE /api/forms/{id}
GET    /api/forms/{id}/submissions        GET .../submissions/export
GET    /api/templates             POST/PATCH/DELETE    POST /api/templates/{id}/preview
GET    /api/conversions           ?days=&kind=

GET    /api/marketing/subscribers POST/PATCH/DELETE
POST   /api/marketing/subscribers/import   GET .../export
GET    /api/marketing/campaigns   POST/PATCH/DELETE
POST   /api/marketing/campaigns/{id}/test  POST .../send { scheduled_for? }  POST .../cancel
GET    /api/marketing/announcements        POST/PATCH/DELETE
GET    /api/marketing/utm-links   POST/DELETE
```

### Analytics, publishing, operations, compliance (2.9–2.12)

```
GET    /api/analytics/overview    ?days=30      the KPI set
GET    /api/analytics/pages       GET /api/analytics/portfolio
GET    /api/analytics/integrations             DELETE /api/analytics/data?confirm=true

GET    /api/publishing/status     what is live, what is edited, what is scheduled
GET    /api/publishing/hooks      POST/PATCH/DELETE
POST   /api/publishing/trigger    { hook_id?, reason? }
POST   /api/publishing/invalidate { paths[] }   POST /api/publishing/sitemap
GET    /api/publishing/api-keys   POST (secret shown once)  DELETE (revoke)

GET    /api/ops/overview          queue depths, health, backups, trash
GET    /api/ops/errors            POST /api/ops/errors/{id}/resolve   DELETE /api/ops/errors
GET    /api/ops/notifications     POST .../{id}/read   POST .../read-all
GET    /api/ops/trash             POST /api/ops/trash/empty?confirm=true&older_than_days=
GET    /api/ops/backups           POST /api/ops/backups { kind }
GET    /api/ops/health-checks     POST/PATCH/DELETE   POST .../{id}/run

GET    /api/compliance/consent    GET /api/compliance/consent/{email}/current
GET    /api/compliance/requests   POST /api/compliance/requests
GET    /api/compliance/requests/{id}/export     JSON bundle, six tables
POST   /api/compliance/requests/{id}/erase?confirm=true&mode=anonymize|delete
GET    /api/compliance/lookup?email=
GET    /api/compliance/retention  PUT (policy)   POST .../preview   POST .../run
```

### Multi-site control plane

Everything under `/api/platform` needs `sites.manage` (Super Admin), except
`my-sites` and `switch`, which any account uses to move between the sites it
can reach.

```
GET    /api/platform/sites             ?q=&include_archived=   portfolio + usage
POST   /api/platform/sites             create and fully provision a site
GET    /api/platform/sites/{slug}      usage vs limits, domains, people, audit
PATCH  /api/platform/sites/{slug}      { name, plan, notes, infra }
PUT    /api/platform/sites/{slug}/status   { status, reason }  ends sessions
PUT    /api/platform/sites/{slug}/limits   { limits }          per-site ceilings
DELETE /api/platform/sites/{slug}?confirm_slug=<slug>          archived only
POST   /api/platform/sites/{slug}/refresh-usage

GET    /api/platform/sites/{slug}/domains
POST   /api/platform/sites/{slug}/domains          { domain, make_primary }
POST   /api/platform/sites/{slug}/domains/{id}/verify
POST   /api/platform/sites/{slug}/domains/{id}/primary
DELETE /api/platform/sites/{slug}/domains/{id}

GET    /api/platform/users             ?q=   cross-site account directory
GET    /api/platform/events            site lifecycle audit
GET    /api/platform/isolation         which isolation layers are enforced

GET    /api/platform/my-sites          sites this account can switch into
POST   /api/platform/switch?tenant_slug=   re-points the current session
GET    /api/platform/sites/{slug}/members
POST   /api/platform/members           { user_email, tenant_slug, role }
DELETE /api/platform/members/{id}      also ends that user's sessions there
```

Site switching is audited twice: as `site.entered` in the target site's own
activity log, and as `session_switched` in `tenant_events`. A support engineer
entering a client's site is exactly the event that client will later ask about.

### Public API — what a static frontend calls

No session. Content reads are open unless the site turns on
`api.require_key`, in which case send `Authorization: Bearer <key>`.

```
GET  /api/v1/{site}/config                     identity, menus, blocks,
                                               announcements, analytics ids,
                                               cookie consent, content types
GET  /api/v1/{site}/menus/{menu}
GET  /api/v1/{site}/content/{type}             ?term=&taxonomy=&limit=&offset=&order=
GET  /api/v1/{site}/content/{type}/{slug}
GET  /api/v1/{site}/all                        ?since=  everything, for a build
GET  /api/v1/preview/{token}                   the draft, no-store + noindex
GET  /api/v1/{site}/sitemap.xml                also /sitemap-{name}.xml
GET  /api/v1/{site}/robots.txt
GET  /api/v1/{site}/redirect?path=/old         { match, toPath, statusCode }
GET  /api/v1/{site}/confirm/{token}            newsletter double opt-in
GET  /api/v1/{site}/unsubscribe/{token}        one click, no sign-in
GET  /api/v1/{site}/l/{code}                   short UTM link, 302 + click count

POST /api/public/{site}/forms/{form}           lead intake
POST /api/public/{site}/subscribe              newsletter sign-up (always opt-in)
POST /api/public/{site}/conversions            { kind, name, page }
POST /api/public/{site}/collect                page-view beacon (204, no body)
POST /api/public/{site}/consent                { purpose, granted, policy_version }
POST /api/v1/{site}/not-found                  { path, referrer }  logs a 404

GET  /p/{site}/{slug}                          block-builder page (rendered HTML)
GET  /media/{key}                              local media (MEDIA_STORAGE=local only)
```

Wire the beacon, the 404 reporter and the conversion calls into your frontend
and the analytics dashboard fills itself. Skip them and the CMS still works —
the KPIs that need traffic data report `null` rather than a misleading `0`.

### Page builder

Pages are stored as a validated list of typed JSON blocks — never as HTML.
`pagebuilder.py` cleans every block against a per-type field spec on write
(scheme-checked links, hex-only colours, no raw-HTML block) and escapes every
value again at render time, so page authors cannot introduce stored XSS.

Text supports a markdown subset rendered escape-first on the server:
`**bold**`, `*italic*`, `~~strike~~`, `` `code` ``, `[label](url)` (scheme
allow-listed; a `javascript:` link renders as its label), plus in rich-text
blocks `## headings`, `-`/`1.` lists, `>` quotes and `---` dividers. The
editor drawer has a toolbar (with ⌘B/⌘I) that inserts the syntax.
Editing writes the draft columns only; the public URL serves a snapshot taken
at publish, and each publish records a revision (newest 20 kept) that can be
restored into the draft. A `form` block renders one of the tenant's intake
forms inline — `page-form.js` posts it to the same intake endpoint with the
usual honeypot, fill-time and first-touch attribution handling. Published
pages get their own strict CSP (`img-src https:` so image blocks can point
anywhere, everything else locked to self) and a 60-second public cache TTL.

### The content model

A content type is a schema, not a table. `content_types.field_schema` describes
the extra fields items of that type carry, so "products need an SKU and a
price" is a row change, not a migration. Six types are provisioned per
workspace — page, post, product, case study, testimonial, FAQ — and you can add
more from the API.

Fields are typed (`text`, `textarea`, `richtext`, `number`, `boolean`, `date`,
`datetime`, `select`, `multiselect`, `url`, `email`, `media`, `reference`,
`json`) and coerced against the schema on write. Unknown keys are dropped
rather than rejected: removing a field from a type should not make every
existing item unsaveable.

Editing writes the live columns. The public API serves only
`published_snapshot`, a frozen copy taken at publish — so an editor can keep
working on a published page and nothing reaches the site until they publish
again. `/api/publishing/status` lists exactly those pages: live, but with newer
edits waiting.

A type with `route_prefix: null` (testimonials, FAQs) has no URL of its own. It
gets no sitemap entry and no redirect on rename, because it is pulled into
other pages rather than visited.

#### Rich text and embeds

`content_items.body` is the one place this platform stores markup, so it is
also the one place an XSS could enter. Everything goes through
`app/sanitize.py` on write — nh3, an allow-list cleaner, plus:

- `<iframe>` survives only when its `src` host is on `EMBED_ALLOWED_HOSTS`;
  a frame whose src is rejected is removed rather than left as a blank box.
- `style` is dropped entirely — layout is the frontend's job.
- `rel` stays author-controlled (SEO needs `nofollow` and `sponsored`) but is
  filtered to a token allow-list.
- `javascript:` and `data:` URLs are refused by the scheme allow-list.

Sanitizing on write, not on read, means a later template change cannot start
emitting raw stored HTML.

### Media pipeline

An upload is validated by magic bytes, not by the `Content-Type` the client
claims. Images are additionally decoded — a file Pillow cannot open is refused
whatever it calls itself — with the pixel count checked before the full decode
so a small "decompression bomb" cannot exhaust memory. A `.php` named
`image/jpeg` is refused on the extension mismatch, and an SVG containing
`<script>`, an event handler or an external reference is refused outright
rather than rewritten.

Every raster upload then produces WebP and AVIF derivatives at 320/640/1024/
1600/2400 px (never upscaled), plus an optimized fallback in a widely supported
format. EXIF is stripped — it carries GPS coordinates that have no business on
a public site. Keys are content-addressed, so every derivative is immutable and
served with a one-year `immutable` cache header; re-uploading identical bytes
returns the existing row instead of paying for a second copy.

`media_usage` is rebuilt from the content on every save rather than counted
incrementally, because a drifting count means a delete either blocks wrongly or
breaks a live page. Deleting a file that is still referenced returns 409 until
you pass `?force=true`.

### Background workers

Six loops, all in the process where `RUN_WORKERS=1`:

| Worker | Does |
| --- | --- |
| `scheduled_publish_worker` | Publishes content whose `scheduled_for` has passed, snapshots it, then fires one rebuild per tenant per batch |
| `campaign_worker` | Sends scheduled campaigns through the shared outbox |
| `build_worker` | Drains `build_runs` and `cdn_invalidations` with backoff |
| `health_worker` | Probes health checks whose interval has elapsed; alerts on the second consecutive failure, not the first |
| `retention_worker` | Applies each tenant's retention policies, plus housekeeping that keeps expired tokens and settled queue rows from accumulating |
| `backup_worker` | One `pg_dump` a day if none has succeeded in 24 hours — only when `BACKUP_S3_BUCKET` is set |

Each claims work with `FOR UPDATE SKIP LOCKED` or a conditional `UPDATE`, so
two replicas cannot double-publish or double-send. Nothing is held in process
memory: a restart mid-batch loses nothing.

### Receiving a webhook

Every request carries `x-crm-signature: sha256=<hex>` over `{timestamp}.{body}`,
plus `x-crm-idempotency-key` for deduplication. Verify before trusting the body:

```python
import hashlib, hmac

expected = "sha256=" + hmac.new(
    SECRET.encode(),
    f"{request.headers['x-crm-timestamp']}.{raw_body}".encode(),
    hashlib.sha256,
).hexdigest()

if not hmac.compare_digest(expected, request.headers["x-crm-signature"]):
    raise HTTPException(401, "bad signature")
```

The signing secret is shown once, at endpoint creation, and never listed again.

## Using it from WordPress

**Client-side** — enqueue `form-embed.js` and point it at the CRM. It captures
first-touch UTMs in `sessionStorage`, so a visitor who arrives on a campaign,
browses three pages and then converts still credits the campaign.

```php
add_action( 'wp_enqueue_scripts', function () {
    if ( ! is_page( 'contact' ) ) {
        return;
    }

    wp_enqueue_script(
        'crm-form',
        get_stylesheet_directory_uri() . '/assets/js/form-embed.js',
        [],
        '1.0.0',
        true
    );

    // Endpoint and slugs are public by design — the CRM validates server-side.
    wp_add_inline_script(
        'crm-form',
        sprintf(
            'window.CRM_ENDPOINT=%s;window.CRM_TENANT=%s;window.CRM_FORM=%s;',
            wp_json_encode( 'https://crm.example.com' ),
            wp_json_encode( 'acme' ),
            wp_json_encode( 'contact' )
        ),
        'before'
    );
} );
```

**Server-side** — if you'd rather keep the browser out of it, forward from a
form handler. Sanitise on the way in, verify the nonce in the caller:

```php
/**
 * Forward a validated contact submission to the CRM.
 *
 * @param array $fields Raw $_POST slice, already nonce-checked by the caller.
 * @return true|WP_Error
 */
function arc_forward_lead_to_crm( array $fields ) {
    $payload = [
        'full_name' => sanitize_text_field( $fields['full_name'] ?? '' ),
        'email'     => sanitize_email( $fields['email'] ?? '' ),
        'phone'     => sanitize_text_field( $fields['phone'] ?? '' ),
        'company'   => sanitize_text_field( $fields['company'] ?? '' ),
        'message'   => sanitize_textarea_field( $fields['message'] ?? '' ),
        'meta'      => [
            'source_page'  => esc_url_raw( wp_get_referer() ?: home_url() ),
            'utm_source'   => sanitize_text_field( $fields['utm_source'] ?? '' ),
            'utm_medium'   => sanitize_text_field( $fields['utm_medium'] ?? '' ),
            'utm_campaign' => sanitize_text_field( $fields['utm_campaign'] ?? '' ),
        ],
    ];

    $response = wp_remote_post(
        'https://crm.example.com/api/public/acme/forms/contact',
        [
            'timeout'  => 8,
            'headers'  => [ 'Content-Type' => 'application/json' ],
            'body'     => wp_json_encode( $payload ),
            'blocking' => true,
        ]
    );

    if ( is_wp_error( $response ) ) {
        // Don't lose the lead because the CRM blinked — queue and retry.
        error_log( 'CRM forward failed: ' . $response->get_error_message() );
        return $response;
    }

    $code = wp_remote_retrieve_response_code( $response );
    if ( $code < 200 || $code >= 300 ) {
        return new WP_Error( 'crm_rejected', 'CRM returned HTTP ' . $code );
    }

    return true;
}
```

Note the failure branch: a synchronous forward means a CRM outage costs you the
lead. In production, write the submission to your own table first, then forward
from a cron or Action Scheduler job.

## Security notes

Verified by test: CSRF rejection on writes without the header, viewer role
blocked from every mutation, cross-tenant read/update/delete returning nothing,
intake rate limit tripping on the fourth request in a used window, `https`-only
plus RFC1918 / loopback / link-local blocked on webhook and build-hook URLs,
unknown fields rejected by `extra="forbid"`, and `/.env` not reachable through
the SPA route.

Also verified for the platform layer: `<script>`, inline event handlers,
`javascript:` hrefs, `style` and non-allow-listed iframes stripped from rich
text; a double-extension upload, an active SVG, a non-image claiming
`image/png` and an oversized file all refused; a Contributor blocked from
publishing and from editing someone else's draft while still able to edit their
own; a TOTP code refused on replay inside its own window and a recovery code
refused on second use; API keys rejected when revoked, expired or missing a
scope; a redirect loop refused at creation; and a media file still referenced
by a page refused deletion without an explicit force.

Things to get right at deploy time:

- **`TRUST_PROXY_HOPS` must match your real proxy depth** (ALB = 1, CloudFront +
  ALB = 2). `client_ip()` counts back from the right-hand end of
  `X-Forwarded-For`; set it too high and a spoofed header defeats the rate
  limiter and poisons lead attribution.
- **Rate limiting is per process.** With `--workers 4` the effective limit is 4×,
  and across Fargate tasks it multiplies again. Move to ElastiCache or an AWS WAF
  rate-based rule before scaling out.
- **Set `RUN_WORKERS=0` on every replica but one.** The webhook loop runs inside
  the app process; `FOR UPDATE SKIP LOCKED` keeps two workers off the same row,
  but every replica polling is wasted database traffic. Better still, run the
  worker as its own task, or move the queue to SQS.
- **Set `TURNSTILE_SECRET` in staging and production.** Without it the only bot
  defence is the honeypot and the fill-time check, and your pipeline fills with
  garbage in week one. The captcha check fails closed if Cloudflare is
  unreachable.
- **Secrets come from Secrets Manager or SSM at task start**, never from a baked
  image or a plaintext task definition.
- Session cookies are `HttpOnly` + `SameSite=Lax`, and `Secure` when `ENV=production`
  — so production must terminate TLS or sessions silently stop working.
- bcrypt runs in a thread (`asyncio.to_thread`); calling it inline would block
  the event loop for ~250ms per sign-in and serialise every other request.
- Passwords over 72 bytes are rejected rather than truncated, since bcrypt
  ignores the tail and a long passphrase would otherwise be matched by its prefix.
- CORS is applied only to `/api/public`. A blanket `CORSMiddleware` would open
  the authenticated API to any listed origin.
- CSV export prefixes leading `=+-@` with an apostrophe, so a lead whose "name"
  is a spreadsheet formula can't execute in Excel. It does mean international
  phone numbers export as `'+971…`.
- The admin renders every value through `textContent`. There is no `innerHTML`
  on server data anywhere, so a lead named `<img onerror=…>` is inert. The one
  `innerHTML` call in `ui.js` is for the sidebar's inline SVG icons, whose
  markup is authored in `app.js` and never comes from the server.
- **Rich text is sanitized on write, in `app/sanitize.py`.** Any new code path
  that stores markup — a new block kind, an imported document — must go
  through `clean_html()`, or it becomes the one place that skipped the cleaner.
- **`EMBED_ALLOWED_HOSTS` is the embed allow-list.** Adding a host to it means
  trusting that host to frame content on your clients' sites; the default list
  is the usual video/maps/booking providers.
- **Media is served only for keys the database knows about.** A file dropped
  into `MEDIA_ROOT` by hand is not web-reachable, and `storage.py` refuses any
  key containing `..`.
- **The analytics beacon is aggregate-only.** It increments daily counters
  rather than writing a row per hit, and the visitor key is a salted daily
  hash — set `ANALYTICS_SALT`, and rotating it severs the link between old and
  new counts. `visitor_days` is pruned after 45 days.
- **Erasure defaults to anonymize, not delete.** Stripping identifiers while
  keeping the lead row means last quarter's revenue reporting does not
  silently change; consent records are kept either way, because they are the
  evidence the erasure was requested.
- Both CSV exports (leads and form submissions) prefix leading `=+-@` so a
  submitted value cannot execute as a formula in Excel or Sheets.
- Public endpoints answer identically for a valid and an unknown site slug, so
  they cannot be used to enumerate which clients are on the install. The one
  exception is deliberate: a *suspended* site answers 503, because pretending a
  client's site never existed would cost them their search rankings.
- **A domain claimed by another site returns 409 without naming it.** Saying
  which site holds it would leak the portfolio to whoever can add a domain.
- **`sites.manage` is the portfolio boundary.** Only Super Admin holds it by
  default, and `_SITE_ADMIN` (what Owner and Admin get) explicitly excludes it.
  Verified: a site Owner is refused on listing, creating, switching and every
  administrative action.
- **Revoking a cross-site membership ends that account's sessions on that
  site immediately** — otherwise revocation would take effect at next sign-in,
  which is not what anyone means by revoking access.
- **Check `/healthz` after deploying.** `isolation.rowLevelSecurity` tells you
  whether the database is enforcing tenancy or merely holding inert policies.
  Security that looks present and is not is worse than none, because you plan
  around it.

## Deploying on AWS

Fargate behind an ALB: one service for web tasks
(`uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 2`, `RUN_WORKERS=0`),
one single-task service for the worker (`RUN_WORKERS=1`), RDS PostgreSQL with
`PGSSL=require`, secrets from Secrets Manager.

**Media must go to S3.** `MEDIA_STORAGE=local` writes to the container
filesystem, which is ephemeral — uploads vanish when the task is replaced. Set
`MEDIA_STORAGE=s3`, `MEDIA_S3_BUCKET`, and `MEDIA_PUBLIC_BASE_URL` to a
CloudFront distribution so the bucket stays private and media is CDN-served.
Derivatives carry a content hash and a one-year `immutable` header, so CDN hit
rates are high and origin requests are rare.

**Cost shape.** RDS dominates; a `db.t4g.micro` carries a few hundred sites'
lead volume comfortably. On the media side, storage is negligible next to
request and egress cost — the expensive mistake is serving a 2 MB original
where a 40 KB WebP would do, which is what the derivative pipeline exists to
avoid. CloudFront invalidations are billed per path beyond the free tier, so
publishing invalidates the changed page plus the sitemap rather than `/*`.
Build hooks are debounced (default 60s) because most providers bill per build
minute and a bulk publish of 40 posts should rebuild once.

**Backups.** `pg_dump` from the app is a safety net, not the plan: prefer RDS
automated backups plus a snapshot schedule, since an in-process loop cannot
survive its task being replaced mid-dump. If you do use it, set
`BACKUP_S3_BUCKET` — a dump on the same disk as the database is not a backup —
and note the Dockerfile installs `postgresql-client` for `pg_dump`. For media,
S3 versioning with a replication rule beats copying objects through the app.

**Per-site scaling.** One busy client does not need a platform change: set
`infra.media_s3_bucket` and `infra.media_public_base_url` on that tenant to
give it its own bucket and distribution, raise its `limits`, and leave every
other site alone. Sites that outgrow shared Postgres are the case for moving
that tenant to its own database — the schema is identical, so it is a dump and
restore plus a `DATABASE_URL`, not a fork.

**Rollback.** The app is stateless, so redeploy the previous task definition.
The one-way doors are the three schema files. Both are written to be re-runnable
and additive (`IF NOT EXISTS` everywhere, `ADD COLUMN IF NOT EXISTS`,
`ALTER TYPE … ADD VALUE IF NOT EXISTS`), so rolling the app back does not meet
a column that no longer exists. The one thing a rollback cannot undo is an
enum value already written to a row: if you roll back past the
`super_admin`/`editor`/`author`/`contributor` additions, any user carrying one
of those roles will fail to load. Demote them first.

Risks worth naming before the first deploy: with `RUN_WORKERS=0` everywhere,
scheduled content never publishes and campaigns never send, silently — the
Operations screen says so, but nothing else will. And `TRUST_PROXY_HOPS` set
wrong makes every rate limit and every piece of lead attribution trust a
spoofable header.

## Notes for anyone extending this

`from __future__ import annotations` is deliberately absent from the router
modules and `security.py`. With postponed annotations, FastAPI hands pydantic
unresolved `ForwardRef`s for dependency classes like `LeadFilters` and raises
`PydanticUserError` at request time — it cost an afternoon here. Python 3.12
evaluates `X | None` natively, so nothing is lost by leaving it out.

The CSV export returns a buffered `Response`, not a `StreamingResponse`. Streaming
through `BaseHTTPMiddleware` (which the security-header middleware uses) produced
`RuntimeError: Response content longer than Content-Length`. At a 10k-row cap the
body is small enough that a correct `Content-Length` is worth more than
incremental delivery.

The platform routers *do* use `from __future__ import annotations`, which the
caveat above warns about. It is safe there because none of them take a
dependency *class* — every `Depends()` is a function or a dependency factory,
and Pydantic body models resolve normally. Add a class-based dependency to one
of those modules and you will need to drop the import from it.

**Drawers are a stack, not a slot.** `ui.openDrawer` pushes a layer and returns
a handle that closes *that* layer. The menu builder and the form field builder
open an editor on top of themselves; with a single slot the child destroyed the
parent and its unsaved state. `kit.formDrawer` closes its own handle rather
than "the top one", because `onSave` may itself have opened a drawer — the
shown-once API key and the 2FA recovery codes both do.

**`ADD COLUMN IF NOT EXISTS` and `ALTER TYPE ADD VALUE` interact badly with
`DO` blocks.** `ALTER TYPE … ADD VALUE` cannot share a transaction with a
statement that uses the new label, and a `DO` block is a transaction — so the
enum extensions in `db/platform.sql` sit at the top level, where psql runs each
statement in its own implicit transaction.

**Explicit `NULL` beats a column default.** `INSERT … VALUES (…, $8, $9)` with
`None` bound writes NULL and does *not* fall back to `DEFAULT`, which is how the
first version of `user_profiles` violated its own `NOT NULL` constraint on
`locale`. `coalesce($8, 'en')` in the insert is the fix.

**Unique constraints ignore NULL.** `UNIQUE (tenant_id, parent_id, slug)` treats
every root-level folder as distinct, because `NULL <> NULL`. `media_folders`
uses a unique index on `coalesce(parent_id, 0)` instead.

**RLS is invisible when it is not working.** Policies can be installed, forced
and completely inert, because superusers and `BYPASSRLS` roles are exempt and
`FORCE ROW LEVEL SECURITY` only reaches the table owner. `db.rls_status()`
probes the connecting role rather than assuming, and reports it on `/healthz`,
in the startup log and in Platform → Isolation. If you add a new tenant table,
re-run `db/tenancy.sql`: the policy block walks every table with a `tenant_id`
column, so it picks the new one up.

**`tenants.status` and `tenants.is_active` are kept in step by a trigger.**
`is_active` is what the original queries read and `status` carries the reason,
so a trigger derives each from the other in both directions. Writing either one
alone is safe; that is the point.

**A site's deletion audit has to outlive the site.** Every business table
cascades from `tenants`, `activity_log` included — so the record of a deletion
cannot live there. `tenant_events` stores `tenant_slug` as plain text beside a
nullable `tenant_id`, and a row whose `tenant_id` is NULL is one whose site is
gone.

## Not built yet

- **Email open and click tracking.** Campaigns send and record delivery, but
  there is no tracking pixel or link rewriting, so "opened" and "clicked" are
  not reported.
- **Bounce and complaint handling.** `subscribers.status` has `bounced` and
  `complained`, and nothing sets them yet — wiring an SES SNS notification
  topic into a webhook endpoint is the missing piece.
- **A visual rich-text editor.** The content editor is a sanitized HTML
  textarea. The sanitizer, the embed allow-list and the SEO indicators are all
  in place; what is missing is a WYSIWYG surface on top.
- **GA4 Data API import.** The dashboard reads first-party counters, not GA4.
  The ids are configured and the snippets generated, but nothing pulls GA4's
  own numbers back.
- **i18n and RTL**, which matters if any UAE client needs Arabic. `locale` is
  stored per workspace and per user and is exposed to the frontend; the admin
  itself is English-only.
- **Automated domain verification.** A domain is verified by an administrator
  asserting it, not by the platform checking a DNS TXT record — a container
  with no egress cannot prove one. Unverified domains are inert, so the gap is
  in convenience, not in safety.
- **Owner invitations.** Creating a site sets the owner's password directly and
  the operator hands it over. An emailed invite with a single-use link would be
  better, and the `password_resets` table already has the right shape for it.
- **Per-tenant database routing.** `tenants.infra` can point one site at its
  own bucket and CDN, but not at its own database — `db.py` holds a single
  pool. That is the next step for a site that outgrows shared Postgres.
- **Distributed rate limiting.** Still an in-process dict — see the caveat
  above. It is also per-process, not per-tenant, so one noisy site's public
  traffic shares a budget with the rest.
- **Field-level permissions.** Permissions are per action, not per field, so
  "an Author may edit the body but not the SEO block" is not expressible.
