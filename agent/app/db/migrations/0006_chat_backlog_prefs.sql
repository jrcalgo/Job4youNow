-- Per-chat backlog filter preferences for Telegram browser.
CREATE TABLE IF NOT EXISTS chat_backlog_prefs (
  chat_id    TEXT PRIMARY KEY,
  filters    JSONB NOT NULL DEFAULT '{}',
  sort_key   TEXT NOT NULL DEFAULT 'retrieved_at',
  sort_dir   TEXT NOT NULL DEFAULT 'desc',
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
