"""Live model listing via the Cursor SDK — a lightweight metadata call, not
a full agent run, so this deliberately does NOT go through
workers/concurrency.py's `cursor_runs` semaphore (that governs actual agent
runs only). Converts `cursor_sdk`'s own dataclasses to this app's Pydantic
models at the boundary — see models/model_catalog.py.
"""
from __future__ import annotations

from cursor_sdk import AsyncClient, AsyncCursor, CursorAgentError

from agent.app.config import Settings
from agent.app.logging import get_logger
from agent.app.models.model_catalog import ModelCatalogEntry

log = get_logger("tools.model_catalog")


class ModelCatalogError(RuntimeError):
    """Raised when the live model list can't be fetched — routing/model_config.py
    turns this into a normal (not exceptional) presenter error, never a 500."""


class ModelCatalogService:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def list_models(self) -> list[ModelCatalogEntry]:
        try:
            async with await AsyncClient.launch_bridge(workspace=str(self._settings.career_ops_dir)) as client:
                sdk_models = await AsyncCursor.models.list(client=client, api_key=self._settings.cursor_api_key)
        except CursorAgentError as error:
            log.warning("model catalog fetch failed", extra={"context": {"error": error.message}})
            raise ModelCatalogError(error.message) from error
        return [ModelCatalogEntry.from_sdk_model(m) for m in sdk_models]
