"""Materializes a PRIVATE_USER response's inline body to storage and swaps
it for a pointer — the ONE place that happens, so no presenter, graph node,
or repository needs to duplicate this logic.

Storage I/O only (S3 + local backup) — this module never touches Aurora.
Persisting the resulting `PrivateArtifactMetadata` as a `private_artifacts`
row is the caller's job; see agent/app/delivery.py, which calls this AND
`db/domain_repo.insert_private_artifact` together so the two never drift
apart.
"""
from __future__ import annotations

import uuid

from agent.app.models.artifacts import ContentVisibility, PrivateArtifactMetadata
from agent.app.models.responses import UserFacingResponse
from agent.app.tools.artifact_store import PrivateStores


async def materialize_private_response(
    response: UserFacingResponse, chat_id: str, private_stores: PrivateStores
) -> UserFacingResponse:
    """No-op for anything that isn't an unmaterialized private response —
    safe to call unconditionally on every response, public or private,
    already-materialized or not (see agent/app/delivery.py's single call
    site for both the deterministic and predictive-task paths)."""
    if response.visibility != ContentVisibility.PRIVATE_USER or response.body is None:
        return response

    # Known limitation: unlike resume artifacts (which flow through
    # db_gate.py's idempotency-key-checked transaction), a task retry here
    # writes a new object under a new random key and a new
    # `private_artifacts` row rather than reusing one from a prior attempt.
    # That is wasted storage, never a privacy or correctness problem — the
    # data is still exactly as private either way — so it is left as a
    # known trade-off rather than added complexity for a rare case.
    content = response.body.encode("utf-8")
    relative_key = f"responses/{uuid.uuid4().hex[:12]}.md"
    location = await private_stores.write_private_bytes(relative_key, content)
    artifact = PrivateArtifactMetadata(chat_id=chat_id, kind="private_response", location=location)
    return response.model_copy(update={"body": None, "private_artifact": artifact})
