"""Runs career-ops's own zero-token `scan.mjs` as a plain subprocess — no
Cursor SDK call, no LLM tokens spent at all. This covers `modes/scan.md`'s
Level 0 (local parser scripts) and Level 2 (ATS APIs: Greenhouse, Ashby,
Lever, ...) in one shot; see tools/scan_tool.py for where Level 1
(Playwright)/Level 3 (WebSearch) get delegated to an actual Cursor SDK
agent run instead.

The result is read back from `data/scan-history.tsv` and `data/scan-runs.tsv`
— career-ops's own structured, append-only records of what it found —
never parsed from this process's human-readable stdout. Verified against a
real run of the installed career-ops-modified checkout (v1.28.0): the
column set below is what that version's `scan.mjs` actually writes, which
is a superset of what `modes/scan.md`'s prose documents.
"""
from __future__ import annotations

import asyncio
import csv
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from agent.app.logging import get_logger

log = get_logger("tools.career_ops_scripts")

_SCAN_HISTORY_PATH = Path("data") / "scan-history.tsv"
_SCAN_RUNS_PATH = Path("data") / "scan-runs.tsv"


@dataclass
class ScanHistoryRow:
    """One row of `data/scan-history.tsv` — see that file's header for the
    authoritative column list. Only the fields tools/scan_tool.py actually
    needs are pulled out; `fingerprint`/`trust_score`/`trust_flags`/
    `normalized_company` are career-ops's own internal bookkeeping."""

    url: str
    title: str
    company: str
    location: str | None
    posted_at: datetime | None


@dataclass
class ZeroTokenScanRun:
    ok: bool
    new_added: int
    error: str | None = None


async def run_zero_token_scan(career_ops_dir: Path, *, timeout_seconds: float = 300.0) -> ZeroTokenScanRun:
    """Runs `node scan.mjs --quiet` to completion. Returns how many new
    listings it appended to `data/scan-history.tsv` this run — the caller
    (tools/scan_tool.py) reads that file separately via
    `read_recently_added_listings` for the actual listing data."""
    try:
        process = await asyncio.create_subprocess_exec(
            "node",
            "scan.mjs",
            "--quiet",
            cwd=str(career_ops_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except (FileNotFoundError, OSError) as error:
        return ZeroTokenScanRun(ok=False, new_added=0, error=f"could not start scan.mjs: {error}")

    try:
        _stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        return ZeroTokenScanRun(ok=False, new_added=0, error=f"scan.mjs exceeded {timeout_seconds}s timeout")

    if process.returncode != 0:
        error_tail = stderr.decode(errors="replace")[-500:]
        log.warning("scan.mjs exited non-zero", extra={"context": {"returncode": process.returncode, "stderr": error_tail}})
        return ZeroTokenScanRun(ok=False, new_added=0, error=error_tail or f"scan.mjs exited with code {process.returncode}")

    new_added = _read_last_run_new_added_count(career_ops_dir / _SCAN_RUNS_PATH)
    return ZeroTokenScanRun(ok=True, new_added=new_added)


def count_added_rows(career_ops_dir: Path) -> int:
    """Total `status=="added"` rows in `data/scan-history.tsv` right now.
    tools/scan_tool.py calls this before AND after a scan (subprocess, then
    optionally an agent-driven fallback) and reads back exactly the
    difference — more robust than trusting `scan-runs.tsv`'s own counter,
    which only reflects `scan.mjs`'s own additions, not any further rows an
    agent run appends by following `modes/scan.md`'s own workflow steps."""
    path = career_ops_dir / _SCAN_HISTORY_PATH
    if not path.is_file():
        return 0
    with path.open(newline="", encoding="utf-8") as handle:
        return sum(1 for row in csv.DictReader(handle, delimiter="\t") if row.get("status") == "added")


def read_recently_added_listings(career_ops_dir: Path, *, count: int) -> list[ScanHistoryRow]:
    """Reads the trailing `count` `status=="added"` rows from
    `data/scan-history.tsv` — career-ops's own append-only ledger of every
    URL it has ever seen (modes/scan.md). Scanning from the end and
    stopping once `count` matching rows are found is robust to whatever
    other statuses (`skipped_dup`, `skipped_title`, ...) this run also
    logged interleaved with them."""
    path = career_ops_dir / _SCAN_HISTORY_PATH
    if count <= 0 or not path.is_file():
        return []

    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle, delimiter="\t"))

    matched: list[ScanHistoryRow] = []
    for row in reversed(rows):
        if row.get("status") != "added":
            continue
        matched.append(_row_to_scan_history_row(row))
        if len(matched) >= count:
            break
    matched.reverse()
    return matched


def _row_to_scan_history_row(row: dict[str, str]) -> ScanHistoryRow:
    return ScanHistoryRow(
        url=row.get("url", ""),
        title=row.get("title", ""),
        company=row.get("company", ""),
        location=row.get("location") or None,
        posted_at=_parse_posted_at(row.get("posted_at")),
    )


def _parse_posted_at(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return None


@dataclass
class SkillGapResult:
    ok: bool
    existing: list[str] = field(default_factory=list)
    supported_by_resume: list[str] = field(default_factory=list)
    gap: list[str] = field(default_factory=list)
    low_confidence_reason: str | None = None
    error: str | None = None


def write_scratch_job_description(career_ops_dir: Path, job_description: str) -> Path:
    """Saves a job description under `jds/`, matching `modes/pdf.md`'s own
    documented workflow ("write the JD to a scratch file... if it isn't
    already one"). Returns the path so the SAME file can be reused both for
    `run_skill_gap_check` below and referenced directly in the tailoring
    agent's prompt — never write the JD to two different places."""
    jds_dir = career_ops_dir / "jds"
    jds_dir.mkdir(parents=True, exist_ok=True)
    jd_path = jds_dir / f"scratch-{uuid.uuid4().hex[:12]}.md"
    jd_path.write_text(job_description, encoding="utf-8")
    return jd_path


async def run_skill_gap_check(career_ops_dir: Path, jd_path: Path, *, timeout_seconds: float = 60.0) -> SkillGapResult:
    """Runs career-ops's zero-LLM `jd-skill-gap.mjs` against an already-saved
    JD file (see `write_scratch_job_description`) and parses its JSON
    stdout directly — this is how tools/resume_tool.py gets
    `ResumeResult.skill_gaps` without trusting an LLM's own account of what
    `cv.md` does or doesn't support. Kept as a SEPARATE subprocess call from
    the main tailoring agent run: this script is deterministic and cheap,
    and running it independently means the reported gaps can't drift from
    what the classifier actually found."""
    try:
        process = await asyncio.create_subprocess_exec(
            "node",
            "jd-skill-gap.mjs",
            str(jd_path.relative_to(career_ops_dir)),
            cwd=str(career_ops_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except (FileNotFoundError, OSError) as error:
        return SkillGapResult(ok=False, error=f"could not start jd-skill-gap.mjs: {error}")

    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        return SkillGapResult(ok=False, error=f"jd-skill-gap.mjs exceeded {timeout_seconds}s timeout")

    if process.returncode != 0:
        return SkillGapResult(ok=False, error=stderr.decode(errors="replace")[-500:] or f"exited with code {process.returncode}")

    try:
        payload = json.loads(stdout.decode())
    except json.JSONDecodeError as error:
        return SkillGapResult(ok=False, error=f"jd-skill-gap.mjs did not print valid JSON: {error}")

    low_confidence = payload.get("lowConfidence")
    return SkillGapResult(
        ok=True,
        existing=list(payload.get("existing", [])),
        supported_by_resume=list(payload.get("supportedByResume", [])),
        gap=list(payload.get("gap", [])),
        low_confidence_reason=low_confidence.get("reason") if low_confidence else None,
    )


def _read_last_run_new_added_count(scan_runs_path: Path) -> int:
    if not scan_runs_path.is_file():
        return 0
    with scan_runs_path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle, delimiter="\t"))
    if not rows:
        return 0
    try:
        return int(rows[-1]["new_added"])
    except (KeyError, ValueError):
        return 0
