"""One shared "stay inside this root" path check — used by
artifact_store.py's LocalPrivateBackupStore (writing backups), so
path-escape validation is written once, not twice. Mirrors the same safety
property
telegram/src/protocol/core.mjs's assertSafeArtifactPath enforces for the
existing bot's artifact ingestion, applied here to the agent app's local
private-data directories instead of an S3 upload root.
"""
from __future__ import annotations

from pathlib import Path


class UnsafePathError(ValueError):
    """Raised when a relative path would resolve outside its root — e.g. via
    `../../etc/passwd` — rather than silently clamping or ignoring it."""


def safe_join(root: Path, relative: str) -> Path:
    """Resolve `relative` under `root`, rejecting anything that would escape
    it. Callers pass a relative path they built themselves (a template id, a
    generated artifact key) — this exists to catch a bug or unexpected input
    before it ever touches the filesystem, not to sanitize arbitrary
    user-typed paths."""
    resolved_root = root.resolve()
    candidate = (resolved_root / relative).resolve()
    if candidate != resolved_root and resolved_root not in candidate.parents:
        raise UnsafePathError(f"{relative!r} escapes {resolved_root}")
    return candidate
