"""Builds and runs the predictive-task graph. `build_supervisor_graph` is
called once at startup (see main.py) — dependencies are bound into each
node via a closure over `deps`, so the compiled graph itself is stateless
and safe to reuse across every task a worker runs.
"""
from __future__ import annotations

import functools

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from agent.app.graph.nodes import (
    GraphDeps,
    apply_writes,
    format_response,
    load_context,
    run_tool,
    supervisor_decide,
    validate_output,
)
from agent.app.graph.state import GraphState
from agent.app.models.tasks import AgentTask


def build_supervisor_graph(deps: GraphDeps) -> CompiledStateGraph:
    graph = StateGraph(GraphState)
    # functools.partial, NOT `lambda state: node_fn(state, deps)` — every
    # node_fn below is `async def`, and LangGraph decides whether to await a
    # node's result by checking `inspect.iscoroutinefunction` on the exact
    # callable passed to add_node (see langgraph._internal._runnable's
    # is_async_callable). A plain lambda is never a coroutine function, even
    # though its BODY calls one, so LangGraph ran it synchronously and got
    # back a bare, never-awaited coroutine as the "node output" — which
    # failed every single task with LangGraph's own
    # INVALID_GRAPH_NODE_RETURN_VALUE ("Expected dict, got <coroutine
    # object ...>"), 100% of the time, confirmed against a real run.
    # functools.partial is different: inspect.iscoroutinefunction correctly
    # unwraps it (recurses into `.func`), so LangGraph awaits it properly.
    graph.add_node("load_context", functools.partial(load_context, deps=deps))
    graph.add_node("supervisor_decide", functools.partial(supervisor_decide, deps=deps))
    graph.add_node("run_tool", functools.partial(run_tool, deps=deps))
    graph.add_node("validate_output", functools.partial(validate_output, deps=deps))
    graph.add_node("apply_writes", functools.partial(apply_writes, deps=deps))
    graph.add_node("format_response", functools.partial(format_response, deps=deps))

    graph.add_edge(START, "load_context")
    graph.add_edge("load_context", "supervisor_decide")
    graph.add_edge("supervisor_decide", "run_tool")
    graph.add_edge("run_tool", "validate_output")
    graph.add_edge("validate_output", "apply_writes")
    graph.add_edge("apply_writes", "format_response")
    graph.add_edge("format_response", END)
    return graph.compile()


async def run_supervisor_graph(compiled_graph: CompiledStateGraph, task: AgentTask) -> GraphState:
    result = await compiled_graph.ainvoke(GraphState(task=task))
    return GraphState.model_validate(result)
