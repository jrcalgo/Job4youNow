"""Tools the LangGraph supervisor can call. Every tool takes a typed request
and returns a `ToolResult` (see models/tool_results.py) — none of them touch
Aurora for writes; a tool that discovers new domain data hands its typed
result back to the graph, which routes it through db/db_gate.py.
"""
