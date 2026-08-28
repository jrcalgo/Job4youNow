"""S3 + local-disk artifact storage — the only module that writes artifact
bytes for either destination. Two S3 buckets exist as separate `ArtifactStore`
instances (job-artifacts, private-user-artifacts — see main.py's
AppServices); private content additionally always goes through
`LocalPrivateBackupStore`. One shared checksum helper (`sha256_bytes`) so an
S3 upload and its local backup always agree on what "the same bytes" hashes
to — never recompute a checksum independently in a tool.

This app only ever WRITES artifacts. Reading one back for Telegram delivery
is the (thin) adapter's job, via its own cache — see
telegram/src/artifacts/store.mjs.
"""
from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from pathlib import Path

import boto3

from agent.app.models.artifacts import ArtifactBucket, ArtifactLocation
from agent.app.tools.safe_path import safe_join

_s3_client_singleton = None


def _s3_client():
    global _s3_client_singleton
    if _s3_client_singleton is None:
        _s3_client_singleton = boto3.client("s3")
    return _s3_client_singleton


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


@dataclass
class LocalBackupReceipt:
    path: str
    checksum_sha256: str
    byte_size: int


class ArtifactStore:
    """One instance per S3 bucket. `bucket_kind` is stamped onto every
    `ArtifactLocation` this store produces, so a consumer can tell which
    bucket a pointer refers to without threading a second argument through
    every call site."""

    def __init__(self, *, bucket_kind: ArtifactBucket, bucket_name: str, prefix: str) -> None:
        self.bucket_kind = bucket_kind
        self._bucket_name = bucket_name
        self._prefix = prefix

    async def put_bytes(self, key: str, content: bytes) -> ArtifactLocation:
        checksum = sha256_bytes(content)
        full_key = f"{self._prefix}{key}"

        def put() -> None:
            _s3_client().put_object(
                Bucket=self._bucket_name,
                Key=full_key,
                Body=content,
                Metadata={"sha256": checksum},
            )

        await asyncio.to_thread(put)
        # `key` (bare, NOT `full_key`) — the Telegram adapter's
        # store.mjs::getArtifactPathFromBucket applies ITS OWN prefix
        # (config.prefix()) when it later fetches this object, exactly the
        # same way it already does for job_artifacts keys (which are always
        # bare there too — see artifactKey()). Returning `full_key` here
        # bakes the prefix in TWICE — hit for real as a live "The specified
        # key does not exist" S3 error, because the adapter fetched
        # `{prefix}{prefix}{key}`, which was never actually written.
        return ArtifactLocation(bucket=self.bucket_kind, key=key, checksum_sha256=checksum, byte_size=len(content))


class LocalPrivateBackupStore:
    """Durable local copy of every private artifact this app writes,
    independent of S3 reachability or IAM policy correctness. Writes
    atomically (temp file + rename) so a crash mid-write never leaves a
    half-written file at the final path."""

    def __init__(self, root: Path) -> None:
        self._root = root

    async def write_bytes(self, relative_key: str, content: bytes) -> LocalBackupReceipt:
        def write() -> LocalBackupReceipt:
            path = safe_join(self._root, relative_key)
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = path.with_name(path.name + ".tmp")
            tmp_path.write_bytes(content)
            tmp_path.replace(path)
            return LocalBackupReceipt(path=str(path), checksum_sha256=sha256_bytes(content), byte_size=len(content))

        return await asyncio.to_thread(write)


@dataclass
class PrivateStores:
    """Bundles the bucket and the local backup together — the only two
    things a private-output write must always go through as a pair (see
    tools/resume_tool.py and formatting/delivery.py, the only call sites).
    Bundled rather than passed as two separate constructor args everywhere,
    so a new private-output tool can't accidentally write to only one."""

    bucket: ArtifactStore
    local_backup: LocalPrivateBackupStore

    async def write_private_bytes(self, relative_key: str, content: bytes) -> ArtifactLocation:
        local = await self.local_backup.write_bytes(relative_key, content)
        location = await self.bucket.put_bytes(relative_key, content)
        return location.model_copy(update={"local_backup_path": local.path})
