"""Periodic scan schedule models — see db/schedule_repo.py for persistence
and workers/scheduler.py for the loop that turns a due schedule into a
`TaskSubmission(source=SCHEDULER, ...)`.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from pydantic import BaseModel, Field


def new_schedule_id() -> str:
    return f"sched-{uuid.uuid4().hex[:12]}"


class ScanSchedule(BaseModel):
    id: str = Field(default_factory=new_schedule_id)
    chat_id: str
    role_id: str
    query: str
    interval_seconds: int = Field(ge=300)
    next_run_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    enabled: bool = True


class ScheduleCreateRequest(BaseModel):
    """Request body for `POST /schedules`."""

    chat_id: str
    role_id: str
    query: str
    interval_seconds: int = Field(ge=300, description="Minimum 5 minutes between scans for one role.")
