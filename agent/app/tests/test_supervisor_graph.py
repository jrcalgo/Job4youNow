"""graph/supervisor.py's build_supervisor_graph + run_supervisor_graph against
the REAL compiled LangGraph graph — every other graph-adjacent test (tools,
individual node functions) exercises its piece in isolation, never the
actual compiled graph's real (async) invocation path end to end.
"""
from __future__ import annotations

import pytest

from agent.app.formatting.presenters import Presenters
from agent.app.graph.nodes import GraphDeps
from agent.app.graph.supervisor import build_supervisor_graph, run_supervisor_graph
from agent.app.models.tasks import ResearchCompanyPayload, TaskSource, TaskSubmission
from agent.app.models.tool_results import CursorRunResult, ToolName, ToolResult


class _FakeCareerOpsTool:
    async def run(self, query: str, *, chat_id: str) -> ToolResult:
        return ToolResult(
            tool=ToolName.CAREER_OPS,
            ok=True,
            summary="ok",
            cursor_run=CursorRunResult(ok=True, status="finished", text=f"Here is your research on: {query}"),
        )


def _deps(**overrides: object) -> GraphDeps:
    base: dict[str, object] = {
        "scan_tool": None,
        "resume_tool": None,
        "career_ops_tool": _FakeCareerOpsTool(),
        "db_read_tool": None,
        "db_gate": None,
        "presenters": Presenters(),
    }
    base.update(overrides)
    return GraphDeps(**base)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_supervisor_graph_actually_awaits_every_async_node() -> None:
    """Regression test for a real, 100%-reproducing production bug found
    live: every node in build_supervisor_graph was wired as `lambda state:
    node_fn(state, deps)` — a plain (non-async) lambda wrapping an `async
    def` node function. LangGraph decides whether to await a node's result
    by checking `inspect.iscoroutinefunction` on the exact callable handed
    to `add_node` (see langgraph._internal._runnable.is_async_callable); a
    lambda never passes that check, even though its body calls an async
    function, so LangGraph ran it synchronously and received a bare,
    never-awaited coroutine back — failing EVERY task with LangGraph's own
    INVALID_GRAPH_NODE_RETURN_VALUE ("Expected dict, got <coroutine object
    load_context ...>"). No test previously built the real compiled graph,
    so nothing caught it until a live end-to-end run did."""
    deps = _deps()
    graph = build_supervisor_graph(deps)
    submission = TaskSubmission(
        chat_id="42",
        source=TaskSource.USER,
        payload=ResearchCompanyPayload(query="is Acme still hiring remote?"),
        idempotency_key="task-1",
    )

    result = await run_supervisor_graph(graph, submission.to_task())

    assert result.response is not None
    assert "Here is your research on: is Acme still hiring remote?" in (result.response.body or "")
    # One result per node actually ran (not a bare coroutine standing in
    # for one) — proves load_context -> supervisor_decide -> run_tool ->
    # validate_output -> apply_writes -> format_response all executed.
    assert len(result.tool_results) == 1
    assert result.selected_tool == ToolName.CAREER_OPS
