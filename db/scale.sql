-- =====================================================================
-- CRM Admin — scale: job queues, shared rate limiting, cache signalling
--
-- Applied after connectors.sql and before tenancy.sql:
--
--   schema.sql → platform.sql → connectors.sql → scale.sql → tenancy.sql
--
-- tenancy.sql goes last because its RLS block walks every table that
-- exists; re-running it is how these tables get their policy.
-- =====================================================================

SET client_min_messages = warning;

-- --------------------------------------------------------------- enums
DO $$ BEGIN
  CREATE TYPE media_state AS ENUM ('pending', 'processing', 'ready', 'failed');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- =====================================================================
-- Media processing, off the request path
-- ---------------------------------------------------------------------
-- Building six responsive derivatives from a 12 MP photo is ~1.7s of
-- CPU. Doing that inside the upload request blocked the whole event
-- loop, so one client's photo gallery stalled every other request on
-- that worker. The upload now stores the original and returns; a
-- worker does the encoding.
-- =====================================================================

ALTER TABLE media ADD COLUMN IF NOT EXISTS state media_state NOT NULL DEFAULT 'ready';
ALTER TABLE media ADD COLUMN IF NOT EXISTS processing_error TEXT;
-- Bytes of the file as uploaded, before optimization — so the saving
-- is reportable once processing finishes.
ALTER TABLE media ADD COLUMN IF NOT EXISTS original_bytes BIGINT;

CREATE INDEX IF NOT EXISTS media_state_idx ON media (tenant_id, state)
  WHERE state <> 'ready';

CREATE TABLE IF NOT EXISTS media_jobs (
  id              BIGSERIAL PRIMARY KEY,
  tenant_id       BIGINT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  media_id        BIGINT NOT NULL REFERENCES media(id) ON DELETE CASCADE,
  kind            TEXT NOT NULL DEFAULT 'derivatives'
                  CHECK (kind IN ('derivatives', 'reprocess')),
  status          job_status NOT NULL DEFAULT 'pending',
  attempts        INT NOT NULL DEFAULT 0,
  next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  -- The source object to read back. The bytes are never queued: a
  -- 25 MB payload in a jsonb column would make this table the
  -- platform's biggest, and the file is already in storage.
  source_key      TEXT NOT NULL,
  mime_type       TEXT NOT NULL,
  filename        TEXT NOT NULL,
  error           TEXT,
  duration_ms     INT,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  started_at      TIMESTAMPTZ,
  finished_at     TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS media_jobs_due_idx
  ON media_jobs (status, next_attempt_at) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS media_jobs_media_idx ON media_jobs (media_id);

-- =====================================================================
-- Shared rate limiting
-- ---------------------------------------------------------------------
-- The in-process dict multiplies the effective limit by the number of
-- processes, so "8 submissions per 10 minutes" became 8 × workers ×
-- tasks. This table is the shared-state backend: one row per key per
-- window, incremented atomically.
--
-- Postgres rather than Redis because it needs no new infrastructure at
-- this size. RATE_LIMIT_BACKEND=redis is there for when the write
-- volume justifies it.
-- =====================================================================

CREATE UNLOGGED TABLE IF NOT EXISTS rate_limits (
  bucket      TEXT NOT NULL,
  window_start BIGINT NOT NULL,     -- epoch seconds, floored to the window
  hits        INT NOT NULL DEFAULT 0,
  expires_at  TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (bucket, window_start)
);

CREATE INDEX IF NOT EXISTS rate_limits_expiry_idx ON rate_limits (expires_at);

-- UNLOGGED: this data is worthless after a crash — a reset rate-limit
-- window is a rounding error, and skipping the WAL makes the writes
-- roughly twice as cheap on the hottest small table in the system.

-- =====================================================================
-- API versioning
-- ---------------------------------------------------------------------
-- A static frontend is deployed separately and can lag the platform by
-- months, so a version cannot be retired the moment its replacement
-- ships. Recording usage per version per site turns "can we drop v1?"
-- into a query rather than a guess.
-- =====================================================================

CREATE TABLE IF NOT EXISTS api_version_usage (
  tenant_id   BIGINT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  version     TEXT NOT NULL,
  day         DATE NOT NULL,
  requests    BIGINT NOT NULL DEFAULT 0,
  last_path   TEXT,
  last_agent  TEXT,
  last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, version, day)
);

CREATE INDEX IF NOT EXISTS api_version_usage_day_idx
  ON api_version_usage (day DESC);

-- =====================================================================
-- Indexes added from measurement, not guesswork
-- ---------------------------------------------------------------------
-- Each index costs write throughput and storage, so this section holds
-- only the ones that changed a measured plan on a realistic dataset
-- (250k leads, 60k content items, 80k activity rows, 105 tenants).
--
-- Checked and NOT added, because the existing indexes already served
-- them at that volume:
--   activity_log audit filter   0.07 ms — the (tenant_id, created_at)
--                               index plus a cheap LIKE filter
--   analytics top pages         0.14 ms — page_view_day_idx
--   conversion counts           0.06 ms — index-only on
--                               conversion_events_kind_idx
--   content list by type        0.10 ms — content_items_tenant_idx
--   scheduled publish scan      0.02 ms — the partial due index
-- =====================================================================

-- /api/leads/counts runs on every admin page load (the sidebar badge)
-- and aggregates a whole tenant's pipeline. On a 62k-lead tenant that
-- was a 31 ms bitmap heap scan; as a partial index it becomes an
-- index-only scan at 3.9 ms, and it stays index-only because the
-- filtered rows never need a heap visit.
--
-- Partial on `NOT is_spam` because that is what every pipeline query
-- filters by, and it keeps the index to a third the size of a full one.
CREATE INDEX IF NOT EXISTS leads_counts_idx
  ON leads (tenant_id, status) WHERE NOT is_spam;

-- Growth note: this is O(tenant's lead count) per page load — ~30 ms
-- at 500k leads. A tenant past roughly a million leads wants a
-- maintained counter row instead of an aggregate; tenant_usage is
-- already the place for it.
