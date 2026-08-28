"""Direct async Cursor SDK calls — no `cursor-agent` CLI subprocess. This
replaces the sync `Agent.prompt(...)` proof in agent/demo_career_ops_agent.py
with the async client/agent lifecycle a long-running service needs (see the
SDK skill's "Python SDK is sync by default... for servers, bots, and
concurrent orchestration, use AsyncClient").

Graph nodes only ever see `run_prompt(request) -> CursorRunResult`. They
never import `AsyncClient`, `AsyncAgent`, `LocalAgentOptions`, or
`CursorAgentError` themselves — this module is the one place the SDK's
lifecycle and its "two kinds of failure" distinction get handled.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Union

from cursor_sdk import AgentOptions, AsyncAgent, AsyncClient, CursorAgentError, LocalAgentOptions, ModelSelection
from pydantic import BaseModel, ConfigDict

from agent.app.config import Settings
from agent.app.db import model_config_repo
from agent.app.db.aurora_client import AuroraClient
from agent.app.logging import get_logger
from agent.app.models.tasks import TaskKind
from agent.app.models.tool_results import CursorRunResult, ToolName, ToolResult

log = get_logger("tools.cursor_sdk")


class CursorPromptRequest(BaseModel):
    """What a graph node/tool provides to run one Cursor SDK prompt. `cwd`
    defaults to the career-ops submodule so career-ops workflows (see
    career_ops_tool.py) and general research prompts both "just work"
    without every call site repeating the same path.

    `chat_id` + `task_kind` are how CursorSdkTool resolves which saved
    model configuration to use (see its module-level resolution order
    below) — both optional so an ad hoc prompt with an explicit `model`
    still works without them.

    `tools`/`disallowed_tools` pass straight through to
    `AsyncAgent.create(...)` — `None` (the default) means the SDK's own
    unrestricted standard toolset, unchanged from before this field existed.
    tools/scan_tool.py and tools/resume_tool.py set these explicitly when
    running a career-ops mode that documents its own required toolset (e.g.
    `shell` to run its `.mjs` scripts) and forbids spawning subagents."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    prompt: str
    cwd: Path | None = None
    model: Union[str, ModelSelection, None] = None
    chat_id: str | None = None
    task_kind: TaskKind | None = None
    timeout_seconds: float | None = None
    tools: list[str] | None = None
    disallowed_tools: list[str] | None = None


class CursorSdkTool:
    """One instance per process, shared across every graph run. `run_limiter`
    is the global "at most N concurrent Cursor runs" semaphore from
    workers/concurrency.py — acquired here, not by callers, so no call site
    can forget it and no call site needs to know the limit's value."""

    def __init__(self, settings: Settings, run_limiter: asyncio.Semaphore, aurora_client: AuroraClient) -> None:
        self._settings = settings
        self._run_limiter = run_limiter
        self._aurora_client = aurora_client

    async def run_prompt(self, request: CursorPromptRequest) -> CursorRunResult:
        async with self._run_limiter:
            return await self._run_prompt_unlimited(request)

    async def _resolve_model(self, request: CursorPromptRequest) -> str | ModelSelection:
        """Resolution order: (1) an explicit `request.model` always wins —
        the escape hatch for one-off overrides; (2) else, if a saved
        per-(chat_id, task_kind) configuration exists, use it; (3) else,
        `settings.cursor_model` (defaults to `config.DEFAULT_MODEL_ID`,
        i.e. Grok 4.5 — see that constant's docstring)."""
        if request.model is not None:
            return request.model

        if request.chat_id is not None and request.task_kind is not None:
            config = await model_config_repo.get_config(self._aurora_client, request.chat_id, request.task_kind)
            if config is not None:
                return config.to_model_selection()

        return self._settings.cursor_model

    async def _run_prompt_unlimited(self, request: CursorPromptRequest) -> CursorRunResult:
        cwd = request.cwd or self._settings.career_ops_dir
        model = await self._resolve_model(request)
        timeout_seconds = request.timeout_seconds or self._settings.cursor_run_timeout_seconds

        # `tools`/`disallowed_tools` aren't flat kwargs on AsyncAgent.create
        # (only model/api_key/name/local/cloud/idempotency_key are) — they
        # go through `options=`, which the SDK deep-merges with the flat
        # kwargs below rather than replacing them (verified against the
        # installed SDK's _resolve_create_agent_options/options_to_json).
        options = (
            AgentOptions(tools=request.tools, disallowed_tools=request.disallowed_tools)
            if request.tools is not None or request.disallowed_tools is not None
            else None
        )

        try:
            async with await AsyncClient.launch_bridge(workspace=str(cwd)) as client:
                async with await AsyncAgent.create(
                    options=options,
                    client=client,
                    model=model,
                    api_key=self._settings.cursor_api_key,
                    local=LocalAgentOptions(cwd=str(cwd)),
                ) as agent:
                    run = await agent.send(request.prompt)
                    log.info("cursor sdk run started", extra={"context": {"agentId": agent.agent_id, "runId": run.run_id}})
                    result = await asyncio.wait_for(run.wait(), timeout=timeout_seconds)
                    return CursorRunResult.from_sdk_result(
                        agent_id=result.agent_id,
                        run_id=result.id,
                        model=result.model.id if result.model else _model_id_for_log(model),
                        status=result.status,
                        text=result.result,
                    )
        except CursorAgentError as error:
            # The run never started at all — auth, config, network. Different
            # from a `result.status == "error"` run that DID start and failed
            # mid-flight, which the branch above already returns as ok=False.
            log.warning("cursor sdk run failed to start", extra={"context": {"error": error.message, "retryable": error.is_retryable}})
            return CursorRunResult.startup_error(message=error.message, retryable=error.is_retryable)
        except asyncio.TimeoutError:
            log.warning("cursor sdk run timed out", extra={"context": {"timeoutSeconds": timeout_seconds}})
            return CursorRunResult.startup_error(message=f"run exceeded {timeout_seconds}s timeout", retryable=True)

    async def run_prompt_as_tool_result(self, request: CursorPromptRequest) -> ToolResult:
        run = await self.run_prompt(request)
        summary = run.text[:200] if run.ok and run.text else (run.error or "cursor run did not complete")
        return ToolResult(tool=ToolName.CURSOR_SDK, ok=run.ok, summary=summary, cursor_run=run)


def _model_id_for_log(model: str | ModelSelection) -> str:
    return model if isinstance(model, str) else model.id
