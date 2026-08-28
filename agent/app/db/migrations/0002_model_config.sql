-- db/migrations/0002_model_config.sql — per-(chat, task kind) Cursor SDK
-- model configuration, chosen via the Telegram MODELS menu (see
-- routing/model_config.py). Additive, separate from 0001 — this is a
-- distinct, later feature; keep migrations incremental going forward
-- rather than growing one file indefinitely.
--
-- Design notes:
--   - Operator configuration, not LLM-derived domain data — no DB gate,
--     no idempotency-key dance. db/model_config_repo.py writes here
--     directly, the same way db/schedule_repo.py already does for
--     scan_schedules.
--   - `params` mirrors cursor_sdk.ModelSelection.params exactly: a JSON
--     array of `{"id": ..., "value": ...}`, applied as-is when
--     tools/cursor_sdk_tool.py builds a ModelSelection for a run.
--   - No row for a given (chat_id, task_kind) means "use the default
--     model" (see config.py's DEFAULT_MODEL_ID) — there is deliberately
--     no separate "is this the default" flag to keep in sync.

CREATE TABLE IF NOT EXISTS agent_model_config (
  chat_id             TEXT NOT NULL,
  task_kind           TEXT NOT NULL,
  model_id            TEXT NOT NULL,
  model_display_name  TEXT NOT NULL,
  params              JSONB NOT NULL DEFAULT '[]',
  updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (chat_id, task_kind)
);
