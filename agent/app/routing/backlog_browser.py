"""Paginated backlog browser for Telegram."""
from __future__ import annotations

from agent.app.cache.backlog_browser_cache import BacklogBrowserCache
from agent.app.cache.ttl_cache import TtlCache
from agent.app.db import backlog_prefs_repo, domain_repo
from agent.app.db.aurora_client import AuroraClient
from agent.app.formatting.presenters import Presenters
from agent.app.models.backlog import BacklogBrowserState
from agent.app.models.responses import UserFacingResponse
from agent.app.models.telegram import TelegramCallback, TelegramInlineButton


async def handle_backlog_browser_callback(
    chat_id: str,
    callback: TelegramCallback | None,
    *,
    presenters: Presenters,
    client: AuroraClient,
    settings,
    browser_cache: BacklogBrowserCache | None = None,
    cache: TtlCache | None = None,
) -> UserFacingResponse:
    if browser_cache is None and cache is not None:
        browser_cache = BacklogBrowserCache(cache)

    prefs = await backlog_prefs_repo.get_prefs(client, chat_id)
    state = browser_cache.get(chat_id) if browser_cache else None
    if state is None:
        state = BacklogBrowserState()

    if callback is None:
        return await _show_card(chat_id, state, prefs, client, presenters, browser_cache)

    action = callback.action
    value = callback.value

    if action == "role" and value:
        filters = prefs.filters.model_copy()
        filters.role_ids = [value]
        state = BacklogBrowserState(offset=0, view_role_id=value)
        if browser_cache:
            browser_cache.set(chat_id, state)
        prefs = prefs.model_copy(update={"filters": filters})
        return await _show_card(chat_id, state, prefs, client, presenters, browser_cache)

    if action != "bl" or not value:
        return presenters.error("Unrecognized backlog action.")

    if value == "prev":
        state = BacklogBrowserState(offset=max(0, state.offset - 1), view_role_id=state.view_role_id)
    elif value == "next":
        state = BacklogBrowserState(offset=state.offset + 1, view_role_id=state.view_role_id)
    elif value == "settings":
        from agent.app.routing.backlog_filters import handle_backlog_filter_menu
        from agent.app.cache.backlog_filter_cache import BacklogFilterWizardCache

        filter_cache = BacklogFilterWizardCache(cache) if cache else BacklogFilterWizardCache(TtlCache())
        return await handle_backlog_filter_menu(
            chat_id, TelegramCallback(action="settings", value="backlog"), presenters=presenters, client=client, filter_cache=filter_cache
        )
    elif value == "mark:skipped":
        result = await domain_repo.query_job_listings(
            client, prefs.filters, sort_key=prefs.sort_key, sort_dir=prefs.sort_dir, offset=state.offset, limit=1
        )
        if result.items:
            await domain_repo.update_listing_status(client, result.items[0].id, "skipped")
            return await _show_card(chat_id, state, prefs, client, presenters, browser_cache)
        return presenters.error("No listing at this position.")

    if browser_cache:
        browser_cache.set(chat_id, state)
    return await _show_card(chat_id, state, prefs, client, presenters, browser_cache)


async def _show_card(
    chat_id: str,
    state: BacklogBrowserState,
    prefs,
    client: AuroraClient,
    presenters: Presenters,
    browser_cache: BacklogBrowserCache | None,
) -> UserFacingResponse:
    result = await domain_repo.query_job_listings(
        client,
        prefs.filters,
        sort_key=prefs.sort_key,
        sort_dir=prefs.sort_dir,
        offset=state.offset,
        limit=1,
    )
    if not result.items:
        if state.offset > 0:
            state = BacklogBrowserState(offset=max(0, state.offset - 1), view_role_id=state.view_role_id)
            if browser_cache:
                browser_cache.set(chat_id, state)
            return await _show_card(chat_id, state, prefs, client, presenters, browser_cache)
        return presenters.error("No listings match your filters.", next_action="Adjust filters in Settings.")

    listing = result.items[0]
    enqueue_btn = TelegramInlineButton(text="Save to queue", callback_data=f"tg:bl:enqueue:{listing.id}")
    return presenters.backlog_card(
        listing,
        offset=state.offset,
        total=result.total_count,
        enqueue_button=enqueue_btn,
    )
