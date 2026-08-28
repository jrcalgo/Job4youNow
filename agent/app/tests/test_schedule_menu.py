"""Schedule menu edit/setint flow and interval persistence."""
from __future__ import annotations

import pytest

from agent.app.config import DEFAULT_MODEL_ID, Settings
from agent.app.db import schedule_repo
from agent.app.db.aurora_client import AuroraClient
from agent.app.formatting.presenters import Presenters
from agent.app.models.telegram import TelegramCallback
from agent.app.routing.schedule_menu import handle_schedule_menu_callback
from agent.app.tests.helpers.fake_rds_client import FakeRdsDataClient, formatted_records, param_value


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


def _schedule_row(**overrides) -> dict:
    base = {
        "id": "sched-abc123",
        "chat_id": "42",
        "role_id": "backend",
        "query": "python roles",
        "interval_seconds": 86400,
        "next_run_at": "2026-08-27T00:00:00+00:00",
        "enabled": True,
        "created_at": "2026-08-01T00:00:00+00:00",
    }
    base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_sched_edit_shows_interval_picker() -> None:
    fake = FakeRdsDataClient()

    def on_exec(kwargs: dict) -> dict:
        if "WHERE id = :id" in kwargs.get("sql", ""):
            return formatted_records([_schedule_row()])
        return {"numberOfRecordsUpdated": 0}

    fake.when("execute_statement", on_exec)
    client = _client(fake)
    response = await handle_schedule_menu_callback(
        "42",
        TelegramCallback(action="sched", value="edit:sched-abc123"),
        presenters=Presenters(),
        client=client,
        settings=_settings(),
    )
    assert response.title == "Change interval — backend"
    assert any("sched:setint:sched-abc123:24" in btn.callback_data for row in response.buttons for btn in row)


@pytest.mark.asyncio
async def test_sched_setint_updates_interval_and_returns_list() -> None:
    fake = FakeRdsDataClient()
    updates: list[dict] = []

    def on_exec(kwargs: dict) -> dict:
        sql = kwargs.get("sql", "")
        if "UPDATE scan_schedules" in sql and "interval_seconds" in sql:
            updates.append(kwargs)
            return {"numberOfRecordsUpdated": 1}
        if "FROM scan_schedules WHERE chat_id" in sql:
            return formatted_records([_schedule_row(interval_seconds=43200)])
        if "WHERE id = :id" in sql and "SELECT" in sql:
            return formatted_records([_schedule_row()])
        return {"numberOfRecordsUpdated": 0}

    fake.when("execute_statement", on_exec)
    client = _client(fake)
    response = await handle_schedule_menu_callback(
        "42",
        TelegramCallback(action="sched", value="setint:sched-abc123:12"),
        presenters=Presenters(),
        client=client,
        settings=_settings(),
    )
    assert response.title == "Scan schedules"
    assert len(updates) == 1
    params = updates[0]["parameters"]
    assert param_value(params, "interval_seconds") == {"longValue": 43200}


@pytest.mark.asyncio
async def test_update_interval_repo_sets_next_run_immediately() -> None:
    fake = FakeRdsDataClient()
    updates: list[dict] = []

    def on_exec(kwargs: dict) -> dict:
        sql = kwargs.get("sql", "")
        if "UPDATE scan_schedules" in sql and "interval_seconds" in sql:
            updates.append(kwargs)
            return {"numberOfRecordsUpdated": 1}
        return {"numberOfRecordsUpdated": 0}

    fake.when("execute_statement", on_exec)
    client = _client(fake)
    await schedule_repo.update_interval(client, "sched-abc123", 21600)
    assert len(updates) == 1
    assert param_value(updates[0]["parameters"], "interval_seconds") == {"longValue": 21600}
    assert "next_run_at = now()" in updates[0]["sql"]
