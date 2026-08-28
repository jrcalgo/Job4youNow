"""Typed cache keys. One function per cached read, instead of ad hoc f-strings
scattered across call sites — see cache/invalidation.py, which needs to
construct the exact same keys a reader used in order to invalidate them.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CacheKey:
    namespace: str
    parts: tuple[str, ...] = ()

    def as_string(self) -> str:
        return ":".join((self.namespace, *self.parts))


BACKLOG_NAMESPACE = "backlog"
JOB_LISTINGS_NAMESPACE = "job_listings"


def backlog() -> CacheKey:
    return CacheKey(BACKLOG_NAMESPACE)


def job_listings_by_role(role_id: str) -> CacheKey:
    return CacheKey(JOB_LISTINGS_NAMESPACE, (role_id,))
