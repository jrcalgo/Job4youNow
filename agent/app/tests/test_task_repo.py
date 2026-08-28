"""db/task_repo.py against a fake Data API client: proves the idempotent
create, the claim query's shape (source/owner/lease params, SKIP LOCKED
present so two pools never block each other), and that a `None` result
means "nothing to claim" rather than raising.
"""
from __future__ import annotations

import pytest

from agent.app.db import task_repo
from agent.app.db.aurora_client import AuroraClient
from agent.app.models.tasks import ScanRolePayload, TaskSource, TaskSubmission
from agent.app.tests.helpers.fake_rds_client import FakeRdsDataClient, formatted_records, param_value


def _client(fake: FakeRdsDataClient) -> AuroraClient:
    return AuroraClient(fake, resource_arn="arn:aurora", secret_arn="arn:secret", database="job4younow")


def _task_row(**overrides) -> dict:
    task = TaskSubmission(
        chat_id="42", source=TaskSource.USER, payload=ScanRolePayload(role_id="backend", query="python"), idempotency_key="k-1"
    ).to_task()
    row = task.model_dump(mode="json")
    row.update(overrides)
    return row


@pytest.mark.asyncio
async def test_create_task_inserts_then_selects_by_idempotency_key() -> None:
    fake = FakeRdsDataClient()
    row = _task_row()
    fake.when("execute_statement", lambda kwargs: formatted_records([row]) if "SELECT" in kwargs["sql"] else {"numberOfRecordsUpdated": 1})

    submission = TaskSubmission(
        chat_id="42", source=TaskSource.USER, payload=ScanRolePayload(role_id="backend", query="python"), idempotency_key="k-1"
    )
    created = await task_repo.create_task(_client(fake), submission.to_task())

    assert created.idempotency_key == "k-1"
    insert_call, select_call = fake.calls
    assert "INSERT INTO agent_tasks" in insert_call.kwargs["sql"]
    assert "ON CONFLICT (idempotency_key) DO NOTHING" in insert_call.kwargs["sql"]
    assert param_value(select_call.kwargs["parameters"], "idempotency_key") == {"stringValue": "k-1"}


@pytest.mark.asyncio
async def test_claim_next_task_sends_source_owner_and_lease_seconds() -> None:
    fake = FakeRdsDataClient()
    fake.when("execute_statement", lambda kwargs: formatted_records([_task_row(status="running")]))

    claimed = await task_repo.claim_next_task(_client(fake), source=TaskSource.USER, owner="worker-1", lease_seconds=600)

    assert claimed is not None
    call = fake.calls[0]
    assert "FOR UPDATE SKIP LOCKED" in call.kwargs["sql"]
    assert param_value(call.kwargs["parameters"], "source") == {"stringValue": "user"}
    assert param_value(call.kwargs["parameters"], "owner") == {"stringValue": "worker-1"}
    assert param_value(call.kwargs["parameters"], "lease_seconds") == {"longValue": 600}


@pytest.mark.asyncio
async def test_claim_next_task_returns_none_when_queue_is_empty() -> None:
    fake = FakeRdsDataClient()
    fake.when("execute_statement", lambda kwargs: formatted_records([]))

    claimed = await task_repo.claim_next_task(_client(fake), source=TaskSource.SCHEDULER, owner="worker-1", lease_seconds=600)

    assert claimed is None


@pytest.mark.asyncio
async def test_complete_task_sets_succeeded_status() -> None:
    fake = FakeRdsDataClient()
    await task_repo.complete_task(_client(fake), "task-1")

    call = fake.calls[0]
    assert param_value(call.kwargs["parameters"], "status") == {"stringValue": "succeeded"}
    assert param_value(call.kwargs["parameters"], "id") == {"stringValue": "task-1"}


@pytest.mark.asyncio
async def test_fail_task_records_the_error_message() -> None:
    fake = FakeRdsDataClient()
    await task_repo.fail_task(_client(fake), "task-1", "boom")

    call = fake.calls[0]
    assert param_value(call.kwargs["parameters"], "status") == {"stringValue": "failed"}
    assert param_value(call.kwargs["parameters"], "error") == {"stringValue": "boom"}
