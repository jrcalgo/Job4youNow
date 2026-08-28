"""Invokes the career-ops skill (in the career-ops-modified submodule)
through the Cursor SDK, the same pattern agent/demo_career_ops_agent.py
proved out — but async, and returning a typed ToolResult instead of
printing to stdout. This is the tool `research_company` tasks and other
career-ops-flavored predictive requests route to.
"""
from __future__ import annotations

from agent.app.models.tasks import TaskKind
from agent.app.models.tool_results import ToolName, ToolResult
from agent.app.tools.cursor_sdk_tool import CursorPromptRequest, CursorSdkTool

_SKILL_INSTRUCTIONS = (
    "Read .cursor/skills/career-ops/SKILL.md in this working directory and follow its "
    "instructions for the request below. Do not execute a submit/apply action and do not "
    "make any file changes — this call is for research/analysis output only.\n\nRequest: {query}"
)


class CareerOpsTool:
    def __init__(self, cursor_sdk_tool: CursorSdkTool) -> None:
        self._cursor_sdk_tool = cursor_sdk_tool

    async def run(self, query: str, *, chat_id: str) -> ToolResult:
        request = CursorPromptRequest(
            prompt=_SKILL_INSTRUCTIONS.format(query=query), chat_id=chat_id, task_kind=TaskKind.RESEARCH_COMPANY
        )
        run = await self._cursor_sdk_tool.run_prompt(request)
        if not run.ok or not run.text:
            return ToolResult(tool=ToolName.CAREER_OPS, ok=False, summary=run.error or "career-ops run produced no output")
        return ToolResult(tool=ToolName.CAREER_OPS, ok=True, summary=run.text[:200], cursor_run=run)
