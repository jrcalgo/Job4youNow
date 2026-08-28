"""Proves the property the plan's concurrency design exists for: a backlog
of scheduled-scan tasks must never delay a user-requested task, because the
two run through separate TaskWorkerPools against separate semaphores and
separate `source` queues (see workers/concurrency.py and
db/task_repo.py's claim_next_task).

Uses a fake compiled graph (not the real LangGraph/Cursor SDK stack) so this
test is purely about pool/claim behavior, not tool execution.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agent.app.config import DEFAULT_MODEL_ID, Settings
from agent.app.db.aurora_client import AuroraClient
from agent.app.formatting.presenters import Presenters
from agent.app.models.artifacts import ArtifactBucket, ContentVisibility
from agent.app.models.responses import UserFacingResponse
from agent.app.models.tasks import ScanRolePayload, TaskSource, TaskSubmission
from agent.app.tests.helpers.fake_rds_client import FakeRdsDataClient, formatted_records, param_value
from agent.app.tools.artifact_store import ArtifactStore, LocalPrivateBackupStore, PrivateStores
from agent.app.workers.task_worker import TaskWorkerPool


def _task_row(*, task_id: str, source: str, priority: int) -> dict:
    task = TaskSubmission(
        chat_id="42",
        source=TaskSource(source),
        payload=ScanRolePayload(role_id="backend", query="python"),
        idempotency_key=task_id,
    ).to_task()
    row = task.model_dump(mode="json")
    row.update(id=task_id, priority=priority)
    return row


class ConcurrencyTracker:
    def __init__(self) -> None:
        self.completed_task_ids: list[str] = []

    def record_completion(self, task_id: str) -> None:
        self.completed_task_ids.append(task_id)


class FakeCompiledGraph:
    """Stands in for the real LangGraph-compiled graph — just sleeps to
    simulate work, then reports a trivial UserFacingResponse."""

    def __init__(self, delay_seconds: float, tracker: ConcurrencyTracker) -> None:
        self._delay_seconds = delay_seconds
        self._tracker = tracker

    async def ainvoke(self, state) -> dict:
        await asyncio.sleep(self._delay_seconds)
        self._tracker.record_completion(state.task.id)
        response = UserFacingResponse(visibility=ContentVisibility.PUBLIC_JOB_SEARCH, body="done")
        return {"task": state.task, "tool_results": [], "response": response}


class InMemoryTaskQueue:
    """A minimal in-memory stand-in for the `agent_tasks` table, just
    faithful enough to drive claim/complete semantics for this test: matches
    on SQL shape (same technique test_task_repo.py uses), not real SQL."""

    def __init__(self, rows: list[dict]) -> None:
        self._tasks = {row["id"]: row for row in rows}
        self.fake = FakeRdsDataClient()
        self.fake.when("execute_statement", self._handle)

    def _handle(self, kwargs: dict) -> dict:
        sql = kwargs["sql"]
        parameters = kwargs.get("parameters", [])

        if "FOR UPDATE SKIP LOCKED" in sql:
            source = param_value(parameters, "source")["stringValue"]
            pending = [t for t in self._tasks.values() if t["source"] == source and t["status"] == "pending"]
            if not pending:
                return formatted_records([])
            pending.sort(key=lambda t: (-t["priority"], t["created_at"]))
            chosen = pending[0]
            chosen["status"] = "running"
            return formatted_records([chosen])

        if "SET status = :status" in sql:
            task_id = param_value(parameters, "id")["stringValue"]
            new_status = param_value(parameters, "status")["stringValue"]
            if task_id in self._tasks:
                self._tasks[task_id]["status"] = new_status
            return {"numberOfRecordsUpdated": 1}

        return {"numberOfRecordsUpdated": 0}


def _settings(**overrides) -> Settings:
    return Settings(
        AWS_REGION="us-east-1",
        AURORA_RESOURCE_ARN="arn:aurora",
        AURORA_SECRET_ARN="arn:secret",
        JOB_ARTIFACTS_BUCKET="job-bucket",
        PRIVATE_USER_ARTIFACTS_BUCKET="private-bucket",
        CURSOR_API_KEY="cursor_test_key",
        # Pinned explicitly so this test is hermetic against whatever a
        # real repo-root .env (if one happens to exist locally) sets —
        # pydantic-settings reads that file as a fallback for any field not
        # passed here, which would otherwise make this value environment-dependent.
        CURSOR_MODEL=DEFAULT_MODEL_ID,
        AGENT_WORKER_POLL_SECONDS=0.01,
        **overrides,
    )


@pytest.mark.asyncio
async def test_user_task_completes_promptly_despite_a_backlog_of_scheduled_scans(tmp_path: Path) -> None:
    scan_rows = [_task_row(task_id=f"scan-{i}", source="scheduler", priority=10) for i in range(5)]
    user_row = _task_row(task_id="user-1", source="user", priority=100)
    queue = InMemoryTaskQueue([*scan_rows, user_row])
    client = AuroraClient(queue.fake, resource_arn="a", secret_arn="s", database="d")

    tracker = ConcurrencyTracker()
    fake_graph = FakeCompiledGraph(delay_seconds=0.05, tracker=tracker)
    settings = _settings()
    presenters = Presenters()
    private_stores = PrivateStores(
        bucket=ArtifactStore(bucket_kind=ArtifactBucket.PRIVATE_USER_ARTIFACTS, bucket_name="private-bucket", prefix=""),
        local_backup=LocalPrivateBackupStore(tmp_path),
    )

    user_pool = TaskWorkerPool(
        client=client,
        source=TaskSource.USER,
        limiter=asyncio.Semaphore(1),
        settings=settings,
        compiled_graph=fake_graph,
        presenters=presenters,
        private_stores=private_stores,
    )
    scan_pool = TaskWorkerPool(
        client=client,
        source=TaskSource.SCHEDULER,
        limiter=asyncio.Semaphore(1),
        settings=settings,
        compiled_graph=fake_graph,
        presenters=presenters,
        private_stores=private_stores,
    )

    stop_event = asyncio.Event()
    runners = [asyncio.create_task(user_pool.run_forever(stop_event)), asyncio.create_task(scan_pool.run_forever(stop_event))]

    await asyncio.sleep(0.12)
    stop_event.set()
    await asyncio.gather(*runners)

    assert "user-1" in tracker.completed_task_ids
    # The user task ran in its own pool, so it finishes among the first
    # completions overall rather than waiting behind the 5 queued scans.
    assert tracker.completed_task_ids.index("user-1") <= 1
    assert len(tracker.completed_task_ids) >= 3  # both pools made real progress
