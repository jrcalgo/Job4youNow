"""Telegram transport state — offsets and last-handled-update-id. This is
the ONLY Telegram-related state the agent app persists on the adapter's
behalf; everything else (which command means what) is derived fresh from
each update, never stored.
"""
from __future__ import annotations

from agent.app.db.aurora_client import AuroraClient


async def get_offset(client: AuroraClient, chat_id: str) -> int:
    result = await client.exec(
        "SELECT offset_value FROM telegram_transport_state WHERE chat_id = :chat_id",
        {"chat_id": chat_id},
    )
    return int(result.rows[0]["offset_value"]) if result.rows else 0


async def advance_offset(client: AuroraClient, chat_id: str, *, offset: int, last_update_id: int) -> None:
    await client.exec(
        """
        INSERT INTO telegram_transport_state (chat_id, offset_value, last_update_id)
        VALUES (:chat_id, :offset, :last_update_id)
        ON CONFLICT (chat_id) DO UPDATE
          SET offset_value = EXCLUDED.offset_value,
              last_update_id = EXCLUDED.last_update_id,
              updated_at = now()
        """,
        {"chat_id": chat_id, "offset": offset, "last_update_id": last_update_id},
    )


async def get_hub_message_id(client: AuroraClient, chat_id: str) -> int | None:
    result = await client.exec(
        "SELECT hub_message_id FROM telegram_transport_state WHERE chat_id = :chat_id",
        {"chat_id": chat_id},
    )
    if not result.rows:
        return None
    value = result.rows[0].get("hub_message_id")
    return int(value) if value is not None else None


async def set_hub_message_id(client: AuroraClient, chat_id: str, hub_message_id: int | None) -> None:
    await client.exec(
        """
        INSERT INTO telegram_transport_state (chat_id, offset_value, hub_message_id)
        VALUES (:chat_id, 0, :hub_message_id)
        ON CONFLICT (chat_id) DO UPDATE
          SET hub_message_id = EXCLUDED.hub_message_id,
              updated_at = now()
        """,
        {"chat_id": chat_id, "hub_message_id": hub_message_id},
    )
