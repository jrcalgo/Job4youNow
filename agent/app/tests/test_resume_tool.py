"""ResumeTool end to end against fakes: refuses to run without a cv.md,
gets skill_gaps from a faked (zero-token) skill-gap check rather than
trusting the agent's own account, finds the PDF the fake agent run
"produced" by diffing output/ before and after, extracts its text via a
faked PdfReader (never a real PDF fixture — see FakePdfReader below), and
writes ONLY that plain text to both the private bucket and a local backup
— never the PDF bytes, and never resume content itself in the returned
ToolResult beyond the artifact pointer.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from agent.app.config import DEFAULT_MODEL_ID, Settings
from agent.app.models.artifacts import ArtifactBucket
from agent.app.models.tasks import AugmentResumePayload
from agent.app.models.tool_results import CursorRunResult
from agent.app.tools import artifact_store as artifact_store_module
from agent.app.tools import resume_tool as resume_tool_module
from agent.app.tools.artifact_store import ArtifactStore, LocalPrivateBackupStore, PrivateStores
from agent.app.tools.career_ops_scripts import SkillGapResult
from agent.app.tools.cursor_sdk_tool import CursorPromptRequest
from agent.app.tools.resume_tool import ResumeTool


class FakeS3Client:
    def __init__(self) -> None:
        self.put_calls: list[dict] = []

    def put_object(self, **kwargs) -> None:
        self.put_calls.append(kwargs)


class FakeCursorSdkTool:
    """`pdf_to_create`, when given, is written as a side effect of
    `run_prompt` — simulating the real agent run actually producing a PDF
    file under `output/`, which is what ResumeTool looks for afterward.
    The bytes written are never actually parsed as a PDF in these tests —
    see FakePdfReader, which replaces the real pypdf.PdfReader — so they
    can be any placeholder content; only the file's existence/mtime is
    exercised by ResumeTool's own diffing logic."""

    def __init__(self, run_result: CursorRunResult, *, pdf_to_create: Path | None = None, pdf_bytes: bytes = b"%PDF-fake") -> None:
        self._run_result = run_result
        self._pdf_to_create = pdf_to_create
        self._pdf_bytes = pdf_bytes
        self.last_request: CursorPromptRequest | None = None

    async def run_prompt(self, request: CursorPromptRequest) -> CursorRunResult:
        self.last_request = request
        if self._pdf_to_create is not None:
            self._pdf_to_create.parent.mkdir(parents=True, exist_ok=True)
            self._pdf_to_create.write_bytes(self._pdf_bytes)
        return self._run_result


class FakePdfPage:
    def __init__(self, text: str) -> None:
        self._text = text

    def extract_text(self) -> str:
        return self._text


class FakePdfReader:
    """Stands in for pypdf.PdfReader so these tests never need a real,
    parseable PDF fixture — only resume_tool.py's OWN orchestration
    (finding the new PDF, calling PdfReader, joining page text, escaping
    nothing, uploading the result) is under test here; pypdf's own text
    extraction has its own test suite upstream."""

    def __init__(self, path: str, *, pages: list[str] | None = None) -> None:
        self.path = path
        self._pages = [FakePdfPage(text) for text in (pages if pages is not None else FakePdfReader.default_pages)]

    @property
    def pages(self) -> list[FakePdfPage]:
        return self._pages


FakePdfReader.default_pages = ["Jane Smith — Senior Backend Engineer\nPython, Kubernetes, distributed systems."]


def _patch_pdf_reader(monkeypatch: pytest.MonkeyPatch, *, pages: list[str] | None = None) -> None:
    def factory(path: str) -> FakePdfReader:
        return FakePdfReader(path, pages=pages)

    monkeypatch.setattr(resume_tool_module, "PdfReader", factory)


@pytest.fixture(autouse=True)
def _fake_s3(monkeypatch: pytest.MonkeyPatch) -> FakeS3Client:
    fake = FakeS3Client()
    monkeypatch.setattr(artifact_store_module, "_s3_client", lambda: fake)
    return fake


def _fake_write_scratch(career_ops_dir: Path, job_description: str) -> Path:
    jds_dir = career_ops_dir / "jds"
    jds_dir.mkdir(parents=True, exist_ok=True)
    jd_path = jds_dir / "scratch-test.md"
    jd_path.write_text(job_description, encoding="utf-8")
    return jd_path


def _patch_career_ops_scripts(monkeypatch: pytest.MonkeyPatch, *, skill_gap_result: SkillGapResult) -> None:
    monkeypatch.setattr(resume_tool_module, "write_scratch_job_description", _fake_write_scratch)

    async def _fake_run_skill_gap_check(career_ops_dir: Path, jd_path: Path, *, timeout_seconds: float = 60.0) -> SkillGapResult:
        return skill_gap_result

    monkeypatch.setattr(resume_tool_module, "run_skill_gap_check", _fake_run_skill_gap_check)


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


def _tool(tmp_path: Path, cursor_tool: FakeCursorSdkTool) -> ResumeTool:
    stores = PrivateStores(
        bucket=ArtifactStore(bucket_kind=ArtifactBucket.PRIVATE_USER_ARTIFACTS, bucket_name="b", prefix="private/"),
        local_backup=LocalPrivateBackupStore(tmp_path / "backup"),
    )
    return ResumeTool(cursor_tool, _settings(tmp_path), stores)


def _payload(**overrides) -> AugmentResumePayload:
    defaults = dict(target_role="backend", job_description="## Requirements\n- Python\n- Kubernetes")
    defaults.update(overrides)
    return AugmentResumePayload(**defaults)


@pytest.mark.asyncio
async def test_augment_reports_a_friendly_error_when_cv_is_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_career_ops_scripts(monkeypatch, skill_gap_result=SkillGapResult(ok=True))
    cursor_tool = FakeCursorSdkTool(CursorRunResult(ok=True, status="finished", text="unused"))
    tool = _tool(tmp_path, cursor_tool)

    result = await tool.augment(_payload(), chat_id="42")

    assert result.ok is False
    assert "cv.md" in result.summary
    assert cursor_tool.last_request is None  # never even attempted a run


@pytest.mark.asyncio
async def test_augment_extracts_pdf_text_and_writes_both_stores_with_skill_gaps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _fake_s3: FakeS3Client
) -> None:
    (tmp_path / "cv.md").write_text("# My CV")
    _patch_career_ops_scripts(monkeypatch, skill_gap_result=SkillGapResult(ok=True, existing=["Python"], gap=["Kubernetes", "Rust"]))
    _patch_pdf_reader(monkeypatch, pages=["Jane Smith\nBackend Engineer", "Skills: Python, Kubernetes"])

    pdf_path = tmp_path / "output" / "cv-jane-acme-2026-08-25.pdf"
    cursor_tool = FakeCursorSdkTool(
        CursorRunResult(ok=True, status="finished", run_id="run-1", text="done"), pdf_to_create=pdf_path, pdf_bytes=b"%PDF-1.4 fake"
    )
    tool = _tool(tmp_path, cursor_tool)

    result = await tool.augment(_payload(target_role="backend", job_description="## Requirements\n- Kubernetes"), chat_id="42")

    assert result.ok is True
    assert result.resume is not None
    assert result.resume.skill_gaps == ["Kubernetes", "Rust"]
    assert result.resume.artifact.bucket == ArtifactBucket.PRIVATE_USER_ARTIFACTS
    assert result.resume.artifact.local_backup_path is not None
    # The PDF's own bytes never reach private storage — only its extracted,
    # plain (unescaped — see this module's header comment) text does.
    backup_text = Path(result.resume.artifact.local_backup_path).read_text(encoding="utf-8")
    assert backup_text == "Jane Smith\nBackend Engineer\n\nSkills: Python, Kubernetes"
    assert _fake_s3.put_calls[0]["Body"] == backup_text.encode("utf-8")
    assert b"%PDF" not in _fake_s3.put_calls[0]["Body"]

    # The prompt references the saved JD file, not the raw JD text inline.
    assert "scratch-test.md" in cursor_tool.last_request.prompt
    assert cursor_tool.last_request.tools == ["shell", "read", "edit"]
    assert cursor_tool.last_request.disallowed_tools == ["task"]


@pytest.mark.asyncio
async def test_augment_reports_failure_when_the_pdf_has_no_extractable_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "cv.md").write_text("# My CV")
    _patch_career_ops_scripts(monkeypatch, skill_gap_result=SkillGapResult(ok=True))
    _patch_pdf_reader(monkeypatch, pages=["", "   "])  # e.g. a rasterized/scanned PDF with no selectable text
    pdf_path = tmp_path / "output" / "cv.pdf"
    cursor_tool = FakeCursorSdkTool(CursorRunResult(ok=True, status="finished", text="done"), pdf_to_create=pdf_path)
    tool = _tool(tmp_path, cursor_tool)

    result = await tool.augment(_payload(), chat_id="42")

    assert result.ok is False
    assert result.resume is None
    assert "no selectable text" in result.summary


@pytest.mark.asyncio
async def test_augment_reports_failure_when_the_cursor_run_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "cv.md").write_text("# My CV")
    _patch_career_ops_scripts(monkeypatch, skill_gap_result=SkillGapResult(ok=True))
    cursor_tool = FakeCursorSdkTool(CursorRunResult(ok=False, status="error", error="boom"))
    tool = _tool(tmp_path, cursor_tool)

    result = await tool.augment(_payload(), chat_id="42")

    assert result.ok is False
    assert result.resume is None
    assert "boom" in result.summary


@pytest.mark.asyncio
async def test_augment_reports_failure_when_no_new_pdf_is_produced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "cv.md").write_text("# My CV")
    _patch_career_ops_scripts(monkeypatch, skill_gap_result=SkillGapResult(ok=True))
    cursor_tool = FakeCursorSdkTool(CursorRunResult(ok=True, status="finished", text="done"))  # no pdf_to_create
    tool = _tool(tmp_path, cursor_tool)

    result = await tool.augment(_payload(), chat_id="42")

    assert result.ok is False
    assert result.resume is None
    assert "PDF" in result.summary


@pytest.mark.asyncio
async def test_augment_ignores_pre_existing_pdfs_and_only_picks_up_the_new_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "cv.md").write_text("# My CV")
    _patch_career_ops_scripts(monkeypatch, skill_gap_result=SkillGapResult(ok=True))
    _patch_pdf_reader(monkeypatch, pages=["Text from the newly rendered PDF"])
    (tmp_path / "output").mkdir()
    (tmp_path / "output" / "old.pdf").write_bytes(b"stale")

    new_pdf = tmp_path / "output" / "fresh.pdf"
    cursor_tool = FakeCursorSdkTool(CursorRunResult(ok=True, status="finished", text="done"), pdf_to_create=new_pdf, pdf_bytes=b"new bytes")
    tool = _tool(tmp_path, cursor_tool)

    result = await tool.augment(_payload(), chat_id="42")

    assert result.ok is True
    assert Path(result.resume.artifact.local_backup_path).read_text(encoding="utf-8") == "Text from the newly rendered PDF"


@pytest.mark.asyncio
async def test_augment_proceeds_with_empty_skill_gaps_when_the_check_itself_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A broken/unavailable zero-token skill-gap script must not block
    resume tailoring entirely — it's a nice-to-have surfaced to the user,
    not a hard prerequisite for producing the PDF."""
    (tmp_path / "cv.md").write_text("# My CV")
    _patch_career_ops_scripts(monkeypatch, skill_gap_result=SkillGapResult(ok=False, error="node not found"))
    _patch_pdf_reader(monkeypatch)
    pdf_path = tmp_path / "output" / "cv.pdf"
    cursor_tool = FakeCursorSdkTool(CursorRunResult(ok=True, status="finished", text="done"), pdf_to_create=pdf_path)
    tool = _tool(tmp_path, cursor_tool)

    result = await tool.augment(_payload(), chat_id="42")

    assert result.ok is True
    assert result.resume.skill_gaps == []
