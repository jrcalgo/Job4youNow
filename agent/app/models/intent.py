"""Intent models — the output of routing/intent_router.py. An IntentDecision
is the one and only thing the API layer inspects to decide whether to answer
immediately (deterministic) or create a task (predictive); see
agent/app/routing/intent_router.py.
"""
from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel

from agent.app.models.tasks import TaskKind, TaskPayload
from agent.app.models.telegram import TelegramCallback


class RouteKind(StrEnum):
    """Deterministic routes — plain code, no LLM, no DB write. Anything not
    in this enum is predictive and becomes a task instead."""

    BACKLOG = "backlog"
    JOB_LIST = "job_list"
    TASK_STATUS = "task_status"
    SCHEDULE_MENU = "schedule_menu"
    HELP = "help"
    MAIN_MENU = "main_menu"
    SETTINGS_MENU = "settings_menu"
    BACKLOG_FILTER_MENU = "backlog_filter_menu"
    BACKLOG_BROWSER = "backlog_browser"
    # One route for the whole model-configuration wizard rather than one
    # per screen — the specific screen is resolved from `callback` below
    # (its action/value), inside routing/model_config.py. Keeps this enum
    # from growing by one entry per wizard screen.
    MODEL_CONFIG = "model_config"


class DeterministicQuery(BaseModel):
    """Deterministic routes only ever need "which role" or "which task" —
    one small shared shape instead of a payload type per route. If a future
    deterministic route needs more than this, give IT its own payload rather
    than growing this one into a catch-all."""

    role_id: str | None = None
    task_id: str | None = None


class IntentDecision(BaseModel):
    chat_id: str
    is_deterministic: bool
    route: RouteKind | None = None
    query: DeterministicQuery | None = None
    task_kind: TaskKind | None = None
    task_payload: TaskPayload | None = None
    # Only set for RouteKind.MODEL_CONFIG — the wizard's screen logic reads
    # the raw action/value itself rather than this model growing a field
    # per screen (see routing/model_config.py).
    callback: TelegramCallback | None = None

    @classmethod
    def for_route(
        cls,
        chat_id: str,
        route: RouteKind,
        query: DeterministicQuery | None = None,
        *,
        callback: TelegramCallback | None = None,
    ) -> "IntentDecision":
        return cls(chat_id=chat_id, is_deterministic=True, route=route, query=query or DeterministicQuery(), callback=callback)

    @classmethod
    def for_task(cls, chat_id: str, payload: TaskPayload) -> "IntentDecision":
        return cls(chat_id=chat_id, is_deterministic=False, task_kind=payload.kind, task_payload=payload)
