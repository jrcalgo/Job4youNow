"""Telegram outbox — messages formatting/chunking.py has already prepared
(and, for private content, agent/app/delivery.py has already materialized),
waiting for the (thin) Telegram adapter to deliver via `GET
/telegram/outbox` + `POST /telegram/outbox/{id}/delivered`.

`TelegramOutboundMessage` (models/telegram.py) already enforces that only
`PUBLIC_TEXT` carries inline text and every other kind carries an artifact
pointer — this module trusts that invariant rather than re-checking it, and
simply stores the whole validated message under whichever column matches
its `delivery_kind`. There is no way to construct an invalid message that
reaches this far; the model's own validator is the single enforcement point.
"""
from __future__ import annotations

import uuid

from agent.app.db.aurora_client import AuroraClient
from agent.app.models.telegram import DeliveryKind, TelegramOutboundMessage


def _new_outbox_id() -> str:
    return f"outbox-{uuid.uuid4().hex[:12]}"


def _row_from_message(message: TelegramOutboundMessage, *, id_: str, task_id: str | None) -> dict:
    is_public = message.delivery_kind == DeliveryKind.PUBLIC_TEXT
    serialized = message.model_dump(mode="json")
    return {
        "id": id_,
        "chat_id": message.chat_id,
        "task_id": task_id,
        "delivery_kind": message.delivery_kind.value,
        "public_payload": serialized if is_public else None,
        "artifact_ref": None if is_public else serialized,
    }


async def enqueue_messages(
    client: AuroraClient,
    messages: list[TelegramOutboundMessage],
    *,
    task_id: str | None = None,
    transaction_id: str | None = None,
) -> None:
    if not messages:
        return
    rows = [_row_from_message(message, id_=_new_outbox_id(), task_id=task_id) for message in messages]
    await client.exec_batch(
        """
        INSERT INTO telegram_outbox (id, chat_id, task_id, delivery_kind, public_payload, artifact_ref)
        VALUES (:id, :chat_id, :task_id, :delivery_kind, :public_payload::jsonb, :artifact_ref::jsonb)
        """,
        rows,
        transaction_id=transaction_id,
    )


async def list_pending(client: AuroraClient, *, limit: int = 50) -> list[dict]:
    result = await client.exec(
        """
        SELECT id, chat_id, delivery_kind, public_payload, artifact_ref
          FROM telegram_outbox
         WHERE status = 'pending'
         ORDER BY created_at ASC
         LIMIT :limit
        """,
        {"limit": limit},
    )
    return result.rows


async def mark_delivered(client: AuroraClient, outbox_id: str) -> None:
    await client.exec(
        "UPDATE telegram_outbox SET status = 'delivered', delivered_at = now() WHERE id = :id",
        {"id": outbox_id},
    )
