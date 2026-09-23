-- =====================================================================
-- CRM Admin — PostgreSQL schema
-- Multi-tenant by design: every business table carries tenant_id and
-- every query in the app layer is scoped by it (see src/lib/db.js).
-- Run: psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f db/schema.sql
-- =====================================================================

-- Every statement is IF NOT EXISTS so re-running is safe; hide the
-- resulting "already exists, skipping" notices.
SET client_min_messages = warning;

CREATE EXTENSION IF NOT EXISTS pgcrypto;   -- gen_random_uuid(), digest()
CREATE EXTENSION IF NOT EXISTS citext;     -- case-insensitive email

-- --------------------------------------------------------------- enums
DO $$ BEGIN
  CREATE TYPE lead_status AS ENUM
    ('new', 'contacted', 'qualified', 'proposal', 'won', 'lost');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
  CREATE TYPE user_role AS ENUM ('owner', 'admin', 'agent', 'viewer');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
  CREATE TYPE delivery_status AS ENUM ('pending', 'delivered', 'failed', 'dead');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- ------------------------------------------------------------- tenants
-- One row per client site / brand. The control plane manages many.
CREATE TABLE IF NOT EXISTS tenants (
  id          BIGSERIAL PRIMARY KEY,
  slug        CITEXT NOT NULL UNIQUE,          -- 'braen', 'srds'
  name        TEXT   NOT NULL,
  primary_domain TEXT,                          -- used to resolve public form posts
  is_active   BOOLEAN NOT NULL DEFAULT TRUE,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- --------------------------------------------------------------- users
CREATE TABLE IF NOT EXISTS users (
  id            BIGSERIAL PRIMARY KEY,
  tenant_id     BIGINT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  email         CITEXT NOT NULL,
  password_hash TEXT   NOT NULL,               -- bcrypt, cost 12
  display_name  TEXT   NOT NULL,
  role          user_role NOT NULL DEFAULT 'agent',
  is_active     BOOLEAN NOT NULL DEFAULT TRUE,
  failed_logins INT     NOT NULL DEFAULT 0,
  locked_until  TIMESTAMPTZ,
  last_login_at TIMESTAMPTZ,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, email)
);

-- ------------------------------------------------------------ sessions
-- Opaque token lives in an httpOnly cookie; only its SHA-256 is stored.
CREATE TABLE IF NOT EXISTS sessions (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  token_hash  TEXT NOT NULL UNIQUE,
  csrf_token  TEXT NOT NULL,
  user_id     BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  tenant_id   BIGINT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  ip          INET,
  user_agent  TEXT,
  expires_at  TIMESTAMPTZ NOT NULL,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS sessions_expires_idx ON sessions (expires_at);

-- --------------------------------------------------------------- forms
-- A public form definition. Field schema is JSON so admins can add
-- fields without a migration; intake validates against it.
CREATE TABLE IF NOT EXISTS forms (
  id            BIGSERIAL PRIMARY KEY,
  tenant_id     BIGINT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  slug          CITEXT NOT NULL,
  name          TEXT   NOT NULL,
  fields        JSONB  NOT NULL DEFAULT '[]'::jsonb,
  notify_emails TEXT[] NOT NULL DEFAULT '{}',
  is_active     BOOLEAN NOT NULL DEFAULT TRUE,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, slug)
);

-- --------------------------------------------------------------- leads
-- First-class lead record, not a form-submission log line.
CREATE TABLE IF NOT EXISTS leads (
  id            BIGSERIAL PRIMARY KEY,
  tenant_id     BIGINT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  form_id       BIGINT REFERENCES forms(id) ON DELETE SET NULL,

  full_name     TEXT NOT NULL,
  email         CITEXT,
  phone         TEXT,
  company       TEXT,
  message       TEXT,
  extra         JSONB NOT NULL DEFAULT '{}'::jsonb,   -- non-core form fields

  status        lead_status NOT NULL DEFAULT 'new',
  assigned_to   BIGINT REFERENCES users(id) ON DELETE SET NULL,
  follow_up_on  DATE,
  value_amount  NUMERIC(14,2),

  -- attribution
  source_page   TEXT,
  referrer      TEXT,
  utm_source    TEXT,
  utm_medium    TEXT,
  utm_campaign  TEXT,
  utm_term      TEXT,
  utm_content   TEXT,

  ip            INET,
  user_agent    TEXT,
  is_spam       BOOLEAN NOT NULL DEFAULT FALSE,

  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS leads_tenant_created_idx  ON leads (tenant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS leads_tenant_status_idx   ON leads (tenant_id, status);
CREATE INDEX IF NOT EXISTS leads_tenant_assigned_idx ON leads (tenant_id, assigned_to);
CREATE INDEX IF NOT EXISTS leads_followup_idx        ON leads (tenant_id, follow_up_on)
  WHERE follow_up_on IS NOT NULL;

-- Full-text-ish search across the fields people actually search by.
CREATE INDEX IF NOT EXISTS leads_search_idx ON leads
  USING GIN (to_tsvector('simple',
    coalesce(full_name,'') || ' ' || coalesce(email::text,'') || ' ' ||
    coalesce(phone,'')     || ' ' || coalesce(company,'')    || ' ' ||
    coalesce(message,'')));

-- ---------------------------------------------------------- lead notes
CREATE TABLE IF NOT EXISTS lead_notes (
  id         BIGSERIAL PRIMARY KEY,
  tenant_id  BIGINT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  lead_id    BIGINT NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
  user_id    BIGINT REFERENCES users(id) ON DELETE SET NULL,
  body       TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS lead_notes_lead_idx ON lead_notes (lead_id, created_at DESC);

-- -------------------------------------------------------- activity log
CREATE TABLE IF NOT EXISTS activity_log (
  id          BIGSERIAL PRIMARY KEY,
  tenant_id   BIGINT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  user_id     BIGINT REFERENCES users(id) ON DELETE SET NULL,
  action      TEXT NOT NULL,                 -- 'lead.status_changed'
  object_type TEXT,
  object_id   BIGINT,
  meta        JSONB NOT NULL DEFAULT '{}'::jsonb,
  ip          INET,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS activity_tenant_created_idx ON activity_log (tenant_id, created_at DESC);

-- ------------------------------------------------------------ webhooks
CREATE TABLE IF NOT EXISTS webhook_endpoints (
  id         BIGSERIAL PRIMARY KEY,
  tenant_id  BIGINT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  name       TEXT NOT NULL,
  url        TEXT NOT NULL,
  secret     TEXT NOT NULL,                  -- HMAC-SHA256 signing key
  events     TEXT[] NOT NULL DEFAULT '{lead.created}',
  is_active  BOOLEAN NOT NULL DEFAULT TRUE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS webhook_deliveries (
  id              BIGSERIAL PRIMARY KEY,
  tenant_id       BIGINT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  endpoint_id     BIGINT NOT NULL REFERENCES webhook_endpoints(id) ON DELETE CASCADE,
  event           TEXT NOT NULL,
  idempotency_key TEXT NOT NULL,
  payload         JSONB NOT NULL,
  status          delivery_status NOT NULL DEFAULT 'pending',
  attempts        INT NOT NULL DEFAULT 0,
  next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  response_code   INT,
  last_error      TEXT,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (endpoint_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS deliveries_due_idx ON webhook_deliveries (status, next_attempt_at)
  WHERE status = 'pending';

-- --------------------------------------------------------- email outbox
-- Outbound email as rows, like webhook_deliveries: a crash mid-send
-- leaves a pending row the worker retries. Reuses delivery_status.
CREATE TABLE IF NOT EXISTS email_outbox (
  id              BIGSERIAL PRIMARY KEY,
  tenant_id       BIGINT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  to_email        CITEXT NOT NULL,
  subject         TEXT NOT NULL,
  body            TEXT NOT NULL,                -- plain text
  kind            TEXT NOT NULL DEFAULT 'generic',  -- 'lead.notification', 'auth.reset'
  status          delivery_status NOT NULL DEFAULT 'pending',
  attempts        INT NOT NULL DEFAULT 0,
  next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_error      TEXT,
  sent_at         TIMESTAMPTZ,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS email_due_idx ON email_outbox (status, next_attempt_at)
  WHERE status = 'pending';

-- ------------------------------------------------------ password resets
-- Single-use tokens; only the SHA-256 is stored, like session tokens.
CREATE TABLE IF NOT EXISTS password_resets (
  id         BIGSERIAL PRIMARY KEY,
  token_hash TEXT NOT NULL UNIQUE,
  user_id    BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  tenant_id  BIGINT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  expires_at TIMESTAMPTZ NOT NULL,
  used_at    TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS password_resets_expiry_idx ON password_resets (expires_at);

-- ------------------------------------------------------------ settings
CREATE TABLE IF NOT EXISTS settings (
  tenant_id  BIGINT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  key        TEXT NOT NULL,
  value      JSONB NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, key)
);

-- ------------------------------------------------------------ api keys
-- For static frontends / other services calling the intake API.
CREATE TABLE IF NOT EXISTS api_keys (
  id           BIGSERIAL PRIMARY KEY,
  tenant_id    BIGINT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  name         TEXT NOT NULL,
  key_prefix   TEXT NOT NULL UNIQUE,         -- shown in UI, e.g. 'ck_9f2a'
  key_hash     TEXT NOT NULL,                -- SHA-256 of the full key
  scopes       TEXT[] NOT NULL DEFAULT '{leads:write}',
  last_used_at TIMESTAMPTZ,
  revoked_at   TIMESTAMPTZ,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- --------------------------------------------------------------- pages
-- WordPress-style pages. Content is a validated JSON block list, never
-- stored HTML; the renderer escapes every value at output time. The
-- draft (blocks/theme) is separate from the published_* snapshot, so
-- editing never disturbs the live page until an explicit publish.
DO $$ BEGIN
  CREATE TYPE page_status AS ENUM ('draft', 'published');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE TABLE IF NOT EXISTS pages (
  id            BIGSERIAL PRIMARY KEY,
  tenant_id     BIGINT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  slug          CITEXT NOT NULL,
  title         TEXT   NOT NULL,
  description   TEXT,                                   -- meta description
  blocks        JSONB  NOT NULL DEFAULT '[]'::jsonb,    -- draft content
  theme         JSONB  NOT NULL DEFAULT '{}'::jsonb,
  -- The rest of the head: meta_title, canonical, noindex/nofollow,
  -- og_* and twitter_*. Same shape and same validator as
  -- content_items.seo (app/content.py clean_seo), minus
  -- meta_description — that is the `description` column above, and two
  -- copies of it could disagree.
  seo           JSONB  NOT NULL DEFAULT '{}'::jsonb,
  -- 'blocks': the block builder. 'html': the page IS one HTML document
  -- the author writes, stored as a single html block so that publish,
  -- revisions and sanitize-on-write all work unchanged.
  mode          TEXT   NOT NULL DEFAULT 'blocks' CHECK (mode IN ('blocks', 'html')),
  status        page_status NOT NULL DEFAULT 'draft',

  -- snapshot served at /p/{tenant}/{slug}
  published_title       TEXT,
  published_description TEXT,
  published_blocks      JSONB,
  published_theme       JSONB,
  published_seo         JSONB,
  published_mode        TEXT,
  published_at          TIMESTAMPTZ,

  updated_by    BIGINT REFERENCES users(id) ON DELETE SET NULL,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, slug)
);
CREATE INDEX IF NOT EXISTS pages_tenant_idx ON pages (tenant_id, updated_at DESC);

-- Snapshot taken on every publish; the newest 20 per page are kept.
CREATE TABLE IF NOT EXISTS page_revisions (
  id         BIGSERIAL PRIMARY KEY,
  tenant_id  BIGINT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  page_id    BIGINT NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
  title      TEXT  NOT NULL,
  blocks     JSONB NOT NULL,
  theme      JSONB NOT NULL,
  -- Meta travels with the revision: restoring last week's page and
  -- keeping this week's canonical tag would be a silent SEO change.
  description TEXT,
  seo        JSONB NOT NULL DEFAULT '{}'::jsonb,
  mode       TEXT  NOT NULL DEFAULT 'blocks',
  created_by BIGINT REFERENCES users(id) ON DELETE SET NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS page_revisions_page_idx ON page_revisions (page_id, created_at DESC);

-- ------------------------------------------------------ updated_at hook
CREATE OR REPLACE FUNCTION touch_updated_at() RETURNS trigger AS $$
BEGIN NEW.updated_at = now(); RETURN NEW; END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS leads_touch ON leads;
CREATE TRIGGER leads_touch BEFORE UPDATE ON leads
  FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

DROP TRIGGER IF EXISTS pages_touch ON pages;
CREATE TRIGGER pages_touch BEFORE UPDATE ON pages
  FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
