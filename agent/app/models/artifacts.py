"""Storage/privacy boundary vocabulary — what every other layer uses to
decide "does this touch Aurora as content, or only as a pointer" and "which
of the two S3 buckets does this belong in". See the plan's "Target Storage
Policy" for the decision table this encodes:

- Public job-search data (listings/companies/contacts, public text
  responses): Aurora content is fine.
- Private user data (resumes, anything derived from them, private
  responses): Aurora may only ever hold an ArtifactLocation pointer — the
  actual bytes live in the private-user S3 bucket plus a local backup.

Exactly two buckets exist by design (see ArtifactBucket) — do not add a
bucket per output kind. tools/artifact_store.py is the only code that
constructs an ArtifactLocation from real bytes.
"""
from __future__ import annotations

import uuid
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


class ContentVisibility(StrEnum):
    """Attached to every UserFacingResponse — never inferred from context.
    PRIVATE_USER is the conservative default; a presenter must explicitly
    opt into PUBLIC_JOB_SEARCH (see formatting/presenters.py)."""

    PUBLIC_JOB_SEARCH = "public_job_search"
    PRIVATE_USER = "private_user"


class ArtifactBucket(StrEnum):
    JOB_ARTIFACTS = "job_artifacts"
    PRIVATE_USER_ARTIFACTS = "private_user_artifacts"


PrivateArtifactKind = Literal["template_resume", "augmented_resume", "private_response", "private_report"]


class ArtifactLocation(BaseModel):
    """A pointer to file content — never the content itself. This is the
    only artifact shape Aurora is allowed to store for anything derived
    from private user data."""

    bucket: ArtifactBucket
    key: str
    checksum_sha256: str
    byte_size: int
    local_backup_path: str | None = None


def new_private_artifact_id() -> str:
    return f"private-artifact-{uuid.uuid4().hex[:12]}"


class PrivateArtifactMetadata(BaseModel):
    """Attached to a UserFacingResponse in place of body text once private
    content has been materialized to storage — see
    formatting/delivery.py's materialize_private_response(). Also the exact
    shape persisted (as metadata only) in Aurora's `private_artifacts`
    table, via models/db_writes.py's PrivateArtifactWrite.

    `id` is generated here, client-side, the moment content is written to
    storage — the SAME id then flows into both the DB write and the
    response pointer, so nothing needs a round trip to Aurora to learn
    "which row did that just become" before it can tell the user about it."""

    id: str = Field(default_factory=new_private_artifact_id)
    chat_id: str
    kind: PrivateArtifactKind
    location: ArtifactLocation
    source_task_id: str | None = None
