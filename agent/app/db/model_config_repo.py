"""Per-(chat, task kind) Cursor SDK model configuration — read by
tools/cursor_sdk_tool.py before every run, written by
routing/model_config.py's wizard. Plain deterministic reads/writes, same
style as db/schedule_repo.py — this is operator configuration, not
LLM-derived domain data, so it never goes through db/db_gate.py.
"""
from __future__ import annotations

from agent.app.db.aurora_client import AuroraClient
from agent.app.models.model_catalog import ModelConfig
from agent.app.models.tasks import TaskKind


def _row_to_config(row: dict) -> ModelConfig:
    return ModelConfig.model_validate(row)


async def get_config(client: AuroraClient, chat_id: str, task_kind: TaskKind) -> ModelConfig | None:
    result = await client.exec(
        "SELECT chat_id, task_kind, model_id, model_display_name, params FROM agent_model_config"
        " WHERE chat_id = :chat_id AND task_kind = :task_kind",
        {"chat_id": chat_id, "task_kind": task_kind.value},
    )
    return _row_to_config(result.rows[0]) if result.rows else None


async def list_configs(client: AuroraClient, chat_id: str) -> dict[TaskKind, ModelConfig]:
    """Used by the `MODELS` entry screen to show what's currently
    configured (or defaulted) per task kind before the user picks one to
    change."""
    result = await client.exec(
        "SELECT chat_id, task_kind, model_id, model_display_name, params FROM agent_model_config WHERE chat_id = :chat_id",
        {"chat_id": chat_id},
    )
    configs = [_row_to_config(row) for row in result.rows]
    return {config.task_kind: config for config in configs}


async def upsert_config(client: AuroraClient, config: ModelConfig) -> None:
    await client.exec(
        """
        INSERT INTO agent_model_config (chat_id, task_kind, model_id, model_display_name, params)
        VALUES (:chat_id, :task_kind, :model_id, :model_display_name, :params::jsonb)
        ON CONFLICT (chat_id, task_kind) DO UPDATE SET
          model_id = EXCLUDED.model_id,
          model_display_name = EXCLUDED.model_display_name,
          params = EXCLUDED.params,
          updated_at = now()
        """,
        {
            "chat_id": config.chat_id,
            "task_kind": config.task_kind.value,
            "model_id": config.model_id,
            "model_display_name": config.model_display_name,
            "params": [p.model_dump(mode="json") for p in config.params],
        },
    )
