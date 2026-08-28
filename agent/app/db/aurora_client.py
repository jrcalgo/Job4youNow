"""Thin wrapper over boto3's `rds-data` client (Aurora Data API) — the Python
mirror of telegram/src/db/client.mjs. Every SQL call in this app goes
through `exec` / `exec_batch` / `transaction` here, for the same two reasons
the Node client centralizes them:

1. `formatRecordsAs="JSON"` still needs its `formattedRecords` string parsed
   on every call.
2. Every call needs the auto-pause/resume retry below — centralizing it
   means no call site can forget it.

boto3 is synchronous; every call is pushed through `asyncio.to_thread` so it
never blocks the event loop the FastAPI app and workers share. This is
simpler than adding an async AWS SDK dependency for what is, at this
traffic level, an occasional blocking call.
"""
from __future__ import annotations

import asyncio
import json
import random
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, AsyncIterator

import boto3
from botocore.exceptions import ClientError

from agent.app.config import Settings
from agent.app.logging import get_logger

log = get_logger("db.aurora_client")

# Aurora Serverless v2 with min ACU 0 auto-pauses after inactivity. The FIRST
# Data API call after a pause throws DatabaseResumingException; a scale-up
# mid-write can throw DatabaseUnavailableException. Both are transient and
# worth retrying — see telegram/src/lib/retry.mjs, which this mirrors.
_RETRYABLE_ERROR_CODES = {"DatabaseResumingException", "DatabaseUnavailableException"}
_RESUME_DELAY_SECONDS = 3.0


def _error_code(exc: BaseException) -> str | None:
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return None
    return response.get("Error", {}).get("Code")


async def _with_aurora_retry(call: Any, *, max_attempts: int = 8) -> Any:
    for attempt in range(1, max_attempts + 1):
        try:
            return await asyncio.to_thread(call)
        except ClientError as exc:
            code = _error_code(exc)
            if code not in _RETRYABLE_ERROR_CODES or attempt == max_attempts:
                raise
            base_delay = _RESUME_DELAY_SECONDS if code == "DatabaseResumingException" else min(10.0, 2 ** (attempt - 1))
            delay = base_delay * (0.75 + random.random() * 0.5)
            log.warning(
                "aurora data api: retrying after transient error",
                extra={"context": {"attempt": attempt, "delaySeconds": round(delay, 2), "errorCode": code}},
            )
            await asyncio.sleep(delay)


def _format_timestamp(value: datetime) -> str:
    """The Data API's `TIMESTAMP` typeHint only accepts `YYYY-MM-DD
    HH:MM:SS[.FFF]` — a space separator, millisecond precision, NO
    timezone suffix (see
    https://docs.aws.amazon.com/rdsdataservice/latest/APIReference/API_SqlParameter.html).
    `datetime.isoformat()` produces `...THH:MM:SS.ffffff+00:00` instead —
    the `T` and the offset are both "invalid characters" as far as the Data
    API's parser is concerned, which fails the whole call with
    `DatabaseErrorException: Parse Error for TimeStamp`. Every `agent_tasks`/
    `job_listings` column this ever binds against is TIMESTAMPTZ, and Aurora
    PostgreSQL's Data API always returns those in UTC, so an aware datetime
    is normalized to UTC before formatting; a naive one is assumed to
    already be UTC (this codebase only ever constructs naive datetimes, if
    any, as UTC — see models/tasks.py's `datetime.now(timezone.utc)`)."""
    aware = value.astimezone(timezone.utc) if value.tzinfo is not None else value
    return aware.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def to_param(name: str, value: Any) -> dict[str, Any]:
    """Convert one Python value into an RDS Data API SqlParameter. Mirrors
    telegram/src/db/client.mjs's toParam byte-for-byte in intent, so both
    services encode the same Python/JS types identically against the same
    schema (e.g. dict/list -> JSON-typed string, paired with an explicit
    `::jsonb` cast in the SQL text — typeHint alone is not a substitute)."""
    if value is None:
        return {"name": name, "value": {"isNull": True}}
    if isinstance(value, bool):
        return {"name": name, "value": {"booleanValue": value}}
    if isinstance(value, int):
        return {"name": name, "value": {"longValue": value}}
    if isinstance(value, float):
        return {"name": name, "value": {"doubleValue": value}}
    if isinstance(value, datetime):
        return {"name": name, "value": {"stringValue": _format_timestamp(value)}, "typeHint": "TIMESTAMP"}
    if isinstance(value, (list, dict)):
        return {"name": name, "value": {"stringValue": json.dumps(value)}, "typeHint": "JSON"}
    return {"name": name, "value": {"stringValue": str(value)}}


def _params_from(params: dict[str, Any] | None) -> list[dict[str, Any]]:
    return [to_param(key, value) for key, value in (params or {}).items()]


def _coerce_json_strings(value: Any) -> Any:
    """The Data API's `formatRecordsAs="JSON"` mode returns json/jsonb
    COLUMN values as their own escaped JSON string, not a nested
    object/array — `columnMetadata` (which would otherwise reveal a
    column's real type) is blank in this mode, so the Data API can't
    return it any other way. See
    https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/data-api-json.html
    and telegram/src/db/client.mjs's `coerceJsonStrings` (the Node mirror
    of this). Hit for real: `AgentTask.model_validate(row)` raised
    `payload  Input should be a valid dictionary or object` the first time
    a predictive Telegram request actually reached task_repo.create_task,
    because `row["payload"]` was still the raw jsonb string, never parsed.
    Every repo function here expects a json/jsonb column already parsed
    (most go straight into a Pydantic model_validate), so this runs once,
    here, on every row rather than leaving each call site to remember.
    Only strings that actually look like a JSON object/array AND parse
    cleanly are touched — a plain string that merely starts with '{' but
    isn't valid JSON passes through unchanged."""
    if isinstance(value, str):
        stripped = value.strip()
        if stripped[:1] in ("{", "["):
            try:
                return _coerce_json_strings(json.loads(stripped))
            except (json.JSONDecodeError, ValueError):
                return value
        return value
    if isinstance(value, list):
        return [_coerce_json_strings(v) for v in value]
    if isinstance(value, dict):
        return {key: _coerce_json_strings(v) for key, v in value.items()}
    return value


@dataclass
class ExecResult:
    rows: list[dict[str, Any]] = field(default_factory=list)
    records_updated: int = 0


class AuroraClient:
    """Construct via `build_aurora_client(settings)` in normal code. Tests
    construct this directly with a fake object implementing the same four
    boto3 `rds-data` methods used below — see
    agent/app/tests/helpers/fake_rds_client.py.
    """

    def __init__(self, rds_data: Any, *, resource_arn: str, secret_arn: str, database: str) -> None:
        self._client = rds_data
        self._common = {"resourceArn": resource_arn, "secretArn": secret_arn, "database": database}

    async def exec(
        self,
        sql: str,
        params: dict[str, Any] | None = None,
        *,
        transaction_id: str | None = None,
    ) -> ExecResult:
        kwargs: dict[str, Any] = {
            **self._common,
            "sql": sql,
            "parameters": _params_from(params),
            "formatRecordsAs": "JSON",
        }
        if transaction_id:
            kwargs["transactionId"] = transaction_id

        def call() -> ExecResult:
            response = self._client.execute_statement(**kwargs)
            formatted = response.get("formattedRecords")
            if formatted:
                rows = [_coerce_json_strings(row) for row in json.loads(formatted)]
                return ExecResult(rows=rows)
            return ExecResult(records_updated=response.get("numberOfRecordsUpdated", 0))

        return await _with_aurora_retry(call)

    async def exec_batch(
        self,
        sql: str,
        param_sets: list[dict[str, Any]],
        *,
        transaction_id: str | None = None,
    ) -> None:
        if not param_sets:
            return
        kwargs: dict[str, Any] = {
            **self._common,
            "sql": sql,
            "parameterSets": [_params_from(p) for p in param_sets],
        }
        if transaction_id:
            kwargs["transactionId"] = transaction_id
        await _with_aurora_retry(lambda: self._client.batch_execute_statement(**kwargs))

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[str]:
        """Run a block of `exec`/`exec_batch` calls atomically. Commits on a
        clean exit, rolls back on any exception (including one raised
        inside the `with` block), and re-raises so the caller's error
        handling still runs. Every call made inside the block MUST pass
        `transaction_id=` through, or it runs outside the transaction."""
        begun = await _with_aurora_retry(lambda: self._client.begin_transaction(**self._common))
        transaction_id = begun["transactionId"]
        try:
            yield transaction_id
        except Exception:
            try:
                await asyncio.to_thread(self._client.rollback_transaction, **self._common, transactionId=transaction_id)
            except Exception as rollback_error:  # noqa: BLE001 - logging, not swallowing the original error
                log.error("transaction rollback also failed", extra={"context": {"error": str(rollback_error)}})
            raise
        else:
            await _with_aurora_retry(
                lambda: self._client.commit_transaction(**self._common, transactionId=transaction_id)
            )


def build_aurora_client(settings: Settings) -> AuroraClient:
    rds_data = boto3.client("rds-data", region_name=settings.aws_region)
    return AuroraClient(
        rds_data=rds_data,
        resource_arn=settings.aurora_resource_arn,
        secret_arn=settings.aurora_secret_arn,
        database=settings.aurora_database,
    )
