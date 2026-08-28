"""Ephemeral per-chat backlog browser offset."""
from __future__ import annotations

from agent.app.cache.ttl_cache import TtlCache
from agent.app.models.backlog import BacklogBrowserState

_NAMESPACE = "backlog_browser"
_TTL_SECONDS = 900.0


class BacklogBrowserCache:
    def __init__(self, cache: TtlCache) -> None:
        self._cache = cache

    def _key(self, chat_id: str) -> str:
        return f"{_NAMESPACE}:{chat_id}"

    def get(self, chat_id: str) -> BacklogBrowserState | None:
        return self._cache.get(self._key(chat_id))

    def set(self, chat_id: str, state: BacklogBrowserState) -> None:
        self._cache.set(self._key(chat_id), state, ttl_seconds=_TTL_SECONDS)

    def clear(self, chat_id: str) -> None:
        self._cache.invalidate(self._key(chat_id))
