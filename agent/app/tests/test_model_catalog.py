"""ModelCatalogEntry.from_sdk_model's conversion of real cursor_sdk dataclass
shapes, and ModelCatalogService.list_models against a fake AsyncCursor —
never a real cursor-sdk-bridge subprocess or network call.
"""
from __future__ import annotations

import pytest
from cursor_sdk import (
    CursorAgentError,
    ModelParameterDefinition,
    ModelParameterDefinitionValue,
    ModelParameterValue,
    ModelVariant,
    SDKModel,
)

from agent.app.config import DEFAULT_MODEL_ID, Settings
from agent.app.models.model_catalog import ModelCatalogEntry
from agent.app.tools import model_catalog as model_catalog_module
from agent.app.tools.model_catalog import ModelCatalogError, ModelCatalogService
from agent.app.tests.helpers.fake_cursor_sdk import FakeAsyncClient, FakeAsyncCursor, FakeAsyncCursorModels


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


def _sdk_model() -> SDKModel:
    return SDKModel(
        id="grok-4.5",
        display_name="Grok 4.5",
        description="Fast, general-purpose model.",
        parameters=[
            ModelParameterDefinition(
                id="reasoning_effort",
                display_name="Reasoning effort",
                values=[
                    ModelParameterDefinitionValue(value="low", display_name="Low"),
                    ModelParameterDefinitionValue(value="high", display_name="High"),
                ],
            )
        ],
        variants=[
            ModelVariant(
                display_name="Thinking",
                description="Higher reasoning effort.",
                is_default=True,
                params=[ModelParameterValue(id="reasoning_effort", value="high")],
            )
        ],
    )


@pytest.fixture(autouse=True)
def _patch_sdk_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(model_catalog_module, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(model_catalog_module, "AsyncCursor", FakeAsyncCursor)
    FakeAsyncClient.configure(None)


def test_model_catalog_entry_from_sdk_model_converts_nested_shapes() -> None:
    entry = ModelCatalogEntry.from_sdk_model(_sdk_model())

    assert entry.id == "grok-4.5"
    assert entry.display_name == "Grok 4.5"
    assert entry.description == "Fast, general-purpose model."

    assert len(entry.parameters) == 1
    param = entry.parameters[0]
    assert param.id == "reasoning_effort"
    assert [v.value for v in param.values] == ["low", "high"]
    assert [v.display_name for v in param.values] == ["Low", "High"]

    assert len(entry.variants) == 1
    variant = entry.variants[0]
    assert variant.display_name == "Thinking"
    assert variant.is_default is True


def test_model_catalog_entry_from_sdk_model_variant_params_round_trip() -> None:
    entry = ModelCatalogEntry.from_sdk_model(_sdk_model())
    variant = entry.variants[0]
    assert len(variant.params) == 1
    assert variant.params[0].id == "reasoning_effort"
    assert variant.params[0].value == "high"


@pytest.mark.asyncio
async def test_list_models_converts_every_sdk_model() -> None:
    FakeAsyncCursorModels.configure([_sdk_model()])
    service = ModelCatalogService(_settings())

    models = await service.list_models()

    assert len(models) == 1
    assert isinstance(models[0], ModelCatalogEntry)
    assert models[0].id == "grok-4.5"
    assert FakeAsyncCursorModels.calls[0]["api_key"] == "cursor_test_key"


@pytest.mark.asyncio
async def test_list_models_returns_empty_list_when_catalog_is_empty() -> None:
    FakeAsyncCursorModels.configure([])
    service = ModelCatalogService(_settings())

    models = await service.list_models()

    assert models == []


@pytest.mark.asyncio
async def test_list_models_raises_model_catalog_error_on_sdk_failure() -> None:
    FakeAsyncCursorModels.configure(CursorAgentError("no api key configured", is_retryable=False))
    service = ModelCatalogService(_settings())

    with pytest.raises(ModelCatalogError, match="no api key"):
        await service.list_models()
