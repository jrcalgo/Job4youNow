"""db/domain_repo.py's job-listing read/write paths against a fake Data API
client — specifically the `location`/`posted_at` columns added by
migrations/0003_job_listing_scan_fields.sql to carry what career-ops's real
`scan` mode actually reports per offer (see tools/scan_tool.py).
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from agent.app.db import domain_repo
from agent.app.db.aurora_client import AuroraClient, _format_timestamp
from agent.app.models.db_writes import DbWriteSet, JobListingWrite
from agent.app.tests.helpers.fake_rds_client import FakeRdsDataClient, formatted_records, param_value


def _client(fake: FakeRdsDataClient) -> AuroraClient:
    return AuroraClient(fake, resource_arn="arn:aurora", secret_arn="arn:secret", database="job4younow")


@pytest.mark.asyncio
async def test_insert_job_listing_sends_location_and_posted_at() -> None:
    fake = FakeRdsDataClient()
    fake.when(
        "execute_statement",
        lambda kwargs: formatted_records([{"id": "company-1"}]) if "INSERT INTO companies" in kwargs["sql"] else {"numberOfRecordsUpdated": 1},
    )
    posted_at = datetime(2026, 1, 5, tzinfo=timezone.utc)
    write_set = DbWriteSet(
        idempotency_key="task-1",
        source_task_id="task-1",
        chat_id="42",
        writes=[
            JobListingWrite(
                role_id="backend", company_name="Acme", title="Senior SWE", location="Remote — Europe", posted_at=posted_at
            )
        ],
    )

    async with _client(fake).transaction() as transaction_id:
        await domain_repo.apply_write_set(_client(fake), transaction_id, write_set)

    insert_call = next(c for c in fake.calls if "INSERT INTO job_listings" in c.kwargs.get("sql", ""))
    assert "location" in insert_call.kwargs["sql"]
    assert "posted_at" in insert_call.kwargs["sql"]
    assert param_value(insert_call.kwargs["parameters"], "location") == {"stringValue": "Remote — Europe"}
    # Data API TIMESTAMP format, not posted_at.isoformat() — see
    # aurora_client.py's _format_timestamp doc comment for why the two
    # differ (a real DatabaseErrorException hit in production otherwise).
    assert param_value(insert_call.kwargs["parameters"], "posted_at") == {"stringValue": _format_timestamp(posted_at)}
    assert param_value(insert_call.kwargs["parameters"], "posted_at") == {"stringValue": "2026-01-05 00:00:00.000"}

    posted_at_parameter = next(p for p in insert_call.kwargs["parameters"] if p["name"] == "posted_at")
    assert posted_at_parameter["typeHint"] == "TIMESTAMP"


@pytest.mark.asyncio
async def test_insert_job_listing_allows_missing_location_and_posted_at() -> None:
    fake = FakeRdsDataClient()
    fake.when(
        "execute_statement",
        lambda kwargs: formatted_records([{"id": "company-1"}]) if "INSERT INTO companies" in kwargs["sql"] else {"numberOfRecordsUpdated": 1},
    )
    write_set = DbWriteSet(
        idempotency_key="task-1",
        source_task_id="task-1",
        chat_id="42",
        writes=[JobListingWrite(role_id="backend", company_name="Acme", title="Senior SWE")],
    )

    async with _client(fake).transaction() as transaction_id:
        await domain_repo.apply_write_set(_client(fake), transaction_id, write_set)

    insert_call = next(c for c in fake.calls if "INSERT INTO job_listings" in c.kwargs.get("sql", ""))
    assert param_value(insert_call.kwargs["parameters"], "location") == {"isNull": True}
    assert param_value(insert_call.kwargs["parameters"], "posted_at") == {"isNull": True}


@pytest.mark.asyncio
async def test_list_job_listings_selects_and_hydrates_location_and_posted_at() -> None:
    fake = FakeRdsDataClient()

    def execute_statement(kwargs: dict) -> dict:
        if "JOIN contacts" in kwargs["sql"]:
            return formatted_records([])
        return formatted_records(
            [
                {
                    "id": "listing-1",
                    "role_id": "backend",
                    "company_name": "Acme",
                    "title": "Senior SWE",
                    "url": None,
                    "location": "Berlin",
                    "posted_at": "2026-01-05T00:00:00+00:00",
                    "summary": [],
                    "status": "pending",
                }
            ]
        )

    fake.when("execute_statement", execute_statement)

    listings = await domain_repo.list_job_listings(_client(fake), "backend")

    select_call = fake.calls[0]
    assert "jl.location" in select_call.kwargs["sql"]
    assert "jl.posted_at" in select_call.kwargs["sql"]
    assert listings[0].location == "Berlin"
    assert listings[0].posted_at is not None
