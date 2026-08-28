"""Backlog filter settings menu for Telegram."""
from __future__ import annotations

from agent.app.cache.backlog_filter_cache import BacklogFilterWizardCache
from agent.app.db import backlog_prefs_repo, domain_repo
from agent.app.db.aurora_client import AuroraClient
from agent.app.formatting.presenters import Presenters
from agent.app.models.backlog import BacklogFilterWizardState, BacklogPrefs
from agent.app.models.responses import UserFacingResponse
from agent.app.models.telegram import TelegramCallback


async def handle_backlog_filter_menu(
    chat_id: str,
    callback: TelegramCallback | None,
    *,
    presenters: Presenters,
    client: AuroraClient,
    filter_cache: BacklogFilterWizardCache,
) -> UserFacingResponse:
    if callback is None or (callback.action == "settings" and callback.value == "backlog"):
        saved = await backlog_prefs_repo.get_prefs(client, chat_id)
        filter_cache.set(chat_id, BacklogFilterWizardState(pending=saved.filters.model_copy(), sort_key=saved.sort_key, sort_dir=saved.sort_dir))
        options = await domain_repo.list_distinct_filter_options(client)
        state = filter_cache.get(chat_id)
        return presenters.backlog_filter_menu(state.pending, options, sort_key=state.sort_key, sort_dir=state.sort_dir)

    if callback.action == "bl" and callback.value:
        value = callback.value
        state = filter_cache.get(chat_id)
        if state is None:
            saved = await backlog_prefs_repo.get_prefs(client, chat_id)
            state = BacklogFilterWizardState(pending=saved.filters.model_copy(), sort_key=saved.sort_key, sort_dir=saved.sort_dir)
            filter_cache.set(chat_id, state)

        if value == "fil:save":
            prefs = BacklogPrefs(filters=state.pending, sort_key=state.sort_key, sort_dir=state.sort_dir)
            await backlog_prefs_repo.upsert_prefs(client, chat_id, prefs)
            filter_cache.clear(chat_id)
            return presenters.backlog_filters_saved()

        if value == "fil:clear":
            state = BacklogFilterWizardState()
            filter_cache.set(chat_id, state)
            options = await domain_repo.list_distinct_filter_options(client)
            return presenters.backlog_filter_menu(state.pending, options)

        if value.startswith("fil:toggle:"):
            parts = value.split(":", 3)
            if len(parts) < 4:
                return presenters.error("Invalid filter toggle.")
            dim, item = parts[2], parts[3]
            pending = state.pending.model_copy()
            if dim == "role":
                lst = list(pending.role_ids)
                if item in lst:
                    lst.remove(item)
                else:
                    lst.append(item)
                pending.role_ids = lst
            elif dim == "company":
                lst = list(pending.company_names)
                if item in lst:
                    lst.remove(item)
                else:
                    lst.append(item)
                pending.company_names = lst
            elif dim == "work":
                lst = list(pending.work_modes)
                if item in lst:
                    lst.remove(item)
                else:
                    lst.append(item)
                pending.work_modes = lst
            filter_cache.set(chat_id, state.model_copy(update={"pending": pending}))
            options = await domain_repo.list_distinct_filter_options(client)
            return presenters.backlog_filter_menu(pending, options, sort_key=state.sort_key, sort_dir=state.sort_dir)

        if value.startswith("fil:sort:"):
            sort_key = value.split(":", 2)[2]
            filter_cache.set(chat_id, state.model_copy(update={"sort_key": sort_key}))
            options = await domain_repo.list_distinct_filter_options(client)
            return presenters.backlog_filter_menu(state.pending, options, sort_key=sort_key, sort_dir=state.sort_dir)

    options = await domain_repo.list_distinct_filter_options(client)
    saved = await backlog_prefs_repo.get_prefs(client, chat_id)
    return presenters.backlog_filter_menu(saved.filters, options, sort_key=saved.sort_key, sort_dir=saved.sort_dir)
