"""db/db_gate.py is the one path a domain write can take — these tests prove
its three real jobs: no-op on an empty write set, replay-safety via
idempotency_key (a re-run must not re-insert), and cache invalidation on a
genuinely new write. Nothing here calls domain_repo/db_gate with raw dicts —
by construction, only models/db_writes.py's typed models can reach this far
(see test_models.py for the rejection side of that guarantee).
"""
from __future__ import annotations

import pytest

from agent.app.cache.invalidation import CacheInvalidator
from agent.app.cache.ttl_cache import TtlCache
from agent.app.db.aurora_client import AuroraClient
from agent.app.db.db_gate import DbGate
from agent.app.models.artifacts import ArtifactBucket, ArtifactLocation, PrivateArtifactMetadata
from agent.app.models.db_writes import DbWriteSet, JobListingWrite, PrivateArtifactWrite
from agent.app.tests.helpers.fake_rds_client import FakeRdsDataClient, formatted_records


def _client(fake: FakeRdsDataClient) -> AuroraClient:
    return AuroraClient(fake, resource_arn="arn:aurora", secret_arn="arn:secret", database="job4younow")


def _gate(fake: FakeRdsDataClient) -> DbGate:
    return DbGate(_client(fake), CacheInvalidator(TtlCache()))


def _fake_with_no_existing_receipt() -> FakeRdsDataClient:
    fake = FakeRdsDataClient()

    def execute_statement(kwargs: dict) -> dict:
        sql = kwargs["sql"]
        if "FROM db_write_receipts" in sql:
            return formatted_records([])
        if "INSERT INTO companies" in sql:
            return formatted_records([{"id": "company-1"}])
        return {"numberOfRecordsUpdated": 1}

    fake.when("execute_statement", execute_statement)
    return fake


@pytest.mark.asyncio
async def test_apply_with_no_writes_is_a_noop_and_touches_no_db_call() -> None:
    fake = FakeRdsDataClient()
    write_set = DbWriteSet(idempotency_key="k", source_task_id="task-1", chat_id="42", writes=[])

    receipt = await _gate(fake).apply(write_set)

    assert receipt.applied is False
    assert fake.calls == []


@pytest.mark.asyncio
async def test_apply_persists_job_listing_and_reports_counts() -> None:
    fake = _fake_with_no_existing_receipt()
    write_set = DbWriteSet(
        idempotency_key="task-1",
        source_task_id="task-1",
        chat_id="42",
        writes=[JobListingWrite(role_id="backend", company_name="Acme", title="Senior SWE")],
    )

    receipt = await _gate(fake).apply(write_set)

    assert receipt.applied is True
    assert receipt.counts["job_listings"] == 1
    assert receipt.affected_role_ids == ["backend"]
    method_sequence = [call.method for call in fake.calls]
    assert "begin_transaction" in method_sequence
    assert "commit_transaction" in method_sequence


@pytest.mark.asyncio
async def test_apply_persists_a_private_artifact_pointer_never_content() -> None:
    fake = _fake_with_no_existing_receipt()
    artifact = PrivateArtifactMetadata(
        chat_id="42",
        kind="augmented_resume",
        location=ArtifactLocation(bucket=ArtifactBucket.PRIVATE_USER_ARTIFACTS, key="k", checksum_sha256="c", byte_size=1),
    )
    write_set = DbWriteSet(
        idempotency_key="task-1", source_task_id="task-1", chat_id="42", writes=[PrivateArtifactWrite(metadata=artifact)]
    )

    receipt = await _gate(fake).apply(write_set)

    assert receipt.applied is True
    assert receipt.counts["private_artifacts"] == 1
    # No role_id on a PrivateArtifactWrite, so it never shows up as an
    # "affected role" the way a job listing write does.
    assert receipt.affected_role_ids == []
    insert_calls = [call for call in fake.calls if "INSERT INTO private_artifacts" in call.kwargs.get("sql", "")]
    assert len(insert_calls) == 1


@pytest.mark.asyncio
async def test_apply_is_idempotent_for_a_repeated_write_set() -> None:
    fake = FakeRdsDataClient()
    existing_row = {"counts": {"job_listings": 1}, "affected_role_ids": ["backend"]}
    fake.when(
        "execute_statement",
        lambda kwargs: formatted_records([existing_row]) if "FROM db_write_receipts" in kwargs["sql"] else {"numberOfRecordsUpdated": 0},
    )
    write_set = DbWriteSet(
        idempotency_key="task-1",
        source_task_id="task-1",
        chat_id="42",
        writes=[JobListingWrite(role_id="backend", company_name="Acme", title="Senior SWE")],
    )

    receipt = await _gate(fake).apply(write_set)

    assert receipt.counts == {"job_listings": 1}
    # Only the existence check ran — no transaction, no re-insert.
    assert all(call.method != "begin_transaction" for call in fake.calls)


@pytest.mark.asyncio
async def test_apply_invalidates_backlog_cache_on_a_new_write() -> None:
    fake = _fake_with_no_existing_receipt()
    cache = TtlCache()
    from agent.app.cache import keys

    cache.set(keys.backlog().as_string(), ["stale"], ttl_seconds=60)
    gate = DbGate(_client(fake), CacheInvalidator(cache))

    write_set = DbWriteSet(
        idempotency_key="task-1",
        source_task_id="task-1",
        chat_id="42",
        writes=[JobListingWrite(role_id="backend", company_name="Acme", title="Senior SWE")],
    )
    await gate.apply(write_set)

    assert cache.get(keys.backlog().as_string()) is None
