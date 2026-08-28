"""Executes a deterministic IntentDecision end to end: cached read -> typed
data -> presenter -> UserFacingResponse. Nothing in this module calls
LangGraph or the DB gate — that split is what guarantees a deterministic
route can never trigger an LLM call or a domain write. (The model-config
wizard, dispatched to below, is the one exception to "no I/O beyond cached
Aurora reads": it also calls Cursor's live model-listing API and writes
plain operator configuration — never an LLM call, never a DB-gated domain
write, so the invariant that actually matters here still holds.)
"""
from __future__ import annotations

from agent.app.cache import keys as cache_keys
from agent.app.cache.backlog_browser_cache import BacklogBrowserCache
from agent.app.cache.backlog_filter_cache import BacklogFilterWizardCache
from agent.app.cache.model_wizard_cache import ModelWizardCache
from agent.app.cache.ttl_cache import TtlCache, get_or_load
from agent.app.config import Settings
from agent.app.db import domain_repo, task_repo
from agent.app.db.aurora_client import AuroraClient
from agent.app.formatting.presenters import Presenters
from agent.app.models.artifacts import ContentVisibility
from agent.app.models.domain import JobListingRow, RoleBacklog
from agent.app.models.intent import DeterministicQuery, IntentDecision, RouteKind
from agent.app.models.telegram import TelegramCallback
from agent.app.models.responses import UserFacingResponse
from agent.app.models.tasks import AgentTask
from agent.app.routing.model_config import handle_model_config_callback
from agent.app.routing.schedule_menu import handle_schedule_menu_callback
from agent.app.routing.backlog_browser import handle_backlog_browser_callback
from agent.app.routing.backlog_filters import handle_backlog_filter_menu
from agent.app.tools.model_catalog import ModelCatalogService


class ReadService:
    """Cached facade over the read-only domain queries. Task status is
    intentionally NOT cached — it is cheap, single-row, and must never show
    a stale "running" after the task has actually finished."""

    def __init__(self, client: AuroraClient, cache: TtlCache, *, ttl_seconds: float) -> None:
        self._client = client
        self._cache = cache
        self._ttl_seconds = ttl_seconds

    async def list_backlog(self) -> list[RoleBacklog]:
        return await get_or_load(
            self._cache, cache_keys.backlog(), lambda: domain_repo.list_backlog_by_role(self._client), ttl_seconds=self._ttl_seconds
        )

    async def list_job_listings(self, role_id: str) -> list[JobListingRow]:
        return await get_or_load(
            self._cache,
            cache_keys.job_listings_by_role(role_id),
            lambda: domain_repo.list_job_listings(self._client, role_id),
            ttl_seconds=self._ttl_seconds,
        )

    async def get_task(self, task_id: str) -> AgentTask | None:
        return await task_repo.get_task(self._client, task_id)


async def handle_deterministic_route(
    intent: IntentDecision,
    reads: ReadService,
    presenters: Presenters,
    *,
    client: AuroraClient,
    settings: Settings,
    model_catalog: ModelCatalogService,
    wizard_cache: ModelWizardCache,
    browser_cache: BacklogBrowserCache,
    filter_cache: BacklogFilterWizardCache,
) -> UserFacingResponse:
    query = intent.query or DeterministicQuery()

    if intent.route == RouteKind.MAIN_MENU:
        return presenters.main_menu()

    if intent.route == RouteKind.SETTINGS_MENU:
        return presenters.settings_menu()

    if intent.route == RouteKind.BACKLOG:
        return presenters.backlog(await reads.list_backlog())

    if intent.route == RouteKind.BACKLOG_BROWSER:
        return await handle_backlog_browser_callback(
            intent.chat_id,
            intent.callback if intent.callback else TelegramCallback(action="role", value=query.role_id) if query.role_id else None,
            presenters=presenters,
            client=client,
            settings=settings,
            browser_cache=browser_cache,
        )

    if intent.route == RouteKind.BACKLOG_FILTER_MENU:
        return await handle_backlog_filter_menu(
            intent.chat_id,
            intent.callback if intent.callback else TelegramCallback(action="settings", value="backlog"),
            presenters=presenters,
            client=client,
            filter_cache=filter_cache,
        )

    if intent.route == RouteKind.JOB_LIST:
        if not query.role_id:
            return presenters.error("No role specified.", next_action="Send BACKLOG to see available roles.")
        return presenters.job_list(query.role_id, await reads.list_job_listings(query.role_id))

    if intent.route == RouteKind.TASK_STATUS:
        if not query.task_id:
            return presenters.error("No task id specified.", next_action="Send STATUS <task id>.")
        task = await reads.get_task(query.task_id)
        if not task:
            return presenters.error(f"No task found with id {query.task_id}.")
        return presenters.task_status(task)

    if intent.route == RouteKind.SCHEDULE_MENU:
        return await handle_schedule_menu_callback(
            intent.chat_id,
            intent.callback,
            presenters=presenters,
            client=client,
            settings=settings,
        )

    if intent.route == RouteKind.MODEL_CONFIG:
        return await handle_model_config_callback(
            intent.chat_id,
            intent.callback,
            client=client,
            settings=settings,
            model_catalog=model_catalog,
            wizard_cache=wizard_cache,
            presenters=presenters,
        )

    if intent.route == RouteKind.HELP:
        return presenters.help()

    return presenters.help()
