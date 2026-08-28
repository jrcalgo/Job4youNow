"""Proves task_repo.save_task_result enforces the storage policy at the
persistence boundary itself, not just by convention: a PRIVATE_USER
response that still has inline body text (never materialized) must be
rejected here, before any DB call — never silently stored inline.
"""
from __future__ import annotations

import pytest

from agent.app.db import task_repo
from agent.app.db.aurora_client import AuroraClient
from agent.app.models.artifacts import ArtifactBucket, ArtifactLocation, ContentVisibility, PrivateArtifactMetadata
from agent.app.models.responses import UserFacingResponse
from agent.app.tests.helpers.fake_rds_client import FakeRdsDataClient, param_value


def _client(fake: FakeRdsDataClient) -> AuroraClient:
    return AuroraClient(fake, resource_arn="a", secret_arn="s", database="d")


@pytest.mark.asyncio
async def test_save_task_result_rejects_an_unmaterialized_private_response() -> None:
    fake = FakeRdsDataClient()
    response = UserFacingResponse(visibility=ContentVisibility.PRIVATE_USER, body="still has inline text")

    with pytest.raises(ValueError, match="materialized"):
        await task_repo.save_task_result(_client(fake), "task-1", response=response, tool_result_metadata=[])

    assert fake.calls == []


@pytest.mark.asyncio
async def test_save_task_result_stores_public_response_inline() -> None:
    fake = FakeRdsDataClient()
    response = UserFacingResponse(visibility=ContentVisibility.PUBLIC_JOB_SEARCH, body="backlog text")

    await task_repo.save_task_result(_client(fake), "task-1", response=response, tool_result_metadata=[])

    params = fake.calls[0].kwargs["parameters"]
    assert param_value(params, "visibility") == {"stringValue": "public_job_search"}
    assert param_value(params, "public_response")["stringValue"]
    assert param_value(params, "private_artifact_id") == {"isNull": True}


@pytest.mark.asyncio
async def test_save_task_result_stores_only_the_pointer_for_a_materialized_private_response() -> None:
    fake = FakeRdsDataClient()
    artifact = PrivateArtifactMetadata(
        chat_id="42",
        kind="private_response",
        location=ArtifactLocation(bucket=ArtifactBucket.PRIVATE_USER_ARTIFACTS, key="k", checksum_sha256="abc", byte_size=1),
    )
    response = UserFacingResponse(visibility=ContentVisibility.PRIVATE_USER, private_artifact=artifact)

    await task_repo.save_task_result(_client(fake), "task-1", response=response, tool_result_metadata=[])

    params = fake.calls[0].kwargs["parameters"]
    assert param_value(params, "visibility") == {"stringValue": "private_user"}
    assert param_value(params, "public_response") == {"isNull": True}
    assert param_value(params, "private_artifact_id") == {"stringValue": artifact.id}
