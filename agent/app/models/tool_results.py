"""Tool result models — everything a LangGraph tool node can hand back to the
supervisor. Every tool in agent/app/tools/ returns one of these, never a raw
dict or a bare string, so graph/nodes.py can branch on `ok` and route to
`db_gate`/`format_response` without re-parsing tool-specific shapes.
"""
from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field

from agent.app.models.artifacts import ArtifactLocation


class ToolName(StrEnum):
    CURSOR_SDK = "cursor_sdk"
    CAREER_OPS = "career_ops"
    SCAN = "scan"
    RESUME = "resume"
    DB_READ = "db_read"


class CursorRunResult(BaseModel):
    """Normalized outcome of one Cursor SDK run — see
    tools/cursor_sdk_tool.py. Distinguishes a run that never started
    (`ok=False, retryable=...`) from one that started and failed
    (`ok=False, retryable=False, status="error"`), matching the SDK's own
    "two kinds of failure" distinction so callers don't have to know about
    `CursorAgentError` at all.
    """

    ok: bool
    status: str
    agent_id: str | None = None
    run_id: str | None = None
    model: str | None = None
    text: str | None = None
    error: str | None = None
    retryable: bool = False

    @classmethod
    def from_sdk_result(cls, *, agent_id: str, run_id: str, model: str, status: str, text: str | None) -> "CursorRunResult":
        return cls(ok=status == "finished", status=status, agent_id=agent_id, run_id=run_id, model=model, text=text)

    @classmethod
    def startup_error(cls, *, message: str, retryable: bool) -> "CursorRunResult":
        return cls(ok=False, status="startup_error", error=message, retryable=retryable)


class ScannedListing(BaseModel):
    """Mirrors what career-ops's `scan` mode actually records per offer in
    `data/scan-history.tsv` (see tools/scan_tool.py) — title/url/company
    plus location and the ATS-reported posting date when available. No
    `contacts` field: career-ops never extracts contacts at scan time
    (that's the separate, not-yet-integrated `contacto` mode), and no
    `summary` either — scan is discovery only, not evaluation."""

    company_name: str
    title: str
    url: str | None = None
    location: str | None = None
    posted_at: datetime | None = None
    summary: list[str] = Field(default_factory=list)


class ScanResult(BaseModel):
    role_id: str
    listings: list[ScannedListing] = Field(default_factory=list)


class ResumeResult(BaseModel):
    """`artifact` points at the private-bucket + local-backup location of
    the tailored resume's EXTRACTED TEXT — never the rendered PDF's own
    bytes (see tools/resume_tool.py's `_extract_pdf_text`; private user
    data is only ever delivered to Telegram as chat text, never a
    downloadable file). `summary_text` is a short, non-sensitive caption,
    never resume content itself. `skill_gaps` surfaces the mode's own
    zero-LLM skill-gap classifier's `gap` bucket — JD requirements
    `cv.md` has no trace of — so the user is told what's missing rather
    than having it silently invented (the mode's pipeline explicitly
    forbids that)."""

    artifact: ArtifactLocation
    summary_text: str
    skill_gaps: list[str] = Field(default_factory=list)


class ToolResult(BaseModel):
    """Envelope every tool call produces, wrapping the tool-specific payload
    above. `summary` is a short, human-readable one-liner nodes can log or
    fold into a UserFacingResponse without re-deriving it from `data`."""

    tool: ToolName
    ok: bool
    summary: str
    cursor_run: CursorRunResult | None = None
    scan: ScanResult | None = None
    resume: ResumeResult | None = None
