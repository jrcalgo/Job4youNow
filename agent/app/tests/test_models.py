"""Discriminated unions are the mechanism the plan's "make invalid states
hard to represent" rule leans on hardest — these tests exist to prove that
mechanism actually rejects what it should, not just that the happy path
parses.
"""
from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError

from agent.app.models.db_writes import DbWriteSet, DomainWrite, JobListingWrite
from agent.app.models.tasks import AgentTask, ScanRolePayload, TaskKind, TaskPayload, TaskSource, TaskSubmission


def test_task_payload_discriminates_on_kind() -> None:
    adapter = TypeAdapter(TaskPayload)
    payload = adapter.validate_python({"kind": "scan_role", "role_id": "backend", "query": "senior python"})
    assert isinstance(payload, ScanRolePayload)
    assert payload.max_results == 25  # default applied


def test_task_payload_rejects_unknown_kind() -> None:
    adapter = TypeAdapter(TaskPayload)
    with pytest.raises(ValidationError):
        adapter.validate_python({"kind": "not_a_real_kind"})


def test_task_submission_assigns_priority_by_source() -> None:
    user_task = TaskSubmission(
        chat_id="42",
        source=TaskSource.USER,
        payload=ScanRolePayload(role_id="backend", query="python"),
        idempotency_key="key-1",
    ).to_task()
    scheduler_task = TaskSubmission(
        chat_id="42",
        source=TaskSource.SCHEDULER,
        payload=ScanRolePayload(role_id="backend", query="python"),
        idempotency_key="key-2",
    ).to_task()

    assert user_task.priority > scheduler_task.priority
    assert user_task.kind == TaskKind.SCAN_ROLE


def test_agent_task_round_trips_through_json_like_dump() -> None:
    task = TaskSubmission(
        chat_id="42", source=TaskSource.USER, payload=ScanRolePayload(role_id="backend", query="python"), idempotency_key="k"
    ).to_task()
    dumped = task.model_dump(mode="json")
    rebuilt = AgentTask.model_validate(dumped)
    assert rebuilt == task


def test_domain_write_discriminates_on_kind() -> None:
    adapter = TypeAdapter(DomainWrite)
    write = adapter.validate_python({"kind": "job_listing", "role_id": "backend", "company_name": "Acme", "title": "SWE"})
    assert isinstance(write, JobListingWrite)
    assert write.contacts == []


def test_db_write_set_requires_chat_id_and_source_task() -> None:
    with pytest.raises(ValidationError):
        DbWriteSet(idempotency_key="k", writes=[])  # type: ignore[call-arg]
