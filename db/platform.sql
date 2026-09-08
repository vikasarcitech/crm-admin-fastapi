-- =====================================================================
-- CRM Admin — platform schema (content, SEO, media, marketing, ops)
--
-- Companion to db/schema.sql, which holds the original core (tenants,
-- users, sessions, leads, forms, pages). Kept in a second file so each
-- stays readable; both are idempotent and applied in order:
--
--   psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f db/schema.sql
--   psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f db/platform.sql
--
-- Every business table carries tenant_id and is queried through
-- TenantDB (app/db.py), so a missing WHERE cannot leak across sites.
-- =====================================================================

SET client_min_messages = warning;

-- --------------------------------------------------------------- enums
-- ALTER TYPE ... ADD VALUE must stay at top level: inside a DO block it
-- shares a transaction with the statements that use the new label.
ALTER TYPE user_role ADD VALUE IF NOT EXISTS 'super_admin';
ALTER TYPE user_role ADD VALUE IF NOT EXISTS 'editor';
ALTER TYPE user_role ADD VALUE IF NOT EXISTS 'author';
ALTER TYPE user_role ADD VALUE IF NOT EXISTS 'contributor';

DO $$ BEGIN
  CREATE TYPE content_status AS ENUM ('draft', 'published', 'scheduled', 'trashed');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
  CREATE TYPE subscriber_status AS ENUM
    ('pending', 'subscribed', 'unsubscribed', 'bounced', 'complained');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
  CREATE TYPE campaign_status AS ENUM
    ('draft', 'scheduled', 'sending', 'sent', 'failed', 'cancelled');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
  CREATE TYPE job_status AS ENUM ('pending', 'running', 'complete', 'failed');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
  CREATE TYPE request_kind AS ENUM ('export', 'deletion', 'rectification');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- =====================================================================
-- 2.1  CONTENT MANAGEMENT
-- ---------------------------------------------------------------------
-- A content type is a schema, not a table: `field_schema` describes the
-- extra fields an item of that type carries, so adding "products" or
-- "case studies" never needs a migration. Items keep their draft in the
-- live columns and what the public API serves in published_snapshot, so
-- an in-progress edit can never leak (same split as pages).
-- =====================================================================

CREATE TABLE IF NOT EXISTS content_types (
  id           BIGSERIAL PRIMARY KEY,
  tenant_id    BIGINT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  slug         CITEXT NOT NULL,                 -- 'post', 'case-study'
  name         TEXT   NOT NULL,                 -- 'Post'
  plural_name  TEXT   NOT NULL,                 -- 'Posts'
  description  TEXT,
  -- 'collection' = many items (posts); 'single' = exactly one (homepage).
  kind         TEXT   NOT NULL DEFAULT 'collection'
               CHECK (kind IN ('collection', 'single')),
  route_prefix TEXT,                            -- '/blog' — used to build URLs
  field_schema JSONB  NOT NULL DEFAULT '[]'::jsonb,
  -- {"seo":true,"revisions":true,"taxonomies":["category","tag"],"body":true}
  supports     JSONB  NOT NULL DEFAULT '{}'::jsonb,
  icon         TEXT,
  sort_order   INT    NOT NULL DEFAULT 0,
  is_builtin   BOOLEAN NOT NULL DEFAULT FALSE,  -- builtins cannot be deleted
  is_active    BOOLEAN NOT NULL DEFAULT TRUE,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, slug)
);

CREATE TABLE IF NOT EXISTS content_items (
  id            BIGSERIAL PRIMARY KEY,
  tenant_id     BIGINT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  type_id       BIGINT NOT NULL REFERENCES content_types(id) ON DELETE CASCADE,

  slug          CITEXT NOT NULL,
  title         TEXT   NOT NULL,
  excerpt       TEXT,
  body          TEXT,                                  -- sanitized HTML
  fields        JSONB  NOT NULL DEFAULT '{}'::jsonb,   -- per type_schema
  -- meta_title, meta_description, og_*, twitter_*, canonical, noindex,
  -- nofollow, focus_keyword, schema_org
  seo           JSONB  NOT NULL DEFAULT '{}'::jsonb,

  status        content_status NOT NULL DEFAULT 'draft',
  author_id     BIGINT REFERENCES users(id) ON DELETE SET NULL,
  featured_media_id BIGINT,                             -- FK added after media
  menu_order    INT    NOT NULL DEFAULT 0,

  scheduled_for TIMESTAMPTZ,       -- status 'scheduled' until the worker fires
  published_at  TIMESTAMPTZ,
  trashed_at    TIMESTAMPTZ,       -- soft delete; purged on explicit request

  -- Frozen copy served by the public content API.
  published_snapshot JSONB,

  created_by    BIGINT REFERENCES users(id) ON DELETE SET NULL,
  updated_by    BIGINT REFERENCES users(id) ON DELETE SET NULL,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, type_id, slug)
);

CREATE INDEX IF NOT EXISTS content_items_tenant_idx
  ON content_items (tenant_id, type_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS content_items_status_idx
  ON content_items (tenant_id, status);
CREATE INDEX IF NOT EXISTS content_items_author_idx
  ON content_items (tenant_id, author_id);
-- The scheduled-publish worker scans only this slice.
CREATE INDEX IF NOT EXISTS content_items_due_idx
  ON content_items (scheduled_for)
  WHERE status = 'scheduled';
CREATE INDEX IF NOT EXISTS content_items_search_idx ON content_items
  USING GIN (to_tsvector('simple',
    coalesce(title,'') || ' ' || coalesce(excerpt,'') || ' ' || coalesce(body,'')));

-- Snapshot on every save and publish; the newest 30 per item are kept.
CREATE TABLE IF NOT EXISTS content_revisions (
  id         BIGSERIAL PRIMARY KEY,
  tenant_id  BIGINT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  item_id    BIGINT NOT NULL REFERENCES content_items(id) ON DELETE CASCADE,
  title      TEXT  NOT NULL,
  excerpt    TEXT,
  body       TEXT,
  fields     JSONB NOT NULL DEFAULT '{}'::jsonb,
  seo        JSONB NOT NULL DEFAULT '{}'::jsonb,
  reason     TEXT,                    -- 'save', 'publish', 'restore'
  created_by BIGINT REFERENCES users(id) ON DELETE SET NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS content_revisions_item_idx
  ON content_revisions (item_id, created_at DESC);

-- Draft preview links: an unguessable token so a client can see an
-- unpublished item without an admin account.
CREATE TABLE IF NOT EXISTS preview_tokens (
  id         BIGSERIAL PRIMARY KEY,
  tenant_id  BIGINT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  item_id    BIGINT NOT NULL REFERENCES content_items(id) ON DELETE CASCADE,
  token_hash TEXT NOT NULL UNIQUE,
  expires_at TIMESTAMPTZ NOT NULL,
  created_by BIGINT REFERENCES users(id) ON DELETE SET NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS preview_tokens_expiry_idx ON preview_tokens (expires_at);

-- ----------------------------------------------------------- taxonomies
CREATE TABLE IF NOT EXISTS taxonomies (
  id             BIGSERIAL PRIMARY KEY,
  tenant_id      BIGINT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  slug           CITEXT NOT NULL,                -- 'category', 'tag'
  name           TEXT   NOT NULL,
  plural_name    TEXT   NOT NULL,
  is_hierarchical BOOLEAN NOT NULL DEFAULT FALSE, -- categories nest, tags don't
  is_builtin     BOOLEAN NOT NULL DEFAULT FALSE,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, slug)
);

CREATE TABLE IF NOT EXISTS terms (
  id          BIGSERIAL PRIMARY KEY,
  tenant_id   BIGINT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  taxonomy_id BIGINT NOT NULL REFERENCES taxonomies(id) ON DELETE CASCADE,
  parent_id   BIGINT REFERENCES terms(id) ON DELETE SET NULL,
  slug        CITEXT NOT NULL,
  name        TEXT   NOT NULL,
  description TEXT,
  seo         JSONB  NOT NULL DEFAULT '{}'::jsonb,
  sort_order  INT    NOT NULL DEFAULT 0,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, taxonomy_id, slug)
);
CREATE INDEX IF NOT EXISTS terms_taxonomy_idx ON terms (tenant_id, taxonomy_id);

-- Which content types a taxonomy applies to.
CREATE TABLE IF NOT EXISTS taxonomy_types (
  tenant_id   BIGINT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  taxonomy_id BIGINT NOT NULL REFERENCES taxonomies(id) ON DELETE CASCADE,
  type_id     BIGINT NOT NULL REFERENCES content_types(id) ON DELETE CASCADE,
  PRIMARY KEY (taxonomy_id, type_id)
);

CREATE TABLE IF NOT EXISTS content_terms (
  tenant_id BIGINT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  item_id   BIGINT NOT NULL REFERENCES content_items(id) ON DELETE CASCADE,
  term_id   BIGINT NOT NULL REFERENCES terms(id) ON DELETE CASCADE,
  PRIMARY KEY (item_id, term_id)
);
CREATE INDEX IF NOT EXISTS content_terms_term_idx ON content_terms (term_id);

-- =====================================================================
-- 2.2  SEO & SITE DISCOVERY
-- =====================================================================

-- Manual and automatic 301/302s. A slug change writes one of these
-- automatically so old inbound links and search rankings survive.
CREATE TABLE IF NOT EXISTS redirects (
  id           BIGSERIAL PRIMARY KEY,
  tenant_id    BIGINT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  from_path    TEXT NOT NULL,                 -- '/old-page', normalised
  to_path      TEXT NOT NULL,                 -- path or absolute URL
  status_code  INT  NOT NULL DEFAULT 301 CHECK (status_code IN (301, 302, 307, 308)),
  is_active    BOOLEAN NOT NULL DEFAULT TRUE,
  is_automatic BOOLEAN NOT NULL DEFAULT FALSE, -- created by a slug change
  note         TEXT,
  hits         BIGINT NOT NULL DEFAULT 0,
  last_hit_at  TIMESTAMPTZ,
  created_by   BIGINT REFERENCES users(id) ON DELETE SET NULL,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, from_path)
);
CREATE INDEX IF NOT EXISTS redirects_active_idx ON redirects (tenant_id, is_active);

-- One row per distinct missing path, with a hit counter — a raw log of
-- every 404 would be mostly bot noise and would grow without bound.
CREATE TABLE IF NOT EXISTS not_found_log (
  id            BIGSERIAL PRIMARY KEY,
  tenant_id     BIGINT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  path          TEXT NOT NULL,
  hits          BIGINT NOT NULL DEFAULT 1,
  last_referrer TEXT,
  last_user_agent TEXT,
  is_ignored    BOOLEAN NOT NULL DEFAULT FALSE,
  resolved_redirect_id BIGINT REFERENCES redirects(id) ON DELETE SET NULL,
  first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_seen_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, path)
);
CREATE INDEX IF NOT EXISTS not_found_hits_idx
  ON not_found_log (tenant_id, hits DESC)
  WHERE NOT is_ignored AND resolved_redirect_id IS NULL;

-- Cached sitemap, regenerated on publish rather than on every request.
CREATE TABLE IF NOT EXISTS sitemap_cache (
  tenant_id    BIGINT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  name         TEXT NOT NULL,                -- 'index', 'post', 'page'
  xml          TEXT NOT NULL,
  url_count    INT  NOT NULL DEFAULT 0,
  generated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, name)
);

-- =====================================================================
-- 2.3  MEDIA MANAGEMENT
-- =====================================================================

CREATE TABLE IF NOT EXISTS media_folders (
  id         BIGSERIAL PRIMARY KEY,
  tenant_id  BIGINT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  parent_id  BIGINT REFERENCES media_folders(id) ON DELETE CASCADE,
  name       TEXT   NOT NULL,
  slug       CITEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- coalesce(): a plain UNIQUE would treat every root folder as distinct,
-- because NULL <> NULL in a unique constraint.
CREATE UNIQUE INDEX IF NOT EXISTS media_folders_slug_idx
  ON media_folders (tenant_id, coalesce(parent_id, 0), slug);

CREATE TABLE IF NOT EXISTS media (
  id           BIGSERIAL PRIMARY KEY,
  tenant_id    BIGINT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  folder_id    BIGINT REFERENCES media_folders(id) ON DELETE SET NULL,

  storage_key  TEXT NOT NULL,          -- 'tenant/3/2026/09/hero-a1b2c3.webp'
  filename     TEXT NOT NULL,
  original_filename TEXT NOT NULL,
  mime_type    TEXT NOT NULL,
  byte_size    BIGINT NOT NULL,
  checksum     TEXT,                   -- sha256, for duplicate detection
  width        INT,
  height       INT,

  alt_text     TEXT,
  title        TEXT,
  caption      TEXT,
  tags         TEXT[] NOT NULL DEFAULT '{}',

  -- [{"label":"md","key":"...","width":800,"mime":"image/webp","bytes":41233}]
  variants     JSONB NOT NULL DEFAULT '[]'::jsonb,

  uploaded_by  BIGINT REFERENCES users(id) ON DELETE SET NULL,
  deleted_at   TIMESTAMPTZ,            -- trash; purge is a separate step
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, storage_key)
);
CREATE INDEX IF NOT EXISTS media_tenant_idx ON media (tenant_id, created_at DESC)
  WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS media_folder_idx ON media (tenant_id, folder_id);
CREATE INDEX IF NOT EXISTS media_checksum_idx ON media (tenant_id, checksum);
CREATE INDEX IF NOT EXISTS media_search_idx ON media
  USING GIN (to_tsvector('simple',
    coalesce(original_filename,'') || ' ' || coalesce(alt_text,'') || ' ' ||
    coalesce(title,'') || ' ' || coalesce(caption,'')));

-- Answers "is this file still used?" before a delete is allowed.
CREATE TABLE IF NOT EXISTS media_usage (
  tenant_id   BIGINT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  media_id    BIGINT NOT NULL REFERENCES media(id) ON DELETE CASCADE,
  object_type TEXT   NOT NULL,          -- 'content_item', 'block', 'campaign'
  object_id   BIGINT NOT NULL,
  field       TEXT   NOT NULL DEFAULT 'body',
  PRIMARY KEY (media_id, object_type, object_id, field)
);
CREATE INDEX IF NOT EXISTS media_usage_object_idx
  ON media_usage (tenant_id, object_type, object_id);

-- Deferred FK: content_items is created before media above.
DO $$ BEGIN
  ALTER TABLE content_items
    ADD CONSTRAINT content_items_featured_media_fkey
    FOREIGN KEY (featured_media_id) REFERENCES media(id) ON DELETE SET NULL;
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- =====================================================================
-- 2.4  USERS, ROLES & SECURITY
-- =====================================================================

-- Role → permission defaults live in app/permissions.py; rows here are
-- per-tenant overrides on top of them, which is what makes permissions
-- "fine-grained" without a role explosion.
CREATE TABLE IF NOT EXISTS role_permissions (
  tenant_id  BIGINT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  role       user_role NOT NULL,
  permission TEXT NOT NULL,
  allowed    BOOLEAN NOT NULL,
  updated_by BIGINT REFERENCES users(id) ON DELETE SET NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, role, permission)
);

-- Site-level access. users.tenant_id stays the home workspace; a row
-- here grants one user access to another site with its own role, which
-- is how an agency operator works across a portfolio.
CREATE TABLE IF NOT EXISTS tenant_memberships (
  id         BIGSERIAL PRIMARY KEY,
  tenant_id  BIGINT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  user_id    BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  role       user_role NOT NULL DEFAULT 'editor',
  granted_by BIGINT REFERENCES users(id) ON DELETE SET NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, user_id)
);
CREATE INDEX IF NOT EXISTS memberships_user_idx ON tenant_memberships (user_id);

CREATE TABLE IF NOT EXISTS user_profiles (
  user_id         BIGINT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
  tenant_id       BIGINT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  avatar_media_id BIGINT REFERENCES media(id) ON DELETE SET NULL,
  bio             TEXT,
  job_title       TEXT,
  phone           TEXT,
  -- {"x":"https://x.com/…","linkedin":"…","github":"…","website":"…"}
  social          JSONB NOT NULL DEFAULT '{}'::jsonb,
  locale          TEXT NOT NULL DEFAULT 'en',
  timezone        TEXT NOT NULL DEFAULT 'UTC',
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Optional TOTP second factor. The shared secret is stored so codes can
-- be verified; recovery codes are stored only as SHA-256, like sessions.
CREATE TABLE IF NOT EXISTS user_totp (
  user_id        BIGINT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
  tenant_id      BIGINT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  secret         TEXT NOT NULL,
  confirmed_at   TIMESTAMPTZ,          -- NULL until the first code verifies
  recovery_hashes TEXT[] NOT NULL DEFAULT '{}',
  last_used_step BIGINT,               -- blocks replay of the same code
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Half-finished logins that still owe a 2FA code. Short-lived and
-- separate from sessions, so a pending factor is never a live session.
CREATE TABLE IF NOT EXISTS totp_challenges (
  id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  token_hash TEXT NOT NULL UNIQUE,
  user_id    BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  tenant_id  BIGINT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  attempts   INT NOT NULL DEFAULT 0,
  expires_at TIMESTAMPTZ NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS totp_challenges_expiry_idx ON totp_challenges (expires_at);

-- Session list in the UI wants "last seen", which the original schema
-- did not track.
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS last_seen_at TIMESTAMPTZ;

-- =====================================================================
-- 2.5  SITE STRUCTURE & GLOBAL SETTINGS
-- =====================================================================

CREATE TABLE IF NOT EXISTS menus (
  id         BIGSERIAL PRIMARY KEY,
  tenant_id  BIGINT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  slug       CITEXT NOT NULL,
  name       TEXT   NOT NULL,
  location   TEXT,                     -- 'header', 'footer', 'mobile'
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, slug)
);

CREATE TABLE IF NOT EXISTS menu_items (
  id         BIGSERIAL PRIMARY KEY,
  tenant_id  BIGINT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  menu_id    BIGINT NOT NULL REFERENCES menus(id) ON DELETE CASCADE,
  parent_id  BIGINT REFERENCES menu_items(id) ON DELETE CASCADE,
  label      TEXT NOT NULL,
  -- 'custom' uses url; 'content'/'term' resolve object_id to a live URL
  -- at read time, so a slug change does not strand the menu entry.
  link_type  TEXT NOT NULL DEFAULT 'custom'
             CHECK (link_type IN ('custom', 'content', 'term', 'page')),
  url        TEXT,
  object_id  BIGINT,
  target     TEXT CHECK (target IS NULL OR target IN ('_self', '_blank')),
  rel        TEXT,
  icon       TEXT,
  sort_order INT NOT NULL DEFAULT 0,
  is_active  BOOLEAN NOT NULL DEFAULT TRUE
);
CREATE INDEX IF NOT EXISTS menu_items_menu_idx
  ON menu_items (menu_id, coalesce(parent_id, 0), sort_order);

-- Reusable blocks / widgets: CTAs, banners, footer columns, promos.
CREATE TABLE IF NOT EXISTS reusable_blocks (
  id         BIGSERIAL PRIMARY KEY,
  tenant_id  BIGINT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  slug       CITEXT NOT NULL,
  name       TEXT   NOT NULL,
  kind       TEXT   NOT NULL DEFAULT 'html'
             CHECK (kind IN ('html', 'cta', 'banner', 'footer', 'promo', 'custom')),
  content    JSONB  NOT NULL DEFAULT '{}'::jsonb,
  is_active  BOOLEAN NOT NULL DEFAULT TRUE,
  updated_by BIGINT REFERENCES users(id) ON DELETE SET NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, slug)
);

-- =====================================================================
-- 2.7  FORMS & CONVERSION
-- ---------------------------------------------------------------------
-- forms already exists in db/schema.sql; these columns turn it into an
-- admin-managed builder rather than a fixed contact form.
-- =====================================================================

-- {"success_message":"…","redirect_url":"…","honeypot":true,"captcha":"turnstile",
--  "min_fill_ms":2500,"store_submission":true}
ALTER TABLE forms ADD COLUMN IF NOT EXISTS settings JSONB NOT NULL DEFAULT '{}'::jsonb;
-- Maps form field names onto lead columns: {"your_email":"email"}
ALTER TABLE forms ADD COLUMN IF NOT EXISTS lead_mapping JSONB NOT NULL DEFAULT '{}'::jsonb;
-- [{"to":["sales@…"],"when":{"field":"budget","op":"gt","value":"10000"}}]
ALTER TABLE forms ADD COLUMN IF NOT EXISTS notification_rules JSONB NOT NULL DEFAULT '[]'::jsonb;
-- {"enabled":true,"template_slug":"thanks-for-enquiry"}
ALTER TABLE forms ADD COLUMN IF NOT EXISTS autoresponder JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE forms ADD COLUMN IF NOT EXISTS submit_count BIGINT NOT NULL DEFAULT 0;
ALTER TABLE forms ADD COLUMN IF NOT EXISTS spam_count BIGINT NOT NULL DEFAULT 0;
ALTER TABLE forms ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now();

-- Raw submission log, kept beside the lead it produced. Spam rows have
-- no lead, which is how the pipeline stays clean while the log stays
-- complete enough to tune the filters.
CREATE TABLE IF NOT EXISTS form_submissions (
  id          BIGSERIAL PRIMARY KEY,
  tenant_id   BIGINT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  form_id     BIGINT REFERENCES forms(id) ON DELETE CASCADE,
  lead_id     BIGINT REFERENCES leads(id) ON DELETE SET NULL,
  payload     JSONB NOT NULL DEFAULT '{}'::jsonb,
  is_spam     BOOLEAN NOT NULL DEFAULT FALSE,
  spam_reason TEXT,
  ip          INET,
  user_agent  TEXT,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS form_submissions_idx
  ON form_submissions (tenant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS form_submissions_form_idx
  ON form_submissions (tenant_id, form_id, created_at DESC);

-- Transactional templates. Bodies are stored with {{placeholders}} and
-- rendered by app/templating.py, which escapes every substituted value.
CREATE TABLE IF NOT EXISTS email_templates (
  id         BIGSERIAL PRIMARY KEY,
  tenant_id  BIGINT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  slug       CITEXT NOT NULL,
  name       TEXT   NOT NULL,
  subject    TEXT   NOT NULL,
  body_text  TEXT   NOT NULL,
  body_html  TEXT,
  kind       TEXT   NOT NULL DEFAULT 'transactional'
             CHECK (kind IN ('transactional', 'autoresponder', 'notification', 'campaign')),
  is_active  BOOLEAN NOT NULL DEFAULT TRUE,
  updated_by BIGINT REFERENCES users(id) ON DELETE SET NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, slug)
);

-- Conversion events from the public site: form submits, CTA clicks and
-- phone/email/WhatsApp taps, so conversion rate is measured rather than
-- inferred from lead count alone.
CREATE TABLE IF NOT EXISTS conversion_events (
  id          BIGSERIAL PRIMARY KEY,
  tenant_id   BIGINT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  kind        TEXT NOT NULL CHECK (kind IN
              ('form', 'cta', 'phone', 'email', 'whatsapp', 'download', 'custom')),
  name        TEXT NOT NULL,
  label       TEXT,
  source_page TEXT,
  referrer    TEXT,
  lead_id     BIGINT REFERENCES leads(id) ON DELETE SET NULL,
  visitor_key TEXT,                     -- rotating hash, never an IP
  value_amount NUMERIC(14,2),
  utm_source  TEXT,
  utm_medium  TEXT,
  utm_campaign TEXT,
  meta        JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS conversion_events_idx
  ON conversion_events (tenant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS conversion_events_kind_idx
  ON conversion_events (tenant_id, kind, created_at DESC);

-- =====================================================================
-- 2.8  MARKETING & NEWSLETTER
-- =====================================================================

CREATE TABLE IF NOT EXISTS subscribers (
  id            BIGSERIAL PRIMARY KEY,
  tenant_id     BIGINT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  email         CITEXT NOT NULL,
  name          TEXT,
  status        subscriber_status NOT NULL DEFAULT 'pending',
  source        TEXT,                          -- 'footer-form', 'import'
  tags          TEXT[] NOT NULL DEFAULT '{}',
  -- Double opt-in token (hash only) and the permanent unsubscribe token.
  confirm_hash  TEXT,
  unsubscribe_token TEXT NOT NULL DEFAULT encode(gen_random_bytes(16), 'hex'),
  confirmed_at  TIMESTAMPTZ,
  unsubscribed_at TIMESTAMPTZ,
  meta          JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, email)
);
CREATE INDEX IF NOT EXISTS subscribers_status_idx ON subscribers (tenant_id, status);
CREATE INDEX IF NOT EXISTS subscribers_token_idx ON subscribers (unsubscribe_token);

CREATE TABLE IF NOT EXISTS campaigns (
  id            BIGSERIAL PRIMARY KEY,
  tenant_id     BIGINT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  name          TEXT NOT NULL,
  subject       TEXT NOT NULL,
  preheader     TEXT,
  body_text     TEXT NOT NULL,
  body_html     TEXT,
  from_name     TEXT,
  from_email    CITEXT,
  -- {"status":"subscribed","tags":["customers"],"exclude_tags":[]}
  audience      JSONB NOT NULL DEFAULT '{}'::jsonb,
  status        campaign_status NOT NULL DEFAULT 'draft',
  scheduled_for TIMESTAMPTZ,
  sent_at       TIMESTAMPTZ,
  -- {"recipients":420,"delivered":417,"failed":3}
  stats         JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_by    BIGINT REFERENCES users(id) ON DELETE SET NULL,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS campaigns_due_idx ON campaigns (scheduled_for)
  WHERE status = 'scheduled';

CREATE TABLE IF NOT EXISTS campaign_recipients (
  id            BIGSERIAL PRIMARY KEY,
  tenant_id     BIGINT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  campaign_id   BIGINT NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
  subscriber_id BIGINT NOT NULL REFERENCES subscribers(id) ON DELETE CASCADE,
  status        delivery_status NOT NULL DEFAULT 'pending',
  error         TEXT,
  sent_at       TIMESTAMPTZ,
  UNIQUE (campaign_id, subscriber_id)
);

-- Announcement bars, popups, banners and scheduled promotions. The
-- public config API returns only what is live right now.
CREATE TABLE IF NOT EXISTS announcements (
  id         BIGSERIAL PRIMARY KEY,
  tenant_id  BIGINT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  name       TEXT NOT NULL,
  kind       TEXT NOT NULL DEFAULT 'bar'
             CHECK (kind IN ('bar', 'popup', 'banner', 'slide-in')),
  -- {"heading":"…","body":"…","cta_label":"…","cta_href":"…","dismissible":true}
  content    JSONB NOT NULL DEFAULT '{}'::jsonb,
  -- {"paths":["/","/pricing"],"exclude_paths":[],"delay_ms":3000,"frequency":"session"}
  placement  JSONB NOT NULL DEFAULT '{}'::jsonb,
  priority   INT NOT NULL DEFAULT 0,
  starts_at  TIMESTAMPTZ,
  ends_at    TIMESTAMPTZ,
  is_active  BOOLEAN NOT NULL DEFAULT TRUE,
  created_by BIGINT REFERENCES users(id) ON DELETE SET NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS announcements_live_idx
  ON announcements (tenant_id, priority DESC) WHERE is_active;

-- Campaign URL builder. short_code makes them shareable and countable.
CREATE TABLE IF NOT EXISTS utm_links (
  id           BIGSERIAL PRIMARY KEY,
  tenant_id    BIGINT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  name         TEXT NOT NULL,
  base_url     TEXT NOT NULL,
  utm_source   TEXT NOT NULL,
  utm_medium   TEXT NOT NULL,
  utm_campaign TEXT NOT NULL,
  utm_term     TEXT,
  utm_content  TEXT,
  short_code   TEXT NOT NULL UNIQUE,
  clicks       BIGINT NOT NULL DEFAULT 0,
  last_click_at TIMESTAMPTZ,
  created_by   BIGINT REFERENCES users(id) ON DELETE SET NULL,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- =====================================================================
-- 2.9  ANALYTICS
-- ---------------------------------------------------------------------
-- First-party and aggregate-only: the beacon increments daily counters
-- instead of writing a row per hit, so the table stays small and no raw
-- IP or per-visitor trail is retained. GA4/GTM still run client-side
-- via the ids in settings — these tables are what the dashboard reads.
-- =====================================================================

CREATE TABLE IF NOT EXISTS page_view_daily (
  tenant_id  BIGINT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  day        DATE NOT NULL,
  path       TEXT NOT NULL,
  views      BIGINT NOT NULL DEFAULT 0,
  visitors   BIGINT NOT NULL DEFAULT 0,
  PRIMARY KEY (tenant_id, day, path)
);
CREATE INDEX IF NOT EXISTS page_view_day_idx ON page_view_daily (tenant_id, day DESC);

CREATE TABLE IF NOT EXISTS traffic_source_daily (
  tenant_id    BIGINT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  day          DATE NOT NULL,
  source       TEXT NOT NULL DEFAULT 'direct',
  medium       TEXT NOT NULL DEFAULT 'none',
  campaign     TEXT NOT NULL DEFAULT '',
  sessions     BIGINT NOT NULL DEFAULT 0,
  PRIMARY KEY (tenant_id, day, source, medium, campaign)
);

-- Rotating per-day visitor hash, used once to decide "is this a new
-- visitor today" and then never read again. Pruned after 45 days.
CREATE TABLE IF NOT EXISTS visitor_days (
  tenant_id    BIGINT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  day          DATE NOT NULL,
  visitor_hash TEXT NOT NULL,
  PRIMARY KEY (tenant_id, day, visitor_hash)
);

-- =====================================================================
-- 2.10  DEPLOYMENT & PUBLISHING
-- =====================================================================

-- Build trigger for the static frontend: Vercel/Netlify deploy hooks,
-- a GitHub repository_dispatch, or any generic POST endpoint.
CREATE TABLE IF NOT EXISTS build_hooks (
  id           BIGSERIAL PRIMARY KEY,
  tenant_id    BIGINT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  name         TEXT NOT NULL,
  provider     TEXT NOT NULL DEFAULT 'generic'
               CHECK (provider IN ('vercel', 'netlify', 'github', 'cloudflare', 'generic')),
  url          TEXT NOT NULL,
  -- Optional bearer/token header value; write-only in the API.
  auth_token   TEXT,
  trigger_events TEXT[] NOT NULL DEFAULT '{content.published}',
  -- Debounce: many publishes in a row should be one rebuild.
  debounce_seconds INT NOT NULL DEFAULT 60,
  is_active    BOOLEAN NOT NULL DEFAULT TRUE,
  last_triggered_at TIMESTAMPTZ,
  created_by   BIGINT REFERENCES users(id) ON DELETE SET NULL,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS build_runs (
  id            BIGSERIAL PRIMARY KEY,
  tenant_id     BIGINT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  hook_id       BIGINT NOT NULL REFERENCES build_hooks(id) ON DELETE CASCADE,
  status        job_status NOT NULL DEFAULT 'pending',
  reason        TEXT,                     -- 'content.published:42', 'manual'
  attempts      INT NOT NULL DEFAULT 0,
  next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  response_code INT,
  error         TEXT,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at   TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS build_runs_due_idx ON build_runs (status, next_attempt_at)
  WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS build_runs_tenant_idx ON build_runs (tenant_id, created_at DESC);

-- CloudFront (or any CDN) path invalidation, queued the same way.
CREATE TABLE IF NOT EXISTS cdn_invalidations (
  id           BIGSERIAL PRIMARY KEY,
  tenant_id    BIGINT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  provider     TEXT NOT NULL DEFAULT 'cloudfront',
  paths        TEXT[] NOT NULL,
  status       job_status NOT NULL DEFAULT 'pending',
  attempts     INT NOT NULL DEFAULT 0,
  next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  reference    TEXT,                      -- provider invalidation id
  error        TEXT,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at  TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS cdn_invalidations_due_idx
  ON cdn_invalidations (status, next_attempt_at) WHERE status = 'pending';

-- api_keys already exists; these make it manageable and expirable.
ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;
ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS created_by BIGINT
  REFERENCES users(id) ON DELETE SET NULL;
ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS last_used_ip INET;
ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS note TEXT;

-- =====================================================================
-- 2.11  BACKUPS, MONITORING & LOGS
-- =====================================================================

CREATE TABLE IF NOT EXISTS backups (
  id          BIGSERIAL PRIMARY KEY,
  -- NULL = whole-install backup rather than one tenant's data.
  tenant_id   BIGINT REFERENCES tenants(id) ON DELETE CASCADE,
  kind        TEXT NOT NULL CHECK (kind IN ('database', 'media', 'full')),
  status      job_status NOT NULL DEFAULT 'pending',
  destination TEXT,                    -- 's3://bucket/prefix'
  object_key  TEXT,
  byte_size   BIGINT,
  checksum    TEXT,
  trigger     TEXT NOT NULL DEFAULT 'manual'
              CHECK (trigger IN ('manual', 'scheduled')),
  error       TEXT,
  started_at  TIMESTAMPTZ,
  finished_at TIMESTAMPTZ,
  created_by  BIGINT REFERENCES users(id) ON DELETE SET NULL,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS backups_created_idx ON backups (created_at DESC);

-- Errors are folded by fingerprint: one row per distinct problem with a
-- counter, so a loop that throws 10k times does not fill the disk.
CREATE TABLE IF NOT EXISTS error_log (
  id            BIGSERIAL PRIMARY KEY,
  tenant_id     BIGINT REFERENCES tenants(id) ON DELETE CASCADE,
  level         TEXT NOT NULL DEFAULT 'error'
                CHECK (level IN ('warning', 'error', 'critical')),
  source        TEXT NOT NULL DEFAULT 'app',
  message       TEXT NOT NULL,
  fingerprint   TEXT NOT NULL,
  detail        JSONB NOT NULL DEFAULT '{}'::jsonb,
  request_method TEXT,
  request_path  TEXT,
  user_id       BIGINT REFERENCES users(id) ON DELETE SET NULL,
  count         BIGINT NOT NULL DEFAULT 1,
  is_resolved   BOOLEAN NOT NULL DEFAULT FALSE,
  first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_seen_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS error_log_fingerprint_idx
  ON error_log (coalesce(tenant_id, 0), fingerprint);
CREATE INDEX IF NOT EXISTS error_log_recent_idx ON error_log (last_seen_at DESC)
  WHERE NOT is_resolved;

-- Central notification centre: new leads, failed publishes, system events.
CREATE TABLE IF NOT EXISTS notifications (
  id         BIGSERIAL PRIMARY KEY,
  tenant_id  BIGINT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  -- NULL = broadcast to everyone in the workspace.
  user_id    BIGINT REFERENCES users(id) ON DELETE CASCADE,
  kind       TEXT NOT NULL,             -- 'lead.created', 'build.failed'
  level      TEXT NOT NULL DEFAULT 'info'
             CHECK (level IN ('info', 'success', 'warning', 'error')),
  title      TEXT NOT NULL,
  body       TEXT,
  link       TEXT,                      -- in-app hash route
  read_at    TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS notifications_unread_idx
  ON notifications (tenant_id, created_at DESC) WHERE read_at IS NULL;

-- Uptime/health probes for the published sites.
CREATE TABLE IF NOT EXISTS health_checks (
  id            BIGSERIAL PRIMARY KEY,
  tenant_id     BIGINT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  name          TEXT NOT NULL,
  url           TEXT NOT NULL,
  expect_status INT NOT NULL DEFAULT 200,
  expect_text   TEXT,
  interval_seconds INT NOT NULL DEFAULT 300,
  is_active     BOOLEAN NOT NULL DEFAULT TRUE,
  last_status   INT,
  last_latency_ms INT,
  last_error    TEXT,
  last_checked_at TIMESTAMPTZ,
  consecutive_failures INT NOT NULL DEFAULT 0,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS health_checks_due_idx
  ON health_checks (last_checked_at) WHERE is_active;

-- =====================================================================
-- 2.12  COMPLIANCE & DATA PROTECTION
-- =====================================================================

-- Proof of consent. subject_hash lets a record be found by email without
-- storing the address when consent was given anonymously.
CREATE TABLE IF NOT EXISTS consent_records (
  id             BIGSERIAL PRIMARY KEY,
  tenant_id      BIGINT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  subject_email  CITEXT,
  subject_hash   TEXT NOT NULL,
  purpose        TEXT NOT NULL,        -- 'marketing', 'cookies.analytics'
  granted        BOOLEAN NOT NULL,
  policy_version TEXT,
  source_page    TEXT,
  evidence       JSONB NOT NULL DEFAULT '{}'::jsonb,
  ip             INET,
  user_agent     TEXT,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS consent_subject_idx
  ON consent_records (tenant_id, subject_hash, created_at DESC);
CREATE INDEX IF NOT EXISTS consent_purpose_idx
  ON consent_records (tenant_id, purpose, created_at DESC);

-- Data-subject requests, worked as a queue with an audit trail.
CREATE TABLE IF NOT EXISTS data_requests (
  id            BIGSERIAL PRIMARY KEY,
  tenant_id     BIGINT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  kind          request_kind NOT NULL,
  subject_email CITEXT NOT NULL,
  status        job_status NOT NULL DEFAULT 'pending',
  note          TEXT,
  -- Row counts touched, so a completed request can be evidenced.
  result        JSONB NOT NULL DEFAULT '{}'::jsonb,
  requested_by  BIGINT REFERENCES users(id) ON DELETE SET NULL,
  completed_by  BIGINT REFERENCES users(id) ON DELETE SET NULL,
  completed_at  TIMESTAMPTZ,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS data_requests_idx ON data_requests (tenant_id, created_at DESC);

-- How long each kind of record is kept. The retention worker enforces it.
CREATE TABLE IF NOT EXISTS retention_policies (
  tenant_id  BIGINT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  scope      TEXT NOT NULL CHECK (scope IN
             ('leads', 'form_submissions', 'activity_log', 'conversion_events',
              'not_found_log', 'error_log', 'visitor_days', 'notifications')),
  days       INT NOT NULL CHECK (days >= 1),
  action     TEXT NOT NULL DEFAULT 'delete' CHECK (action IN ('delete', 'anonymize')),
  is_active  BOOLEAN NOT NULL DEFAULT TRUE,
  last_run_at TIMESTAMPTZ,
  last_affected BIGINT NOT NULL DEFAULT 0,
  updated_by BIGINT REFERENCES users(id) ON DELETE SET NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, scope)
);

-- ---------------------------------------------------- updated_at hooks
DO $$
DECLARE t TEXT;
BEGIN
  FOREACH t IN ARRAY ARRAY['content_types', 'content_items', 'media', 'menus',
                           'reusable_blocks', 'forms', 'email_templates',
                           'subscribers', 'campaigns', 'announcements']
  LOOP
    EXECUTE format('DROP TRIGGER IF EXISTS %I ON %I', t || '_touch', t);
    EXECUTE format(
      'CREATE TRIGGER %I BEFORE UPDATE ON %I FOR EACH ROW
         EXECUTE FUNCTION touch_updated_at()', t || '_touch', t);
  END LOOP;
END $$;
