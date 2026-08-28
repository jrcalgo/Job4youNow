"""Turns a ScanRolePayload into a ScanResult by running career-ops's real
`scan` mode — never by asking an LLM to freely "research job openings" and
hope it doesn't invent listings. Two passes:

1. `node scan.mjs` as a plain subprocess (tools/career_ops_scripts.py) —
   zero-token, covers Level 0 (local parsers) + Level 2 (ATS APIs).
2. If that alone didn't find `max_results` new listings, one Cursor SDK
   agent run instructed to read `modes/scan.md` and complete ONLY Level 1
   (Playwright) + Level 3 (WebSearch) for whatever the zero-token pass
   didn't cover, plus one ad hoc WebSearch pass for this request's
   role/query — with restricted tools (no subagent spawning).

Either way, the result comes from reading `data/scan-history.tsv`
afterward (career-ops's own structured record of what it found) — never
from parsing agent prose. No Aurora access here; the graph hands the
resulting ScanResult to models/db_writes.py's JobListingWrite conversion
before it ever reaches db/db_gate.py.
"""
from __future__ import annotations

from agent.app.config import Settings
from agent.app.logging import get_logger
from agent.app.models.tasks import ScanRolePayload, TaskKind
from agent.app.models.tool_results import ScanResult, ScannedListing, ToolName, ToolResult
from agent.app.tools.career_ops_scripts import count_added_rows, read_recently_added_listings, run_zero_token_scan
from agent.app.tools.cursor_sdk_tool import CursorPromptRequest, CursorSdkTool

log = get_logger("tools.scan")

_SCAN_AGENT_TOOLS = ["shell", "webSearch", "read", "edit"]
_SCAN_AGENT_DISALLOWED_TOOLS = ["task"]

_SCAN_AGENT_PROMPT_TEMPLATE = """\
Read modes/scan.md in this working directory and follow it. career-ops's \
zero-token scan (node scan.mjs) already ran and covered Level 0 (local \
parsers) and Level 2 (ATS APIs) for every company in portals.yml this run \
— do not repeat those; do not re-check any company already covered by a \
successful local parser or API call via Playwright or a redundant request.

Complete ONLY Level 1 (Playwright, for tracked_companies not already \
covered above) and Level 3 (WebSearch) as documented, PLUS one additional \
ad hoc WebSearch pass for this specific request — treat it exactly like \
one more `search_queries` entry, with the same title-filter, \
location-filter, dedup, and liveness-verification rules already documented:

Role: {role_id}
Request: {query}
Companies already tracked for this role (do not treat as new discoveries): {known_companies}

Record every result exactly as modes/scan.md's Workflow section specifies \
(data/pipeline.md's Pending section, data/scan-history.tsv). This is \
discovery only — do not evaluate, tailor, or apply to anything. This run \
is already single-pass by design; do not spawn a background subagent.\
"""


class ScanTool:
    def __init__(self, cursor_sdk_tool: CursorSdkTool, settings: Settings) -> None:
        self._cursor_sdk_tool = cursor_sdk_tool
        self._settings = settings

    async def scan(self, payload: ScanRolePayload, *, chat_id: str, known_companies: list[str] | None = None) -> ToolResult:
        career_ops_dir = self._settings.career_ops_dir
        before_count = count_added_rows(career_ops_dir)

        zero_token = await run_zero_token_scan(career_ops_dir, timeout_seconds=self._settings.scan_subprocess_timeout_seconds)
        if not zero_token.ok:
            return ToolResult(tool=ToolName.SCAN, ok=False, summary=f"career-ops scan.mjs failed: {zero_token.error}")

        if zero_token.new_added < payload.max_results:
            await self._run_agent_fallback(payload, chat_id=chat_id, known_companies=known_companies)

        after_count = count_added_rows(career_ops_dir)
        new_count = after_count - before_count
        if new_count <= 0:
            return ToolResult(
                tool=ToolName.SCAN, ok=True, summary="no new listings found", scan=ScanResult(role_id=payload.role_id, listings=[])
            )

        rows = read_recently_added_listings(career_ops_dir, count=min(new_count, payload.max_results))
        listings = [
            ScannedListing(company_name=row.company, title=row.title, url=row.url, location=row.location, posted_at=row.posted_at)
            for row in rows
        ]
        scan_result = ScanResult(role_id=payload.role_id, listings=listings)
        return ToolResult(tool=ToolName.SCAN, ok=True, summary=f"found {len(listings)} listing(s)", scan=scan_result)

    async def _run_agent_fallback(self, payload: ScanRolePayload, *, chat_id: str, known_companies: list[str] | None) -> None:
        prompt = _SCAN_AGENT_PROMPT_TEMPLATE.format(
            role_id=payload.role_id,
            query=payload.query,
            known_companies=", ".join(known_companies) if known_companies else "(none yet)",
        )
        request = CursorPromptRequest(
            prompt=prompt,
            chat_id=chat_id,
            task_kind=TaskKind.SCAN_ROLE,
            tools=_SCAN_AGENT_TOOLS,
            disallowed_tools=_SCAN_AGENT_DISALLOWED_TOOLS,
            timeout_seconds=self._settings.scan_agent_timeout_seconds,
        )
        run = await self._cursor_sdk_tool.run_prompt(request)
        if not run.ok:
            # Non-fatal: whatever the zero-token pass already found in
            # data/scan-history.tsv is still valid and gets returned.
            log.warning("scan agent fallback did not complete", extra={"context": {"role_id": payload.role_id, "error": run.error}})
