"""Light integration tests: settings callbacks reach the right presenter screen."""
from __future__ import annotations

import pytest

from agent.app.cache.backlog_browser_cache import BacklogBrowserCache
from agent.app.cache.backlog_filter_cache import BacklogFilterWizardCache
from agent.app.cache.model_wizard_cache import ModelWizardCache
from agent.app.cache.ttl_cache import TtlCache
from agent.app.config import DEFAULT_MODEL_ID, Settings
from agent.app.db.aurora_client import AuroraClient
from agent.app.formatting.presenters import Presenters
from agent.app.models.intent import IntentDecision, RouteKind
from agent.app.models.telegram import TelegramCallback
from agent.app.routing.deterministic import ReadService, handle_deterministic_route
from agent.app.routing.schedule_menu import handle_schedule_menu_callback
from agent.app.tests.helpers.fake_rds_client import FakeRdsDataClient, formatted_records


def _settings() -> Settings:
    return Settings(
        AWS_REGION="us-east-1",
        AURORA_RESOURCE_ARN="arn:aurora",
        AURORA_SECRET_ARN="arn:secret",
        JOB_ARTIFACTS_BUCKET="job-bucket",
        PRIVATE_USER_ARTIFACTS_BUCKET="private-bucket",
        CURSOR_API_KEY="cursor_test_key",
        CURSOR_MODEL=DEFAULT_MODEL_ID,
    )


def _client(fake: FakeRdsDataClient) -> AuroraClient:
    return AuroraClient(fake, resource_arn="arn:aurora", secret_arn="arn:secret", database="job4younow")


class _FakeCatalog:
    async def list_models(self):
        return []


@pytest.mark.asyncio
async def test_settings_menu_route_renders_settings_title() -> None:
    fake = FakeRdsDataClient()
    client = _client(fake)
    cache = TtlCache()
    intent = IntentDecision.for_route("42", RouteKind.SETTINGS_MENU)
    response = await handle_deterministic_route(
        intent,
        ReadService(client, cache, ttl_seconds=60),
        Presenters(),
        client=client,
        settings=_settings(),
        model_catalog=_FakeCatalog(),
        wizard_cache=ModelWizardCache(cache),
        browser_cache=BacklogBrowserCache(cache),
        filter_cache=BacklogFilterWizardCache(cache),
    )
    assert response.title == "Settings"


@pytest.mark.asyncio
async def test_settings_schedules_callback_renders_schedule_list_title() -> None:
    fake = FakeRdsDataClient()
    fake.when("execute_statement", lambda kwargs: formatted_records([]))
    client = _client(fake)
    response = await handle_schedule_menu_callback(
        "42",
        TelegramCallback(action="sched", value="list"),
        presenters=Presenters(),
        client=client,
        settings=_settings(),
    )
    assert response.title == "Scan schedules"
