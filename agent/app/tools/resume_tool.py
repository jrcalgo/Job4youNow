"""Turns an AugmentResumePayload into a ResumeResult by running career-ops's
real `pdf` mode — never by asking an LLM to freely rewrite resume text.
That mode reads the one canonical `cv.md` in career-ops-modified, runs a
zero-LLM skill-gap classifier that forbids inventing skills, then a hard
fact-gate before ever rendering an actual ATS-clean PDF.

Two independent pieces of career-ops run for every request:

1. `jd-skill-gap.mjs` as a plain subprocess (career_ops_scripts.py) —
   zero-token, gives us `ResumeResult.skill_gaps` directly, never trusting
   the agent's own account of what the CV does or doesn't support.
2. One Cursor SDK agent run instructed to read `modes/pdf.md` and run its
   full pipeline (keyword extraction, fact gate, HTML build, PDF render)
   against the saved JD — with restricted tools (no subagent spawning).

The resulting PDF is found by diffing `output/**/*.pdf` before and after
the agent run (career-ops names the file itself per its own convention;
diffing is more robust than predicting that name) — but the PDF itself is
never uploaded anywhere or handed back as `ResumeResult.artifact`. Private
user data must never leave this bot as a downloadable file, only as
Telegram chat text (see telegram/src/agent/outbox.mjs's private-artifact
delivery), so `_extract_pdf_text` below reads the PDF's own selectable
text back out instead — modes/pdf.md's ATS rules already require "UTF-8,
selectable text (not rasterized)", so this reuses that same guarantee
rather than re-deriving the tailored content a second way. Only THAT
extracted text (uploaded, PLAIN, to the private-user S3 bucket + local
backup in one call — see tools/artifact_store.py's PrivateStores) ever
becomes a DB write; the PDF stays wherever career-ops's own pipeline left
it, and resume content itself never touches Aurora, matching the
product's stated privacy boundary. Escaping for Telegram's
parse_mode=HTML happens later, at delivery time, in
telegram/src/agent/outbox.mjs's deliverPrivateArtifactAsText — not here —
so what lands in storage stays plain, legible text.
"""
from __future__ import annotations

from pathlib import Path

from pypdf import PdfReader

from agent.app.config import Settings
from agent.app.models.tasks import AugmentResumePayload, TaskKind
from agent.app.models.tool_results import ResumeResult, ToolName, ToolResult
from agent.app.tools.artifact_store import PrivateStores
from agent.app.tools.career_ops_scripts import run_skill_gap_check, write_scratch_job_description
from agent.app.tools.cursor_sdk_tool import CursorPromptRequest, CursorSdkTool

_RESUME_AGENT_TOOLS = ["shell", "read", "edit"]
_RESUME_AGENT_DISALLOWED_TOOLS = ["task"]

_RESUME_AGENT_PROMPT_TEMPLATE = """\
Read modes/pdf.md in this working directory and follow it to tailor cv.md \
for the job description already saved at {jd_relative_path}. Do not ask the \
user for the JD — it is already saved there. Run the mode's full pipeline \
(keyword extraction, fact gate, HTML build, PDF render) and produce a real \
PDF under output/ (or an application bundle path, per the mode's own \
"Application-scoped artifacts" section, if one applies).

Target role: {target_role}
Additional notes from the user (do not treat as a replacement for the \
mode's own pipeline steps): {notes}

The `--hm-audit` opt-in pass stays OFF unless modes/_custom.md turns it on \
— do not pass that flag yourself. This run is single-pass by design; do \
not spawn a background subagent for any step.\
"""


class ResumeTool:
    def __init__(self, cursor_sdk_tool: CursorSdkTool, settings: Settings, private_stores: PrivateStores) -> None:
        self._cursor_sdk_tool = cursor_sdk_tool
        self._settings = settings
        self._private_stores = private_stores

    async def augment(self, payload: AugmentResumePayload, *, chat_id: str) -> ToolResult:
        career_ops_dir = self._settings.career_ops_dir
        cv_path = career_ops_dir / "cv.md"
        if not cv_path.is_file():
            return ToolResult(
                tool=ToolName.RESUME,
                ok=False,
                summary="No cv.md found in career-ops-modified — see agent/infra/career-ops-setup.md to set one up.",
            )

        jd_path = write_scratch_job_description(career_ops_dir, payload.job_description)
        skill_gap = await run_skill_gap_check(career_ops_dir, jd_path)
        skill_gaps = skill_gap.gap if skill_gap.ok else []

        pdfs_before = _existing_pdfs(career_ops_dir)

        prompt = _RESUME_AGENT_PROMPT_TEMPLATE.format(
            jd_relative_path=jd_path.relative_to(career_ops_dir),
            target_role=payload.target_role,
            notes=payload.notes or "(none)",
        )
        request = CursorPromptRequest(
            prompt=prompt,
            chat_id=chat_id,
            task_kind=TaskKind.AUGMENT_RESUME,
            tools=_RESUME_AGENT_TOOLS,
            disallowed_tools=_RESUME_AGENT_DISALLOWED_TOOLS,
            timeout_seconds=self._settings.resume_agent_timeout_seconds,
        )
        run = await self._cursor_sdk_tool.run_prompt(request)
        if not run.ok:
            return ToolResult(tool=ToolName.RESUME, ok=False, summary=run.error or "resume tailoring did not complete")

        new_pdfs = sorted(_existing_pdfs(career_ops_dir) - pdfs_before, key=lambda p: p.stat().st_mtime)
        if not new_pdfs:
            return ToolResult(
                tool=ToolName.RESUME, ok=False, summary="career-ops did not produce a new PDF — check the run's own output."
            )
        pdf_path = new_pdfs[-1]

        resume_text = _extract_pdf_text(pdf_path)
        if not resume_text:
            return ToolResult(
                tool=ToolName.RESUME,
                ok=False,
                summary="career-ops produced a PDF but no selectable text could be extracted from it.",
            )

        relative_key = f"resumes/tailored/{payload.target_role}/{pdf_path.stem}.md"
        location = await self._private_stores.write_private_bytes(relative_key, resume_text.encode("utf-8"))
        resume_result = ResumeResult(
            artifact=location, summary_text=f"Tailored your resume for {payload.target_role}.", skill_gaps=skill_gaps
        )
        return ToolResult(tool=ToolName.RESUME, ok=True, summary=resume_result.summary_text, resume=resume_result)


def _existing_pdfs(career_ops_dir: Path) -> set[Path]:
    output_dir = career_ops_dir / "output"
    if not output_dir.is_dir():
        return set()
    return set(output_dir.rglob("*.pdf"))


def _extract_pdf_text(pdf_path: Path) -> str:
    """Reads back the PDF's own selectable text — see this module's header
    comment for why that (rather than the PDF bytes themselves) is what
    ends up in private storage and, eventually, in the user's chat."""
    reader = PdfReader(str(pdf_path))
    pages = [page.extract_text() or "" for page in reader.pages]
    return "\n\n".join(page_text.strip() for page_text in pages if page_text.strip()).strip()
