"""Callback-action recognition (routing/intent_router.py) and every wizard
screen transition (routing/model_config.py) for the Telegram model
configuration menu — against a fake ModelCatalogService and a fake Aurora
Data API client, never the real Cursor SDK or network.
"""
from __future__ import annotations

import pytest

from agent.app.cache.model_wizard_cache import ModelWizardCache
from agent.app.cache.ttl_cache import TtlCache
from agent.app.config import DEFAULT_MODEL_ID, Settings
from agent.app.db.aurora_client import AuroraClient
from agent.app.formatting.presenters import Presenters
from agent.app.models.intent import RouteKind
from agent.app.models.model_catalog import ModelCatalogEntry, ModelParamChoice, ModelParamDefinition, ModelParamValueOption, ModelVariantInfo
from agent.app.models.responses import UserFacingResponse
from agent.app.models.tasks import TaskKind
from agent.app.models.telegram import TelegramCallback, TelegramCommand
from agent.app.routing.intent_router import parse_callback, route_telegram_command
from agent.app.routing.model_config import handle_model_config_callback
from agent.app.tests.helpers.fake_rds_client import FakeRdsDataClient, formatted_records, param_value


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


def _client(fake: FakeRdsDataClient) -> AuroraClient:
    return AuroraClient(fake, resource_arn="arn:aurora", secret_arn="arn:secret", database="job4younow")


def _empty_config_rds() -> FakeRdsDataClient:
    fake = FakeRdsDataClient()
    fake.when("execute_statement", lambda kwargs: formatted_records([]))
    return fake


def _model_with_variant_and_param() -> ModelCatalogEntry:
    return ModelCatalogEntry(
        id="grok-4.5",
        display_name="Grok 4.5",
        description="Fast, general-purpose model.",
        parameters=[
            ModelParamDefinition(
                id="reasoning_effort",
                display_name="Reasoning effort",
                values=[
                    ModelParamValueOption(value="low", display_name="Low"),
                    ModelParamValueOption(value="high", display_name="High"),
                ],
            )
        ],
        variants=[
            ModelVariantInfo(
                display_name="Thinking",
                description="Higher reasoning effort.",
                is_default=True,
                params=[ModelParamChoice(id="reasoning_effort", value="high")],
            )
        ],
    )


def _plain_model() -> ModelCatalogEntry:
    return ModelCatalogEntry(id="plain-model", display_name="Plain Model", description="No knobs.", parameters=[], variants=[])


class FakeModelCatalogService:
    def __init__(self, models: list[ModelCatalogEntry]) -> None:
        self._models = models
        self.call_count = 0

    async def list_models(self) -> list[ModelCatalogEntry]:
        self.call_count += 1
        return self._models


class _Dispatcher:
    """Bundles the dependencies `handle_model_config_callback` needs so
    each test only has to name what actually varies (chat_id, action,
    value) — everything else is the same fake wiring."""

    def __init__(self, models: list[ModelCatalogEntry], *, fake_rds: FakeRdsDataClient | None = None) -> None:
        self.catalog = FakeModelCatalogService(models)
        self.wizard_cache = ModelWizardCache(TtlCache())
        self.fake_rds = fake_rds or _empty_config_rds()
        self.client = _client(self.fake_rds)
        self.presenters = Presenters()

    async def send(self, chat_id: str, action: str | None, value: str | None = None) -> UserFacingResponse:
        callback = None if action is None else TelegramCallback(action=action, value=value)
        return await handle_model_config_callback(
            chat_id,
            callback,
            client=self.client,
            settings=_settings(),
            model_catalog=self.catalog,
            wizard_cache=self.wizard_cache,
            presenters=self.presenters,
        )


# -- Callback action recognition -- routing/intent_router.py


@pytest.mark.parametrize(
    "raw_callback_data",
    [
        "modelmenu",
        "modeltask:scan_role",
        "modelrefresh",
        "modelpick:0",
        "modelvariant:0",
        "modeldefault",
        "modelcustom",
        "modelparam:0",
        "modelback",
    ],
)
def test_every_model_config_action_routes_to_model_config(raw_callback_data: str) -> None:
    callback = parse_callback(raw_callback_data)
    intent = route_telegram_command(TelegramCommand(chat_id="42", update_id=1, callback=callback))

    assert intent.route == RouteKind.MODEL_CONFIG
    assert intent.callback == callback


def test_models_text_command_routes_to_model_config_with_no_callback() -> None:
    intent = route_telegram_command(TelegramCommand(chat_id="42", update_id=1, text="MODELS"))
    assert intent.route == RouteKind.MODEL_CONFIG
    assert intent.callback is None


def test_slash_models_command_also_routes_to_model_config() -> None:
    intent = route_telegram_command(TelegramCommand(chat_id="42", update_id=1, text="/models"))
    assert intent.route == RouteKind.MODEL_CONFIG


# -- Wizard screen transitions -- routing/model_config.py


@pytest.mark.asyncio
async def test_no_callback_shows_task_menu_with_default_model_label() -> None:
    dispatcher = _Dispatcher([])
    response = await dispatcher.send("42", None)

    assert "Scan roles" in response.body
    assert "grok-4.5 (default)" in response.body
    assert response.buttons[1][0].callback_data == "modeltask:scan_role"


@pytest.mark.asyncio
async def test_modeltask_fetches_live_models_and_caches_wizard_state() -> None:
    dispatcher = _Dispatcher([_model_with_variant_and_param(), _plain_model()])

    response = await dispatcher.send("42", "modeltask", "scan_role")

    assert dispatcher.catalog.call_count == 1
    assert len(response.buttons) == 5  # main + 2 models + refresh + back
    state = dispatcher.wizard_cache.get("42")
    assert state is not None
    assert state.task_kind == TaskKind.SCAN_ROLE
    assert len(state.models) == 2


@pytest.mark.asyncio
async def test_modelrefresh_always_refetches_for_the_cached_task_kind() -> None:
    dispatcher = _Dispatcher([_model_with_variant_and_param()])
    await dispatcher.send("42", "modeltask", "scan_role")

    await dispatcher.send("42", "modelrefresh")

    assert dispatcher.catalog.call_count == 2  # never cached -- always a live fetch, per the plan's non-goals


@pytest.mark.asyncio
async def test_modelpick_shows_model_detail_with_variant_and_customize_buttons() -> None:
    dispatcher = _Dispatcher([_model_with_variant_and_param()])
    await dispatcher.send("42", "modeltask", "scan_role")

    response = await dispatcher.send("42", "modelpick", "0")

    callback_data_values = [button.callback_data for row in response.buttons for button in row]
    assert "modelvariant:0" in callback_data_values
    assert "modelcustom" in callback_data_values
    assert "modeldefault" in callback_data_values


@pytest.mark.asyncio
async def test_modelpick_with_an_out_of_range_index_fails_gracefully() -> None:
    dispatcher = _Dispatcher([_model_with_variant_and_param()])
    await dispatcher.send("42", "modeltask", "scan_role")

    response = await dispatcher.send("42", "modelpick", "99")

    assert "no longer in the list" in response.body


@pytest.mark.asyncio
async def test_modelvariant_persists_config_and_confirms() -> None:
    dispatcher = _Dispatcher([_model_with_variant_and_param()])
    await dispatcher.send("42", "modeltask", "scan_role")
    await dispatcher.send("42", "modelpick", "0")

    response = await dispatcher.send("42", "modelvariant", "0")

    assert "Grok 4.5" in response.body
    insert_call = next(c for c in dispatcher.fake_rds.calls if "INSERT INTO agent_model_config" in c.kwargs.get("sql", ""))
    assert param_value(insert_call.kwargs["parameters"], "model_id") == {"stringValue": "grok-4.5"}
    assert dispatcher.wizard_cache.get("42") is None  # cleared once saved


@pytest.mark.asyncio
async def test_modeldefault_persists_config_with_empty_params() -> None:
    dispatcher = _Dispatcher([_model_with_variant_and_param()])
    await dispatcher.send("42", "modeltask", "scan_role")
    await dispatcher.send("42", "modelpick", "0")

    response = await dispatcher.send("42", "modeldefault")

    assert "default settings" in response.body
    insert_call = next(c for c in dispatcher.fake_rds.calls if "INSERT INTO agent_model_config" in c.kwargs.get("sql", ""))
    params_parameter = next(p for p in insert_call.kwargs["parameters"] if p["name"] == "params")
    assert params_parameter["value"]["stringValue"] == "[]"


@pytest.mark.asyncio
async def test_modelcustom_then_modelparam_walks_every_parameter_then_saves() -> None:
    dispatcher = _Dispatcher([_model_with_variant_and_param()])
    await dispatcher.send("42", "modeltask", "scan_role")
    await dispatcher.send("42", "modelpick", "0")

    step_response = await dispatcher.send("42", "modelcustom")
    assert "step 1 of 1" in step_response.title.lower()

    final_response = await dispatcher.send("42", "modelparam", "1")  # index 1 = "high"

    assert "reasoning_effort=high" in final_response.body
    insert_call = next(c for c in dispatcher.fake_rds.calls if "INSERT INTO agent_model_config" in c.kwargs.get("sql", ""))
    assert param_value(insert_call.kwargs["parameters"], "model_id") == {"stringValue": "grok-4.5"}


@pytest.mark.asyncio
async def test_modelcustom_on_a_model_with_no_parameters_saves_defensively_with_empty_params() -> None:
    """The presenter never renders a "Customize" button when a model has no
    parameters, but the handler itself must still not crash if it somehow
    receives that action anyway."""
    dispatcher = _Dispatcher([_plain_model()])
    await dispatcher.send("42", "modeltask", "scan_role")
    await dispatcher.send("42", "modelpick", "0")

    response = await dispatcher.send("42", "modelcustom")

    assert "default settings" in response.body


@pytest.mark.asyncio
@pytest.mark.parametrize("action,value", [("modelrefresh", None), ("modelpick", "0"), ("modelvariant", "0"), ("modeldefault", None), ("modelcustom", None), ("modelparam", "0")])
async def test_every_stateful_action_fails_gracefully_without_a_prior_wizard_session(action: str, value: str | None) -> None:
    dispatcher = _Dispatcher([])
    response = await dispatcher.send("42", action, value)
    assert "expired" in response.body.lower()


@pytest.mark.asyncio
async def test_unrecognized_action_returns_a_friendly_error() -> None:
    dispatcher = _Dispatcher([])
    response = await dispatcher.send("42", "modelbogus")
    assert "Unrecognized" in response.body


@pytest.mark.asyncio
async def test_modelback_returns_to_the_task_menu() -> None:
    dispatcher = _Dispatcher([_model_with_variant_and_param()])
    await dispatcher.send("42", "modeltask", "scan_role")

    response = await dispatcher.send("42", "modelback")

    assert "Scan roles" in response.body
    assert "Resume augmentation" in response.body


# -- callback_data stays within Telegram's 64-byte limit --


def test_model_list_callback_data_stays_within_64_bytes_even_with_many_models() -> None:
    models = [
        ModelCatalogEntry(id=f"model-{i}", display_name=f"Model {i}", description="", parameters=[], variants=[])
        for i in range(99)
    ]
    response = Presenters().model_list(TaskKind.RESEARCH_COMPANY, models, current_model_id=None)

    for row in response.buttons:
        for button in row:
            assert len(button.callback_data.encode("utf-8")) <= 64


def test_model_task_menu_callback_data_stays_within_64_bytes_for_every_task_kind() -> None:
    response = Presenters().model_task_menu({}, "grok-4.5")
    for row in response.buttons:
        for button in row:
            assert len(button.callback_data.encode("utf-8")) <= 64
