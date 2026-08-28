"""Proves deterministic vs predictive classification for every recognized
command shape — and, just as importantly, that unrecognized free text always
becomes a predictive task rather than silently being dropped.
"""
from __future__ import annotations

from agent.app.models.intent import RouteKind
from agent.app.models.tasks import AugmentResumePayload, ScanRolePayload, TaskKind
from agent.app.models.telegram import TelegramCallback, TelegramCommand
from agent.app.routing.intent_router import parse_callback, route_telegram_command


def _command(**kwargs) -> TelegramCommand:
    return TelegramCommand(chat_id="42", update_id=1, **kwargs)


def test_backlog_command_is_deterministic() -> None:
    intent = route_telegram_command(_command(text="BACKLOG"))
    assert intent.is_deterministic
    assert intent.route == RouteKind.BACKLOG


def test_empty_text_routes_to_main_menu() -> None:
    intent = route_telegram_command(_command(text=""))
    assert intent.is_deterministic
    assert intent.route == RouteKind.MAIN_MENU


def test_start_at_bot_routes_to_main_menu() -> None:
    intent = route_telegram_command(_command(text="/start@MyJobBot"))
    assert intent.is_deterministic
    assert intent.route == RouteKind.MAIN_MENU


def test_scan_command_is_predictive_scan_role_task() -> None:
    intent = route_telegram_command(_command(text="SCAN backend senior python roles"))
    assert not intent.is_deterministic
    assert intent.task_kind == TaskKind.SCAN_ROLE
    assert isinstance(intent.task_payload, ScanRolePayload)
    assert intent.task_payload.role_id == "backend"
    assert intent.task_payload.query == "senior python roles"


def test_status_command_carries_task_id_in_query() -> None:
    intent = route_telegram_command(_command(text="STATUS task-abc123"))
    assert intent.is_deterministic
    assert intent.route == RouteKind.TASK_STATUS
    assert intent.query.task_id == "task-abc123"


def test_unrecognized_free_text_becomes_predictive_research() -> None:
    intent = route_telegram_command(_command(text="What does Acme Corp's engineering culture look like?"))
    assert not intent.is_deterministic
    assert intent.task_kind == TaskKind.RESEARCH_COMPANY


def test_role_callback_routes_to_backlog_browser_with_role_id() -> None:
    callback = parse_callback("role:backend")
    intent = route_telegram_command(_command(callback=callback))
    assert intent.is_deterministic
    assert intent.route == RouteKind.BACKLOG_BROWSER
    assert intent.query.role_id == "backend"


def test_backlog_callback_routes_to_backlog_same_as_the_text_command() -> None:
    """The Telegram adapter's main-menu Backlog button sends a bare
    "backlog" callback_data (see telegram/src/protocol/core.mjs's
    mainMenuKeyboard) — must route identically to typing BACKLOG."""
    callback = parse_callback("backlog")
    intent = route_telegram_command(_command(callback=callback))
    assert intent.is_deterministic
    assert intent.route == RouteKind.BACKLOG


def test_unknown_callback_action_falls_back_to_main_menu() -> None:
    callback = TelegramCallback(action="unknown_action", value="x")
    intent = route_telegram_command(_command(callback=callback))
    assert intent.route == RouteKind.MAIN_MENU


def test_resume_command_is_predictive_augment_resume_task_with_a_job_description() -> None:
    intent = route_telegram_command(_command(text="RESUME backend :: Senior Python engineer, remote, AI platform team"))
    assert not intent.is_deterministic
    assert intent.task_kind == TaskKind.AUGMENT_RESUME
    assert isinstance(intent.task_payload, AugmentResumePayload)
    assert intent.task_payload.target_role == "backend"
    assert "Python" in intent.task_payload.job_description


def test_settingsmenu_routes_to_settings_hub() -> None:
    intent = route_telegram_command(_command(callback=parse_callback("settingsmenu")))
    assert intent.route == RouteKind.SETTINGS_MENU


def test_settings_models_routes_to_model_config() -> None:
    intent = route_telegram_command(_command(callback=parse_callback("settings:models")))
    assert intent.route == RouteKind.MODEL_CONFIG
    assert intent.callback is not None
    assert intent.callback.action == "modelmenu"


def test_settings_schedules_routes_to_schedule_list() -> None:
    intent = route_telegram_command(_command(callback=parse_callback("settings:schedules")))
    assert intent.route == RouteKind.SCHEDULE_MENU
    assert intent.callback is not None
    assert intent.callback.action == "sched"
    assert intent.callback.value == "list"


def test_settings_backlog_routes_to_filter_menu() -> None:
    intent = route_telegram_command(_command(callback=parse_callback("settings:backlog")))
    assert intent.route == RouteKind.BACKLOG_FILTER_MENU
    assert intent.callback is not None
    assert intent.callback.action == "settings"
    assert intent.callback.value == "backlog"
