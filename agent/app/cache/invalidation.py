"""The one place that decides which cache keys a DB write invalidates.
db/db_gate.py calls this after every successful write — no other module
should call `TtlCache.invalidate*` directly, or a future new cached read
could silently go stale after writes that should have cleared it.
"""
from __future__ import annotations

from agent.app.cache import keys
from agent.app.cache.ttl_cache import TtlCache
from agent.app.models.db_writes import DbWriteReceipt


class CacheInvalidator:
    def __init__(self, cache: TtlCache) -> None:
        self._cache = cache

    async def invalidate_for_receipt(self, receipt: DbWriteReceipt) -> None:
        if not receipt.applied:
            return
        # Any new/changed listing changes backlog counts, regardless of role.
        self._cache.invalidate(keys.backlog().as_string())
        for role_id in receipt.affected_role_ids:
            self._cache.invalidate(keys.job_listings_by_role(role_id).as_string())
