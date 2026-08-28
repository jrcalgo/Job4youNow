"""The one model every Telegram-facing output must go through. Presenters
(formatting/presenters.py) are the only code that constructs these — graph
nodes, tools, and repositories return data, never a UserFacingResponse.

Every response carries an explicit `visibility` (see models/artifacts.py) —
never inferred later from content. When `visibility` is PRIVATE_USER,
`body` is only a STAGING value: formatting/delivery.py's
materialize_private_response() writes it to the private artifact store and
replaces it with `private_artifact` before anything downstream persists
this model to Aurora or hands it to chunking. Nothing after that step is
allowed to see private `body` text again — see db/outbox_repo.py and
db/task_repo.py, which reject a PRIVATE_USER response that still has one.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

from agent.app.models.artifacts import ContentVisibility, PrivateArtifactMetadata
from agent.app.models.telegram import TelegramInlineButton


class UserFacingResponse(BaseModel):
    visibility: ContentVisibility
    title: str | None = None
    body: str | None = None
    private_artifact: PrivateArtifactMetadata | None = None
    buttons: list[list[TelegramInlineButton]] = Field(default_factory=list)
    parse_mode: Literal["HTML"] = "HTML"
    label_prefix: str | None = None

    @model_validator(mode="after")
    def _require_body_or_private_artifact(self) -> "UserFacingResponse":
        if self.body is None and self.private_artifact is None:
            raise ValueError("UserFacingResponse requires body or private_artifact")
        return self

    @property
    def is_materialized(self) -> bool:
        """True once private content has been written to storage and this
        response only carries a pointer — see formatting/delivery.py."""
        return self.visibility == ContentVisibility.PRIVATE_USER and self.private_artifact is not None
