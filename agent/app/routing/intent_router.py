"""Classifies one TelegramCommand into an IntentDecision. Pure function, no
I/O — everything here is string parsing, which is what makes it fast to unit
test exhaustively (see tests/test_intent_router.py) and safe to call on
every single update without worrying about cost or side effects.

Deliberately simple command grammar (Occam's Razor): a handful of literal
prefixes and one callback_data convention (`action:value`). Add a new
recognized prefix only when a real menu button needs it — don't build a
generic command-parsing framework for a half-dozen commands.
"""
from __future__ import annotations

from agent.app.models.intent import DeterministicQuery, IntentDecision, RouteKind
from agent.app.models.tasks import AugmentResumePayload, ResearchCompanyPayload, ScanRolePayload
from agent.app.models.telegram import TelegramCallback, TelegramCommand
from agent.app.routing.schedule_menu import parse_schedule_text

_HELP_COMMANDS = {"HELP", "/HELP", "/START", "SETTINGS", "/SETTINGS"}
_BACKLOG_COMMANDS = {"BACKLOG", "/BACKLOG"}
_MODELS_COMMANDS = {"MODELS", "/MODELS"}

# Settings sub-menu buttons use callback_data "settings:<sub>" which parse_callback
# splits into action="settings", value="<sub>" — never action="settings:schedules".

# Every callback action the model-configuration wizard recognizes — see
# routing/model_config.py, which is what actually interprets `value` for
# each of these. Listed here only so this router can tell "this callback
# belongs to the model wizard" from "this callback belongs to something
# else" without the wizard's own logic leaking into this file.
_MODEL_CONFIG_ACTIONS = {
    "modelmenu", "modeltask", "modelrefresh", "modelpick",
    "modelvariant", "modeldefault", "modelcustom", "modelparam", "modelback",
}


def parse_callback(data: str) -> TelegramCallback:
    """"role:backend" -> action="role", value="backend". "help" -> action="help", value=None."""
    action, _, value = data.partition(":")
    return TelegramCallback(action=action, value=value or None)


def _normalize_command_text(text: str) -> str:
    """Strip Telegram /command@BotName suffix so /start@MyBot routes like START."""
    raw = text.strip()
    if raw.startswith("/") and "@" in raw:
        raw = raw.split("@", 1)[0]
    return raw


def route_telegram_command(command: TelegramCommand) -> IntentDecision:
    chat_id = command.chat_id
    if command.callback is not None:
        return _route_callback(chat_id, command.callback)

    text = _normalize_command_text(command.text or "")
    upper = text.upper()

    if not text or upper in _HELP_COMMANDS:
        return IntentDecision.for_route(chat_id, RouteKind.MAIN_MENU)
    if upper in _BACKLOG_COMMANDS:
        return IntentDecision.for_route(chat_id, RouteKind.BACKLOG)
    if upper in _MODELS_COMMANDS:
        return IntentDecision.for_route(chat_id, RouteKind.MODEL_CONFIG)
    if upper.startswith("STATUS "):
        return IntentDecision.for_route(chat_id, RouteKind.TASK_STATUS, DeterministicQuery(task_id=_second_token(text)))
    if upper.startswith("SCAN "):
        return IntentDecision.for_task(chat_id, _parse_scan(text))
    if upper.startswith("RESUME "):
        return IntentDecision.for_task(chat_id, _parse_resume(text))
    if upper.startswith("SCHEDULE "):
        parsed = parse_schedule_text(text)
        if parsed:
            role_id, query, hours = parsed
            return IntentDecision.for_route(
                chat_id,
                RouteKind.SCHEDULE_MENU,
                callback=TelegramCallback(action="sched", value=f"create:{role_id}:{query}:{hours}"),
            )
        return IntentDecision.for_route(chat_id, RouteKind.SCHEDULE_MENU, callback=TelegramCallback(action="sched", value="list"))

    # No recognized command shape -> free-text predictive research request.
    return IntentDecision.for_task(chat_id, ResearchCompanyPayload(query=command.text or text))


def _route_callback(chat_id: str, callback: TelegramCallback) -> IntentDecision:
    if callback.action == "main":
        return IntentDecision.for_route(chat_id, RouteKind.MAIN_MENU)
    if callback.action == "help":
        return IntentDecision.for_route(chat_id, RouteKind.HELP)
    if callback.action == "settingsmenu":
        return IntentDecision.for_route(chat_id, RouteKind.SETTINGS_MENU)
    if callback.action == "settings" and callback.value == "models":
        return IntentDecision.for_route(
            chat_id, RouteKind.MODEL_CONFIG, callback=TelegramCallback(action="modelmenu", value=None)
        )
    if callback.action == "settings" and callback.value == "schedules":
        return IntentDecision.for_route(
            chat_id, RouteKind.SCHEDULE_MENU, callback=TelegramCallback(action="sched", value="list")
        )
    if callback.action == "settings" and callback.value == "backlog":
        return IntentDecision.for_route(
            chat_id, RouteKind.BACKLOG_FILTER_MENU,
            callback=TelegramCallback(action="settings", value="backlog"),
        )
    if callback.action == "settings":
        return IntentDecision.for_route(chat_id, RouteKind.SETTINGS_MENU)
    if callback.action == "role" and callback.value:
        return IntentDecision.for_route(chat_id, RouteKind.BACKLOG_BROWSER, DeterministicQuery(role_id=callback.value))
    if callback.action == "task" and callback.value:
        return IntentDecision.for_route(chat_id, RouteKind.TASK_STATUS, DeterministicQuery(task_id=callback.value))
    # "backlog" — the Telegram adapter's main-menu Backlog button (see
    # telegram/src/protocol/core.mjs's mainMenuKeyboard). Bare action, no
    # value, same as the text command's own BACKLOG route below.
    if callback.action == "backlog":
        return IntentDecision.for_route(chat_id, RouteKind.BACKLOG)
    if callback.action in _MODEL_CONFIG_ACTIONS:
        return IntentDecision.for_route(chat_id, RouteKind.MODEL_CONFIG, callback=callback)
    if callback.action == "sched":
        return IntentDecision.for_route(chat_id, RouteKind.SCHEDULE_MENU, callback=callback)
    if callback.action == "bl":
        if callback.value and callback.value.startswith("fil:"):
            return IntentDecision.for_route(chat_id, RouteKind.BACKLOG_FILTER_MENU, callback=callback)
        return IntentDecision.for_route(chat_id, RouteKind.BACKLOG_BROWSER, callback=callback)
    return IntentDecision.for_route(chat_id, RouteKind.MAIN_MENU)


def _second_token(text: str) -> str:
    parts = text.split(None, 1)
    return parts[1].strip() if len(parts) > 1 else ""


def _parse_scan(text: str) -> ScanRolePayload:
    """"SCAN backend senior python, series B" -> role_id="backend", query="senior python, series B"."""
    remainder = _second_token(text)
    role_id, _, query = remainder.partition(" ")
    return ScanRolePayload(role_id=role_id.lower(), query=query.strip() or role_id)


def _parse_resume(text: str) -> AugmentResumePayload:
    """Prototype syntax: "RESUME <role> :: <job description>". Tailors the
    one canonical `cv.md` in career-ops-modified (see tools/resume_tool.py,
    which runs career-ops's `pdf` mode) — there is no template picker
    anymore, so this no longer takes a template id. A real UI would let the
    user paste/forward a full job posting instead of typing it inline;
    this stays a rough prototype grammar."""
    remainder = _second_token(text)
    role, _, job_description = remainder.partition("::")
    return AugmentResumePayload(
        target_role=role.strip() or "unspecified",
        job_description=job_description.strip() or "General tailoring request — no specific job description given.",
    )
