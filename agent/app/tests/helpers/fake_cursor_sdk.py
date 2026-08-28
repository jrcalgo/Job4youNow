"""Fakes for cursor_sdk's AsyncClient/AsyncAgent, wired in via monkeypatching
the names imported into agent.app.tools.cursor_sdk_tool (see
tests/test_cursor_sdk_tool.py). Lets CursorSdkTool's lifecycle/error-handling
code run for real without spawning an actual `cursor-sdk-bridge` subprocess.
"""
from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any


@dataclass
class FakeRunResult:
    id: str = "run-fake-1"
    agent_id: str = "agent-fake-1"
    status: str = "finished"
    result: str = "fake response text"
    model_id: str = "composer-2.5"

    @property
    def model(self) -> SimpleNamespace | None:
        return SimpleNamespace(id=self.model_id) if self.model_id else None


class FakeAsyncRun:
    def __init__(self, run_result: FakeRunResult) -> None:
        self.run_id = run_result.id
        self._run_result = run_result

    async def wait(self) -> FakeRunResult:
        return self._run_result


class FakeAsyncAgent:
    """`outcome` is either a FakeRunResult (send() succeeds) or an exception
    instance to raise from `create()` (simulates a startup failure, e.g.
    CursorAgentError)."""

    #: Records the kwargs of the most recent `create()` call (in particular
    #: `model=`) so tests can assert on model-resolution behavior without
    #: caring about the run outcome itself.
    last_create_kwargs: dict[str, Any] = {}

    def __init__(self, outcome: Any) -> None:
        self.agent_id = "agent-fake-1"
        self._outcome = outcome

    @classmethod
    async def create(cls, *, client: "FakeAsyncClient", **kwargs: Any) -> "FakeAsyncAgent":
        cls.last_create_kwargs = kwargs
        if isinstance(client.outcome, BaseException):
            raise client.outcome
        return cls(client.outcome)

    async def send(self, _prompt: str) -> FakeAsyncRun:
        return FakeAsyncRun(self._outcome)

    async def __aenter__(self) -> "FakeAsyncAgent":
        return self

    async def __aexit__(self, *_exc: Any) -> bool:
        return False


class FakeAsyncClient:
    """Configure with `FakeAsyncClient.configure(outcome)` before calling
    code that does `await AsyncClient.launch_bridge(...)` — `launch_bridge`
    is a classmethod in the real SDK too, so it can't take per-call
    constructor args from the caller under test."""

    _next_outcome: Any = None

    def __init__(self, outcome: Any) -> None:
        self.outcome = outcome

    @classmethod
    def configure(cls, outcome: Any) -> None:
        cls._next_outcome = outcome

    @classmethod
    async def launch_bridge(cls, **_: Any) -> "FakeAsyncClient":
        return cls(cls._next_outcome)

    async def __aenter__(self) -> "FakeAsyncClient":
        return self

    async def __aexit__(self, *_exc: Any) -> bool:
        return False


class FakeAsyncCursorModels:
    """Fakes `AsyncCursor.models` — configure with
    `FakeAsyncCursorModels.configure(outcome)` before calling code that does
    `await AsyncCursor.models.list(...)` (see tools/model_catalog.py).
    `outcome` is either a list of SDKModel-shaped objects (list() succeeds)
    or an exception instance to raise (simulates a CursorAgentError)."""

    _outcome: Any = ()
    calls: list[dict[str, Any]] = []

    @classmethod
    def configure(cls, outcome: Any) -> None:
        cls._outcome = outcome
        cls.calls = []

    @staticmethod
    async def list(*, client: Any, api_key: str | None = None) -> Any:
        FakeAsyncCursorModels.calls.append({"client": client, "api_key": api_key})
        if isinstance(FakeAsyncCursorModels._outcome, BaseException):
            raise FakeAsyncCursorModels._outcome
        return FakeAsyncCursorModels._outcome


class FakeAsyncCursor:
    models = FakeAsyncCursorModels
