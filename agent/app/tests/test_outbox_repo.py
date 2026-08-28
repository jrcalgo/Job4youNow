"""outbox_repo.py against a fake Data API client — proves public text and
artifact deliveries land in the correct, disjoint columns, matching
TelegramOutboundMessage's own delivery_kind/content pairing (see that
model's validator — this module trusts it rather than re-checking it).
"""
from __future__ import annotations

import pytest

from agent.app.db import outbox_repo
from agent.app.db.aurora_client import AuroraClient
from agent.app.models.artifacts import ArtifactBucket, ArtifactLocation
from agent.app.models.telegram import DeliveryKind, TelegramOutboundMessage
from agent.app.tests.helpers.fake_rds_client import FakeRdsDataClient, param_value


def _client(fake: FakeRdsDataClient) -> AuroraClient:
    return AuroraClient(fake, resource_arn="a", secret_arn="s", database="d")


@pytest.mark.asyncio
async def test_enqueue_public_text_message_stores_public_payload_only() -> None:
    fake = FakeRdsDataClient()
    message = TelegramOutboundMessage(chat_id="42", delivery_kind=DeliveryKind.PUBLIC_TEXT, text="hello")

    await outbox_repo.enqueue_messages(_client(fake), [message], task_id="task-1")

    params = fake.calls[0].kwargs["parameterSets"][0]
    assert param_value(params, "delivery_kind") == {"stringValue": "public_text"}
    assert param_value(params, "public_payload")["stringValue"]
    assert param_value(params, "artifact_ref") == {"isNull": True}


@pytest.mark.asyncio
async def test_enqueue_private_artifact_message_stores_artifact_ref_only() -> None:
    fake = FakeRdsDataClient()
    artifact = ArtifactLocation(bucket=ArtifactBucket.PRIVATE_USER_ARTIFACTS, key="k", checksum_sha256="abc", byte_size=1)
    message = TelegramOutboundMessage(
        chat_id="42", delivery_kind=DeliveryKind.PRIVATE_ARTIFACT, artifact=artifact, caption="Your resume"
    )

    await outbox_repo.enqueue_messages(_client(fake), [message], task_id="task-1")

    params = fake.calls[0].kwargs["parameterSets"][0]
    assert param_value(params, "delivery_kind") == {"stringValue": "private_artifact"}
    assert param_value(params, "public_payload") == {"isNull": True}
    assert param_value(params, "artifact_ref")["stringValue"]


@pytest.mark.asyncio
async def test_enqueue_messages_is_a_noop_for_an_empty_list() -> None:
    fake = FakeRdsDataClient()
    await outbox_repo.enqueue_messages(_client(fake), [])
    assert fake.calls == []


@pytest.mark.asyncio
async def test_list_pending_selects_the_delivery_kind_aware_columns() -> None:
    fake = FakeRdsDataClient()
    await outbox_repo.list_pending(_client(fake))
    assert "delivery_kind" in fake.calls[0].kwargs["sql"]
    assert "artifact_ref" in fake.calls[0].kwargs["sql"]
