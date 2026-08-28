"""Ephemeral, per-chat progress through the Telegram model-config wizard —
a thin wrapper around the existing TtlCache (see cache/ttl_cache.py), not a
new storage mechanism. Never persisted to Aurora: losing this on a restart
mid-wizard is fine, the user just restarts the flow with the `MODELS`
command. A ~15 minute TTL is generous for a human to click through a few
screens without expiring mid-flow, short enough not to accumulate stale
state indefinitely.
"""
from __future__ import annotations

from agent.app.cache.ttl_cache import TtlCache
from agent.app.models.model_catalog import ModelWizardState

_NAMESPACE = "model_wizard"
_TTL_SECONDS = 900.0


class ModelWizardCache:
    def __init__(self, cache: TtlCache) -> None:
        self._cache = cache

    def _key(self, chat_id: str) -> str:
        return f"{_NAMESPACE}:{chat_id}"

    def get(self, chat_id: str) -> ModelWizardState | None:
        return self._cache.get(self._key(chat_id))

    def set(self, chat_id: str, state: ModelWizardState) -> None:
        self._cache.set(self._key(chat_id), state, ttl_seconds=_TTL_SECONDS)

    def clear(self, chat_id: str) -> None:
        self._cache.invalidate(self._key(chat_id))
