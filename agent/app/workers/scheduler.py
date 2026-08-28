"""Turns due `scan_schedules` rows into scheduler-sourced tasks. Never runs a
scan itself — it only enqueues, so a slow or stuck scan can never stall the
scheduler loop or delay other schedules from firing.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from agent.app.config import Settings
from agent.app.db import schedule_repo, task_repo
from agent.app.db.aurora_client import AuroraClient
from agent.app.logging import get_logger
from agent.app.models.tasks import ScanRolePayload, TaskSource, TaskSubmission

log = get_logger("workers.scheduler")


class Scheduler:
    def __init__(self, client: AuroraClient, settings: Settings) -> None:
        self._client = client
        self._settings = settings

    async def run_forever(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            try:
                await self._enqueue_due_schedules()
            except Exception as exc:  # noqa: BLE001 - one bad poll must not end the scheduler forever
                log.error("schedule poll failed, will retry next cycle", extra={"context": {"error": str(exc)}})
            await asyncio.sleep(self._settings.scheduler_poll_seconds)

    async def _enqueue_due_schedules(self) -> None:
        for schedule in await schedule_repo.due_schedules(self._client):
            # Idempotency key includes today's date as a defense-in-depth
            # measure: even if the scheduler polls again before
            # `reschedule` below has pushed next_run_at forward, the second
            # attempt hits the same key and create_task becomes a no-op.
            idempotency_key = f"{schedule.id}-{datetime.now(timezone.utc).date().isoformat()}"
            submission = TaskSubmission(
                chat_id=schedule.chat_id,
                source=TaskSource.SCHEDULER,
                payload=ScanRolePayload(role_id=schedule.role_id, query=schedule.query),
                idempotency_key=idempotency_key,
            )
            await task_repo.create_task(self._client, submission.to_task())
            await schedule_repo.reschedule(self._client, schedule.id)
            log.info("scheduled scan enqueued", extra={"context": {"scheduleId": schedule.id, "roleId": schedule.role_id}})
