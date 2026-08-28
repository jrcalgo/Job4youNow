"""Job listing / company / contact persistence — the only domain data this
app stores (see migrations/0001_agent_core.sql's header note). Two kinds of
callers:

- Read queries (`list_backlog_by_role`, `list_job_listings`) are called
  directly by routing/deterministic.py through a cached read service —
  no LLM, no transaction, no write.
- `apply_write_set` is called ONLY by db/db_gate.py, inside a transaction it
  owns. This module never opens its own transaction for a write.
"""
from __future__ import annotations

import uuid

from agent.app.db.aurora_client import AuroraClient
from agent.app.db import schedule_repo
from agent.app.models.artifacts import PrivateArtifactMetadata
from agent.app.models.db_writes import DbWriteSet, JobListingWrite
from agent.app.models.backlog import BacklogFilters, BacklogQueryResult
from agent.app.models.domain import ContactRow, JobListingRow, RoleBacklog
from agent.app.models.schedules import ScanSchedule


def _derive_work_mode(location: str | None) -> str:
    if not location:
        return "unknown"
    lower = location.lower()
    if "remote" in lower:
        return "remote"
    if "hybrid" in lower:
        return "hybrid"
    if "on-site" in lower or "onsite" in lower or "in-person" in lower or "in person" in lower:
        return "onsite"
    return "unknown"


_SORT_COLUMNS = {
    "retrieved_at": "COALESCE(jl.retrieved_at, jl.created_at)",
    "posted_at": "jl.posted_at",
    "created_at": "jl.created_at",
    "company_name": "c.name",
    "title": "jl.title",
    "applicant_count": "jl.applicant_count",
}


async def list_backlog_by_role(client: AuroraClient) -> list[RoleBacklog]:
    result = await client.exec(
        """
        SELECT role_id, count(*) AS pending_count
          FROM job_listings
         WHERE status = 'pending'
         GROUP BY role_id
         ORDER BY pending_count DESC
        """
    )
    return [RoleBacklog(role_id=row["role_id"], pending_count=int(row["pending_count"])) for row in result.rows]


async def list_job_listings(client: AuroraClient, role_id: str, *, limit: int = 20) -> list[JobListingRow]:
    result = await client.exec(
        """
        SELECT jl.id, jl.role_id, c.name AS company_name, jl.title, jl.url, jl.location, jl.posted_at, jl.summary, jl.status
          FROM job_listings jl
          JOIN companies c ON c.id = jl.company_id
         WHERE jl.role_id = :role_id AND jl.status = 'pending'
         ORDER BY jl.created_at DESC
         LIMIT :limit
        """,
        {"role_id": role_id, "limit": limit},
    )
    listings = [JobListingRow.model_validate(row) for row in result.rows]
    contacts_by_listing = await _contacts_for_listings(client, [listing.id for listing in listings])
    for listing in listings:
        listing.contacts = contacts_by_listing.get(listing.id, [])
    return listings


async def get_job_listing(client: AuroraClient, listing_id: str) -> JobListingRow | None:
    result = await client.exec(
        """
        SELECT jl.id, jl.role_id, c.name AS company_name, jl.title, jl.url, jl.location,
               jl.posted_at, COALESCE(jl.retrieved_at, jl.created_at) AS retrieved_at,
               jl.work_mode, jl.applicant_count, jl.summary, jl.status
          FROM job_listings jl
          JOIN companies c ON c.id = jl.company_id
         WHERE jl.id = :id
        """,
        {"id": listing_id},
    )
    if not result.rows:
        return None
    listing = JobListingRow.model_validate(result.rows[0])
    contacts_by_listing = await _contacts_for_listings(client, [listing.id])
    listing.contacts = contacts_by_listing.get(listing.id, [])
    return listing


async def update_listing_status(client: AuroraClient, listing_id: str, status: str) -> bool:
    result = await client.exec(
        "UPDATE job_listings SET status = :status WHERE id = :id AND status = 'pending'",
        {"id": listing_id, "status": status},
    )
    return result.records_updated > 0


async def query_job_listings(
    client: AuroraClient,
    filters: BacklogFilters,
    *,
    sort_key: str = "retrieved_at",
    sort_dir: str = "desc",
    offset: int = 0,
    limit: int = 1,
) -> BacklogQueryResult:
    conditions = ["jl.status = 'pending'"]
    params: dict = {"offset": offset, "limit": limit}

    if filters.role_ids:
        conditions.append("jl.role_id = ANY(:role_ids::text[])")
        params["role_ids"] = filters.role_ids
    if filters.company_names:
        conditions.append("c.name = ANY(:company_names::text[])")
        params["company_names"] = filters.company_names
    if filters.work_modes:
        conditions.append("jl.work_mode = ANY(:work_modes::text[])")
        params["work_modes"] = filters.work_modes
    if filters.posted_at_min:
        conditions.append("jl.posted_at >= :posted_at_min")
        params["posted_at_min"] = filters.posted_at_min
    if filters.posted_at_max:
        conditions.append("jl.posted_at <= :posted_at_max")
        params["posted_at_max"] = filters.posted_at_max
    if filters.retrieved_at_min:
        conditions.append("COALESCE(jl.retrieved_at, jl.created_at) >= :retrieved_at_min")
        params["retrieved_at_min"] = filters.retrieved_at_min
    if filters.retrieved_at_max:
        conditions.append("COALESCE(jl.retrieved_at, jl.created_at) <= :retrieved_at_max")
        params["retrieved_at_max"] = filters.retrieved_at_max
    if filters.applicant_count_min is not None:
        conditions.append("jl.applicant_count >= :applicant_count_min")
        params["applicant_count_min"] = filters.applicant_count_min
    if filters.applicant_count_max is not None:
        conditions.append("jl.applicant_count <= :applicant_count_max")
        params["applicant_count_max"] = filters.applicant_count_max

    where_sql = " AND ".join(conditions)
    sort_col = _SORT_COLUMNS.get(sort_key, _SORT_COLUMNS["retrieved_at"])
    direction = "DESC" if sort_dir.lower() == "desc" else "ASC"

    count_result = await client.exec(
        f"""
        SELECT count(*) AS total
          FROM job_listings jl
          JOIN companies c ON c.id = jl.company_id
         WHERE {where_sql}
        """,
        params,
    )
    total = int(count_result.rows[0]["total"]) if count_result.rows else 0

    result = await client.exec(
        f"""
        SELECT jl.id, jl.role_id, c.name AS company_name, jl.title, jl.url, jl.location,
               jl.posted_at, COALESCE(jl.retrieved_at, jl.created_at) AS retrieved_at,
               jl.work_mode, jl.applicant_count, jl.summary, jl.status
          FROM job_listings jl
          JOIN companies c ON c.id = jl.company_id
         WHERE {where_sql}
         ORDER BY {sort_col} {direction} NULLS LAST
         OFFSET :offset LIMIT :limit
        """,
        params,
    )
    listings = [JobListingRow.model_validate(row) for row in result.rows]
    contacts_by_listing = await _contacts_for_listings(client, [listing.id for listing in listings])
    for listing in listings:
        listing.contacts = contacts_by_listing.get(listing.id, [])
    return BacklogQueryResult(items=listings, total_count=total, offset=offset)


async def list_distinct_filter_options(client: AuroraClient) -> dict[str, list[str]]:
    roles = await client.exec(
        "SELECT DISTINCT role_id FROM job_listings WHERE status = 'pending' ORDER BY role_id"
    )
    companies = await client.exec(
        """
        SELECT DISTINCT c.name FROM job_listings jl
          JOIN companies c ON c.id = jl.company_id
         WHERE jl.status = 'pending'
         ORDER BY c.name
        """
    )
    return {
        "role_ids": [row["role_id"] for row in roles.rows],
        "company_names": [row["name"] for row in companies.rows],
        "work_modes": ["remote", "hybrid", "onsite", "unknown"],
    }


async def _contacts_for_listings(client: AuroraClient, listing_ids: list[str]) -> dict[str, list[ContactRow]]:
    if not listing_ids:
        return {}
    result = await client.exec(
        """
        SELECT jl.id AS listing_id, ct.name, ct.role_title, ct.email, ct.phone
          FROM job_listings jl
          JOIN contacts ct ON ct.company_id = jl.company_id
         WHERE jl.id = ANY(:listing_ids::text[])
        """,
        {"listing_ids": listing_ids},
    )
    by_listing: dict[str, list[ContactRow]] = {}
    for row in result.rows:
        by_listing.setdefault(row["listing_id"], []).append(ContactRow.model_validate(row))
    return by_listing


async def _upsert_company(client: AuroraClient, transaction_id: str, name: str) -> str:
    company_id = f"company-{uuid.uuid4().hex[:12]}"
    result = await client.exec(
        """
        INSERT INTO companies (id, name) VALUES (:id, :name)
        ON CONFLICT (name) DO UPDATE SET name = EXCLUDED.name
        RETURNING id
        """,
        {"id": company_id, "name": name},
        transaction_id=transaction_id,
    )
    return result.rows[0]["id"]


async def _insert_job_listing(
    client: AuroraClient,
    transaction_id: str,
    write: JobListingWrite,
    *,
    source_task_id: str,
) -> None:
    company_id = await _upsert_company(client, transaction_id, write.company_name)
    listing_id = f"listing-{uuid.uuid4().hex[:12]}"
    work_mode = _derive_work_mode(write.location)
    await client.exec(
        """
        INSERT INTO job_listings (
          id, role_id, company_id, title, url, location, posted_at, summary, source_task_id,
          work_mode, retrieved_at
        )
        VALUES (
          :id, :role_id, :company_id, :title, :url, :location, :posted_at, :summary::jsonb, :source_task_id,
          :work_mode, now()
        )
        """,
        {
            "id": listing_id,
            "role_id": write.role_id,
            "company_id": company_id,
            "title": write.title,
            "url": write.url,
            "location": write.location,
            "posted_at": write.posted_at,
            "summary": write.summary,
            "source_task_id": source_task_id,
            "work_mode": work_mode,
        },
        transaction_id=transaction_id,
    )
    if write.contacts:
        await client.exec_batch(
            """
            INSERT INTO contacts (id, company_id, name, role_title, email, phone)
            VALUES (:id, :company_id, :name, :role_title, :email, :phone)
            """,
            [
                {
                    "id": f"contact-{uuid.uuid4().hex[:12]}",
                    "company_id": company_id,
                    "name": contact.name,
                    "role_title": contact.role_title,
                    "email": contact.email,
                    "phone": contact.phone,
                }
                for contact in write.contacts
            ],
            transaction_id=transaction_id,
        )


async def insert_private_artifact(
    client: AuroraClient,
    metadata: PrivateArtifactMetadata,
    *,
    source_task_id: str | None,
    transaction_id: str | None = None,
) -> None:
    """The one place a `private_artifacts` row gets written — a pointer
    only (bucket/key/checksum/local path), never content. Called two ways:

    - Inside db/db_gate.py's transaction, for artifacts a TOOL already
      produced as part of its normal work (e.g. an augmented resume) —
      see this module's `apply_write_set`, via a `PrivateArtifactWrite`.
    - Standalone (no transaction), from agent/app/delivery.py, for a
      private TEXT response materialized at response-formatting time —
      there's no compound domain write to keep atomic with it, so the
      heavier DbWriteSet/db_gate path would add nothing here.

    Both paths persist the exact same `PrivateArtifactMetadata` shape the
    caller also attaches to a `UserFacingResponse` — see that model's
    docstring on why the id is generated client-side, not by this insert."""
    await client.exec(
        """
        INSERT INTO private_artifacts
          (id, chat_id, kind, bucket_name, object_key, local_backup_path, checksum_sha256, byte_size, source_task_id)
        VALUES
          (:id, :chat_id, :kind, :bucket_name, :object_key, :local_backup_path, :checksum_sha256, :byte_size, :source_task_id)
        """,
        {
            "id": metadata.id,
            "chat_id": metadata.chat_id,
            "kind": metadata.kind,
            "bucket_name": metadata.location.bucket.value,
            "object_key": metadata.location.key,
            "local_backup_path": metadata.location.local_backup_path,
            "checksum_sha256": metadata.location.checksum_sha256,
            "byte_size": metadata.location.byte_size,
            "source_task_id": source_task_id,
        },
        transaction_id=transaction_id,
    )


async def apply_write_set(client: AuroraClient, transaction_id: str, write_set: DbWriteSet) -> dict[str, int]:
    """Persist every write in `write_set.writes`, inside the caller's
    transaction. Returns per-kind counts for the DB gate's receipt — see
    db/db_gate.py, the only caller."""
    counts = {"job_listings": 0, "companies": 0, "contacts": 0, "private_artifacts": 0, "schedules": 0, "contacts_skipped": 0}

    for write in write_set.writes:
        if write.kind == "job_listing":
            await _insert_job_listing(client, transaction_id, write, source_task_id=write_set.source_task_id)
            counts["job_listings"] += 1
            counts["contacts"] += len(write.contacts)

        elif write.kind == "company":
            await _upsert_company(client, transaction_id, write.name)
            counts["companies"] += 1

        elif write.kind == "contact":
            if not write.company_name:
                counts["contacts_skipped"] += 1
                continue
            company_id = await _upsert_company(client, transaction_id, write.company_name)
            await client.exec(
                """
                INSERT INTO contacts (id, company_id, name, role_title, email, phone)
                VALUES (:id, :company_id, :name, :role_title, :email, :phone)
                """,
                {
                    "id": f"contact-{uuid.uuid4().hex[:12]}",
                    "company_id": company_id,
                    "name": write.name,
                    "role_title": write.role_title,
                    "email": write.email,
                    "phone": write.phone,
                },
                transaction_id=transaction_id,
            )
            counts["contacts"] += 1

        elif write.kind == "private_artifact":
            await insert_private_artifact(
                client, write.metadata, source_task_id=write_set.source_task_id, transaction_id=transaction_id
            )
            counts["private_artifacts"] += 1

        elif write.kind == "schedule":
            schedule = ScanSchedule(chat_id=write_set.chat_id, role_id=write.role_id, query=write.query, interval_seconds=write.interval_seconds)
            await schedule_repo.create_schedule(client, schedule, transaction_id=transaction_id)
            counts["schedules"] += 1

    return counts
