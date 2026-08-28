"""Exercises AuroraClient directly against FakeRdsDataClient: parameter
encoding, JSON record parsing, and the auto-pause retry — the three things
db/aurora_client.py exists to centralize so no repository has to.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from botocore.exceptions import ClientError

from agent.app.db import aurora_client as aurora_client_module
from agent.app.db.aurora_client import AuroraClient, _coerce_json_strings, to_param
from agent.app.tests.helpers.fake_rds_client import FakeRdsDataClient, formatted_records, param_value


@pytest.fixture(autouse=True)
def _fast_retry_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    """Retry delays default to real seconds (see aurora_client.py's header
    comment on why) — pointless to actually wait that long in a unit test."""
    monkeypatch.setattr(aurora_client_module, "_RESUME_DELAY_SECONDS", 0.001)


def _client(fake: FakeRdsDataClient) -> AuroraClient:
    return AuroraClient(fake, resource_arn="arn:aurora", secret_arn="arn:secret", database="job4younow")


def test_to_param_covers_every_supported_type() -> None:
    assert to_param("a", None) == {"name": "a", "value": {"isNull": True}}
    assert to_param("a", True) == {"name": "a", "value": {"booleanValue": True}}
    assert to_param("a", 5) == {"name": "a", "value": {"longValue": 5}}
    assert to_param("a", 5.5) == {"name": "a", "value": {"doubleValue": 5.5}}
    assert to_param("a", {"x": 1})["typeHint"] == "JSON"
    assert to_param("a", "hello") == {"name": "a", "value": {"stringValue": "hello"}}


def test_to_param_formats_a_utc_datetime_the_way_the_data_api_actually_requires() -> None:
    """Regression test for a real DatabaseErrorException ("Parse Error for
    TimeStamp: input contains invalid characters") hit in production the
    first time a predictive Telegram request reached task_repo.create_task.
    The Data API's TIMESTAMP typeHint only accepts `YYYY-MM-DD
    HH:MM:SS[.FFF]` — no `T`, no timezone offset, millisecond (not
    microsecond) precision — see aurora_client.py's _format_timestamp."""
    value = datetime(2026, 8, 26, 6, 54, 10, 587664, tzinfo=timezone.utc)
    param = to_param("created_at", value)
    assert param == {"name": "created_at", "value": {"stringValue": "2026-08-26 06:54:10.587"}, "typeHint": "TIMESTAMP"}
    assert "T" not in param["value"]["stringValue"]
    assert "+" not in param["value"]["stringValue"]


def test_to_param_normalizes_a_non_utc_aware_datetime_to_utc_before_formatting() -> None:
    tz_plus5 = timezone(timedelta(hours=5))
    value = datetime(2026, 8, 26, 11, 54, 10, 500000, tzinfo=tz_plus5)  # == 06:54:10.5 UTC
    param = to_param("created_at", value)
    assert param["value"]["stringValue"] == "2026-08-26 06:54:10.500"


def test_to_param_formats_a_naive_datetime_as_is() -> None:
    value = datetime(2026, 8, 26, 6, 54, 10, 1000)
    param = to_param("created_at", value)
    assert param["value"]["stringValue"] == "2026-08-26 06:54:10.001"


@pytest.mark.asyncio
async def test_exec_parses_formatted_records_into_rows() -> None:
    fake = FakeRdsDataClient()
    fake.when("execute_statement", lambda kwargs: formatted_records([{"id": "1", "name": "Acme"}]))

    result = await _client(fake).exec("SELECT * FROM companies")

    assert result.rows == [{"id": "1", "name": "Acme"}]


def test_coerce_json_strings_parses_a_json_object_or_array_string_recursively() -> None:
    assert _coerce_json_strings('{"a": 1}') == {"a": 1}
    assert _coerce_json_strings('[1, 2]') == [1, 2]
    # Nested one level deeper (a jsonb column whose own value contains
    # ANOTHER json-encoded string, e.g. a double round-trip) still resolves.
    assert _coerce_json_strings({"payload": '{"kind": "x"}'}) == {"payload": {"kind": "x"}}


def test_coerce_json_strings_leaves_non_json_looking_strings_alone() -> None:
    assert _coerce_json_strings("hello") == "hello"
    assert _coerce_json_strings("{not actually valid json") == "{not actually valid json"
    assert _coerce_json_strings(5) == 5
    assert _coerce_json_strings(None) is None
    assert _coerce_json_strings(True) is True


@pytest.mark.asyncio
async def test_exec_parses_a_jsonb_column_that_comes_back_as_its_own_escaped_json_string() -> None:
    """Regression test for a real ValidationError ("payload  Input should
    be a valid dictionary or object") hit in production the first time a
    predictive Telegram request reached task_repo.create_task — see
    _coerce_json_strings's doc comment. Hand-builds `formattedRecords` the
    way the REAL Data API actually returns a jsonb column value (its own
    escaped JSON string) — the fake_rds_client helper's `formatted_records()`
    doesn't model this quirk, which is exactly how this bug went unnoticed
    across the whole existing test suite."""
    fake = FakeRdsDataClient()
    raw = json.dumps([{"id": "task-1", "payload": json.dumps({"kind": "research_company", "query": "hi"})}])
    fake.when("execute_statement", lambda kwargs: {"formattedRecords": raw})

    result = await _client(fake).exec("SELECT * FROM agent_tasks")

    assert result.rows[0]["payload"] == {"kind": "research_company", "query": "hi"}


@pytest.mark.asyncio
async def test_exec_sends_named_parameters() -> None:
    fake = FakeRdsDataClient()
    await _client(fake).exec("SELECT * FROM companies WHERE name = :name", {"name": "Acme"})

    sent = fake.calls[0].kwargs
    assert param_value(sent["parameters"], "name") == {"stringValue": "Acme"}


@pytest.mark.asyncio
async def test_exec_batch_is_a_noop_for_empty_param_sets() -> None:
    fake = FakeRdsDataClient()
    await _client(fake).exec_batch("INSERT INTO x VALUES (:a)", [])
    assert fake.calls == []


@pytest.mark.asyncio
async def test_transaction_commits_on_success() -> None:
    fake = FakeRdsDataClient()
    client = _client(fake)

    async with client.transaction() as transaction_id:
        assert transaction_id == "fake-tx-1"
        await client.exec("UPDATE x SET y = 1", transaction_id=transaction_id)

    method_sequence = [call.method for call in fake.calls]
    assert method_sequence == ["begin_transaction", "execute_statement", "commit_transaction"]


@pytest.mark.asyncio
async def test_transaction_rolls_back_and_reraises_on_error() -> None:
    fake = FakeRdsDataClient()
    client = _client(fake)

    with pytest.raises(RuntimeError, match="boom"):
        async with client.transaction():
            raise RuntimeError("boom")

    method_sequence = [call.method for call in fake.calls]
    assert method_sequence == ["begin_transaction", "rollback_transaction"]


def _database_resuming_error() -> ClientError:
    return ClientError({"Error": {"Code": "DatabaseResumingException", "Message": "resuming"}}, "ExecuteStatement")


@pytest.mark.asyncio
async def test_exec_retries_on_database_resuming_then_succeeds() -> None:
    fake = FakeRdsDataClient()
    attempts = {"count": 0}

    def flaky_then_ok(kwargs: dict) -> dict:
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise _database_resuming_error()
        return {"numberOfRecordsUpdated": 1}

    fake.when("execute_statement", flaky_then_ok)
    result = await _client(fake).exec("UPDATE x SET y = 1")

    assert attempts["count"] == 3
    assert result.records_updated == 1


@pytest.mark.asyncio
async def test_exec_does_not_retry_non_retryable_errors() -> None:
    fake = FakeRdsDataClient()

    def always_fail(kwargs: dict):
        raise ClientError({"Error": {"Code": "BadRequestException", "Message": "bad sql"}}, "ExecuteStatement")

    fake.when("execute_statement", always_fail)

    with pytest.raises(ClientError):
        await _client(fake).exec("NOT VALID SQL")
