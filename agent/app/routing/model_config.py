"""The Telegram model-configuration wizard's screen logic. One `RouteKind`
(`MODEL_CONFIG`, see models/intent.py) covers every screen here — which
screen to show is resolved from the callback's `action`/`value`, dispatched
by `handle_model_config_callback` below, called from
routing/deterministic.py's `handle_deterministic_route`.

Callbacks stay well under Telegram's 64-byte `callback_data` limit because
only the FIRST step (`modeltask:<task_kind>`) carries anything beyond a
short action name — every later step (`modelpick`, `modelvariant`,
`modelparam`, ...) references a short INDEX into the ephemeral
ModelWizardCache's last-fetched model list, never a full model id or
parameter value. If that cache has expired or was never populated (e.g. the
process restarted mid-wizard), every handler below fails safely back to a
friendly "start over" message rather than crashing.
"""
from __future__ import annotations

from agent.app.cache.model_wizard_cache import ModelWizardCache
from agent.app.config import Settings
from agent.app.db import model_config_repo
from agent.app.db.aurora_client import AuroraClient
from agent.app.formatting.presenters import Presenters
from agent.app.models.model_catalog import ModelConfig, ModelParamChoice, ModelWizardState
from agent.app.models.responses import UserFacingResponse
from agent.app.models.tasks import TaskKind
from agent.app.models.telegram import TelegramCallback
from agent.app.tools.model_catalog import ModelCatalogError, ModelCatalogService

_RESTART_HINT = "Send MODELS to start again."


async def handle_model_config_callback(
    chat_id: str,
    callback: TelegramCallback | None,
    *,
    client: AuroraClient,
    settings: Settings,
    model_catalog: ModelCatalogService,
    wizard_cache: ModelWizardCache,
    presenters: Presenters,
) -> UserFacingResponse:
    action = callback.action if callback else "modelmenu"
    value = callback.value if callback else None

    if action in ("modelmenu", "modelback"):
        return await _show_task_menu(chat_id, client, settings, presenters)

    if action == "modeltask" and value:
        try:
            task_kind = TaskKind(value)
        except ValueError:
            return presenters.error("Unrecognized task kind.", next_action=_RESTART_HINT)
        return await _show_model_list(chat_id, task_kind, client, model_catalog, wizard_cache, presenters)

    if action == "modelrefresh":
        state = wizard_cache.get(chat_id)
        if state is None:
            return presenters.error("Your model configuration session expired.", next_action=_RESTART_HINT)
        return await _show_model_list(chat_id, state.task_kind, client, model_catalog, wizard_cache, presenters)

    if action == "modelpick" and value is not None:
        return await _show_model_detail(chat_id, value, wizard_cache, presenters)

    if action == "modelvariant" and value is not None:
        return await _save_variant(chat_id, value, client, wizard_cache, presenters)

    if action == "modeldefault":
        return await _save_with_params(chat_id, [], client, wizard_cache, presenters)

    if action == "modelcustom":
        return await _start_custom_params(chat_id, client, wizard_cache, presenters)

    if action == "modelparam" and value is not None:
        return await _choose_param_value(chat_id, value, client, wizard_cache, presenters)

    return presenters.error("Unrecognized model-configuration action.", next_action=_RESTART_HINT)


async def _show_task_menu(chat_id: str, client: AuroraClient, settings: Settings, presenters: Presenters) -> UserFacingResponse:
    configs = await model_config_repo.list_configs(client, chat_id)
    return presenters.model_task_menu(configs, settings.cursor_model)


async def _show_model_list(
    chat_id: str,
    task_kind: TaskKind,
    client: AuroraClient,
    model_catalog: ModelCatalogService,
    wizard_cache: ModelWizardCache,
    presenters: Presenters,
) -> UserFacingResponse:
    try:
        models = await model_catalog.list_models()
    except ModelCatalogError as error:
        return presenters.error(f"Could not fetch the model list: {error}", next_action="Try MODELS again shortly.")

    wizard_cache.set(chat_id, ModelWizardState(task_kind=task_kind, models=models))
    current = await model_config_repo.get_config(client, chat_id, task_kind)
    return presenters.model_list(task_kind, models, current.model_id if current else None)


async def _show_model_detail(
    chat_id: str, index_str: str, wizard_cache: ModelWizardCache, presenters: Presenters
) -> UserFacingResponse:
    state = wizard_cache.get(chat_id)
    if state is None:
        return presenters.error("Your model configuration session expired.", next_action=_RESTART_HINT)

    index = _safe_int(index_str)
    if index is None or not (0 <= index < len(state.models)):
        return presenters.error("That model is no longer in the list.", next_action="Send MODELS to refresh.")

    wizard_cache.set(chat_id, state.model_copy(update={"selected_model_index": index, "chosen_params": {}, "pending_param_index": None}))
    return presenters.model_detail(state.task_kind, state.models[index])


async def _save_variant(
    chat_id: str, index_str: str, client: AuroraClient, wizard_cache: ModelWizardCache, presenters: Presenters
) -> UserFacingResponse:
    state = wizard_cache.get(chat_id)
    model = state.selected_model if state else None
    if state is None or model is None:
        return presenters.error("Your model configuration session expired.", next_action=_RESTART_HINT)

    index = _safe_int(index_str)
    if index is None or not (0 <= index < len(model.variants)):
        return presenters.error("That option is no longer available.", next_action="Send MODELS to refresh.")

    variant = model.variants[index]
    return await _save_with_params(chat_id, variant.params, client, wizard_cache, presenters)


async def _start_custom_params(
    chat_id: str, client: AuroraClient, wizard_cache: ModelWizardCache, presenters: Presenters
) -> UserFacingResponse:
    state = wizard_cache.get(chat_id)
    model = state.selected_model if state else None
    if state is None or model is None:
        return presenters.error("Your model configuration session expired.", next_action=_RESTART_HINT)
    if not model.parameters:
        # Defensive fallback only — the presenter never shows a "Customize"
        # button for a model with no parameters, so this path should not
        # normally be reachable.
        return await _save_with_params(chat_id, [], client, wizard_cache, presenters)

    wizard_cache.set(chat_id, state.model_copy(update={"pending_param_index": 0, "chosen_params": {}}))
    return presenters.model_param_step(state.task_kind, model, model.parameters[0], step_index=0, total_steps=len(model.parameters))


async def _choose_param_value(
    chat_id: str, value_index_str: str, client: AuroraClient, wizard_cache: ModelWizardCache, presenters: Presenters
) -> UserFacingResponse:
    state = wizard_cache.get(chat_id)
    model = state.selected_model if state else None
    if state is None or model is None or state.pending_param_index is None:
        return presenters.error("Your model configuration session expired.", next_action=_RESTART_HINT)

    param_index = state.pending_param_index
    if not (0 <= param_index < len(model.parameters)):
        return presenters.error("That step is no longer valid.", next_action="Send MODELS to refresh.")
    param = model.parameters[param_index]

    value_index = _safe_int(value_index_str)
    if value_index is None or not (0 <= value_index < len(param.values)):
        return presenters.error("That value is no longer available.", next_action="Send MODELS to refresh.")

    chosen_params = {**state.chosen_params, param.id: param.values[value_index].value}
    next_param_index = param_index + 1

    if next_param_index < len(model.parameters):
        wizard_cache.set(
            chat_id, state.model_copy(update={"chosen_params": chosen_params, "pending_param_index": next_param_index})
        )
        return presenters.model_param_step(
            state.task_kind, model, model.parameters[next_param_index], step_index=next_param_index, total_steps=len(model.parameters)
        )

    params = [ModelParamChoice(id=param_id, value=value) for param_id, value in chosen_params.items()]
    return await _save_with_params(chat_id, params, client, wizard_cache, presenters)


async def _save_with_params(
    chat_id: str,
    params: list[ModelParamChoice],
    client: AuroraClient,
    wizard_cache: ModelWizardCache,
    presenters: Presenters,
) -> UserFacingResponse:
    state = wizard_cache.get(chat_id)
    model = state.selected_model if state else None
    if state is None or model is None:
        return presenters.error("Your model configuration session expired.", next_action=_RESTART_HINT)

    config = ModelConfig(
        chat_id=chat_id, task_kind=state.task_kind, model_id=model.id, model_display_name=model.display_name, params=params
    )
    await model_config_repo.upsert_config(client, config)
    wizard_cache.clear(chat_id)
    return presenters.model_config_saved(state.task_kind, config)


def _safe_int(value: str) -> int | None:
    try:
        return int(value)
    except ValueError:
        return None
