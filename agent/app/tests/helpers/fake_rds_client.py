"""A fake standing in for boto3's `rds-data` client, wired in by constructing
`AuroraClient(rds_data=FakeRdsDataClient(), ...)` directly. Mirrors
telegram/test/helpers/fake-rds-client.mjs: records every call for assertions
on the SQL/params a repository generates, and answers with programmable
canned responses matched by method name — this exercises the real
exec/exec_batch/transaction code paths in aurora_client.py without touching
AWS.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable

_DEFAULT_RESPONSES: dict[str, Callable[[dict], Any]] = {
    "execute_statement": lambda kwargs: {"numberOfRecordsUpdated": 0},
    "batch_execute_statement": lambda kwargs: {"updateResults": []},
    "begin_transaction": lambda kwargs: {"transactionId": "fake-tx-1"},
    "commit_transaction": lambda kwargs: {},
    "rollback_transaction": lambda kwargs: {},
}


@dataclass
class RecordedCall:
    method: str
    kwargs: dict[str, Any]


@dataclass
class FakeRdsDataClient:
    calls: list[RecordedCall] = field(default_factory=list)
    _handlers: dict[str, list[Callable[[dict], Any]]] = field(
        default_factory=lambda: {name: [fn] for name, fn in _DEFAULT_RESPONSES.items()}
    )

    def when(self, method: str, respond: Callable[[dict], Any]) -> None:
        """Registers a handler that takes priority over the default above."""
        self._handlers[method].insert(0, respond)

    def _call(self, method: str, **kwargs: Any) -> Any:
        self.calls.append(RecordedCall(method=method, kwargs=kwargs))
        return self._handlers[method][0](kwargs)

    def execute_statement(self, **kwargs: Any) -> Any:
        return self._call("execute_statement", **kwargs)

    def batch_execute_statement(self, **kwargs: Any) -> Any:
        return self._call("batch_execute_statement", **kwargs)

    def begin_transaction(self, **kwargs: Any) -> Any:
        return self._call("begin_transaction", **kwargs)

    def commit_transaction(self, **kwargs: Any) -> Any:
        return self._call("commit_transaction", **kwargs)

    def rollback_transaction(self, **kwargs: Any) -> Any:
        return self._call("rollback_transaction", **kwargs)


def formatted_records(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Build an execute_statement response carrying `formattedRecords` the
    way `formatRecordsAs="JSON"` really does, given plain row dicts."""
    return {"formattedRecords": json.dumps(rows)}


def param_value(parameters: list[dict[str, Any]], name: str) -> Any:
    for parameter in parameters:
        if parameter["name"] == name:
            return parameter["value"]
    return None
