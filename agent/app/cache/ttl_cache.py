"""A small TTL + LRU cache — deliberately hand-rolled instead of adding a
dependency (`cachetools`) for what is, in total, about thirty lines. Not
thread-safe by design: this app is single-process asyncio, and no method
here awaits mid-operation, so there is no interleaving to guard against.
"""
from __future__ import annotations

import time
from collections import OrderedDict
from typing import Any, Awaitable, Callable, TypeVar

from agent.app.cache.keys import CacheKey

T = TypeVar("T")


class TtlCache:
    def __init__(self, *, max_entries: int = 512) -> None:
        self._entries: OrderedDict[str, tuple[float, Any]] = OrderedDict()
        self._max_entries = max_entries

    def get(self, key: str) -> Any | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if expires_at < time.monotonic():
            del self._entries[key]
            return None
        self._entries.move_to_end(key)
        return value

    def set(self, key: str, value: Any, *, ttl_seconds: float) -> None:
        self._entries[key] = (time.monotonic() + ttl_seconds, value)
        self._entries.move_to_end(key)
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)

    def invalidate(self, key: str) -> None:
        self._entries.pop(key, None)

    def invalidate_namespace(self, namespace: str) -> None:
        prefix = f"{namespace}:"
        stale_keys = [key for key in self._entries if key == namespace or key.startswith(prefix)]
        for key in stale_keys:
            del self._entries[key]


async def get_or_load(cache: TtlCache, key: CacheKey, loader: Callable[[], Awaitable[T]], *, ttl_seconds: float) -> T:
    """The one read-through helper every deterministic read service call
    should use — see routing/deterministic.py. Centralizing this means no
    service reinvents "check cache, else load and store"."""
    cache_key = key.as_string()
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    value = await loader()
    cache.set(cache_key, value, ttl_seconds=ttl_seconds)
    return value
