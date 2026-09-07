"""Server-owned locks for production renders and the versioned project spine.

Two lock dimensions live here:

1. **Whole-script narration/visual locks** (``scope="story"``) — the original
   production-render lock. ``create_script_lock`` / ``read_script_lock`` persist
   one validated immutable ``VideoScript`` behind an opaque lock id and back the
   ``POST /api/story-lock`` route. Their behaviour is UNCHANGED: the whole-script
   lock is simply the ``story`` scope of the taxonomy below.

2. **Granular project-scope locks** (``content`` / ``visual`` / ``timing``) — a
   per-project registry that Task 9 (Stage C-II) feeds into
   ``backend.projects.commands.apply_command`` through its injectable
   ``lock_checker`` seam. ``granular_lock_checker`` returns a hook that reports a
   conflict when EITHER the version's own ``LockState`` OR this registry locks the
   scope a command touches, so the default whole-version behaviour is preserved
   and only widened.

The granular registry is stored atomically (``write_json_atomically``) under
``{locks_root}/granular/{project_id}.json`` and is path-escape safe: project ids
reuse ``is_valid_job_id`` (``[0-9a-f]{8}``) and every path is ``resolve()``-checked
against the root, mirroring ``backend.projects.store``.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from backend.job_store import create_job_dir, is_valid_job_id, write_json_atomically
from video_contract import VideoScript

__all__ = [
    "WHOLE_SCRIPT_LOCK_SCOPE",
    "GRANULAR_LOCK_SCOPES",
    "LOCK_SCOPES",
    "create_script_lock",
    "read_script_lock",
    "read_scope_locks",
    "set_scope_lock",
    "release_scope_lock",
    "is_scope_locked",
    "locked_scopes",
    "granular_lock_checker",
]


# The whole-script lock (existing production-render behaviour) is the "story"
# scope; the granular project scopes match backend.projects.models.CommandScope.
WHOLE_SCRIPT_LOCK_SCOPE = "story"
GRANULAR_LOCK_SCOPES: tuple[str, ...] = ("content", "visual", "timing")
LOCK_SCOPES: tuple[str, ...] = (WHOLE_SCRIPT_LOCK_SCOPE,) + GRANULAR_LOCK_SCOPES

_GRANULAR_DIRNAME = "granular"
_GRANULAR_LOCK = threading.Lock()


def create_script_lock(locks_root: Path, script_data: dict) -> str:
    """Persist one validated immutable script and return its opaque lock ID."""
    lock_id = create_job_dir(locks_root)
    lock_dir = locks_root / lock_id
    validated = VideoScript.model_validate(script_data).model_dump(mode="json")
    write_json_atomically(lock_dir / "script.json", validated)
    return lock_id


def read_script_lock(locks_root: Path, lock_id: str) -> dict:
    if not is_valid_job_id(lock_id):
        raise ValueError("Invalid script lock ID")
    path = (locks_root / lock_id / "script.json").resolve()
    try:
        path.relative_to(locks_root.resolve())
    except ValueError as exc:
        raise ValueError("Forbidden script lock path") from exc
    if not path.is_file() or path.stat().st_size == 0:
        raise FileNotFoundError("Script lock not found")
    return VideoScript.model_validate_json(path.read_text(encoding="utf-8")).model_dump(mode="json")


# ---------------------------------------------------------------------------
# Granular project-scope lock registry (Stage C-II / C4)
# ---------------------------------------------------------------------------


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _validate_scope(scope: str) -> str:
    if scope not in GRANULAR_LOCK_SCOPES:
        raise ValueError(
            f"Unknown granular lock scope {scope!r}; expected one of {GRANULAR_LOCK_SCOPES}"
        )
    return scope


def _validate_project_id(project_id: str) -> str:
    if not is_valid_job_id(project_id):
        raise ValueError(f"Invalid project id (expected 8 lowercase hex): {project_id!r}")
    return project_id


def _granular_path(locks_root: Path, project_id: str) -> Path:
    _validate_project_id(project_id)
    root_resolved = Path(locks_root).resolve()
    path = (Path(locks_root) / _GRANULAR_DIRNAME / f"{project_id}.json").resolve()
    try:
        path.relative_to(root_resolved)
    except ValueError as exc:  # pragma: no cover - defense in depth
        raise ValueError("Forbidden granular lock path") from exc
    return path


def read_scope_locks(locks_root: Path, project_id: str) -> dict[str, dict]:
    """Return the persisted granular scope locks for one project.

    Absent/corrupt registries fail OPEN to *no granular locks* (the version's own
    ``LockState`` still applies through the default checker); a corrupt registry is
    never treated as a lock so an operator is not silently frozen out.
    """
    path = _granular_path(locks_root, project_id)
    if not path.is_file() or path.stat().st_size == 0:
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    scopes = payload.get("scopes") if isinstance(payload, dict) else None
    if not isinstance(scopes, dict):
        return {}
    return {
        scope: record
        for scope, record in scopes.items()
        if scope in GRANULAR_LOCK_SCOPES and isinstance(record, dict)
    }


def set_scope_lock(
    locks_root: Path,
    project_id: str,
    scope: str,
    *,
    locked: bool,
    locked_by: Optional[str] = None,
    reason: Optional[str] = None,
    now_fn: Callable[[], str] = _utc_now_iso,
) -> dict:
    """Persist (or clear) one granular scope lock and return the resulting record.

    The read-modify-write is serialized under a process lock and written
    atomically so concurrent toggles resolve to exactly one durable state.
    """
    _validate_scope(scope)
    path = _granular_path(locks_root, project_id)
    with _GRANULAR_LOCK:
        scopes = read_scope_locks(locks_root, project_id)
        if locked:
            record = {
                "locked": True,
                "scope": scope,
                "locked_by": (locked_by or "").strip() or None,
                "locked_at": now_fn(),
                "reason": (reason or "").strip() or None,
            }
            scopes[scope] = record
        else:
            record = {"locked": False, "scope": scope}
            scopes.pop(scope, None)
        path.parent.mkdir(parents=True, exist_ok=True)
        write_json_atomically(path, {"project_id": project_id, "scopes": scopes})
    return record


def release_scope_lock(locks_root: Path, project_id: str, scope: str) -> dict:
    """Clear one granular scope lock (convenience wrapper over ``set_scope_lock``)."""
    return set_scope_lock(locks_root, project_id, scope, locked=False)


def is_scope_locked(locks_root: Path, project_id: str, scope: str) -> bool:
    _validate_scope(scope)
    record = read_scope_locks(locks_root, project_id).get(scope)
    return bool(record and record.get("locked"))


def locked_scopes(locks_root: Path, project_id: str) -> list[str]:
    """Ordered list of granular scopes currently locked for one project."""
    scopes = read_scope_locks(locks_root, project_id)
    return [scope for scope in GRANULAR_LOCK_SCOPES if scopes.get(scope, {}).get("locked")]


def granular_lock_checker(
    locks_root: Path,
) -> Callable[[Optional[object], object], list[str]]:
    """Build a ``LockCheckHook`` for ``apply_command`` backed by this registry.

    The returned hook has the exact signature ``apply_command`` expects
    (``(version, command) -> list[str]``) and widens — never narrows — the
    default: it reports a conflict when the version's own ``LockState`` locks the
    command's effective scope (default behaviour) OR this granular registry does.
    ``apply_command``'s signature and default hook are untouched; injection is the
    seam.
    """

    def _checker(version, command) -> list[str]:
        scope = command.effective_scope()
        if scope is None or scope not in GRANULAR_LOCK_SCOPES:
            return []
        conflicts: list[str] = []
        # 1) Preserve the default whole-version LockState behaviour.
        if version is not None and bool(getattr(getattr(version, "locks", None), scope, False)):
            conflicts.append(scope)
        # 2) Widen with the granular registry (bounded to the command's project).
        project_id = getattr(command, "project_id", None)
        if project_id and is_scope_locked(locks_root, project_id, scope):
            if scope not in conflicts:
                conflicts.append(scope)
        return conflicts

    return _checker
