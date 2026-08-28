"""Every route parses its request (FastAPI does that via the type
annotation) and delegates to AgentApplication — no SQL, no Cursor SDK calls,
no Telegram text formatting happens in this file. See application.py for
the actual logic.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from agent.app.application import AgentApplication
from agent.app.models.responses import UserFacingResponse
from agent.app.models.schedules import ScanSchedule, ScheduleCreateRequest
from agent.app.models.telegram import TelegramUpdateEnvelope, TelegramUpdateResult

router = APIRouter()


def get_agent_application(request: Request) -> AgentApplication:
    return request.app.state.agent_application


@router.post("/telegram/update", response_model=TelegramUpdateResult)
async def receive_telegram_update(
    envelope: TelegramUpdateEnvelope,
    app: AgentApplication = Depends(get_agent_application),
) -> TelegramUpdateResult:
    return await app.handle_telegram_update(envelope)


@router.get("/telegram/outbox")
async def get_pending_outbox(
    limit: int = 50,
    app: AgentApplication = Depends(get_agent_application),
) -> list[dict]:
    return await app.list_pending_outbox(limit=limit)


@router.post("/telegram/outbox/{outbox_id}/delivered")
async def acknowledge_outbox_delivery(
    outbox_id: str,
    app: AgentApplication = Depends(get_agent_application),
) -> dict:
    await app.mark_outbox_delivered(outbox_id)
    return {"ok": True}


@router.post("/telegram/transport/hub")
async def set_telegram_hub_message(
    payload: dict,
    app: AgentApplication = Depends(get_agent_application),
) -> dict:
    chat_id = str(payload.get("chat_id", ""))
    hub_message_id = payload.get("hub_message_id")
    if not chat_id:
        return {"ok": False, "error": "chat_id required"}
    await app.set_hub_message_id(chat_id, int(hub_message_id) if hub_message_id is not None else None)
    return {"ok": True}


@router.get("/job-listings/{listing_id}")
async def get_job_listing(
    listing_id: str,
    app: AgentApplication = Depends(get_agent_application),
) -> dict:
    listing = await app.get_job_listing(listing_id)
    if listing is None:
        return {"ok": False, "error": "not_found"}
    return {"ok": True, "listing": listing}


@router.patch("/job-listings/{listing_id}/status")
async def patch_job_listing_status(
    listing_id: str,
    payload: dict,
    app: AgentApplication = Depends(get_agent_application),
) -> dict:
    status = str(payload.get("status", ""))
    ok = await app.update_listing_status(listing_id, status)
    return {"ok": ok}


@router.get("/schedules")
async def list_schedules(
    chat_id: str,
    app: AgentApplication = Depends(get_agent_application),
) -> list[ScanSchedule]:
    return await app.list_schedules(chat_id)


@router.patch("/schedules/{schedule_id}")
async def patch_schedule(
    schedule_id: str,
    payload: dict,
    app: AgentApplication = Depends(get_agent_application),
) -> dict:
    if "enabled" in payload:
        await app.set_schedule_enabled(schedule_id, bool(payload["enabled"]))
    if "interval_seconds" in payload:
        await app.update_schedule_interval(schedule_id, int(payload["interval_seconds"]))
    return {"ok": True}


@router.delete("/schedules/{schedule_id}")
async def delete_schedule_route(
    schedule_id: str,
    app: AgentApplication = Depends(get_agent_application),
) -> dict:
    await app.delete_schedule(schedule_id)
    return {"ok": True}


@router.get("/tasks/{task_id}", response_model=UserFacingResponse)
async def get_task_status(
    task_id: str,
    app: AgentApplication = Depends(get_agent_application),
) -> UserFacingResponse:
    return await app.get_task_status_response(task_id)


@router.post("/schedules", response_model=ScanSchedule)
async def create_schedule(
    payload: ScheduleCreateRequest,
    app: AgentApplication = Depends(get_agent_application),
) -> ScanSchedule:
    return await app.create_schedule(payload)


@router.get("/health")
async def health() -> dict:
    return {"status": "ok"}
