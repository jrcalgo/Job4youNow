"""db/model_config_repo.py against a fake Data API client: proves the
upsert's SQL shape (ON CONFLICT upsert, params cast to jsonb) and that a
get/list against an empty table returns None/{} rather than raising.
"""
from __future__ import annotations

import pytest

from agent.app.db import model_config_repo
from agent.app.db.aurora_client import AuroraClient
from agent.app.models.model_catalog import ModelConfig, ModelParamChoice
from agent.app.models.tasks import TaskKind
from agent.app.tests.helpers.fake_rds_client import FakeRdsDataClient, formatted_records, param_value


def _client(fake: FakeRdsDataClient) -> AuroraClient:
    return AuroraClient(fake, resource_arn="arn:aurora", secret_arn="arn:secret", database="job4younow")


def _config(**overrides) -> ModelConfig:
    defaults = dict(
        chat_id="42",
        task_kind=TaskKind.SCAN_ROLE,
        model_id="grok-4.5",
        model_display_name="Grok 4.5",
        params=[ModelParamChoice(id="reasoning_effort", value="high")],
    )
    defaults.update(overrides)
    return ModelConfig(**defaults)


@pytest.mark.asyncio
async def test_get_config_returns_none_when_no_row_exists() -> None:
    fake = FakeRdsDataClient()
    fake.when("execute_statement", lambda kwargs: formatted_records([]))

    config = await model_config_repo.get_config(_client(fake), "42", TaskKind.SCAN_ROLE)

    assert config is None


@pytest.mark.asyncio
async def test_get_config_parses_a_matching_row() -> None:
    fake = FakeRdsDataClient()
    row = _config().model_dump(mode="json")
    fake.when("execute_statement", lambda kwargs: formatted_records([row]))

    config = await model_config_repo.get_config(_client(fake), "42", TaskKind.SCAN_ROLE)

    assert config is not None
    assert config.model_id == "grok-4.5"
    assert config.params == [ModelParamChoice(id="reasoning_effort", value="high")]

    call = fake.calls[0]
    assert param_value(call.kwargs["parameters"], "chat_id") == {"stringValue": "42"}
    assert param_value(call.kwargs["parameters"], "task_kind") == {"stringValue": "scan_role"}


@pytest.mark.asyncio
async def test_list_configs_returns_a_dict_keyed_by_task_kind() -> None:
    fake = FakeRdsDataClient()
    rows = [
        _config(task_kind=TaskKind.SCAN_ROLE).model_dump(mode="json"),
        _config(task_kind=TaskKind.AUGMENT_RESUME, model_id="composer-2.5", model_display_name="Composer 2.5", params=[]).model_dump(
            mode="json"
        ),
    ]
    fake.when("execute_statement", lambda kwargs: formatted_records(rows))

    configs = await model_config_repo.list_configs(_client(fake), "42")

    assert set(configs.keys()) == {TaskKind.SCAN_ROLE, TaskKind.AUGMENT_RESUME}
    assert configs[TaskKind.AUGMENT_RESUME].model_id == "composer-2.5"


@pytest.mark.asyncio
async def test_list_configs_returns_empty_dict_when_nothing_configured() -> None:
    fake = FakeRdsDataClient()
    fake.when("execute_statement", lambda kwargs: formatted_records([]))

    configs = await model_config_repo.list_configs(_client(fake), "42")

    assert configs == {}


@pytest.mark.asyncio
async def test_upsert_config_sends_an_on_conflict_upsert_with_jsonb_params() -> None:
    fake = FakeRdsDataClient()

    await model_config_repo.upsert_config(_client(fake), _config())

    call = fake.calls[0]
    assert "INSERT INTO agent_model_config" in call.kwargs["sql"]
    assert "ON CONFLICT (chat_id, task_kind) DO UPDATE" in call.kwargs["sql"]
    assert "::jsonb" in call.kwargs["sql"]
    assert param_value(call.kwargs["parameters"], "model_id") == {"stringValue": "grok-4.5"}

    params_parameter = next(p for p in call.kwargs["parameters"] if p["name"] == "params")
    assert params_parameter["typeHint"] == "JSON"
    assert "reasoning_effort" in params_parameter["value"]["stringValue"]


@pytest.mark.asyncio
async def test_upsert_then_get_round_trips_through_a_shared_fake_table() -> None:
    """Simulates persistence across the two calls by having the fake table
    remember the last upserted row and hand it back to the SELECT."""
    fake = FakeRdsDataClient()
    stored: dict = {}

    def handle_execute(kwargs: dict) -> dict:
        if "INSERT INTO agent_model_config" in kwargs["sql"]:
            stored.update(
                chat_id=param_value(kwargs["parameters"], "chat_id")["stringValue"],
                task_kind=param_value(kwargs["parameters"], "task_kind")["stringValue"],
                model_id=param_value(kwargs["parameters"], "model_id")["stringValue"],
                model_display_name=param_value(kwargs["parameters"], "model_display_name")["stringValue"],
                params=[{"id": "reasoning_effort", "value": "high"}],
            )
            return {"numberOfRecordsUpdated": 1}
        return formatted_records([stored] if stored else [])

    fake.when("execute_statement", handle_execute)

    await model_config_repo.upsert_config(_client(fake), _config())
    round_tripped = await model_config_repo.get_config(_client(fake), "42", TaskKind.SCAN_ROLE)

    assert round_tripped is not None
    assert round_tripped.model_id == "grok-4.5"
    assert round_tripped.params[0].value == "high"
