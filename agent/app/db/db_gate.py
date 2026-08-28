"""The only path a domain write can take. Every predictive task result that
proposes writing job listings, companies, contacts, private-artifact
provenance (see models/artifacts.py), or a schedule goes through
`DbGate.apply()` — never through domain_repo functions directly from a
graph node or tool.

Note on scope: this module does not create Telegram outbox messages or
call task_repo.save_task_result. Those happen once, after the graph
finishes, in workers/task_worker.py (via agent/app/delivery.py) — the one
place that owns "what the user sees". This module only owns "what got
written to Aurora", so a task never ends up with two Telegram messages or
a result recorded twice.
"""
from __future__ import annotations

from agent.app.db import domain_repo
from agent.app.db.aurora_client import AuroraClient
from agent.app.cache.invalidation import CacheInvalidator
from agent.app.models.db_writes import DbWriteReceipt, DbWriteSet


class DbGate:
    def __init__(self, client: AuroraClient, cache_invalidator: CacheInvalidator) -> None:
        self._client = client
        self._cache_invalidator = cache_invalidator

    async def apply(self, write_set: DbWriteSet) -> DbWriteReceipt:
        if not write_set.writes:
            return DbWriteReceipt.noop(write_set.idempotency_key)

        existing = await self._existing_receipt(write_set.idempotency_key)
        if existing:
            return existing

        affected_role_ids = sorted({write.role_id for write in write_set.writes if hasattr(write, "role_id")})

        async with self._client.transaction() as transaction_id:
            counts = await domain_repo.apply_write_set(self._client, transaction_id, write_set)
            await self._client.exec(
                """
                INSERT INTO db_write_receipts (idempotency_key, counts, affected_role_ids)
                VALUES (:idempotency_key, :counts::jsonb, :affected_role_ids::jsonb)
                """,
                {
                    "idempotency_key": write_set.idempotency_key,
                    "counts": counts,
                    "affected_role_ids": affected_role_ids,
                },
                transaction_id=transaction_id,
            )

        receipt = DbWriteReceipt(
            idempotency_key=write_set.idempotency_key,
            applied=True,
            counts=counts,
            affected_role_ids=affected_role_ids,
        )
        await self._cache_invalidator.invalidate_for_receipt(receipt)
        return receipt

    async def _existing_receipt(self, idempotency_key: str) -> DbWriteReceipt | None:
        result = await self._client.exec(
            "SELECT counts, affected_role_ids FROM db_write_receipts WHERE idempotency_key = :idempotency_key",
            {"idempotency_key": idempotency_key},
        )
        if not result.rows:
            return None
        row = result.rows[0]
        return DbWriteReceipt(
            idempotency_key=idempotency_key,
            applied=True,
            counts=row["counts"],
            affected_role_ids=row["affected_role_ids"],
        )
