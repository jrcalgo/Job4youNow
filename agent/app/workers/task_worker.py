"""Claims and runs tasks for one TaskSource, up to `limiter`'s concurrency
limit. One TaskWorkerPool per source (see main.py's lifespan) — this is what
lets user-task and scheduled-scan tasks make progress independently.
"""
from __future__ import annotations

import asyncio
import uuid

from langgraph.graph.state import CompiledStateGraph

from agent.app.config import Settings
from agent.app.db import outbox_repo, task_repo
from agent.app.db.aurora_client import AuroraClient
from agent.app.delivery import deliver_response
from agent.app.formatting.presenters import Presenters
from agent.app.graph.supervisor import run_supervisor_graph
from agent.app.logging import get_logger
from agent.app.models.tasks import AgentTask, TaskSource
from agent.app.models.tool_results import ToolResult
from agent.app.tools.artifact_store import PrivateStores

log = get_logger("workers.task_worker")


class TaskWorkerPool:
    def __init__(
        self,
        *,
        client: AuroraClient,
        source: TaskSource,
        limiter: asyncio.Semaphore,
        settings: Settings,
        compiled_graph: CompiledStateGraph,
        presenters: Presenters,
        private_stores: PrivateStores,
    ) -> None:
        self._client = client
        self._source = source
        self._limiter = limiter
        self._settings = settings
        self._compiled_graph = compiled_graph
        self._presenters = presenters
        self._private_stores = private_stores
        self._owner = f"{source.value}-worker-{uuid.uuid4().hex[:8]}"

    async def run_forever(self, stop_event: asyncio.Event) -> None:
        """Acquire a concurrency slot BEFORE claiming a task, so an empty
        queue never holds a slot idle, and release it only after that task
        finishes — the loop can then immediately try to fill the freed slot
        with the next task, without waiting for anything else in flight.

        A failed CLAIM (as opposed to a failed task — see `_run_task`, which
        already can't crash this loop) must not silently end the pool for
        the rest of the process either — Aurora's own retry only covers
        auto-pause/resume, so anything else (throttling, a network blip)
        would otherwise propagate here and kill this pool forever."""
        in_flight: set[asyncio.Task] = set()
        try:
            while not stop_event.is_set():
                await self._limiter.acquire()
                try:
                    task = await task_repo.claim_next_task(
                        self._client, source=self._source, owner=self._owner, lease_seconds=self._settings.task_lease_seconds
                    )
                except Exception as exc:  # noqa: BLE001 - a claim failure must not end this pool
                    log.error("task claim failed, backing off", extra={"context": {"source": self._source.value, "error": str(exc)}})
                    self._limiter.release()
                    await asyncio.sleep(self._settings.worker_poll_seconds)
                    continue

                if task is None:
                    self._limiter.release()
                    await asyncio.sleep(self._settings.worker_poll_seconds)
                    continue

                in_flight = {t for t in in_flight if not t.done()}
                in_flight.add(asyncio.create_task(self._run_and_release(task)))
        finally:
            if in_flight:
                await asyncio.gather(*in_flight, return_exceptions=True)

    async def _run_and_release(self, task: AgentTask) -> None:
        try:
            await self._run_task(task)
        finally:
            self._limiter.release()

    async def _run_task(self, task: AgentTask) -> None:
        tool_results: list[ToolResult] = []
        try:
            state = await run_supervisor_graph(self._compiled_graph, task)
            tool_results = state.tool_results
            response = state.response or self._presenters.error("Task finished with no response.")
            await task_repo.complete_task(self._client, task.id)
        except Exception as exc:  # noqa: BLE001 - one bad task must never kill the worker loop
            log.error("task failed", extra={"context": {"taskId": task.id, "error": str(exc)}})
            response = self._presenters.error("This request could not be completed.", next_action=f"Reference: {task.id}")
            await task_repo.fail_task(self._client, task.id, str(exc))

        materialized_response, messages = await deliver_response(
            self._client, self._private_stores, response, task.chat_id, source_task_id=task.id
        )
        await task_repo.save_task_result(
            self._client,
            task.id,
            response=materialized_response,
            tool_result_metadata=[result.model_dump(mode="json") for result in tool_results],
        )
        await outbox_repo.enqueue_messages(self._client, messages, task_id=task.id)
