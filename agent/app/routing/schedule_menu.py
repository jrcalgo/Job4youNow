"""Telegram schedule CRUD menu — see routing/deterministic.py."""
from __future__ import annotations

import re

from agent.app.config import Settings
from agent.app.db import domain_repo, schedule_repo
from agent.app.db.aurora_client import AuroraClient
from agent.app.formatting.presenters import Presenters
from agent.app.models.responses import UserFacingResponse
from agent.app.models.schedules import ScanSchedule
from agent.app.models.telegram import TelegramCallback

_INTERVAL_PRESETS = [
    ("6h", 6 * 3600),
    ("12h", 12 * 3600),
    ("24h", 24 * 3600),
    ("168h", 168 * 3600),
]

_PENDING_ADD: dict[str, dict] = {}


async def handle_schedule_menu_callback(
    chat_id: str,
    callback: TelegramCallback | None,
    *,
    presenters: Presenters,
    client: AuroraClient,
    settings: Settings,
) -> UserFacingResponse:
    action = callback.action if callback else "sched"
    value = callback.value if callback else "list"

    if action != "sched":
        return presenters.error("Unrecognized schedule action.")

    if value == "list" or value is None:
        schedules = await schedule_repo.list_by_chat(client, chat_id)
        return presenters.schedule_list(schedules)

    if value == "add":
        roles = await domain_repo.list_backlog_by_role(client)
        role_ids = [r.role_id for r in roles] if roles else ["backend"]
        return presenters.schedule_add_role_picker(role_ids)

    if value and value.startswith("addrole:"):
        role_id = value.split(":", 1)[1]
        _PENDING_ADD[chat_id] = {"role_id": role_id, "query": role_id}
        return presenters.schedule_add_interval_picker(role_id)

    if value and value.startswith("interval:"):
        rest = value.split(":", 2)
        if len(rest) < 3:
            return presenters.error("Invalid interval selection.")
        _, role_id, hours_str = rest[0], rest[1], rest[2]
        try:
            hours = int(hours_str)
        except ValueError:
            return presenters.error("Invalid interval hours.")
        pending = _PENDING_ADD.get(chat_id, {})
        query = pending.get("query") or role_id
        schedule = ScanSchedule(chat_id=chat_id, role_id=role_id, query=query, interval_seconds=hours * 3600)
        created = await schedule_repo.create_schedule(client, schedule)
        _PENDING_ADD.pop(chat_id, None)
        return presenters.schedule_created(created)

    if value and value.startswith("edit:"):
        schedule_id = value.split(":", 1)[1]
        sched = await schedule_repo.get_schedule(client, schedule_id)
        if sched is None:
            return presenters.error("Schedule not found.")
        return presenters.schedule_edit_interval_picker(sched)

    if value and value.startswith("setint:"):
        parts = value.split(":")
        if len(parts) < 3:
            return presenters.error("Invalid interval selection.")
        _, schedule_id, hours_str = parts[0], parts[1], parts[2]
        try:
            hours = int(hours_str)
        except ValueError:
            return presenters.error("Invalid interval hours.")
        sched = await schedule_repo.get_schedule(client, schedule_id)
        if sched is None:
            return presenters.error("Schedule not found.")
        await schedule_repo.update_interval(client, schedule_id, max(hours, 1) * 3600)
        schedules = await schedule_repo.list_by_chat(client, chat_id)
        return presenters.schedule_list(schedules)

    if value and value.startswith("toggle:"):
        schedule_id = value.split(":", 1)[1]
        sched = await schedule_repo.get_schedule(client, schedule_id)
        if sched is None:
            return presenters.error("Schedule not found.")
        await schedule_repo.set_enabled(client, schedule_id, not sched.enabled)
        schedules = await schedule_repo.list_by_chat(client, chat_id)
        return presenters.schedule_list(schedules)

    if value and value.startswith("del:"):
        schedule_id = value.split(":", 1)[1]
        return presenters.schedule_confirm_delete(schedule_id)

    if value and value.startswith("delconfirm:"):
        schedule_id = value.split(":", 1)[1]
        await schedule_repo.delete_schedule(client, schedule_id)
        schedules = await schedule_repo.list_by_chat(client, chat_id)
        return presenters.schedule_list(schedules)

    if value and value.startswith("create:"):
        return await _create_from_text(chat_id, value, client, presenters)

    return presenters.error("Unrecognized schedule action.")


async def _create_from_text(
    chat_id: str, value: str, client: AuroraClient, presenters: Presenters
) -> UserFacingResponse:
    """Parse sched:create:<role>:<query>:<hours> from SCHEDULE text command."""
    parts = value.split(":", 3)
    if len(parts) < 4:
        return presenters.error("Could not parse SCHEDULE command.")
    _, role_id, query, hours_str = parts[0], parts[1], parts[2], parts[3]
    try:
        hours = int(hours_str)
    except ValueError:
        return presenters.error("Invalid interval in SCHEDULE command.")
    schedule = ScanSchedule(chat_id=chat_id, role_id=role_id, query=query, interval_seconds=max(hours, 1) * 3600)
    created = await schedule_repo.create_schedule(client, schedule)
    return presenters.schedule_created(created)


def parse_schedule_text(text: str) -> tuple[str, str, int] | None:
    """SCHEDULE <role> <query> every <N>h"""
    remainder = text.strip()
    if remainder.upper().startswith("SCHEDULE "):
        remainder = remainder[9:].strip()
    match = re.match(r"^(\S+)\s+(.+?)\s+every\s+(\d+)\s*h\s*$", remainder, re.IGNORECASE)
    if not match:
        return None
    role_id, query, hours = match.group(1), match.group(2).strip(), int(match.group(3))
    return role_id.lower(), query, hours
