"""Proves CursorSdkTool's "two kinds of failure" handling (see its module
docstring): a run that finishes, one that starts but errors, one that never
starts (CursorAgentError), and one that times out — all without spawning a
real cursor-sdk-bridge subprocess.
"""
from __future__ import annotations

import asyncio

import pytest
from cursor_sdk import AgentOptions, CursorAgentError, ModelSelection

from agent.app.config import DEFAULT_MODEL_ID, Settings
from agent.app.db.aurora_client import AuroraClient
from agent.app.models.model_catalog import ModelConfig
from agent.app.models.tasks import TaskKind
from agent.app.tools import cursor_sdk_tool as cursor_sdk_tool_module
from agent.app.tools.cursor_sdk_tool import CursorPromptRequest, CursorSdkTool
from agent.app.tests.helpers.fake_cursor_sdk import FakeAsyncAgent, FakeAsyncClient, FakeRunResult
from agent.app.tests.helpers.fake_rds_client import FakeRdsDataClient, formatted_records


def _settings() -> Settings:
    return Settings(
        AWS_REGION="us-east-1",
        AURORA_RESOURCE_ARN="arn:aurora",
        AURORA_SECRET_ARN="arn:secret",
        JOB_ARTIFACTS_BUCKET="job-bucket",
        PRIVATE_USER_ARTIFACTS_BUCKET="private-bucket",
        CURSOR_API_KEY="cursor_test_key",
        CURSOR_MODEL=DEFAULT_MODEL_ID,  # hermetic against a real local .env
    )


def _fake_aurora_client(fake: FakeRdsDataClient | None = None) -> AuroraClient:
    """Most tests in this file don't pass `chat_id`/`task_kind`, so
    CursorSdkTool never actually queries this — it just needs to be a real
    AuroraClient instance to satisfy the constructor. The model-resolution
    tests below DO exercise the lookup, passing their own configured fake."""
    return AuroraClient(fake or FakeRdsDataClient(), resource_arn="arn:aurora", secret_arn="arn:secret", database="job4younow")


@pytest.fixture(autouse=True)
def _patch_sdk_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test still picks its own AsyncAgent fake (behavior varies per
    test); only AsyncClient's fake is common to all of them."""
    monkeypatch.setattr(cursor_sdk_tool_module, "AsyncClient", FakeAsyncClient)


@pytest.mark.asyncio
async def test_run_prompt_returns_ok_on_a_finished_run(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cursor_sdk_tool_module, "AsyncAgent", FakeAsyncAgent)
    FakeAsyncClient.configure(FakeRunResult(status="finished", result="the answer"))

    tool = CursorSdkTool(_settings(), asyncio.Semaphore(1), _fake_aurora_client())
    result = await tool.run_prompt(CursorPromptRequest(prompt="hello"))

    assert result.ok is True
    assert result.status == "finished"
    assert result.text == "the answer"
    assert result.model == "composer-2.5"


@pytest.mark.asyncio
async def test_run_prompt_reports_ok_false_when_run_errors_mid_flight(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cursor_sdk_tool_module, "AsyncAgent", FakeAsyncAgent)
    FakeAsyncClient.configure(FakeRunResult(status="error", result=""))

    tool = CursorSdkTool(_settings(), asyncio.Semaphore(1), _fake_aurora_client())
    result = await tool.run_prompt(CursorPromptRequest(prompt="hello"))

    assert result.ok is False
    assert result.status == "error"
    assert result.retryable is False  # started and failed - distinct from a startup error


@pytest.mark.asyncio
async def test_run_prompt_distinguishes_a_startup_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cursor_sdk_tool_module, "AsyncAgent", FakeAsyncAgent)
    FakeAsyncClient.configure(CursorAgentError("no api key configured", is_retryable=False))

    tool = CursorSdkTool(_settings(), asyncio.Semaphore(1), _fake_aurora_client())
    result = await tool.run_prompt(CursorPromptRequest(prompt="hello"))

    assert result.ok is False
    assert result.status == "startup_error"
    assert "no api key" in result.error


@pytest.mark.asyncio
async def test_run_prompt_treats_a_timeout_as_retryable(monkeypatch: pytest.MonkeyPatch) -> None:
    class SlowFakeAgent(FakeAsyncAgent):
        async def send(self, _prompt: str):
            run = await super().send(_prompt)

            class NeverFinishes:
                run_id = run.run_id

                async def wait(self):
                    await asyncio.sleep(10)

            return NeverFinishes()

    monkeypatch.setattr(cursor_sdk_tool_module, "AsyncAgent", SlowFakeAgent)
    FakeAsyncClient.configure(FakeRunResult())

    tool = CursorSdkTool(_settings(), asyncio.Semaphore(1), _fake_aurora_client())
    result = await tool.run_prompt(CursorPromptRequest(prompt="hello", timeout_seconds=0.01))

    assert result.ok is False
    assert result.retryable is True


@pytest.mark.asyncio
async def test_run_limiter_caps_concurrent_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    concurrent_count = 0
    max_observed = 0

    class TrackingFakeAgent(FakeAsyncAgent):
        async def send(self, _prompt: str):
            nonlocal concurrent_count, max_observed
            concurrent_count += 1
            max_observed = max(max_observed, concurrent_count)
            await asyncio.sleep(0.02)
            run = await super().send(_prompt)
            concurrent_count -= 1
            return run

    monkeypatch.setattr(cursor_sdk_tool_module, "AsyncAgent", TrackingFakeAgent)
    FakeAsyncClient.configure(FakeRunResult())

    limiter = asyncio.Semaphore(2)
    tool = CursorSdkTool(_settings(), limiter, _fake_aurora_client())
    await asyncio.gather(*(tool.run_prompt(CursorPromptRequest(prompt="hello")) for _ in range(5)))

    assert max_observed <= 2


# -- Model resolution order: explicit request.model > persisted per-(chat_id,
# -- task_kind) config > settings.cursor_model (DEFAULT_MODEL_ID). See
# -- CursorSdkTool._resolve_model's docstring.


@pytest.mark.asyncio
async def test_resolve_model_prefers_explicit_request_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cursor_sdk_tool_module, "AsyncAgent", FakeAsyncAgent)
    FakeAsyncClient.configure(FakeRunResult())

    fake_rds = FakeRdsDataClient()
    fake_rds.when(
        "execute_statement",
        lambda kwargs: formatted_records([_config_row(model_id="should-not-be-used")]),
    )
    tool = CursorSdkTool(_settings(), asyncio.Semaphore(1), _fake_aurora_client(fake_rds))

    explicit = ModelSelection(id="explicit-override", params=[])
    await tool.run_prompt(CursorPromptRequest(prompt="hello", model=explicit, chat_id="42", task_kind=TaskKind.SCAN_ROLE))

    assert FakeAsyncAgent.last_create_kwargs["model"] is explicit
    assert not fake_rds.calls  # the DB lookup must be skipped entirely when model is explicit


@pytest.mark.asyncio
async def test_resolve_model_uses_persisted_config_when_no_explicit_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cursor_sdk_tool_module, "AsyncAgent", FakeAsyncAgent)
    FakeAsyncClient.configure(FakeRunResult())

    fake_rds = FakeRdsDataClient()
    fake_rds.when(
        "execute_statement",
        lambda kwargs: formatted_records([_config_row(model_id="persisted-model", params=[{"id": "effort", "value": "high"}])]),
    )
    tool = CursorSdkTool(_settings(), asyncio.Semaphore(1), _fake_aurora_client(fake_rds))

    await tool.run_prompt(CursorPromptRequest(prompt="hello", chat_id="42", task_kind=TaskKind.SCAN_ROLE))

    resolved = FakeAsyncAgent.last_create_kwargs["model"]
    assert isinstance(resolved, ModelSelection)
    assert resolved.id == "persisted-model"
    assert len(resolved.params) == 1
    assert resolved.params[0].id == "effort"
    assert resolved.params[0].value == "high"


@pytest.mark.asyncio
async def test_resolve_model_falls_back_to_default_when_no_config_and_no_explicit_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cursor_sdk_tool_module, "AsyncAgent", FakeAsyncAgent)
    FakeAsyncClient.configure(FakeRunResult())

    fake_rds = FakeRdsDataClient()
    fake_rds.when("execute_statement", lambda kwargs: formatted_records([]))
    tool = CursorSdkTool(_settings(), asyncio.Semaphore(1), _fake_aurora_client(fake_rds))

    await tool.run_prompt(CursorPromptRequest(prompt="hello", chat_id="42", task_kind=TaskKind.SCAN_ROLE))

    assert FakeAsyncAgent.last_create_kwargs["model"] == DEFAULT_MODEL_ID


@pytest.mark.asyncio
async def test_resolve_model_falls_back_to_default_when_chat_id_or_task_kind_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """A DB lookup requires BOTH chat_id and task_kind — an ad hoc prompt
    with neither should never even attempt one."""
    monkeypatch.setattr(cursor_sdk_tool_module, "AsyncAgent", FakeAsyncAgent)
    FakeAsyncClient.configure(FakeRunResult())

    fake_rds = FakeRdsDataClient()
    tool = CursorSdkTool(_settings(), asyncio.Semaphore(1), _fake_aurora_client(fake_rds))

    await tool.run_prompt(CursorPromptRequest(prompt="hello"))

    assert FakeAsyncAgent.last_create_kwargs["model"] == DEFAULT_MODEL_ID
    assert not fake_rds.calls


def _config_row(*, model_id: str, params: list[dict] | None = None) -> dict:
    return ModelConfig(
        chat_id="42", task_kind=TaskKind.SCAN_ROLE, model_id=model_id, model_display_name=model_id, params=params or []
    ).model_dump(mode="json")


# -- `tools`/`disallowed_tools` passthrough — see CursorPromptRequest's and
# -- _run_prompt_unlimited's docstrings on why these go through `options=`
# -- rather than as flat kwargs to AsyncAgent.create.


@pytest.mark.asyncio
async def test_run_prompt_passes_tools_and_disallowed_tools_through_options(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cursor_sdk_tool_module, "AsyncAgent", FakeAsyncAgent)
    FakeAsyncClient.configure(FakeRunResult())
    tool = CursorSdkTool(_settings(), asyncio.Semaphore(1), _fake_aurora_client())

    await tool.run_prompt(CursorPromptRequest(prompt="hello", tools=["shell", "webSearch"], disallowed_tools=["task"]))

    options = FakeAsyncAgent.last_create_kwargs["options"]
    assert isinstance(options, AgentOptions)
    assert options.tools == ["shell", "webSearch"]
    assert options.disallowed_tools == ["task"]
    # The flat model/api_key/local kwargs must still be present alongside
    # options — the SDK deep-merges them rather than one replacing the other.
    assert FakeAsyncAgent.last_create_kwargs["model"] == DEFAULT_MODEL_ID
    assert FakeAsyncAgent.last_create_kwargs["api_key"] == "cursor_test_key"


@pytest.mark.asyncio
async def test_run_prompt_passes_no_options_when_tools_are_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unchanged behavior for every existing call site that doesn't set
    tools/disallowed_tools (e.g. CareerOpsTool's research path)."""
    monkeypatch.setattr(cursor_sdk_tool_module, "AsyncAgent", FakeAsyncAgent)
    FakeAsyncClient.configure(FakeRunResult())
    tool = CursorSdkTool(_settings(), asyncio.Semaphore(1), _fake_aurora_client())

    await tool.run_prompt(CursorPromptRequest(prompt="hello"))

    assert FakeAsyncAgent.last_create_kwargs["options"] is None
