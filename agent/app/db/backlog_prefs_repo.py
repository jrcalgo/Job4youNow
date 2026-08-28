"""Per-chat backlog filter preferences."""
from __future__ import annotations

import json

from agent.app.db.aurora_client import AuroraClient
from agent.app.models.backlog import BacklogFilters, BacklogPrefs


def _row_to_prefs(row: dict) -> BacklogPrefs:
    filters_raw = row.get("filters") or {}
    if isinstance(filters_raw, str):
        filters_raw = json.loads(filters_raw)
    return BacklogPrefs(
        filters=BacklogFilters.model_validate(filters_raw),
        sort_key=row.get("sort_key") or "retrieved_at",
        sort_dir=row.get("sort_dir") or "desc",
    )


async def get_prefs(client: AuroraClient, chat_id: str) -> BacklogPrefs:
    result = await client.exec(
        "SELECT filters, sort_key, sort_dir FROM chat_backlog_prefs WHERE chat_id = :chat_id",
        {"chat_id": chat_id},
    )
    if not result.rows:
        return BacklogPrefs()
    return _row_to_prefs(result.rows[0])


async def upsert_prefs(client: AuroraClient, chat_id: str, prefs: BacklogPrefs) -> BacklogPrefs:
    await client.exec(
        """
        INSERT INTO chat_backlog_prefs (chat_id, filters, sort_key, sort_dir)
        VALUES (:chat_id, :filters::jsonb, :sort_key, :sort_dir)
        ON CONFLICT (chat_id) DO UPDATE
          SET filters = EXCLUDED.filters,
              sort_key = EXCLUDED.sort_key,
              sort_dir = EXCLUDED.sort_dir,
              updated_at = now()
        """,
        {
            "chat_id": chat_id,
            "filters": prefs.filters.model_dump(mode="json"),
            "sort_key": prefs.sort_key,
            "sort_dir": prefs.sort_dir,
        },
    )
    return prefs
