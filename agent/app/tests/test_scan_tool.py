"""ScanTool end to end against fakes: the zero-token subprocess and the
agent-driven Level 1/3 fallback are both faked (never spawn a real process
or hit the Cursor SDK), but `data/scan-history.tsv` itself is real —
`count_added_rows`/`read_recently_added_listings` run for real against it,
so these tests exercise the actual before/after diffing and row-parsing
logic, not a mocked version of it.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from agent.app.config import DEFAULT_MODEL_ID, Settings
from agent.app.models.tasks import ScanRolePayload
from agent.app.models.tool_results import CursorRunResult
from agent.app.tools import scan_tool as scan_tool_module
from agent.app.tools.career_ops_scripts import ZeroTokenScanRun
from agent.app.tools.cursor_sdk_tool import CursorPromptRequest
from agent.app.tools.scan_tool import ScanTool

_HEADER = "url\tfirst_seen\tportal\ttitle\tcompany\tstatus\tlocation\tfingerprint\tposted_at\ttrust_score\ttrust_flags\tnormalized_company\n"


def _append_scan_history_row(career_ops_dir: Path, *, url: str, title: str, company: str, location: str = "", posted_at: str = "") -> None:
    data_dir = career_ops_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / "scan-history.tsv"
    if not path.exists():
        path.write_text(_HEADER)
    with path.open("a") as handle:
        handle.write(f"{url}\t2026-01-01\tp\t{title}\t{company}\tadded\t{location}\tfp\t{posted_at}\t\t\t{company.lower()}\n")


class FakeCursorSdkTool:
    """Simulates the agent fallback run: records the request, optionally
    "discovers" listings by appending scan-history.tsv rows as a side
    effect (exactly like a real agent following modes/scan.md would)."""

    def __init__(self, run_result: CursorRunResult, *, discovers: list[dict] | None = None, career_ops_dir: Path | None = None) -> None:
        self._run_result = run_result
        self._discovers = discovers or []
        self._career_ops_dir = career_ops_dir
        self.requests: list[CursorPromptRequest] = []

    async def run_prompt(self, request: CursorPromptRequest) -> CursorRunResult:
        self.requests.append(request)
        for listing in self._discovers:
            _append_scan_history_row(self._career_ops_dir, **listing)
        return self._run_result


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        AWS_REGION="us-east-1",
        AURORA_RESOURCE_ARN="arn:aurora",
        AURORA_SECRET_ARN="arn:secret",
        JOB_ARTIFACTS_BUCKET="job-bucket",
        PRIVATE_USER_ARTIFACTS_BUCKET="private-bucket",
        CURSOR_API_KEY="cursor_test_key",
        CURSOR_MODEL=DEFAULT_MODEL_ID,  # hermetic against a real local .env
        CAREER_OPS_DIR=str(tmp_path),
    )


def _fake_zero_token(
    monkeypatch: pytest.MonkeyPatch, *, ok: bool = True, new_added: int = 0, error: str | None = None, appends: list[dict] | None = None
) -> None:
    """`appends` rows are written as a side effect of the fake call itself
    — matching when the real subprocess would actually touch
    scan-history.tsv, i.e. AFTER `ScanTool.scan()` has already taken its
    `before_count` snapshot, not before the test calls `.scan()` at all."""

    async def fake(career_ops_dir: Path, *, timeout_seconds: float) -> ZeroTokenScanRun:
        for row in appends or []:
            _append_scan_history_row(career_ops_dir, **row)
        return ZeroTokenScanRun(ok=ok, new_added=new_added, error=error)

    monkeypatch.setattr(scan_tool_module, "run_zero_token_scan", fake)


def _payload(**overrides) -> ScanRolePayload:
    defaults = dict(role_id="backend", query="senior python", max_results=25)
    defaults.update(overrides)
    return ScanRolePayload(**defaults)


@pytest.mark.asyncio
async def test_scan_fails_fast_when_zero_token_scan_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_zero_token(monkeypatch, ok=False, error="node not found")
    cursor_tool = FakeCursorSdkTool(CursorRunResult(ok=True, status="finished", text="unused"))
    tool = ScanTool(cursor_tool, _settings(tmp_path))

    result = await tool.scan(_payload(), chat_id="42")

    assert result.ok is False
    assert "node not found" in result.summary
    assert cursor_tool.requests == []


@pytest.mark.asyncio
async def test_scan_skips_agent_fallback_when_zero_token_alone_found_enough(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_zero_token(
        monkeypatch, ok=True, new_added=30, appends=[{"url": "u1", "title": "Backend Engineer", "company": "Acme"}]
    )  # new_added >= max_results, so no fallback needed
    cursor_tool = FakeCursorSdkTool(CursorRunResult(ok=True, status="finished", text="unused"))
    tool = ScanTool(cursor_tool, _settings(tmp_path))

    result = await tool.scan(_payload(max_results=25), chat_id="42")

    assert result.ok is True
    assert cursor_tool.requests == []


@pytest.mark.asyncio
async def test_scan_runs_agent_fallback_when_zero_token_found_too_few(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    career_ops_dir = tmp_path
    _fake_zero_token(monkeypatch, ok=True, new_added=2)
    cursor_tool = FakeCursorSdkTool(
        CursorRunResult(ok=True, status="finished", text="done"),
        discovers=[{"url": "u1", "title": "Backend Engineer", "company": "Acme", "location": "Remote", "posted_at": "2026-01-05"}],
        career_ops_dir=career_ops_dir,
    )
    tool = ScanTool(cursor_tool, _settings(tmp_path))

    result = await tool.scan(_payload(role_id="backend", query="senior python", max_results=25), chat_id="42", known_companies=["Beta Co"])

    assert len(cursor_tool.requests) == 1
    request = cursor_tool.requests[0]
    assert request.tools == ["shell", "webSearch", "read", "edit"]
    assert request.disallowed_tools == ["task"]
    assert "backend" in request.prompt
    assert "senior python" in request.prompt
    assert "Beta Co" in request.prompt

    assert result.ok is True
    assert result.scan is not None
    assert len(result.scan.listings) == 1
    listing = result.scan.listings[0]
    assert listing.company_name == "Acme"
    assert listing.title == "Backend Engineer"
    assert listing.location == "Remote"
    assert listing.posted_at is not None


@pytest.mark.asyncio
async def test_scan_still_returns_zero_token_results_when_agent_fallback_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    career_ops_dir = tmp_path
    _fake_zero_token(monkeypatch, ok=True, new_added=1, appends=[{"url": "u1", "title": "Backend Engineer", "company": "Acme"}])
    cursor_tool = FakeCursorSdkTool(CursorRunResult(ok=False, status="error", error="agent timed out"), career_ops_dir=career_ops_dir)
    tool = ScanTool(cursor_tool, _settings(tmp_path))

    result = await tool.scan(_payload(max_results=25), chat_id="42")

    assert len(cursor_tool.requests) == 1  # fallback was attempted
    assert result.ok is True  # non-fatal -- zero-token's own row is still valid
    assert len(result.scan.listings) == 1
    assert result.scan.listings[0].company_name == "Acme"


@pytest.mark.asyncio
async def test_scan_returns_empty_result_when_nothing_new_found(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_zero_token(monkeypatch, ok=True, new_added=0)
    cursor_tool = FakeCursorSdkTool(CursorRunResult(ok=True, status="finished", text="done"), career_ops_dir=tmp_path)
    tool = ScanTool(cursor_tool, _settings(tmp_path))

    result = await tool.scan(_payload(max_results=25), chat_id="42")

    assert result.ok is True
    assert result.scan.listings == []
    assert "no new listings" in result.summary


@pytest.mark.asyncio
async def test_scan_caps_returned_listings_at_max_results(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    appends = [{"url": f"u{i}", "title": f"Role {i}", "company": f"Co{i}"} for i in range(5)]
    _fake_zero_token(monkeypatch, ok=True, new_added=5, appends=appends)
    cursor_tool = FakeCursorSdkTool(CursorRunResult(ok=True, status="finished", text="done"))
    tool = ScanTool(cursor_tool, _settings(tmp_path))

    result = await tool.scan(_payload(max_results=2), chat_id="42")

    assert result.ok is True
    assert len(result.scan.listings) == 2
    # Caps to the most-recently-added rows.
    assert [listing.company_name for listing in result.scan.listings] == ["Co3", "Co4"]
