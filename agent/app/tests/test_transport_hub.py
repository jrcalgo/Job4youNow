"""Hub message id transport persistence."""
from __future__ import annotations

import pytest

from agent.app.db.aurora_client import AuroraClient
from agent.app.db.transport_repo import set_hub_message_id
from agent.app.tests.helpers.fake_rds_client import FakeRdsDataClient


def _client(fake: FakeRdsDataClient) -> AuroraClient:
    return AuroraClient(fake, resource_arn="arn:aurora", secret_arn="arn:secret", database="job4younow")


@pytest.mark.asyncio
async def test_set_hub_message_id_executes_upsert() -> None:
    fake = FakeRdsDataClient()
    client = _client(fake)
    await set_hub_message_id(client, "42", 12345)
    assert fake.calls
    sql = fake.calls[-1].kwargs.get("sql", "")
    assert "hub_message_id" in sql
