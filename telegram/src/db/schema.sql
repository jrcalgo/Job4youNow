-- db/schema.sql — job4meNow telegram bot schema (Aurora PostgreSQL Serverless v2).
-- Apply once via: node src/cli.mjs migrate
-- (or paste into the Data API / query editor directly — see infra/aurora-setup.md).
--
-- Design notes:
--   - queues / queue_items / item_artifacts are written once at ingest time
--     (cli.mjs ingest) and are read-only from the daemon's perspective,
--     except queue_items.status, which the daemon flips as the user reviews.
--   - sessions is keyed by chat_id directly — one row per Telegram chat this
--     bot serves (in practice exactly one, since TELEGRAM_CHAT_ID is a single
--     allowlisted user). telegram_offset/last_update_id persist across a
--     queue switch (they belong to the Telegram conversation, not the
--     review); cursor/status/stats reset on switch (see repo.switchQueue).
--   - session_notes / session_history are append-only audit logs. Nothing in
--     the running bot reads old rows back on restart — see
--     protocol/core.mjs's header comment for why state.notes / item.history
--     don't need to be rehydrated from these tables. They exist for a human
--     to query directly (psql / Data API ad hoc), not for the app itself.
--   - lease_owner / lease_expires_at guard against two daemon instances (a
--     brief overlap during a redeploy, or a mistaken second `docker compose
--     up`) both long-polling the same bot token — which Telegram would answer
--     with HTTP 409 for one of them anyway. The lease turns that into an
--     immediate, loud startup failure instead of a silent retry loop.

CREATE TABLE IF NOT EXISTS queues (
  id           TEXT PRIMARY KEY,
  title        TEXT NOT NULL,
  source       TEXT,
  item_count   INTEGER NOT NULL,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  ingested_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS queue_items (
  queue_id           TEXT NOT NULL REFERENCES queues(id) ON DELETE CASCADE,
  n                  INTEGER NOT NULL,
  item_id            TEXT NOT NULL,
  report_num         TEXT,
  company            TEXT NOT NULL,
  role               TEXT NOT NULL,
  url                TEXT NOT NULL DEFAULT '',
  score              TEXT NOT NULL DEFAULT '',
  location           TEXT NOT NULL DEFAULT '',
  salary             TEXT NOT NULL DEFAULT '',
  legitimacy         TEXT NOT NULL DEFAULT '',
  summary            JSONB NOT NULL DEFAULT '{}',
  contacts           JSONB NOT NULL DEFAULT '[]',
  can_send_cv        BOOLEAN NOT NULL DEFAULT true,
  can_send_contacts  BOOLEAN NOT NULL DEFAULT true,
  status             TEXT NOT NULL DEFAULT 'pending',
  PRIMARY KEY (queue_id, n)
);

CREATE TABLE IF NOT EXISTS item_artifacts (
  queue_id   TEXT NOT NULL,
  n          INTEGER NOT NULL,
  kind       TEXT NOT NULL,
  s3_key     TEXT NOT NULL,
  byte_size  BIGINT,
  checksum   TEXT,
  PRIMARY KEY (queue_id, n, kind),
  FOREIGN KEY (queue_id, n) REFERENCES queue_items(queue_id, n) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS sessions (
  chat_id           TEXT PRIMARY KEY,
  queue_id          TEXT REFERENCES queues(id),
  status            TEXT NOT NULL DEFAULT 'idle',
  cursor            INTEGER NOT NULL DEFAULT 1,
  telegram_offset   BIGINT NOT NULL DEFAULT 0,
  last_update_id    BIGINT,
  last_message_id   BIGINT,
  stats             JSONB NOT NULL DEFAULT '{"cvs_sent":0,"notes":0,"reviewed":0,"skipped":0}',
  lease_owner       TEXT,
  lease_expires_at  TIMESTAMPTZ,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS session_notes (
  id          BIGSERIAL PRIMARY KEY,
  chat_id     TEXT NOT NULL REFERENCES sessions(chat_id) ON DELETE CASCADE,
  queue_id    TEXT,
  item_id     TEXT,
  n           INTEGER,
  text        TEXT NOT NULL,
  source      TEXT NOT NULL DEFAULT 'telegram',
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS session_history (
  id          BIGSERIAL PRIMARY KEY,
  chat_id     TEXT NOT NULL REFERENCES sessions(chat_id) ON DELETE CASCADE,
  queue_id    TEXT,
  item_id     TEXT,
  n           INTEGER,
  action      TEXT NOT NULL,
  text        TEXT,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_queue_items_queue ON queue_items(queue_id);
CREATE INDEX IF NOT EXISTS idx_item_artifacts_queue ON item_artifacts(queue_id, n);
CREATE INDEX IF NOT EXISTS idx_session_notes_chat ON session_notes(chat_id, created_at);
CREATE INDEX IF NOT EXISTS idx_session_history_chat ON session_history(chat_id, created_at);
