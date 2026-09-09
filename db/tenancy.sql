-- =====================================================================
-- CRM Admin — tenancy: the control plane
--
-- Applied last, after db/schema.sql and db/platform.sql, because the
-- row-level-security block at the bottom walks every table that
-- already exists and carries a tenant_id.
--
--   psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f db/schema.sql
--   psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f db/platform.sql
--   psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f db/tenancy.sql
--
-- Idempotent: safe to re-run, and re-running is how new tables pick up
-- their RLS policy.
-- =====================================================================

SET client_min_messages = warning;

-- --------------------------------------------------------------- enums
DO $$ BEGIN
  CREATE TYPE tenant_status AS ENUM ('active', 'suspended', 'archived');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- ------------------------------------------------------- tenant record
-- `is_active` stays as the column every existing query already reads;
-- `status` carries the reason, and a trigger keeps the two in step so
-- neither can drift from the other.
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS status tenant_status NOT NULL DEFAULT 'active';
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS plan TEXT NOT NULL DEFAULT 'standard';
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS notes TEXT;
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS created_by BIGINT
  REFERENCES users(id) ON DELETE SET NULL;
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS suspended_at TIMESTAMPTZ;
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS suspended_reason TEXT;
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS archived_at TIMESTAMPTZ;
-- Per-site ceilings. Empty object means "platform defaults" (see
-- app/tenancy.py DEFAULT_LIMITS) — one site can be raised without
-- touching the platform or any other site.
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS limits JSONB NOT NULL DEFAULT '{}'::jsonb;
-- Per-site infrastructure overrides: a different S3 prefix, its own
-- CDN distribution, its own build hook target. Also how one busy site
-- gets its own bucket without a platform change.
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS infra JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now();

CREATE INDEX IF NOT EXISTS tenants_status_idx ON tenants (status);

-- Keep is_active and status consistent in both directions, so old code
-- writing is_active and new code writing status cannot disagree.
CREATE OR REPLACE FUNCTION tenants_sync_status() RETURNS trigger AS $$
BEGIN
  IF TG_OP = 'INSERT' THEN
    IF NOT NEW.is_active AND NEW.status = 'active' THEN
      NEW.status := 'suspended';
    END IF;
    NEW.is_active := (NEW.status = 'active');
    RETURN NEW;
  END IF;

  -- status changed: it wins.
  IF NEW.status IS DISTINCT FROM OLD.status THEN
    NEW.is_active := (NEW.status = 'active');
  -- only is_active changed: derive a status that matches.
  ELSIF NEW.is_active IS DISTINCT FROM OLD.is_active THEN
    NEW.status := CASE WHEN NEW.is_active THEN 'active' ELSE 'suspended' END;
  END IF;

  IF NEW.status = 'suspended' AND NEW.suspended_at IS NULL THEN
    NEW.suspended_at := now();
  ELSIF NEW.status = 'active' THEN
    NEW.suspended_at := NULL;
    NEW.suspended_reason := NULL;
  END IF;
  IF NEW.status = 'archived' AND NEW.archived_at IS NULL THEN
    NEW.archived_at := now();
  END IF;

  NEW.updated_at := now();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS tenants_status_sync ON tenants;
CREATE TRIGGER tenants_status_sync BEFORE INSERT OR UPDATE ON tenants
  FOR EACH ROW EXECUTE FUNCTION tenants_sync_status();

-- ------------------------------------------------------ tenant domains
-- A site can answer on several hostnames (apex, www, a staging domain).
-- Unique across the whole install: two tenants claiming one hostname
-- would make host-based resolution ambiguous, which is a tenant-
-- confusion bug rather than a validation nicety.
CREATE TABLE IF NOT EXISTS tenant_domains (
  id            BIGSERIAL PRIMARY KEY,
  tenant_id     BIGINT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  domain        CITEXT NOT NULL UNIQUE,
  is_primary    BOOLEAN NOT NULL DEFAULT FALSE,
  -- Verified domains are the ones trusted for CORS and host-based
  -- resolution; unverified ones are recorded but inert.
  is_verified   BOOLEAN NOT NULL DEFAULT FALSE,
  verify_token  TEXT NOT NULL DEFAULT encode(gen_random_bytes(16), 'hex'),
  verified_at   TIMESTAMPTZ,
  created_by    BIGINT REFERENCES users(id) ON DELETE SET NULL,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS tenant_domains_tenant_idx ON tenant_domains (tenant_id);
-- At most one primary per tenant.
CREATE UNIQUE INDEX IF NOT EXISTS tenant_domains_primary_idx
  ON tenant_domains (tenant_id) WHERE is_primary;

-- Backfill the single primary_domain column this replaces.
INSERT INTO tenant_domains (tenant_id, domain, is_primary, is_verified, verified_at)
SELECT t.id, lower(t.primary_domain), TRUE, TRUE, now()
  FROM tenants t
 WHERE t.primary_domain IS NOT NULL AND btrim(t.primary_domain) <> ''
ON CONFLICT (domain) DO NOTHING;

-- --------------------------------------------------------- usage rollup
-- Counting content, media bytes and leads across a portfolio on every
-- dashboard load is the kind of query that is fine at ten sites and a
-- problem at three hundred. A worker refreshes this; quota checks read
-- it, then confirm against the live table only when close to a limit.
CREATE TABLE IF NOT EXISTS tenant_usage (
  tenant_id      BIGINT PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE,
  users          INT    NOT NULL DEFAULT 0,
  content_items  INT    NOT NULL DEFAULT 0,
  media_files    INT    NOT NULL DEFAULT 0,
  media_bytes    BIGINT NOT NULL DEFAULT 0,
  leads_total    INT    NOT NULL DEFAULT 0,
  leads_30d      INT    NOT NULL DEFAULT 0,
  subscribers    INT    NOT NULL DEFAULT 0,
  emails_30d     INT    NOT NULL DEFAULT 0,
  page_views_30d BIGINT NOT NULL DEFAULT 0,
  computed_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ------------------------------------------------- provisioning audit
-- Site lifecycle is the one thing in this platform that is not
-- tenant-scoped, so it gets its own log rather than living in the
-- per-tenant activity_log.
CREATE TABLE IF NOT EXISTS tenant_events (
  id         BIGSERIAL PRIMARY KEY,
  tenant_id  BIGINT REFERENCES tenants(id) ON DELETE SET NULL,
  tenant_slug TEXT NOT NULL,          -- kept after a hard delete
  action     TEXT NOT NULL,           -- 'created', 'suspended', 'deleted'
  actor_id   BIGINT REFERENCES users(id) ON DELETE SET NULL,
  actor_email CITEXT,
  detail     JSONB NOT NULL DEFAULT '{}'::jsonb,
  ip         INET,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS tenant_events_created_idx ON tenant_events (created_at DESC);
CREATE INDEX IF NOT EXISTS tenant_events_tenant_idx ON tenant_events (tenant_id, created_at DESC);

-- =====================================================================
-- ROW-LEVEL SECURITY — the data-access layer of tenant isolation
-- ---------------------------------------------------------------------
-- TenantDB (app/db.py) binds $1 to a tenant id so a query cannot be
-- written without the scope. That is the primary control. This is the
-- backstop underneath it: when a connection declares which tenant it
-- is acting for, PostgreSQL refuses to return or write another
-- tenant's rows even if the WHERE clause is wrong.
--
-- The policy is deliberately *scoped-when-declared*:
--
--   app.tenant_id unset  → no restriction (platform code: the worker
--                          loops, portfolio reporting, migrations,
--                          pg_dump, the tenants table itself)
--   app.tenant_id set    → that tenant's rows only, for read and write
--
-- TenantDB always sets it, so every tenant-scoped path is covered.
-- Raw db.fetch/db.execute deliberately do not, which is what keeps
-- cross-tenant platform queries possible — those call sites are the
-- ones to review by hand.
--
-- FORCE ROW LEVEL SECURITY matters: without it PostgreSQL exempts the
-- table owner, and this app connects as the owner, so the policies
-- would silently do nothing.
-- =====================================================================

CREATE OR REPLACE FUNCTION current_tenant_id() RETURNS BIGINT AS $$
  -- nullif so an empty string reads the same as unset; the `true`
  -- argument stops current_setting raising when the GUC is absent.
  SELECT nullif(current_setting('app.tenant_id', true), '')::bigint;
$$ LANGUAGE sql STABLE;

DO $$
DECLARE
  target RECORD;
  nullable BOOLEAN;
  predicate TEXT;
BEGIN
  FOR target IN
    SELECT c.table_name, c.is_nullable
      FROM information_schema.columns c
      JOIN information_schema.tables t
        ON t.table_schema = c.table_schema AND t.table_name = c.table_name
     WHERE c.table_schema = 'public'
       AND c.column_name = 'tenant_id'
       AND t.table_type = 'BASE TABLE'
     ORDER BY c.table_name
  LOOP
    nullable := (target.is_nullable = 'YES');

    -- Rows with a NULL tenant_id are install-wide by design (backups,
    -- error_log) and stay visible to a scoped connection, matching the
    -- `tenant_id = $1 OR tenant_id IS NULL` reads already in ops.py.
    predicate := 'current_tenant_id() IS NULL OR tenant_id = current_tenant_id()';
    IF nullable THEN
      predicate := predicate || ' OR tenant_id IS NULL';
    END IF;

    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', target.table_name);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', target.table_name);
    EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON %I', target.table_name);
    EXECUTE format(
      'CREATE POLICY tenant_isolation ON %I USING (%s) WITH CHECK (%s)',
      target.table_name, predicate, predicate
    );
  END LOOP;
END $$;

-- The tenants table itself is the control plane's own record. It is not
-- tenant-scoped (a session needs to read its own tenant row to resolve
-- a name), so it carries no policy — access to it is gated by the
-- `sites.manage` permission in the application.

-- ------------------------------------------------- the restricted role
-- Superusers and BYPASSRLS roles ignore every policy, and
-- FORCE ROW LEVEL SECURITY only reaches the table *owner*. So the
-- policies above do nothing at all while the app connects as the
-- superuser that setup.sh creates.
--
-- This role is the one the application should connect as. It owns
-- nothing, cannot create extensions, and has no way to opt out of a
-- policy. Migrations keep running as the owner.
--
-- No password is set here — committing one would put a credential in
-- git. Set it from your secret store and point DATABASE_URL at it:
--
--   ALTER ROLE crm_app PASSWORD '<from Secrets Manager>';
--   DATABASE_URL=postgres://crm_app:<pw>@host:5432/crm
--
-- /healthz and the Operations screen both report whether isolation is
-- actually being enforced, so a half-finished switch is visible rather
-- than silent.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'crm_app') THEN
    CREATE ROLE crm_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
                        NOBYPASSRLS NOINHERIT;
  ELSE
    -- Re-assert the attributes that matter, in case they drifted.
    ALTER ROLE crm_app NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
  END IF;
END $$;

GRANT USAGE ON SCHEMA public TO crm_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO crm_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO crm_app;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO crm_app;

-- Tables and sequences added by a later migration, without needing to
-- re-run these grants by hand.
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO crm_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO crm_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT EXECUTE ON FUNCTIONS TO crm_app;
