"""Task persistence and leasing. `claim_next_task` is the one query that
makes user-task and scan-task worker pools safe to run concurrently against
the same table — see workers/task_worker.py, which is the only caller.
"""
from __future__ import annotations

from agent.app.db.aurora_client import AuroraClient
from agent.app.models.artifacts import ContentVisibility
from agent.app.models.responses import UserFacingResponse
from agent.app.models.tasks import AgentTask, TaskSource, TaskStatus


def _row_to_task(row: dict) -> AgentTask:
    # Column names match AgentTask's fields 1:1 (see migrations/0001), and
    # `payload` already carries its own `kind` discriminator, so this is a
    # direct round-trip with no per-field remapping to maintain.
    return AgentTask.model_validate(row)


async def create_task(client: AuroraClient, task: AgentTask) -> AgentTask:
    """Insert `task`, or return the existing row if `idempotency_key` was
    already used — see models/tasks.py's docstring on why that matters."""
    await client.exec(
        """
        INSERT INTO agent_tasks (id, chat_id, kind, source, priority, status, payload, idempotency_key, created_at)
        VALUES (:id, :chat_id, :kind, :source, :priority, :status, :payload::jsonb, :idempotency_key, :created_at)
        ON CONFLICT (idempotency_key) DO NOTHING
        """,
        {
            "id": task.id,
            "chat_id": task.chat_id,
            "kind": task.kind.value,
            "source": task.source.value,
            "priority": task.priority,
            "status": task.status.value,
            "payload": task.payload.model_dump(mode="json"),
            "idempotency_key": task.idempotency_key,
            "created_at": task.created_at,
        },
    )
    result = await client.exec(
        "SELECT * FROM agent_tasks WHERE idempotency_key = :idempotency_key",
        {"idempotency_key": task.idempotency_key},
    )
    return _row_to_task(result.rows[0])


async def claim_next_task(client: AuroraClient, *, source: TaskSource, owner: str, lease_seconds: int) -> AgentTask | None:
    """Atomically claim the highest-priority, oldest eligible task for
    `source`. `FOR UPDATE SKIP LOCKED` is what lets the user-task pool and
    the scan-task pool (and multiple workers within each) poll the same
    table concurrently without blocking on each other. A task whose lease
    expired (its worker crashed mid-run) is eligible again automatically —
    no separate "requeue" step needed."""
    result = await client.exec(
        """
        UPDATE agent_tasks
           SET status = 'running',
               lease_owner = :owner,
               lease_expires_at = now() + (:lease_seconds || ' seconds')::interval,
               started_at = COALESCE(started_at, now())
         WHERE id = (
           SELECT id FROM agent_tasks
            WHERE source = :source
              AND (status = 'pending' OR (status = 'running' AND lease_expires_at < now()))
            ORDER BY priority DESC, created_at ASC
            LIMIT 1
            FOR UPDATE SKIP LOCKED
         )
         RETURNING *
        """,
        {"source": source.value, "owner": owner, "lease_seconds": lease_seconds},
    )
    return _row_to_task(result.rows[0]) if result.rows else None


async def complete_task(client: AuroraClient, task_id: str) -> None:
    await client.exec(
        "UPDATE agent_tasks SET status = :status, finished_at = now() WHERE id = :id",
        {"id": task_id, "status": TaskStatus.SUCCEEDED.value},
    )


async def fail_task(client: AuroraClient, task_id: str, error: str) -> None:
    await client.exec(
        "UPDATE agent_tasks SET status = :status, error = :error, finished_at = now() WHERE id = :id",
        {"id": task_id, "status": TaskStatus.FAILED.value, "error": error},
    )


async def get_task(client: AuroraClient, task_id: str) -> AgentTask | None:
    result = await client.exec("SELECT * FROM agent_tasks WHERE id = :id", {"id": task_id})
    return _row_to_task(result.rows[0]) if result.rows else None


async def save_task_result(
    client: AuroraClient, task_id: str, *, response: UserFacingResponse, tool_result_metadata: list[dict]
) -> None:
    """Persists exactly one of `public_response` / `private_artifact_id`,
    matching `response.visibility` — never both, and never a private
    response's body text. `response` must already be materialized (see
    agent/app/delivery.py) before it reaches here; a PRIVATE_USER response
    that still has inline `body` text is a caller bug, not something this
    function silently accepts."""
    is_public = response.visibility == ContentVisibility.PUBLIC_JOB_SEARCH
    if not is_public and not response.is_materialized:
        raise ValueError("private response must be materialized before save_task_result — see agent/app/delivery.py")

    await client.exec(
        """
        INSERT INTO agent_task_results (task_id, visibility, public_response, private_artifact_id, tool_result_metadata)
        VALUES (:task_id, :visibility, :public_response::jsonb, :private_artifact_id, :tool_result_metadata::jsonb)
        ON CONFLICT (task_id) DO UPDATE SET
          visibility = EXCLUDED.visibility,
          public_response = EXCLUDED.public_response,
          private_artifact_id = EXCLUDED.private_artifact_id,
          tool_result_metadata = EXCLUDED.tool_result_metadata
        """,
        {
            "task_id": task_id,
            "visibility": response.visibility.value,
            "public_response": response.model_dump(mode="json") if is_public else None,
            "private_artifact_id": response.private_artifact.id if response.private_artifact else None,
            "tool_result_metadata": tool_result_metadata,
        },
    )
