-- =====================================================================
-- CRM Admin — integrations: the connector layer
--
-- Applied after schema.sql, platform.sql and tenancy.sql:
--
--   psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f db/connectors.sql
--   psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f db/tenancy.sql   -- re-run
--
-- Re-run tenancy.sql afterwards so the new tables pick up their
-- row-level-security policy; that block walks every table carrying a
-- tenant_id, so it is how a new table gets isolated.
--
-- ---------------------------------------------------------------------
-- Relationship to webhook_endpoints (db/schema.sql)
--
--   webhook_endpoints  raw HMAC-signed JSON to a URL you control. You
--                      build the receiver. Unchanged, still the right
--                      answer for a bespoke endpoint.
--   connectors         we speak the provider's own API: its auth, its
--                      object shape, its field names, its errors.
--
-- Both queue deliveries as rows with retries and an idempotency key,
-- because a crash mid-send must not lose a lead either way.
-- =====================================================================

SET client_min_messages = warning;

-- --------------------------------------------------------------- enums
DO $$ BEGIN
  CREATE TYPE connector_kind AS ENUM
    ('crm', 'email', 'automation', 'analytics', 'storage');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
  CREATE TYPE connector_status AS ENUM
    ('draft', 'connected', 'error', 'expired', 'disabled');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- ---------------------------------------------------------- connectors
CREATE TABLE IF NOT EXISTS connectors (
  id            BIGSERIAL PRIMARY KEY,
  tenant_id     BIGINT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  kind          connector_kind NOT NULL,
  -- Registry key: 'zoho_crm', 'hubspot', 'salesforce', 'ses', 'brevo',
  -- 'sendgrid', 'mailchimp', 'zapier', 'make', 'n8n', 'webhook'.
  provider      TEXT NOT NULL,
  name          TEXT NOT NULL,
  status        connector_status NOT NULL DEFAULT 'draft',

  -- Non-secret provider settings: region, portal id, list id, base URL,
  -- which module to write to. Safe to show in the UI and in logs.
  config        JSONB NOT NULL DEFAULT '{}'::jsonb,

  -- Secrets, encrypted at rest by app/crypto.py. Never selected into
  -- an API response; the router strips it on every path.
  credentials   TEXT,
  -- Non-secret facts *about* the credentials: when the access token
  -- expires, which scopes were granted, which account was connected.
  -- Lets the UI say "expires in 4 days" without decrypting anything.
  credentials_meta JSONB NOT NULL DEFAULT '{}'::jsonb,

  -- {"full_name": "Last_Name", "company": "Company"} — platform field
  -- on the left, the provider's field on the right.
  field_mapping JSONB NOT NULL DEFAULT '{}'::jsonb,
  -- Platform events that trigger a push (app/events.py PLATFORM_EVENTS).
  events        TEXT[] NOT NULL DEFAULT '{lead.created}',

  is_active     BOOLEAN NOT NULL DEFAULT TRUE,
  last_ok_at    TIMESTAMPTZ,
  last_error    TEXT,
  last_error_at TIMESTAMPTZ,
  created_by    BIGINT REFERENCES users(id) ON DELETE SET NULL,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS connectors_tenant_idx ON connectors (tenant_id, kind);
-- The dispatcher's hot path: which live connectors want this event.
CREATE INDEX IF NOT EXISTS connectors_active_idx
  ON connectors (tenant_id) WHERE is_active AND status = 'connected';
-- One configured instance per provider per site. Two Zoho connectors
-- racing to write the same lead would create duplicates in the CRM.
CREATE UNIQUE INDEX IF NOT EXISTS connectors_provider_idx
  ON connectors (tenant_id, provider);

-- -------------------------------------------------------- delivery log
-- Same shape as webhook_deliveries, for the same reason: a delivery is
-- a row, not an in-flight coroutine, so a crash mid-send leaves work
-- the worker picks up again.
CREATE TABLE IF NOT EXISTS connector_deliveries (
  id              BIGSERIAL PRIMARY KEY,
  tenant_id       BIGINT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  connector_id    BIGINT NOT NULL REFERENCES connectors(id) ON DELETE CASCADE,
  event           TEXT NOT NULL,
  -- Derived from the source object, not random: re-emitting
  -- lead.created for lead 42 must not create a second CRM record.
  idempotency_key TEXT NOT NULL,
  payload         JSONB NOT NULL,
  -- What was actually sent after mapping — the field an integrator
  -- needs when the provider says "invalid field" and you need to know
  -- which one.
  request         JSONB,
  status          delivery_status NOT NULL DEFAULT 'pending',
  attempts        INT NOT NULL DEFAULT 0,
  next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  response_code   INT,
  response        JSONB,
  -- The id the provider assigned, so a later update targets the same
  -- record instead of creating another one.
  external_id     TEXT,
  last_error      TEXT,
  duration_ms     INT,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  delivered_at    TIMESTAMPTZ,
  UNIQUE (connector_id, idempotency_key)
);

CREATE INDEX IF NOT EXISTS connector_deliveries_due_idx
  ON connector_deliveries (status, next_attempt_at) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS connector_deliveries_log_idx
  ON connector_deliveries (tenant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS connector_deliveries_connector_idx
  ON connector_deliveries (connector_id, created_at DESC);

-- Maps a platform object to the record the provider created for it, so
-- a second event about the same lead updates rather than duplicates.
CREATE TABLE IF NOT EXISTS connector_links (
  tenant_id     BIGINT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  connector_id  BIGINT NOT NULL REFERENCES connectors(id) ON DELETE CASCADE,
  object_type   TEXT NOT NULL,          -- 'lead', 'subscriber'
  object_id     BIGINT NOT NULL,
  external_id   TEXT NOT NULL,
  external_url  TEXT,
  synced_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (connector_id, object_type, object_id)
);
CREATE INDEX IF NOT EXISTS connector_links_external_idx
  ON connector_links (connector_id, external_id);

-- ------------------------------------------------------- OAuth handshake
-- Short-lived. `state` is the CSRF token the provider echoes back; a
-- callback whose state is not here is rejected, which is what stops an
-- attacker attaching their own CRM account to someone else's site.
CREATE TABLE IF NOT EXISTS connector_oauth_states (
  state         TEXT PRIMARY KEY,
  tenant_id     BIGINT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  connector_id  BIGINT REFERENCES connectors(id) ON DELETE CASCADE,
  provider      TEXT NOT NULL,
  redirect_uri  TEXT NOT NULL,
  -- PKCE verifier, for providers that support it.
  code_verifier TEXT,
  created_by    BIGINT REFERENCES users(id) ON DELETE SET NULL,
  expires_at    TIMESTAMPTZ NOT NULL,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS oauth_states_expiry_idx
  ON connector_oauth_states (expires_at);

-- ------------------------------------------------------ updated_at hook
DROP TRIGGER IF EXISTS connectors_touch ON connectors;
CREATE TRIGGER connectors_touch BEFORE UPDATE ON connectors
  FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
