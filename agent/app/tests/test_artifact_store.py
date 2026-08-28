"""Exercises ArtifactStore (S3) + LocalPrivateBackupStore (disk) directly:
checksum agreement between the two, atomic local writes, and path-escape
rejection — the properties tools/resume_tool.py and formatting/delivery.py
both depend on without re-verifying them themselves.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from agent.app.models.artifacts import ArtifactBucket
from agent.app.tools import artifact_store as artifact_store_module
from agent.app.tools.artifact_store import ArtifactStore, LocalPrivateBackupStore, PrivateStores, sha256_bytes
from agent.app.tools.safe_path import UnsafePathError, safe_join


class FakeS3Client:
    def __init__(self) -> None:
        self.put_calls: list[dict] = []

    def put_object(self, **kwargs) -> None:
        self.put_calls.append(kwargs)


@pytest.fixture(autouse=True)
def _fake_s3(monkeypatch: pytest.MonkeyPatch) -> FakeS3Client:
    fake = FakeS3Client()
    monkeypatch.setattr(artifact_store_module, "_s3_client", lambda: fake)
    return fake


def test_sha256_bytes_matches_hashlib_directly() -> None:
    assert sha256_bytes(b"hello") == hashlib.sha256(b"hello").hexdigest()


@pytest.mark.asyncio
async def test_artifact_store_put_bytes_uploads_and_returns_location(_fake_s3: FakeS3Client) -> None:
    store = ArtifactStore(bucket_kind=ArtifactBucket.PRIVATE_USER_ARTIFACTS, bucket_name="my-bucket", prefix="private/")
    location = await store.put_bytes("resumes/x.md", b"content")

    # Regression test for a real, live "The specified key does not exist"
    # S3 error: location.key must stay BARE (no prefix baked in) — the
    # Telegram adapter's store.mjs::getArtifactPathFromBucket applies its
    # OWN prefix when it later fetches this object (exactly like it already
    # does for job_artifacts keys, which are always bare too). Returning
    # the prefixed key here means the adapter fetches
    # `{prefix}{prefix}{key}`, which was never actually written.
    assert location.bucket == ArtifactBucket.PRIVATE_USER_ARTIFACTS
    assert location.key == "resumes/x.md"
    assert location.byte_size == len(b"content")
    assert location.checksum_sha256 == sha256_bytes(b"content")
    assert location.local_backup_path is None

    # The actual S3 object, meanwhile, DOES need the real prefix — this
    # part of the contract is unchanged.
    call = _fake_s3.put_calls[0]
    assert call["Bucket"] == "my-bucket"
    assert call["Key"] == "private/resumes/x.md"
    assert call["Metadata"]["sha256"] == sha256_bytes(b"content")


@pytest.mark.asyncio
async def test_local_backup_store_writes_atomically_and_leaves_no_tmp_file(tmp_path: Path) -> None:
    store = LocalPrivateBackupStore(tmp_path)
    receipt = await store.write_bytes("nested/file.md", b"backup content")

    written_path = Path(receipt.path)
    assert written_path.read_bytes() == b"backup content"
    assert not written_path.with_name(written_path.name + ".tmp").exists()
    assert receipt.checksum_sha256 == sha256_bytes(b"backup content")


def test_safe_join_rejects_escaping_paths(tmp_path: Path) -> None:
    with pytest.raises(UnsafePathError):
        safe_join(tmp_path, "../../etc/passwd")


def test_safe_join_allows_nested_paths(tmp_path: Path) -> None:
    result = safe_join(tmp_path, "a/b/c.md")
    assert result == (tmp_path / "a" / "b" / "c.md").resolve()


@pytest.mark.asyncio
async def test_private_stores_writes_both_and_stamps_local_backup_path(tmp_path: Path, _fake_s3: FakeS3Client) -> None:
    stores = PrivateStores(
        bucket=ArtifactStore(bucket_kind=ArtifactBucket.PRIVATE_USER_ARTIFACTS, bucket_name="b", prefix=""),
        local_backup=LocalPrivateBackupStore(tmp_path),
    )
    location = await stores.write_private_bytes("x.md", b"dual write")

    assert Path(location.local_backup_path).read_bytes() == b"dual write"
    assert _fake_s3.put_calls[0]["Key"] == "x.md"
    assert location.checksum_sha256 == sha256_bytes(b"dual write")
