"""Artifact storage seam (B5).

Narrow, additive seam so cloud adapters (GCS / Postgres) can be introduced in
Stage G without touching the project spine. Only ``LocalArtifactStorage`` exists
today.
"""

from __future__ import annotations

from backend.storage.backend import (
    ArtifactStorage,
    LocalArtifactStorage,
    InvalidArtifactKeyError,
)

__all__ = ["ArtifactStorage", "LocalArtifactStorage", "InvalidArtifactKeyError"]
