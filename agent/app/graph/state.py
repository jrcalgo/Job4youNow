"""Graph state — deliberately small and flat (see the plan's "keep graph
state minimal and inspectable"). Every node returns a partial dict that
LangGraph merges into this shape; see graph/nodes.py.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from agent.app.models.artifacts import PrivateArtifactMetadata
from agent.app.models.db_writes import DbWriteReceipt, DbWriteSet
from agent.app.models.responses import UserFacingResponse
from agent.app.models.tasks import AgentTask
from agent.app.models.tool_results import ToolName, ToolResult


class GraphState(BaseModel):
    task: AgentTask
    selected_tool: ToolName | None = None
    tool_results: list[ToolResult] = Field(default_factory=list)
    proposed_writes: DbWriteSet | None = None
    write_receipt: DbWriteReceipt | None = None
    response: UserFacingResponse | None = None

    # Set by validate_output when a tool already produced a private
    # artifact (e.g. an augmented resume) — the SAME object it wraps into
    # `proposed_writes`, so format_response can hand it straight to
    # presenters.resume_result without rebuilding it. See
    # graph/nodes.py's _resume_result_to_writes.
    private_artifact: PrivateArtifactMetadata | None = None

    # Scratch space `load_context` fills in for `run_tool` to use (e.g.
    # companies already known for a role, so a scan doesn't re-suggest
    # them). Deliberately the only untyped field in this model — it never
    # crosses a boundary outside the graph, unlike everything else here.
    context: dict[str, Any] = Field(default_factory=dict)

    @property
    def last_tool_result(self) -> ToolResult | None:
        return self.tool_results[-1] if self.tool_results else None
