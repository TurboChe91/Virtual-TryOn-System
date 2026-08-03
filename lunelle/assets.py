"""Content-addressed store for every input asset a task can depend on.

Why this exists: a task recorded the *path* of its inputs. Hand models were
written to a fixed `{tone}-{view}.png`, so re-uploading one silently changed the
bytes that a past matrix cell had been generated from — the task's recorded
inputs no longer described what actually produced the image, and re-running it
could not reproduce anything. Style references were already digest-named and so
already immutable; this generalizes that property to every input.

Layout: `{data_dir}/assets/{digest[:2]}/{digest}{ext}`. The name IS the hash, so
writing is idempotent and an "overwrite" is impossible by construction: different
bytes produce a different path, and the old path keeps serving the old bytes.

The `assets` table is the registry — what a digest is, how big, what kind — so a
snapshot can name inputs by digest and a reader can resolve them back to bytes
without guessing at directory conventions.
"""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .db import Database, transaction, utcnow

logger = logging.getLogger(__name__)

#: Asset kinds. `output` covers generated images promoted into the store.
KINDS = ("reference", "plan", "hand_model", "correction_detail", "output", "other")

MIME_BY_EXT = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}


@dataclass(frozen=True)
class AssetRef:
    """A stored asset, identified by content rather than by location."""

    digest: str
    path: Path
    byte_size: int
    mime_type: str
    kind: str

    @property
    def short(self) -> str:
        return self.digest[:12]

    def as_dict(self) -> dict:
        return {
            "digest": self.digest,
            "byte_size": self.byte_size,
            "mime_type": self.mime_type,
            "kind": self.kind,
        }


def assets_root(config: Config) -> Path:
    return config.asset_dir


def content_path(config: Config, digest: str, ext: str) -> Path:
    """Sharded by the first two hex chars: one flat directory would hold tens of
    thousands of files and make directory listing painful on some filesystems."""
    return assets_root(config) / digest[:2] / f"{digest}{ext}"


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def store_bytes(db: Database, config: Config, data: bytes, *, kind: str,
                ext: str, mime_type: str | None = None) -> AssetRef:
    """Put bytes in the store and register them. Idempotent for identical bytes.

    Full sha256 is the identity, not a truncated prefix: a 12-hex-char collision
    is unlikely but would silently swap one operator's asset for another's, and
    the storage cost of the full hex is nothing.
    """
    if kind not in KINDS:
        raise ValueError(f"unknown asset kind {kind!r}; expected one of {KINDS}")
    digest = hashlib.sha256(data).hexdigest()
    mime = mime_type or MIME_BY_EXT.get(ext.lower(), "application/octet-stream")
    path = content_path(config, digest, ext)
    if not path.is_file():
        _atomic_write(path, data)
    elif path.stat().st_size != len(data):  # pragma: no cover - defensive
        # Same digest, different length is a hash collision or a corrupt file.
        # Rewriting is the safe response: the incoming bytes hash to this name.
        logger.warning("asset %s existed with a different size; rewriting", digest[:12])
        _atomic_write(path, data)

    conn = db.conn()
    with transaction(conn):
        conn.execute(
            "INSERT INTO assets (digest, kind, mime_type, byte_size, path, created_at)"
            " VALUES (?,?,?,?,?,?)"
            # First writer wins on metadata; the bytes are identical either way.
            " ON CONFLICT(digest) DO NOTHING",
            (digest, kind, mime, len(data), str(path), utcnow()),
        )
    return AssetRef(digest=digest, path=path, byte_size=len(data),
                    mime_type=mime, kind=kind)


def store_file(db: Database, config: Config, source: Path, *, kind: str,
               mime_type: str | None = None) -> AssetRef:
    """Copy an existing file into the store (used to freeze legacy assets)."""
    data = source.read_bytes()
    return store_bytes(db, config, data, kind=kind, ext=source.suffix.lower(),
                       mime_type=mime_type)


def get_asset(db: Database, digest: str) -> dict | None:
    row = db.conn().execute(
        "SELECT * FROM assets WHERE digest = ?", (digest,)
    ).fetchone()
    return dict(row) if row else None


def resolve_path(db: Database, digest: str) -> Path | None:
    """Path for a digest, or None if unknown or gone from disk."""
    record = get_asset(db, digest)
    if record is None:
        return None
    path = Path(record["path"])
    return path if path.is_file() else None


def digest_of_file(path: Path) -> str:
    """sha256 of a file's contents, read in chunks (inputs can be large)."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def register_locked(conn, path: Path, *, kind: str) -> AssetRef | None:
    """Register a file inside the CALLER's transaction.

    Task creation registers input assets while already holding a write
    transaction, and SQLite cannot nest BEGIN IMMEDIATE. This follows the existing
    `_locked` convention in this codebase (see _transition_locked,
    check_can_queue_locked, store_snapshot_locked).
    """
    if not path.is_file():
        return None
    data = path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    mime = MIME_BY_EXT.get(path.suffix.lower(), "application/octet-stream")
    conn.execute(
        "INSERT INTO assets (digest, kind, mime_type, byte_size, path, created_at)"
        " VALUES (?,?,?,?,?,?) ON CONFLICT(digest) DO NOTHING",
        (digest, kind, mime, len(data), str(path), utcnow()),
    )
    return AssetRef(digest=digest, path=path, byte_size=len(data),
                    mime_type=mime, kind=kind)


def ensure_registered(db: Database, config: Config, path: Path, *,
                      kind: str) -> AssetRef | None:
    """Register a file that already exists outside the store, without moving it.

    Used for assets written before the store existed: their bytes are hashed and
    recorded so a snapshot can still name them by digest. The file stays where it
    is — rewriting historical paths would break the very references being frozen.
    """
    conn = db.conn()
    with transaction(conn):
        return register_locked(conn, path, kind=kind)


def describe_inputs_locked(conn, paths: list[Path], *, kind: str) -> list[dict]:
    """Digest-describe input files for a snapshot, inside the caller's transaction.

    A path that no longer exists is recorded as missing rather than omitted: the
    snapshot has to say "this input was expected and absent", which is a different
    fact from "there was no such input".
    """
    described: list[dict] = []
    for path in paths:
        if not path.is_file():
            described.append({"path": str(path), "missing": True})
            continue
        ref = register_locked(conn, path, kind=kind)
        entry = {"path": str(path), "missing": False}
        if ref is not None:
            entry.update(ref.as_dict())
        described.append(entry)
    return described


def describe_inputs(db: Database, config: Config, paths: list[Path], *,
                    kind: str) -> list[dict]:
    """Transaction-owning wrapper around describe_inputs_locked."""
    conn = db.conn()
    with transaction(conn):
        return describe_inputs_locked(conn, paths, kind=kind)
