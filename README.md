# CRM Admin — FastAPI

A multi-tenant lead CRM with a WordPress-shaped admin panel. FastAPI +
asyncpg + PostgreSQL on the back, vanilla HTML/CSS/JS on the front — no build
step, no framework, no jQuery.

Built for the case where one control plane serves many client sites: every row
carries a `tenant_id`, every query is scoped by it, and a new client is a row in
`tenants` rather than another server to patch.

## Quick start

Three ways in, pick one.

**Fastest — one script** (needs Python 3.11+ and a running PostgreSQL):

```bash
./setup.sh          # Linux / macOS
.\setup.ps1         # Windows PowerShell
```

It creates the venv, installs, applies the schema, seeds a workspace and starts
the server. If the database isn't reachable it prints the two `CREATE` commands
you need and stops. Re-run any time; `./setup.sh --start` skips setup.

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
OWNER_EMAIL=you@example.com OWNER_PASSWORD='at-least-12-chars' python -m db.seed

uvicorn app.main:app --reload            # http://localhost:8000
```

Sign in at `/login` with the workspace slug (`demo` by default), your email and
password. A working public form lives at `/form-example.html`. Interactive API
docs are at `/api/docs` in development and disabled in production.

## What's in the box

| Area | Covered |
| --- | --- |
| Pipeline | Lead records with status (New → Contacted → Qualified → Proposal → Won → Lost), owner, follow-up date, deal value, notes |
| Attribution | Landing page, referrer and the full UTM set captured at submission, first-touch persisted per session |
| List management | Search, status tabs with counts, owner/date filters, sortable columns, pagination, bulk status/assign/spam/delete, CSV export |
| Intake | Public JSON endpoint per form, honeypot + fill-time check, per-IP rate limit, Turnstile hook, spam quarantine |
| Outbound | Webhook endpoints with HMAC-SHA256 signing, idempotency keys, exponential backoff, delivery log |
| Access | Four roles (owner/admin/agent/viewer), session auth, account lockout, full audit trail |
| Dashboard | Volume ledger, KPIs, traffic sources, follow-ups due, latest leads |
| Pages | WordPress-style page builder: typed content blocks (hero, rich text, image, features, quote, lead form, …), markdown-subset formatting with an editor toolbar, per-page theme, draft → publish workflow with revision history, live pages served at `/p/{tenant}/{slug}` |
| Sign-up | Self-service registration at `/register` — creates a fresh workspace with the registrant as owner and a starter contact form (`ALLOW_SIGNUPS=0` for invite-only installs) |

## Layout

```
app/
  main.py            app wiring, lifespan, middleware, error shape, static SPA
  config.py          settings from the environment
  db.py              asyncpg pool, type codecs, TenantDB scoping
  security.py        sessions, bcrypt, CSRF, role dependencies
  ratelimit.py       fixed-window limiter (in-process — see caveats)
  events.py          activity log + webhook queue and worker
  schemas.py         pydantic request models
  pagebuilder.py     page block validation + escaped server-side rendering
  routers/
    auth.py          sign in / out / me
    leads.py         list, detail, update, bulk, notes, CSV
    admin.py         dashboard, users, webhooks, activity, settings
    intake.py        public form submission (unauthenticated)
    pages.py         page builder API + published pages (/p/{tenant}/{slug})
db/
  schema.sql         tables, enums, indexes, triggers
  seed.py            tenant + owner + default form + samples
public/              the admin SPA, unchanged from the Node build
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

### Multi-tenancy

`TenantDB` binds `$1` to the tenant id so a call site cannot write a query
without the scope. Verified: signing in to tenant B and requesting tenant A's
lead by id returns 404, patching it returns 404, and a bulk delete of A's ids
from B's session affects zero rows.

For harder isolation, the next step is PostgreSQL row-level security with
`SET LOCAL app.tenant_id` per transaction. The schema already fits it.

## API

Admin routes need a session cookie; writes also need `X-CSRF-Token` (returned by
`/api/auth/me` and `/api/auth/login`). Errors are `{"error": "..."}` throughout —
FastAPI's `{"detail": ...}` shape is remapped in `main.py`, including for
validation failures, so the SPA reads one field.

```
POST   /api/auth/login          { tenant, email, password }
POST   /api/auth/register       { workspace, display_name, email, password }  new tenant + owner
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

POST   /api/public/{tenant}/forms/{form}   unauthenticated intake
GET    /p/{tenant}/{slug}                  published page (unauthenticated HTML)
```

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
plus RFC1918 / loopback / link-local blocked on webhook URLs, unknown fields
rejected by `extra="forbid"`, and `/.env` not reachable through the SPA route.

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
  on server data anywhere, so a lead named `<img onerror=…>` is inert.

## Deploying on AWS

Fargate behind an ALB: one service for web tasks
(`uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 2`, `RUN_WORKERS=0`),
one single-task service for the worker (`RUN_WORKERS=1`), RDS PostgreSQL with
`PGSSL=require`, secrets from Secrets Manager. Cost sits mostly in RDS — a
`db.t4g.micro` carries a few hundred sites' lead volume comfortably.

Rollback: the app is stateless, so redeploy the previous task definition. The
one-way door is `db/schema.sql` — keep migrations additive (add nullable
columns, backfill, then switch reads) so a rollback doesn't meet a column that
no longer exists.

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

## Not built yet

- Newsletter and campaign sending (subscribers, SES adapter, open/click tracking)
- GA4/GTM wiring and the conversion event map
- Form builder UI — `forms.fields` is already JSONB and intake validates against
  it, so this is an admin screen, not a schema change
- Revision history and draft preview for content
- i18n and RTL, which matters if any UAE client needs Arabic
- Provisioning flow for new client sites (currently `db/seed.py`)
- GDPR consent logging and data-subject export/delete
- In-app notification centre
- Transactional email templates and autoresponder
