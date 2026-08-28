"""Proves the cache's three load-bearing behaviors: a hit skips the loader,
a miss calls it exactly once, and invalidation actually forces a reload —
the last one is what db_gate.py's write path depends on to never serve
stale backlog counts after a write.
"""
from __future__ import annotations

import asyncio

import pytest

from agent.app.cache import keys
from agent.app.cache.invalidation import CacheInvalidator
from agent.app.cache.ttl_cache import TtlCache, get_or_load
from agent.app.models.db_writes import DbWriteReceipt


@pytest.mark.asyncio
async def test_get_or_load_calls_loader_once_on_repeated_hits() -> None:
    cache = TtlCache()
    call_count = 0

    async def loader() -> int:
        nonlocal call_count
        call_count += 1
        return 42

    first = await get_or_load(cache, keys.backlog(), loader, ttl_seconds=60)
    second = await get_or_load(cache, keys.backlog(), loader, ttl_seconds=60)

    assert first == second == 42
    assert call_count == 1


@pytest.mark.asyncio
async def test_get_or_load_reloads_after_ttl_expires() -> None:
    cache = TtlCache()
    call_count = 0

    async def loader() -> int:
        nonlocal call_count
        call_count += 1
        return call_count

    await get_or_load(cache, keys.backlog(), loader, ttl_seconds=0.01)
    await asyncio.sleep(0.02)
    second = await get_or_load(cache, keys.backlog(), loader, ttl_seconds=0.01)

    assert second == 2


def test_ttl_cache_evicts_least_recently_used_when_full() -> None:
    cache = TtlCache(max_entries=2)
    cache.set("a", 1, ttl_seconds=60)
    cache.set("b", 2, ttl_seconds=60)
    cache.get("a")  # touch "a" so "b" becomes the least-recently-used entry
    cache.set("c", 3, ttl_seconds=60)

    assert cache.get("a") == 1
    assert cache.get("b") is None
    assert cache.get("c") == 3


@pytest.mark.asyncio
async def test_write_receipt_invalidates_backlog_and_affected_role_keys() -> None:
    cache = TtlCache()
    cache.set(keys.backlog().as_string(), ["stale"], ttl_seconds=60)
    cache.set(keys.job_listings_by_role("backend").as_string(), ["stale"], ttl_seconds=60)
    cache.set(keys.job_listings_by_role("frontend").as_string(), ["untouched"], ttl_seconds=60)

    invalidator = CacheInvalidator(cache)
    receipt = DbWriteReceipt(idempotency_key="k", applied=True, counts={"job_listings": 1}, affected_role_ids=["backend"])
    await invalidator.invalidate_for_receipt(receipt)

    assert cache.get(keys.backlog().as_string()) is None
    assert cache.get(keys.job_listings_by_role("backend").as_string()) is None
    assert cache.get(keys.job_listings_by_role("frontend").as_string()) == ["untouched"]


@pytest.mark.asyncio
async def test_noop_receipt_does_not_invalidate_anything() -> None:
    cache = TtlCache()
    cache.set(keys.backlog().as_string(), ["fresh"], ttl_seconds=60)

    invalidator = CacheInvalidator(cache)
    await invalidator.invalidate_for_receipt(DbWriteReceipt.noop("k"))

    assert cache.get(keys.backlog().as_string()) == ["fresh"]
