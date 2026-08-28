"""The application service the API layer delegates to — see api/routes.py.
This is where "one Telegram update" becomes either an immediate response
(deterministic route) or a queued task (predictive route). Route handlers
stay thin; all the actual orchestration logic lives here, in one place, so
it is testable without spinning up FastAPI at all.
"""
from __future__ import annotations

from agent.app.cache.backlog_browser_cache import BacklogBrowserCache
from agent.app.cache.backlog_filter_cache import BacklogFilterWizardCache
from agent.app.cache.model_wizard_cache import ModelWizardCache
from agent.app.cache.ttl_cache import TtlCache
from agent.app.config import Settings
from agent.app.db import outbox_repo, schedule_repo, task_repo, transport_repo
from agent.app.db.aurora_client import AuroraClient
from agent.app.delivery import deliver_response
from agent.app.formatting.presenters import Presenters
from agent.app.models.responses import UserFacingResponse
from agent.app.models.schedules import ScanSchedule, ScheduleCreateRequest
from agent.app.models.tasks import TaskSource, TaskSubmission
from agent.app.models.telegram import TelegramUpdateEnvelope, TelegramUpdateResult
from agent.app.routing.deterministic import ReadService, handle_deterministic_route
from agent.app.routing.intent_router import route_telegram_command
from agent.app.tools.artifact_store import PrivateStores
from agent.app.tools.model_catalog import ModelCatalogService


class AgentApplication:
    def __init__(
        self,
        client: AuroraClient,
        cache: TtlCache,
        settings: Settings,
        private_stores: PrivateStores,
        model_catalog: ModelCatalogService,
    ) -> None:
        self._client = client
        self._settings = settings
        self._presenters = Presenters()
        self._reads = ReadService(client, cache, ttl_seconds=settings.cache_ttl_seconds)
        self._private_stores = private_stores
        self._model_catalog = model_catalog
        self._wizard_cache = ModelWizardCache(cache)
        self._browser_cache = BacklogBrowserCache(cache)
        self._filter_cache = BacklogFilterWizardCache(cache)

    async def handle_telegram_update(self, envelope: TelegramUpdateEnvelope) -> TelegramUpdateResult:
        command = envelope.command

        # Telegram transport state (offset/last-update-id) is the one thing
        # the adapter would otherwise need its own DB access for — advancing
        # it first, before any routing logic runs, means a downstream bug
        # can never cause the same update to be redelivered forever.
        await transport_repo.advance_offset(
            self._client, command.chat_id, offset=command.update_id + 1, last_update_id=command.update_id
        )

        intent = route_telegram_command(command)

        stored_hub = await transport_repo.get_hub_message_id(self._client, command.chat_id)
        effective_hub = command.hub_message_id or stored_hub

        if intent.is_deterministic:
            response = await handle_deterministic_route(
                intent,
                self._reads,
                self._presenters,
                client=self._client,
                settings=self._settings,
                model_catalog=self._model_catalog,
                wizard_cache=self._wizard_cache,
                browser_cache=self._browser_cache,
                filter_cache=self._filter_cache,
            )
            _, messages = await deliver_response(
                self._client, self._private_stores, response, command.chat_id, source_task_id=None
            )
            return TelegramUpdateResult(
                accepted=True,
                immediate_messages=messages,
                hub_message_id=effective_hub,
            )

        submission = TaskSubmission(
            chat_id=command.chat_id,
            source=TaskSource.USER,
            payload=intent.task_payload,
            idempotency_key=f"update-{command.chat_id}-{command.update_id}",
        )
        task = await task_repo.create_task(self._client, submission.to_task())
        return TelegramUpdateResult(accepted=True, task_id=task.id)

    async def get_task_status_response(self, task_id: str) -> UserFacingResponse:
        task = await self._reads.get_task(task_id)
        if task is None:
            return self._presenters.error(f"No task found with id {task_id}.")
        return self._presenters.task_status(task)

    async def list_pending_outbox(self, *, limit: int = 50) -> list[dict]:
        return await outbox_repo.list_pending(self._client, limit=limit)

    async def mark_outbox_delivered(self, outbox_id: str) -> None:
        await outbox_repo.mark_delivered(self._client, outbox_id)

    async def create_schedule(self, request: ScheduleCreateRequest) -> ScanSchedule:
        schedule = ScanSchedule(
            chat_id=request.chat_id, role_id=request.role_id, query=request.query, interval_seconds=request.interval_seconds
        )
        return await schedule_repo.create_schedule(self._client, schedule)

    async def set_hub_message_id(self, chat_id: str, hub_message_id: int | None) -> None:
        await transport_repo.set_hub_message_id(self._client, chat_id, hub_message_id)

    async def get_job_listing(self, listing_id: str) -> dict | None:
        from agent.app.db import domain_repo

        row = await domain_repo.get_job_listing(self._client, listing_id)
        if row is None:
            return None
        return row.model_dump(mode="json")

    async def update_listing_status(self, listing_id: str, status: str) -> bool:
        from agent.app.db import domain_repo

        return await domain_repo.update_listing_status(self._client, listing_id, status)

    async def list_schedules(self, chat_id: str) -> list[ScanSchedule]:
        return await schedule_repo.list_by_chat(self._client, chat_id)

    async def set_schedule_enabled(self, schedule_id: str, enabled: bool) -> None:
        await schedule_repo.set_enabled(self._client, schedule_id, enabled)

    async def delete_schedule(self, schedule_id: str) -> None:
        await schedule_repo.delete_schedule(self._client, schedule_id)

    async def update_schedule_interval(self, schedule_id: str, interval_seconds: int) -> None:
        await schedule_repo.update_interval(self._client, schedule_id, interval_seconds)
