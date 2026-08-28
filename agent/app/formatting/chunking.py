"""Splits a UserFacingResponse into Telegram-safe TelegramOutboundMessage
rows. `chunk_text` mirrors telegram/src/protocol/core.mjs's chunkMessage:
same 4096-char limit, same preference for breaking on paragraph/bullet
boundaries over a mid-word cut.

By the time `to_outbound_messages` runs, a PRIVATE_USER response must
already be materialized (see formatting/delivery.py's
materialize_private_response, called first by agent/app/delivery.py) — this
function only decides HOW to lay out already-safe content, never whether
content is safe to send inline.
"""
from __future__ import annotations

from agent.app.models.artifacts import ContentVisibility
from agent.app.models.responses import UserFacingResponse
from agent.app.models.telegram import DeliveryKind, DeliveryTarget, TelegramOutboundMessage

TELEGRAM_MAX_MESSAGE_LENGTH = 4096


def chunk_text(text: str, *, limit: int = TELEGRAM_MAX_MESSAGE_LENGTH, label_prefix: str | None = None) -> list[str]:
    raw = text.strip()
    if not raw:
        return [""]
    if len(raw) <= limit:
        return [raw]

    parts: list[str] = []
    rest = raw
    while len(rest) > limit:
        window = rest[:limit]
        cut = max(
            window.rfind("\n\n"),
            window.rfind("\n• "),
            window.rfind("\n- "),
            window.rfind("\n"),
            window.rfind(" "),
        )
        if cut < int(limit * 0.4):
            cut = limit
        parts.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
    if rest:
        parts.append(rest)

    if not label_prefix or len(parts) <= 1:
        return parts
    total = len(parts)
    return [f"{label_prefix} {index + 1}/{total}\n\n{part}" for index, part in enumerate(parts)]


def to_outbound_messages(response: UserFacingResponse, chat_id: str) -> list[TelegramOutboundMessage]:
    """A PRIVATE_USER response delivers as exactly one PRIVATE_ARTIFACT
    message — never chunked text, since by this point its content lives
    only in `response.private_artifact` (see this module's docstring).
    A PUBLIC_JOB_SEARCH response chunks its inline text as before, with
    buttons attached only to the last chunk — mirrors
    telegram/src/telegram/effects.mjs's sendMessage chunking convention, so
    a long message doesn't show an inline keyboard on every page of itself.
    """
    if response.visibility == ContentVisibility.PRIVATE_USER:
        artifact = response.private_artifact
        if artifact is None:
            raise ValueError("private response reached chunking unmaterialized — see agent/app/delivery.py")
        return [
            TelegramOutboundMessage(
                chat_id=chat_id,
                delivery_kind=DeliveryKind.PRIVATE_ARTIFACT,
                artifact=artifact.location,
                caption=response.title or "Private result",
                buttons=response.buttons,
                delivery_target=DeliveryTarget.NEW_MESSAGE,
            )
        ]

    body = response.body or ""
    full_text = body if not response.title else f"<b>{response.title}</b>\n\n{body}"
    chunks = chunk_text(full_text, label_prefix=response.label_prefix)

    if len(chunks) > 1:
        messages = [
            TelegramOutboundMessage(
                chat_id=chat_id,
                delivery_kind=DeliveryKind.PUBLIC_TEXT,
                text=chunk,
                parse_mode=response.parse_mode,
                delivery_target=DeliveryTarget.NEW_MESSAGE,
            )
            for chunk in chunks
        ]
    else:
        messages = [
            TelegramOutboundMessage(
                chat_id=chat_id,
                delivery_kind=DeliveryKind.PUBLIC_TEXT,
                text=chunks[0],
                parse_mode=response.parse_mode,
                delivery_target=DeliveryTarget.HUB,
            )
        ]
    if messages:
        messages[-1].buttons = response.buttons
    return messages
