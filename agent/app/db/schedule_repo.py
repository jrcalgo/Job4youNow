"""Periodic scan schedule persistence — read by workers/scheduler.py, written
by the `POST /schedules` route (a deterministic, non-LLM action).
"""
from __future__ import annotations

from agent.app.db.aurora_client import AuroraClient
from agent.app.models.schedules import ScanSchedule


def _row_to_schedule(row: dict) -> ScanSchedule:
    return ScanSchedule.model_validate(row)


async def create_schedule(client: AuroraClient, schedule: ScanSchedule, *, transaction_id: str | None = None) -> ScanSchedule:
    """Called two ways: directly from the deterministic `POST /schedules`
    route (no transaction — nothing else needs to be atomic with it), and
    from db/domain_repo.py's apply_write_set when a predictive task's
    ScheduleWrite is part of a larger transaction (`transaction_id` passed
    through so it commits/rolls back with the rest of that write set)."""
    await client.exec(
        """
        INSERT INTO scan_schedules (id, chat_id, role_id, query, interval_seconds, next_run_at, enabled)
        VALUES (:id, :chat_id, :role_id, :query, :interval_seconds, :next_run_at, :enabled)
        """,
        schedule.model_dump(mode="json"),
        transaction_id=transaction_id,
    )
    return schedule


async def list_by_chat(client: AuroraClient, chat_id: str) -> list[ScanSchedule]:
    result = await client.exec(
        "SELECT * FROM scan_schedules WHERE chat_id = :chat_id ORDER BY created_at DESC",
        {"chat_id": chat_id},
    )
    return [_row_to_schedule(row) for row in result.rows]


async def set_enabled(client: AuroraClient, schedule_id: str, enabled: bool) -> None:
    await client.exec(
        "UPDATE scan_schedules SET enabled = :enabled WHERE id = :id",
        {"id": schedule_id, "enabled": enabled},
    )


async def delete_schedule(client: AuroraClient, schedule_id: str) -> None:
    await client.exec("DELETE FROM scan_schedules WHERE id = :id", {"id": schedule_id})


async def get_schedule(client: AuroraClient, schedule_id: str) -> ScanSchedule | None:
    result = await client.exec("SELECT * FROM scan_schedules WHERE id = :id", {"id": schedule_id})
    if not result.rows:
        return None
    return _row_to_schedule(result.rows[0])


async def due_schedules(client: AuroraClient) -> list[ScanSchedule]:
    result = await client.exec(
        "SELECT * FROM scan_schedules WHERE enabled AND next_run_at <= now() ORDER BY next_run_at ASC"
    )
    return [_row_to_schedule(row) for row in result.rows]


async def update_interval(client: AuroraClient, schedule_id: str, interval_seconds: int) -> None:
    """Apply a new scan interval immediately — next_run_at resets to now + interval."""
    if interval_seconds < 300:
        raise ValueError("interval_seconds must be at least 300")
    await client.exec(
        """
        UPDATE scan_schedules
           SET interval_seconds = :interval_seconds,
               next_run_at = now() + (:interval_seconds || ' seconds')::interval
         WHERE id = :id
        """,
        {"id": schedule_id, "interval_seconds": interval_seconds},
    )


async def reschedule(client: AuroraClient, schedule_id: str) -> None:
    """Push `next_run_at` forward by the schedule's own interval, computed in
    SQL so this never depends on the worker's clock or the leasing of the
    task the schedule just enqueued."""
    await client.exec(
        """
        UPDATE scan_schedules
           SET next_run_at = now() + (interval_seconds || ' seconds')::interval
         WHERE id = :id
        """,
        {"id": schedule_id},
    )
