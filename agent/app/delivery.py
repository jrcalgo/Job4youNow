"""Orchestrates turning a UserFacingResponse into delivered outbox rows:
materialize private content to storage, persist its metadata pointer, then
chunk for delivery. The ONE place workers/task_worker.py (predictive tasks)
and application.py (deterministic immediate replies) both call, so this
three-step sequence is never duplicated between the two paths.

Lives at this top level (not under formatting/ or db/) because it composes
both — formatting/delivery.py's pure storage-materialization step and
db/domain_repo.py's Aurora pointer-persistence step — and the plan's
dependency direction deliberately keeps those two layers from importing
each other directly.
"""
from __future__ import annotations

from agent.app.db import domain_repo
from agent.app.db.aurora_client import AuroraClient
from agent.app.formatting.chunking import to_outbound_messages
from agent.app.formatting.delivery import materialize_private_response
from agent.app.models.responses import UserFacingResponse
from agent.app.models.telegram import TelegramOutboundMessage
from agent.app.tools.artifact_store import PrivateStores


async def deliver_response(
    client: AuroraClient,
    private_stores: PrivateStores,
    response: UserFacingResponse,
    chat_id: str,
    *,
    source_task_id: str | None,
) -> tuple[UserFacingResponse, list[TelegramOutboundMessage]]:
    """Returns the (possibly materialized) response alongside the outbox
    messages ready to enqueue. Callers pass the SAME materialized response
    to `db/task_repo.save_task_result` afterward — never the original,
    pre-materialization one."""
    materialized = await materialize_private_response(response, chat_id, private_stores)

    # `is not response` means materialization actually ran just now (a
    # no-op returns the SAME object — see materialize_private_response's
    # guard). A response whose `private_artifact` was already set before
    # this call (the resume case — see presenters.resume_result) already
    # had its private_artifacts row written inside db_gate.py's
    # transaction; inserting it again here would be a duplicate.
    just_materialized = materialized is not response
    if just_materialized and materialized.private_artifact is not None:
        await domain_repo.insert_private_artifact(client, materialized.private_artifact, source_task_id=source_task_id)

    return materialized, to_outbound_messages(materialized, chat_id)
