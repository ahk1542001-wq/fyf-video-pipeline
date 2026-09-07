"""B5 - Artifact storage backend seam.

``ArtifactStorage`` is the narrow interface the project spine uses to persist
produced bytes (images, audio, video, manifests). ``LocalArtifactStorage`` is the
only implementation today; GCS / Postgres adapters are ADDITIVE and deferred to
Stage G. The seam is intentionally minimal (put/get/exists/delete/uri_for) so a
cloud adapter can satisfy it without the spine changing.

Path safety mirrors ``backend/lock_store.py``: keys are validated against a
closed pattern and the resolved path is confined to the storage root.
"""

from __future__ import annotations

import os
import re
import uuid
from pathlib import Path
from typing import Protocol, runtime_checkable

__all__ = [
    "ArtifactStorage",
    "LocalArtifactStorage",
    "InvalidArtifactKeyError",
]


class InvalidArtifactKeyError(ValueError):
    """Raised when an artifact key is malformed or attempts path escape."""


# A closed key shape: must start alphanumeric, then alphanumeric/._/- only.
# Absolute paths, leading dots, and control characters are structurally rejected.
_KEY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/\-]{0,254}$")


@runtime_checkable
class ArtifactStorage(Protocol):
    """Narrow storage seam. Cloud adapters implement exactly these methods."""

    def put(self, key: str, data: bytes) -> str:
        """Persist ``data`` under ``key`` atomically; return its stable uri."""
        ...

    def get(self, key: str) -> bytes:
        """Return the bytes stored under ``key`` (FileNotFoundError if absent)."""
        ...

    def exists(self, key: str) -> bool:
        """True when ``key`` is present."""
        ...

    def delete(self, key: str) -> None:
        """Remove ``key`` if present (idempotent)."""
        ...

    def uri_for(self, key: str) -> str:
        """Return the stable uri/reference for ``key`` (no bytes are read)."""
        ...


class LocalArtifactStorage:
    """Filesystem-backed artifact storage with atomic, path-safe writes."""

    def __init__(self, root: Path | str) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

    @property
    def root(self) -> Path:
        return self._root

    # -- key/path safety ----------------------------------------------------
    def _resolve(self, key: str) -> Path:
        if not isinstance(key, str) or not _KEY_PATTERN.fullmatch(key):
            raise InvalidArtifactKeyError(f"Invalid artifact key: {key!r}")
        if any(segment == ".." for segment in key.split("/")):
            raise InvalidArtifactKeyError(f"Path escape in artifact key: {key!r}")
        path = (self._root / key).resolve()
        root_resolved = self._root.resolve()
        try:
            path.relative_to(root_resolved)
        except ValueError as exc:  # pragma: no cover - defense in depth
            raise InvalidArtifactKeyError("Forbidden artifact path") from exc
        return path

    # -- ArtifactStorage ----------------------------------------------------
    def put(self, key: str, data: bytes) -> str:
        if not isinstance(data, (bytes, bytearray)):
            raise TypeError("artifact data must be bytes")
        path = self._resolve(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.tmp.{uuid.uuid4().hex[:8]}")
        try:
            with open(tmp, "wb") as handle:
                handle.write(bytes(data))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass
        return self.uri_for(key)

    def get(self, key: str) -> bytes:
        path = self._resolve(key)
        if not path.is_file():
            raise FileNotFoundError(key)
        return path.read_bytes()

    def exists(self, key: str) -> bool:
        return self._resolve(key).is_file()

    def delete(self, key: str) -> None:
        self._resolve(key).unlink(missing_ok=True)

    def uri_for(self, key: str) -> str:
        # Validate the key but do not require the object to exist yet.
        path = self._resolve(key)
        return f"file://{path}"
