"""Versioned project spine (Stage B-II: B4/B5/B6).

This package is the durable, versioned "spine" that the rest of the studio is
built around. It is intentionally dependency-light and additive:

* :mod:`backend.projects.models`    - interface contracts (commands, versions,
  change sets, events, closed surface union). ``ProjectVersion`` WRAPS
  ``video_contract.VideoScript``; it never duplicates script fields.
* :mod:`backend.projects.store`     - the ``ProjectStore`` seam + its only
  implementation ``FileProjectStore`` (immutable versions, append-only event
  log, per-project lock, path-escape-safe ids).
* :mod:`backend.projects.selection` - pure ``resolve_selection`` (scene /
  object / time-range -> ordered segment ids).
* :mod:`backend.projects.commands`  - ``apply_command``: atomic, validate-ALL,
  zero-partial-edit command application with reject/rebase on stale versions.

Task 5 (durable queue / cancellation / capacity) and Task 9 (chat -> command /
granular locks) extend this spine WITHOUT changing ``apply_command``'s
signature; the lock check is an injectable hook and the store is a Protocol.
"""

from __future__ import annotations

__all__: list[str] = []
