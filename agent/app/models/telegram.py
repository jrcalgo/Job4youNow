"""Telegram transport models — the shapes that cross the wire between the
Telegram adapter and this app's API. Nothing here knows about intents,
tasks, or the DB; see models/intent.py and models/responses.py for what an
update turns into after routing.
"""
from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from agent.app.models.artifacts import ArtifactLocation


class TelegramInlineButton(BaseModel):
    """One inline-keyboard button. Defined once here (a Telegram transport
    concept) and reused by UserFacingResponse in models/responses.py — do not
    redefine this shape a second time for the "business" response model."""

    text: str
    callback_data: str = Field(max_length=64)


class TelegramCallback(BaseModel):
    """A parsed `action:value` callback_data payload, e.g. "role:backend"
    becomes action="role", value="backend". Parsing lives in
    routing/deterministic.py; this model is just the parsed result."""

    action: str
    value: str | None = None


class TelegramCommand(BaseModel):
    """One normalized Telegram update, as forwarded by the (thin) Telegram
    adapter to `POST /telegram/update`. The adapter does no interpretation —
    it only extracts these fields from the raw Bot API update."""

    chat_id: str
    update_id: int
    message_id: int | None = None
    hub_message_id: int | None = None
    text: str | None = None
    callback: TelegramCallback | None = None


class TelegramUpdateEnvelope(BaseModel):
    """Request body for `POST /telegram/update`."""

    command: TelegramCommand


class DeliveryKind(StrEnum):
    """How the (thin) Telegram adapter should deliver one outbound message.
    JOB_ARTIFACT and PRIVATE_ARTIFACT differ only in which S3 bucket the
    adapter fetches from — see telegram/src/artifacts/store.mjs's bucket
    lookup — never in how the adapter constructs the caption, which is
    always pre-sanitized metadata from the agent app, never raw content."""

    PUBLIC_TEXT = "public_text"
    JOB_ARTIFACT = "job_artifact"
    PRIVATE_ARTIFACT = "private_artifact"


class DeliveryTarget(StrEnum):
    """Where the Telegram adapter should render a public_text message.
    HUB edits the persisted menu message; NEW_MESSAGE always sends fresh."""

    HUB = "hub"
    NEW_MESSAGE = "new_message"


class TelegramOutboundMessage(BaseModel):
    """One ready-to-send Telegram message, as stored in `telegram_outbox` and
    fetched by the adapter. Produced by formatting/chunking.py from a
    UserFacingResponse — never constructed by hand elsewhere, so every
    outbound message is guaranteed to have already passed through escaping,
    length-chunking, and the public/private classification below.

    `text` is only ever public content: PUBLIC_TEXT is the only kind that
    carries text, and this class enforces that no artifact delivery can
    carry a body — an artifact's `caption` is the only text alongside it."""

    chat_id: str
    delivery_kind: DeliveryKind
    text: str | None = None
    artifact: ArtifactLocation | None = None
    caption: str | None = None
    parse_mode: Literal["HTML"] = "HTML"
    buttons: list[list[TelegramInlineButton]] = Field(default_factory=list)
    delivery_target: DeliveryTarget = DeliveryTarget.HUB

    @model_validator(mode="after")
    def _require_content_matching_delivery_kind(self) -> "TelegramOutboundMessage":
        if self.delivery_kind == DeliveryKind.PUBLIC_TEXT:
            if self.text is None:
                raise ValueError("public_text delivery requires text")
        elif self.artifact is None:
            raise ValueError(f"{self.delivery_kind} delivery requires an artifact")
        return self


class TelegramUpdateResult(BaseModel):
    """Response body for `POST /telegram/update` — either an immediate reply
    (deterministic route) or an acknowledgement that a task was queued
    (predictive route); the adapter polls `GET /telegram/outbox` for the
    eventual task result either way."""

    accepted: bool
    task_id: str | None = None
    immediate_messages: list[TelegramOutboundMessage] = Field(default_factory=list)
    hub_message_id: int | None = None
