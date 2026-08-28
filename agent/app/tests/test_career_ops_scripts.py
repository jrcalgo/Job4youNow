"""career_ops_scripts.py against a fake `node` executable placed on PATH —
exercises the real subprocess/timeout/parsing machinery without needing an
actual career-ops-modified checkout installed.
"""
from __future__ import annotations

import os
import stat
import textwrap
from pathlib import Path

import pytest

from agent.app.tools.career_ops_scripts import (
    count_added_rows,
    read_recently_added_listings,
    run_skill_gap_check,
    run_zero_token_scan,
    write_scratch_job_description,
)

_SCAN_HISTORY_HEADER = (
    "url\tfirst_seen\tportal\ttitle\tcompany\tstatus\tlocation\tfingerprint\tposted_at\ttrust_score\ttrust_flags\tnormalized_company\n"
)


def _write_fake_node(bin_dir: Path, script_body: str) -> None:
    node_path = bin_dir / "node"
    node_path.write_text(f"#!/usr/bin/env python3\n{textwrap.dedent(script_body)}")
    node_path.chmod(node_path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture
def fake_bin_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    return bin_dir


# -- run_zero_token_scan --


@pytest.mark.asyncio
async def test_run_zero_token_scan_reports_new_added_from_scan_runs_tsv(tmp_path: Path, fake_bin_dir: Path) -> None:
    career_ops_dir = tmp_path / "career-ops"
    data_dir = career_ops_dir / "data"
    data_dir.mkdir(parents=True)
    _write_fake_node(fake_bin_dir, "import sys; sys.exit(0)")
    (data_dir / "scan-runs.tsv").write_text(
        "timestamp\tstatus\tcompanies\tboards\tfound\tnew_added\n2026-01-01T00:00:00Z\tcompleted\t1\t1\t5\t3\n"
    )

    run = await run_zero_token_scan(career_ops_dir, timeout_seconds=5.0)

    assert run.ok is True
    assert run.new_added == 3


@pytest.mark.asyncio
async def test_run_zero_token_scan_reports_failure_on_nonzero_exit(tmp_path: Path, fake_bin_dir: Path) -> None:
    career_ops_dir = tmp_path / "career-ops"
    career_ops_dir.mkdir()
    _write_fake_node(fake_bin_dir, "import sys; sys.stderr.write('boom'); sys.exit(1)")

    run = await run_zero_token_scan(career_ops_dir, timeout_seconds=5.0)

    assert run.ok is False
    assert "boom" in run.error


@pytest.mark.asyncio
async def test_run_zero_token_scan_times_out(tmp_path: Path, fake_bin_dir: Path) -> None:
    career_ops_dir = tmp_path / "career-ops"
    career_ops_dir.mkdir()
    _write_fake_node(fake_bin_dir, "import time; time.sleep(30)")

    run = await run_zero_token_scan(career_ops_dir, timeout_seconds=0.2)

    assert run.ok is False
    assert "timeout" in run.error


@pytest.mark.asyncio
async def test_run_zero_token_scan_reports_failure_when_node_is_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", "")
    career_ops_dir = tmp_path / "career-ops"
    career_ops_dir.mkdir()

    run = await run_zero_token_scan(career_ops_dir, timeout_seconds=5.0)

    assert run.ok is False
    assert run.new_added == 0


# -- count_added_rows / read_recently_added_listings --


def test_count_added_rows_only_counts_added_status(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "scan-history.tsv").write_text(
        _SCAN_HISTORY_HEADER
        + "u1\t2026-01-01\tp\tt1\tc1\tadded\tloc\tfp\t2026-01-01\t\t\tc1\n"
        + "u2\t2026-01-01\tp\tt2\tc2\tskipped_dup\tloc\tfp\t2026-01-01\t\t\tc2\n"
        + "u3\t2026-01-01\tp\tt3\tc3\tadded\tloc\tfp\t2026-01-01\t\t\tc3\n"
    )

    assert count_added_rows(tmp_path) == 2


def test_count_added_rows_returns_zero_when_file_missing(tmp_path: Path) -> None:
    assert count_added_rows(tmp_path) == 0


def test_read_recently_added_listings_takes_the_trailing_matching_rows(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "scan-history.tsv").write_text(
        _SCAN_HISTORY_HEADER
        + "u1\t2026-01-01\tp\tOld Title\tOld Co\tadded\tRemote\tfp\t2026-01-01\t\t\told co\n"
        + "u2\t2026-01-01\tp\tSkipped\tSkip Co\tskipped_dup\tRemote\tfp\t2026-01-02\t\t\tskip co\n"
        + "u3\t2026-01-01\tp\tNew Title\tNew Co\tadded\tBerlin\tfp\t2026-01-03\t\t\tnew co\n"
    )

    rows = read_recently_added_listings(tmp_path, count=1)

    assert len(rows) == 1
    assert rows[0].title == "New Title"
    assert rows[0].company == "New Co"
    assert rows[0].location == "Berlin"
    assert rows[0].posted_at is not None
    assert rows[0].posted_at.date().isoformat() == "2026-01-03"


def test_read_recently_added_listings_handles_empty_posted_at_and_location(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "scan-history.tsv").write_text(_SCAN_HISTORY_HEADER + "u1\t2026-01-01\tp\tTitle\tCo\tadded\t\tfp\t\t\t\tco\n")

    rows = read_recently_added_listings(tmp_path, count=1)

    assert rows[0].location is None
    assert rows[0].posted_at is None


def test_read_recently_added_listings_returns_empty_for_zero_count(tmp_path: Path) -> None:
    assert read_recently_added_listings(tmp_path, count=0) == []


# -- write_scratch_job_description --


def test_write_scratch_job_description_writes_under_jds_and_returns_the_path(tmp_path: Path) -> None:
    path = write_scratch_job_description(tmp_path, "Some JD text")

    assert path.parent == tmp_path / "jds"
    assert path.read_text() == "Some JD text"
    assert path.name.startswith("scratch-")


def test_write_scratch_job_description_generates_unique_paths(tmp_path: Path) -> None:
    path1 = write_scratch_job_description(tmp_path, "a")
    path2 = write_scratch_job_description(tmp_path, "b")

    assert path1 != path2


# -- run_skill_gap_check --


@pytest.mark.asyncio
async def test_run_skill_gap_check_parses_json_stdout(tmp_path: Path, fake_bin_dir: Path) -> None:
    career_ops_dir = tmp_path / "career-ops"
    career_ops_dir.mkdir()
    jd_path = write_scratch_job_description(career_ops_dir, "## Requirements\n- Python")
    _write_fake_node(
        fake_bin_dir,
        """
        import json
        print(json.dumps({"existing": ["Python"], "supportedByResume": [], "gap": ["Rust"], "lowConfidence": None}))
        """,
    )

    result = await run_skill_gap_check(career_ops_dir, jd_path, timeout_seconds=5.0)

    assert result.ok is True
    assert result.existing == ["Python"]
    assert result.gap == ["Rust"]
    assert result.low_confidence_reason is None


@pytest.mark.asyncio
async def test_run_skill_gap_check_surfaces_low_confidence_reason(tmp_path: Path, fake_bin_dir: Path) -> None:
    career_ops_dir = tmp_path / "career-ops"
    career_ops_dir.mkdir()
    jd_path = write_scratch_job_description(career_ops_dir, "no requirements section here")
    _write_fake_node(
        fake_bin_dir,
        """
        import json
        print(json.dumps({
            "existing": [], "supportedByResume": [], "gap": [],
            "lowConfidence": {"reason": "no-requirements-section", "message": "..."},
        }))
        """,
    )

    result = await run_skill_gap_check(career_ops_dir, jd_path, timeout_seconds=5.0)

    assert result.ok is True
    assert result.low_confidence_reason == "no-requirements-section"


@pytest.mark.asyncio
async def test_run_skill_gap_check_reports_failure_on_invalid_json(tmp_path: Path, fake_bin_dir: Path) -> None:
    career_ops_dir = tmp_path / "career-ops"
    career_ops_dir.mkdir()
    jd_path = write_scratch_job_description(career_ops_dir, "text")
    _write_fake_node(fake_bin_dir, "print('not json')")

    result = await run_skill_gap_check(career_ops_dir, jd_path, timeout_seconds=5.0)

    assert result.ok is False
    assert "JSON" in result.error


@pytest.mark.asyncio
async def test_run_skill_gap_check_reports_failure_on_nonzero_exit(tmp_path: Path, fake_bin_dir: Path) -> None:
    career_ops_dir = tmp_path / "career-ops"
    career_ops_dir.mkdir()
    jd_path = write_scratch_job_description(career_ops_dir, "text")
    _write_fake_node(fake_bin_dir, "import sys; sys.stderr.write('parse error'); sys.exit(2)")

    result = await run_skill_gap_check(career_ops_dir, jd_path, timeout_seconds=5.0)

    assert result.ok is False
    assert "parse error" in result.error
