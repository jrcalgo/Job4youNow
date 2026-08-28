"""One function per graph step (see graph/supervisor.py for how these wire
together). Each node takes the current GraphState and the shared GraphDeps,
and returns only the fields it changed — LangGraph merges the rest.

`supervisor_decide` picks a tool purely from `task.kind` for this first
predictive path (one task kind maps to exactly one tool today), not an LLM
call — see the plan's "do not add more graph complexity until the first
path is stable". The natural extension point, once a task kind can
legitimately use more than one tool, is to make this node call the Cursor
SDK to choose; nothing else in the graph would need to change.
"""
from __future__ import annotations

from dataclasses import dataclass

from agent.app.db.db_gate import DbGate
from agent.app.formatting.presenters import Presenters
from agent.app.graph.state import GraphState
from agent.app.models.artifacts import PrivateArtifactMetadata
from agent.app.models.db_writes import DbWriteReceipt, DbWriteSet, JobListingWrite, PrivateArtifactWrite
from agent.app.models.tasks import AugmentResumePayload, ScanRolePayload, TaskKind
from agent.app.models.tool_results import ToolName, ToolResult
from agent.app.tools.career_ops_tool import CareerOpsTool
from agent.app.tools.db_read_tool import DbReadTool
from agent.app.tools.resume_tool import ResumeTool
from agent.app.tools.scan_tool import ScanTool

_TOOL_BY_TASK_KIND = {
    TaskKind.SCAN_ROLE: ToolName.SCAN,
    TaskKind.AUGMENT_RESUME: ToolName.RESUME,
    TaskKind.RESEARCH_COMPANY: ToolName.CAREER_OPS,
}


@dataclass
class GraphDeps:
    """Everything a graph run needs, bound once at app startup and passed to
    every node call — see graph/supervisor.py's `run_supervisor_graph`.
    Plain closures over this (not LangGraph's context_schema/Runtime
    machinery) keep node wiring simple and version-independent."""

    scan_tool: ScanTool
    resume_tool: ResumeTool
    career_ops_tool: CareerOpsTool
    db_read_tool: DbReadTool
    db_gate: DbGate
    presenters: Presenters


async def load_context(state: GraphState, deps: GraphDeps) -> dict:
    if isinstance(state.task.payload, ScanRolePayload):
        known_companies = await deps.db_read_tool.known_company_names(state.task.payload.role_id)
        return {"context": {"known_companies": known_companies}}
    return {}


async def supervisor_decide(state: GraphState, deps: GraphDeps) -> dict:
    return {"selected_tool": _TOOL_BY_TASK_KIND.get(state.task.kind)}


async def run_tool(state: GraphState, deps: GraphDeps) -> dict:
    payload = state.task.payload
    result: ToolResult

    if state.selected_tool == ToolName.SCAN and isinstance(payload, ScanRolePayload):
        result = await deps.scan_tool.scan(payload, chat_id=state.task.chat_id, known_companies=state.context.get("known_companies"))
    elif state.selected_tool == ToolName.RESUME and isinstance(payload, AugmentResumePayload):
        result = await deps.resume_tool.augment(payload, chat_id=state.task.chat_id)
    elif state.selected_tool == ToolName.CAREER_OPS:
        result = await deps.career_ops_tool.run(payload.query, chat_id=state.task.chat_id)
    else:
        result = ToolResult(tool=ToolName.CURSOR_SDK, ok=False, summary=f"no tool registered for task kind {state.task.kind}")

    return {"tool_results": [*state.tool_results, result]}


def _scan_result_to_writes(state: GraphState) -> DbWriteSet | None:
    result = state.last_tool_result
    if result is None or result.scan is None or not result.scan.listings:
        return None
    writes = [
        JobListingWrite(
            role_id=result.scan.role_id,
            company_name=listing.company_name,
            title=listing.title,
            url=listing.url,
            location=listing.location,
            posted_at=listing.posted_at,
            summary=listing.summary,
        )
        for listing in result.scan.listings
    ]
    return DbWriteSet(idempotency_key=state.task.id, source_task_id=state.task.id, chat_id=state.task.chat_id, writes=writes)


def _resume_result_to_writes(state: GraphState) -> tuple[DbWriteSet | None, PrivateArtifactMetadata | None]:
    """Builds ONE `PrivateArtifactMetadata` and returns it twice: once
    wrapped in the `DbWriteSet` the DB gate will persist, once bare for
    `GraphState.private_artifact` — see that field's docstring on why
    format_response must reuse this exact object rather than constructing
    a second one from `result.resume.artifact` itself."""
    result = state.last_tool_result
    payload = state.task.payload
    if result is None or result.resume is None or not isinstance(payload, AugmentResumePayload):
        return None, None
    artifact = PrivateArtifactMetadata(
        chat_id=state.task.chat_id,
        kind="augmented_resume",
        location=result.resume.artifact,
        source_task_id=state.task.id,
    )
    write_set = DbWriteSet(
        idempotency_key=state.task.id,
        source_task_id=state.task.id,
        chat_id=state.task.chat_id,
        writes=[PrivateArtifactWrite(metadata=artifact)],
    )
    return write_set, artifact


async def validate_output(state: GraphState, deps: GraphDeps) -> dict:
    result = state.last_tool_result
    if result is None or not result.ok:
        return {}

    scan_writes = _scan_result_to_writes(state)
    if scan_writes is not None:
        return {"proposed_writes": scan_writes}

    resume_writes, private_artifact = _resume_result_to_writes(state)
    if resume_writes is not None:
        return {"proposed_writes": resume_writes, "private_artifact": private_artifact}

    return {}


async def apply_writes(state: GraphState, deps: GraphDeps) -> dict:
    if state.proposed_writes is None:
        return {}
    receipt = await deps.db_gate.apply(state.proposed_writes)
    return {"write_receipt": receipt}


async def format_response(state: GraphState, deps: GraphDeps) -> dict:
    presenters = deps.presenters
    result = state.last_tool_result

    if result is None or not result.ok:
        message = result.summary if result else "the task did not produce a result"
        return {"response": presenters.error(message)}

    if result.scan is not None:
        receipt = state.write_receipt or DbWriteReceipt.noop(state.task.id)
        return {"response": presenters.scan_summary(result.scan.role_id, receipt)}

    if result.resume is not None and state.private_artifact is not None:
        return {"response": presenters.resume_result(state.private_artifact, skill_gaps=result.resume.skill_gaps)}

    if result.cursor_run is not None and result.cursor_run.text:
        return {"response": presenters.research_result(result.cursor_run.text)}

    return {"response": presenters.error("Task finished with no presentable output.")}
