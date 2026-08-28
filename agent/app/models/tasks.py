"""Task models — the durable unit of work between a Telegram request (or the
scheduler) and the LangGraph supervisor. See db/task_repo.py for persistence
and workers/task_worker.py for how a task gets claimed and run.

Every task payload shape is a discriminated union on `kind`, not a bare
dict — this is what lets routing, the graph, and the DB gate all agree on
what a given task actually means without re-validating a dictionary by hand
at each layer (see the plan's "make invalid states hard to represent" rule).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import StrEnum
from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field


class TaskKind(StrEnum):
    SCAN_ROLE = "scan_role"
    AUGMENT_RESUME = "augment_resume"
    RESEARCH_COMPANY = "research_company"


class TaskSource(StrEnum):
    """Which pool claims this task — see workers/concurrency.py. A live
    Telegram request is always `USER`; periodic scans are always
    `SCHEDULER`. Keeping the two pools distinct is what stops a scheduled
    scan from ever delaying a user-requested task."""

    USER = "user"
    SCHEDULER = "scheduler"


class TaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ScanRolePayload(BaseModel):
    kind: Literal[TaskKind.SCAN_ROLE] = TaskKind.SCAN_ROLE
    role_id: str
    query: str
    max_results: int = Field(default=25, ge=1, le=100)


class AugmentResumePayload(BaseModel):
    """Tailors the ONE canonical `cv.md` living in career-ops-modified (never
    a cloud key — read locally by tools/resume_tool.py, never uploaded) for
    a specific job description via career-ops's `pdf` mode. `job_description`
    is required because that mode's pipeline is JD-driven throughout
    (keyword extraction, skill-gap classification, bullet reordering) — a
    bare target role name isn't enough input for it to run. `notes` is
    optional supplementary guidance forwarded into the run's prompt (e.g.
    "lead with the teaching track"); it does not replace career-ops's own
    fixed pipeline the way free-form `instructions` used to."""

    kind: Literal[TaskKind.AUGMENT_RESUME] = TaskKind.AUGMENT_RESUME
    target_role: str
    job_description: str = Field(min_length=1, max_length=8000)
    notes: str | None = Field(default=None, max_length=2000)


class ResearchCompanyPayload(BaseModel):
    kind: Literal[TaskKind.RESEARCH_COMPANY] = TaskKind.RESEARCH_COMPANY
    query: str = Field(min_length=1, max_length=4000)
    role_id: str | None = None


TaskPayload = Annotated[
    Union[ScanRolePayload, AugmentResumePayload, ResearchCompanyPayload],
    Field(discriminator="kind"),
]


def new_task_id() -> str:
    return f"task-{uuid.uuid4().hex[:12]}"


class AgentTask(BaseModel):
    """One row of `agent_tasks`. `priority` is set once at creation from
    `source` (see TaskSubmission.to_task) and never renegotiated — the
    claim query in db/task_repo.py orders by it directly."""

    id: str = Field(default_factory=new_task_id)
    chat_id: str
    kind: TaskKind
    source: TaskSource
    priority: int
    status: TaskStatus = TaskStatus.PENDING
    payload: TaskPayload
    idempotency_key: str
    error: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    started_at: datetime | None = None
    finished_at: datetime | None = None


# Higher runs first — user requests should never wait behind a background scan.
_PRIORITY_BY_SOURCE: dict[TaskSource, int] = {TaskSource.USER: 100, TaskSource.SCHEDULER: 10}


class TaskSubmission(BaseModel):
    """What a caller (a deterministic route handler, or the scheduler)
    provides to create a task — everything AgentTask needs except the id,
    status, and timestamps, which are assigned at creation time."""

    chat_id: str
    source: TaskSource
    payload: TaskPayload
    idempotency_key: str

    def to_task(self) -> AgentTask:
        return AgentTask(
            chat_id=self.chat_id,
            kind=self.payload.kind,
            source=self.source,
            priority=_PRIORITY_BY_SOURCE[self.source],
            payload=self.payload,
            idempotency_key=self.idempotency_key,
        )
