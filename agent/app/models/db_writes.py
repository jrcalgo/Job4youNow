"""DB write models — the ONLY shapes db/db_gate.py will accept. Nothing
downstream of a LangGraph tool call reaches Aurora directly; a tool's typed
result (models/tool_results.py) gets turned into one of these by
graph/nodes.py's validate_output step, and the DB gate persists exactly this
and nothing else. This is what keeps raw LLM text from ever becoming a row.

Per the product scope: Aurora stores job listings, companies, and contacts
per role (public job-search data), plus ArtifactLocation pointers for
private agent-generated output (resumes, private responses/reports) — never
private content itself, and never free-form LLM prose. A user's `cv.md` is
their own local data (career-ops-modified), read directly by
tools/resume_tool.py, not written here.
"""
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field

from agent.app.models.artifacts import PrivateArtifactMetadata


class ContactWrite(BaseModel):
    """Usable two ways: nested inside a JobListingWrite (company is implied
    by the parent, `company_name` stays None), or as a standalone top-level
    write to add a contact to an existing company (`company_name` required —
    db_gate.py skips, rather than fails, a standalone contact missing one)."""

    kind: Literal["contact"] = "contact"
    company_name: str | None = None
    name: str | None = None
    role_title: str | None = None
    email: str | None = None
    phone: str | None = None


class CompanyWrite(BaseModel):
    kind: Literal["company"] = "company"
    name: str


class JobListingWrite(BaseModel):
    kind: Literal["job_listing"] = "job_listing"
    role_id: str
    company_name: str
    title: str
    url: str | None = None
    location: str | None = None
    posted_at: datetime | None = None
    summary: list[str] = Field(default_factory=list)
    contacts: list[ContactWrite] = Field(default_factory=list)


class PrivateArtifactWrite(BaseModel):
    """Provenance record for user-private agent output (an augmented
    resume, a private text response, a private report) — never the content
    itself. One generic write kind for every private-output flavor, backed
    by the single `private_artifacts` table (see
    db/migrations/0001_agent_core.sql), rather than a table per kind.

    Wraps the SAME `PrivateArtifactMetadata` instance the caller also
    attaches to a `UserFacingResponse` — one object, built once, used both
    as the DB write payload and the response pointer. See
    models/artifacts.py's docstring on why the id is generated client-side."""

    kind: Literal["private_artifact"] = "private_artifact"
    metadata: PrivateArtifactMetadata


class ScheduleWrite(BaseModel):
    """Lets a predictive task set up recurring scans as a side effect (e.g.
    the supervisor interpreting "scan weekly for backend roles"). The
    deterministic `POST /schedules` route does NOT go through this — it has
    no LLM output to gate, so it calls db/schedule_repo.py directly. Both
    paths share the same insert logic; see db_gate.py's docstring."""

    kind: Literal["schedule"] = "schedule"
    role_id: str
    query: str
    interval_seconds: int = Field(ge=300)


DomainWrite = Annotated[
    Union[JobListingWrite, CompanyWrite, ContactWrite, PrivateArtifactWrite, ScheduleWrite],
    Field(discriminator="kind"),
]


class DbWriteSet(BaseModel):
    """What db_gate.py accepts. `idempotency_key` is normally the source
    task's id — replaying the same task (e.g. after a worker crash mid-run)
    must not double-insert the same listings. `chat_id` is who the writes
    are on behalf of, needed for chat-scoped rows like private_artifacts."""

    idempotency_key: str
    source_task_id: str
    chat_id: str
    writes: list[DomainWrite] = Field(default_factory=list)


class DbWriteReceipt(BaseModel):
    """What db_gate.py returns — enough for cache invalidation and for the
    outbox message to say something concrete ("3 new listings for backend")
    instead of a generic "done"."""

    idempotency_key: str
    applied: bool
    counts: dict[str, int] = Field(default_factory=dict)
    affected_role_ids: list[str] = Field(default_factory=list)

    @classmethod
    def noop(cls, idempotency_key: str) -> "DbWriteReceipt":
        return cls(idempotency_key=idempotency_key, applied=False)
