-- db/migrations/0001_agent_core.sql — agent app schema (same Aurora
-- PostgreSQL Serverless v2 cluster telegram/src/db/schema.sql uses).
-- Apply once via: uv run python -m agent.app.db.migrate
--
-- Design notes:
--   - agent_tasks is the durable work queue. Both user-requested and
--     scheduled-scan tasks live in the same table, distinguished by
--     `source`, so a stuck/crashed worker's lease simply expires and the
--     next poll re-claims the row — no separate "dead letter" table needed
--     at this scale.
--   - idempotency_key is UNIQUE so re-submitting the same logical request
--     (e.g. the Telegram adapter retries a POST /telegram/update after a
--     timeout) never creates a duplicate task.
--   - job_listings / companies / contacts are PUBLIC job-search domain
--     data (per the product scope) — safe to store directly in Aurora.
--   - private_artifacts holds ONLY pointers (which bucket, object key,
--     checksum, local backup path) for anything derived from private user
--     data — augmented resumes, private text responses, private reports.
--     The content itself lives in the private-user S3 bucket plus a local
--     backup near career-ops-modified; it is never written to this
--     database. See models/artifacts.py's ContentVisibility/ArtifactLocation.
--   - agent_task_results and telegram_outbox both carry a visibility /
--     delivery_kind column precisely so a private response can never be
--     accidentally stored inline: public_response / public_payload are
--     only ever populated for public content — a private result instead
--     references a private_artifacts row. Application code
--     (db/task_repo.py, db/outbox_repo.py) enforces this pairing before a
--     row is written; it is not just a naming convention.
--   - Every domain write funnels through db/db_gate.py in one transaction;
--     nothing outside that module should INSERT/UPDATE these tables.

CREATE TABLE IF NOT EXISTS agent_tasks (
  id                TEXT PRIMARY KEY,
  chat_id           TEXT NOT NULL,
  kind              TEXT NOT NULL,
  source            TEXT NOT NULL,
  priority          INTEGER NOT NULL DEFAULT 0,
  status            TEXT NOT NULL DEFAULT 'pending',
  payload           JSONB NOT NULL,
  idempotency_key   TEXT NOT NULL,
  lease_owner       TEXT,
  lease_expires_at  TIMESTAMPTZ,
  error             TEXT,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  started_at        TIMESTAMPTZ,
  finished_at       TIMESTAMPTZ,
  UNIQUE (idempotency_key)
);

-- Backs the "claim next task for this source" query in db/task_repo.py —
-- ordered exactly the way that query orders, so it's an index-only scan.
CREATE INDEX IF NOT EXISTS idx_agent_tasks_claim
  ON agent_tasks (source, status, priority DESC, created_at ASC);

-- Provenance-only for private output — see the header note above. No
-- resume/response/report CONTENT lives in this table, only pointers.
-- `kind` is one of: template_resume | augmented_resume | private_response
-- | private_report. `bucket_name` is the ArtifactBucket enum value
-- (job_artifacts | private_user_artifacts — always the latter here), not
-- the real AWS bucket name, which lives only in agent app settings.
CREATE TABLE IF NOT EXISTS private_artifacts (
  id                 TEXT PRIMARY KEY,
  chat_id            TEXT NOT NULL,
  kind               TEXT NOT NULL,
  bucket_name        TEXT NOT NULL,
  object_key         TEXT NOT NULL,
  local_backup_path  TEXT,
  checksum_sha256    TEXT NOT NULL,
  byte_size          BIGINT NOT NULL,
  source_task_id     TEXT REFERENCES agent_tasks(id) ON DELETE SET NULL,
  created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_private_artifacts_chat ON private_artifacts (chat_id, created_at DESC);

-- One row per finished task. `visibility` decides which payload column is
-- populated: 'public_job_search' -> public_response, 'private_user' ->
-- private_artifact_id. Never both, never neither.
CREATE TABLE IF NOT EXISTS agent_task_results (
  task_id               TEXT PRIMARY KEY REFERENCES agent_tasks(id) ON DELETE CASCADE,
  visibility            TEXT NOT NULL DEFAULT 'public_job_search',
  public_response       JSONB,
  private_artifact_id   TEXT REFERENCES private_artifacts(id),
  tool_result_metadata  JSONB NOT NULL DEFAULT '[]',
  created_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One row per Telegram chat this app serves — offsets/handled-update-ids
-- live here instead of in the telegram bot process, per the "Telegram
-- writes no DB at all" boundary.
CREATE TABLE IF NOT EXISTS telegram_transport_state (
  chat_id         TEXT PRIMARY KEY,
  offset_value    BIGINT NOT NULL DEFAULT 0,
  last_update_id  BIGINT,
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Outbound Telegram messages produced by presenters/formatting, awaiting
-- delivery by the (thin) Telegram adapter. `delivery_kind` decides which
-- payload column is populated: 'public_text' -> public_payload (inline
-- text/buttons), 'job_artifact' / 'private_artifact' -> artifact_ref
-- (bucket/key/checksum only — the adapter fetches bytes itself from the
-- corresponding S3 bucket). The adapter never constructs Telegram text
-- itself — it only relays rows from this table.
CREATE TABLE IF NOT EXISTS telegram_outbox (
  id              TEXT PRIMARY KEY,
  chat_id         TEXT NOT NULL,
  task_id         TEXT REFERENCES agent_tasks(id) ON DELETE SET NULL,
  delivery_kind   TEXT NOT NULL DEFAULT 'public_text',
  public_payload  JSONB,
  artifact_ref    JSONB,
  status          TEXT NOT NULL DEFAULT 'pending',
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  delivered_at    TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_telegram_outbox_pending
  ON telegram_outbox (status, created_at)
  WHERE status = 'pending';

CREATE TABLE IF NOT EXISTS scan_schedules (
  id                TEXT PRIMARY KEY,
  chat_id           TEXT NOT NULL,
  role_id           TEXT NOT NULL,
  query             TEXT NOT NULL,
  interval_seconds  INTEGER NOT NULL,
  next_run_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  enabled           BOOLEAN NOT NULL DEFAULT true,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_scan_schedules_due
  ON scan_schedules (next_run_at)
  WHERE enabled;

CREATE TABLE IF NOT EXISTS companies (
  id          TEXT PRIMARY KEY,
  name        TEXT NOT NULL UNIQUE,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS contacts (
  id          TEXT PRIMARY KEY,
  company_id  TEXT NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  name        TEXT,
  role_title  TEXT,
  email       TEXT,
  phone       TEXT,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_contacts_company ON contacts (company_id);

CREATE TABLE IF NOT EXISTS job_listings (
  id              TEXT PRIMARY KEY,
  role_id         TEXT NOT NULL,
  company_id      TEXT NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  title           TEXT NOT NULL,
  url             TEXT,
  summary         JSONB NOT NULL DEFAULT '[]',
  status          TEXT NOT NULL DEFAULT 'pending',
  source_task_id  TEXT REFERENCES agent_tasks(id) ON DELETE SET NULL,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_job_listings_role ON job_listings (role_id, created_at DESC);

-- Makes db/db_gate.py's apply() idempotent: a task re-run after a worker
-- crash (lease expiry, see idx_agent_tasks_claim) must not double-insert the
-- same listings/contacts. Safe as a plain existence check (not a locking
-- read) because task leasing already guarantees at most one worker ever
-- processes a given idempotency_key at a time — see db/task_repo.py.
CREATE TABLE IF NOT EXISTS db_write_receipts (
  idempotency_key    TEXT PRIMARY KEY,
  counts             JSONB NOT NULL DEFAULT '{}',
  affected_role_ids  JSONB NOT NULL DEFAULT '[]',
  created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
