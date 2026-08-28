"""agent/app/delivery.py's deliver_response — the one place that
materializes private content, persists its pointer, and chunks for
delivery, shared by both the deterministic (application.py) and
predictive-task (workers/task_worker.py) paths.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from agent.app.db.aurora_client import AuroraClient
from agent.app.delivery import deliver_response
from agent.app.models.artifacts import ArtifactBucket, ArtifactLocation, ContentVisibility, PrivateArtifactMetadata
from agent.app.models.responses import UserFacingResponse
from agent.app.models.telegram import DeliveryKind
from agent.app.tests.helpers.fake_rds_client import FakeRdsDataClient
from agent.app.tools import artifact_store as artifact_store_module
from agent.app.tools.artifact_store import ArtifactStore, LocalPrivateBackupStore, PrivateStores


class FakeS3Client:
    def put_object(self, **kwargs) -> None:
        pass


@pytest.fixture(autouse=True)
def _fake_s3(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(artifact_store_module, "_s3_client", lambda: FakeS3Client())


def _client(fake: FakeRdsDataClient) -> AuroraClient:
    return AuroraClient(fake, resource_arn="a", secret_arn="s", database="d")


def _private_stores(tmp_path: Path) -> PrivateStores:
    return PrivateStores(
        bucket=ArtifactStore(bucket_kind=ArtifactBucket.PRIVATE_USER_ARTIFACTS, bucket_name="b", prefix=""),
        local_backup=LocalPrivateBackupStore(tmp_path),
    )


@pytest.mark.asyncio
async def test_deliver_response_materializes_and_persists_a_private_text_response(tmp_path: Path) -> None:
    fake = FakeRdsDataClient()
    response = UserFacingResponse(visibility=ContentVisibility.PRIVATE_USER, body="private advice text")

    materialized, messages = await deliver_response(
        _client(fake), _private_stores(tmp_path), response, "42", source_task_id="task-1"
    )

    assert materialized.body is None
    assert materialized.private_artifact is not None
    assert messages[0].delivery_kind == DeliveryKind.PRIVATE_ARTIFACT
    assert any("INSERT INTO private_artifacts" in call.kwargs["sql"] for call in fake.calls)


@pytest.mark.asyncio
async def test_deliver_response_does_not_reinsert_an_already_materialized_resume_artifact(tmp_path: Path) -> None:
    fake = FakeRdsDataClient()
    artifact = PrivateArtifactMetadata(
        chat_id="42",
        kind="augmented_resume",
        location=ArtifactLocation(bucket=ArtifactBucket.PRIVATE_USER_ARTIFACTS, key="k", checksum_sha256="c", byte_size=1),
    )
    response = UserFacingResponse(visibility=ContentVisibility.PRIVATE_USER, private_artifact=artifact)

    _, messages = await deliver_response(_client(fake), _private_stores(tmp_path), response, "42", source_task_id="task-1")

    assert messages[0].artifact == artifact.location
    # Already persisted inside db_gate.py's transaction (see
    # graph/nodes.py's _resume_result_to_writes) — never re-inserted here.
    assert fake.calls == []


@pytest.mark.asyncio
async def test_deliver_response_passes_public_responses_through_untouched(tmp_path: Path) -> None:
    fake = FakeRdsDataClient()
    response = UserFacingResponse(visibility=ContentVisibility.PUBLIC_JOB_SEARCH, body="backlog text")

    materialized, messages = await deliver_response(
        _client(fake), _private_stores(tmp_path), response, "42", source_task_id=None
    )

    assert materialized is response
    assert messages[0].delivery_kind == DeliveryKind.PUBLIC_TEXT
    assert fake.calls == []
