"""The supervisor's only read access to Aurora — a narrow, typed lookup, not
a general SQL tool. Used to avoid re-suggesting companies already in the
backlog for a role (see graph/nodes.py's use before calling ScanTool).
"""
from __future__ import annotations

from agent.app.db.aurora_client import AuroraClient
from agent.app.db.domain_repo import list_job_listings
from agent.app.models.tool_results import ToolName, ToolResult


class DbReadTool:
    def __init__(self, client: AuroraClient) -> None:
        self._client = client

    async def known_companies_for_role(self, role_id: str) -> ToolResult:
        listings = await list_job_listings(self._client, role_id, limit=100)
        names = sorted({listing.company_name for listing in listings})
        return ToolResult(tool=ToolName.DB_READ, ok=True, summary=f"{len(names)} known compan(y/ies) for {role_id}")

    async def known_company_names(self, role_id: str) -> list[str]:
        listings = await list_job_listings(self._client, role_id, limit=100)
        return sorted({listing.company_name for listing in listings})
