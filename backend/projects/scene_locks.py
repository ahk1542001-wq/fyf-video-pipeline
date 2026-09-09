"""Durable scene-scoped lock registry for the project spine.

Global scope locks remain in ``backend.lock_store``. This registry adds the
scene dimension required by the Studio without changing the production
``story`` lock or the existing global lock file format. Records live beside a
project's immutable versions and are written atomically; callers hold the
project transaction when a lock change must be coordinated with a command.

Unlike the legacy global registry, malformed scene-lock state fails closed: a
request cannot silently bypass an operator's scene protection after a torn or
corrupt write.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional

from backend.job_store import is_valid_job_id, write_json_atomically

__all__ = [
    "SCENE_LOCK_SCOPES",
    "read_scene_locks",
    "set_scene_locks",
    "scene_lock_conflicts",
]


SCENE_LOCK_SCOPES: tuple[str, ...] = ("content", "visual", "timing", "voice")
_SCENE_LOCK_FILENAME = "scene_locks.json"
_SCENE_LOCK = threading.Lock()


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _validate_project_id(project_id: str) -> str:
    if not isinstance(project_id, str) or not is_valid_job_id(project_id):
        raise ValueError(f"Invalid project id (expected 8 lowercase hex): {project_id!r}")
    return project_id


def _project_path(root: Path | str, project_id: str) -> Path:
    _validate_project_id(project_id)
    root_path = Path(root)
    root_resolved = root_path.resolve()
    project_dir = (root_path / project_id).resolve()
    try:
        project_dir.relative_to(root_resolved)
    except ValueError as exc:
        raise ValueError("Forbidden scene lock project path") from exc
    if project_dir.is_symlink():
        raise ValueError("Symlinked scene lock project path is forbidden")
    return project_dir / _SCENE_LOCK_FILENAME


def _clean_scene_id(scene_id: str) -> str:
    if not isinstance(scene_id, str) or not scene_id.strip():
        raise ValueError("scene_id must not be blank")
    return scene_id.strip()


def _validate_scope(scope: str) -> str:
    if scope not in SCENE_LOCK_SCOPES:
        raise ValueError(
            f"Unknown scene lock scope {scope!r}; expected one of {SCENE_LOCK_SCOPES}"
        )
    return scope


def read_scene_locks(root: Path | str, project_id: str) -> dict[str, dict[str, dict]]:
    """Read scene locks, rejecting malformed state so protections fail closed."""

    path = _project_path(root, project_id)
    if not path.is_file() or path.stat().st_size == 0:
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("scene lock registry is unreadable") from exc
    if not isinstance(payload, dict) or payload.get("project_id") != project_id:
        raise ValueError("scene lock registry has invalid project identity")
    scenes = payload.get("scenes")
    if not isinstance(scenes, dict):
        raise ValueError("scene lock registry has invalid scenes")
    result: dict[str, dict[str, dict]] = {}
    for raw_scene, raw_scopes in scenes.items():
        if not isinstance(raw_scene, str) or not raw_scene.strip() or not isinstance(raw_scopes, dict):
            raise ValueError("scene lock registry has invalid scene record")
        scene_id = raw_scene.strip()
        valid_scopes: dict[str, dict] = {}
        for scope, record in raw_scopes.items():
            if scope not in SCENE_LOCK_SCOPES or not isinstance(record, dict):
                raise ValueError("scene lock registry has invalid scope record")
            if record.get("locked") is not True:
                raise ValueError("scene lock registry contains a non-locked record")
            valid_scopes[scope] = record
        if valid_scopes:
            result[scene_id] = valid_scopes
    return result


def set_scene_locks(
    root: Path | str,
    project_id: str,
    scene_ids: Iterable[str],
    scope: str,
    *,
    locked: bool,
    locked_by: Optional[str] = None,
    reason: Optional[str] = None,
    now_fn: Callable[[], str] = _utc_now_iso,
) -> dict[str, dict[str, dict]]:
    """Set/clear one scope for all ``scene_ids`` as one atomic registry write."""

    _validate_scope(scope)
    clean_ids = [_clean_scene_id(scene_id) for scene_id in scene_ids]
    if not clean_ids:
        raise ValueError("scene_ids must not be empty")
    if len(set(clean_ids)) != len(clean_ids):
        raise ValueError("scene_ids must be unique")
    path = _project_path(root, project_id)
    with _SCENE_LOCK:
        scenes = read_scene_locks(root, project_id)
        for scene_id in clean_ids:
            scopes = scenes.setdefault(scene_id, {})
            if locked:
                scopes[scope] = {
                    "locked": True,
                    "scope": scope,
                    "scene_id": scene_id,
                    "locked_by": (locked_by or "").strip() or None,
                    "locked_at": now_fn(),
                    "reason": (reason or "").strip() or None,
                }
            else:
                scopes.pop(scope, None)
                if not scopes:
                    scenes.pop(scene_id, None)
        path.parent.mkdir(parents=True, exist_ok=True)
        write_json_atomically(path, {"project_id": project_id, "scenes": scenes})
    return scenes


def scene_lock_conflicts(
    root: Path | str,
    project_id: str,
    scene_ids: Iterable[str],
    scopes: Iterable[str],
) -> dict[str, list[str]]:
    """Return ``scene_id -> locked scopes`` for a selected set of scenes."""

    wanted_scenes = {_clean_scene_id(scene_id) for scene_id in scene_ids}
    wanted_scopes = {_validate_scope(scope) for scope in scopes}
    records = read_scene_locks(root, project_id)
    return {
        scene_id: sorted(scope for scope in wanted_scopes if scope in records.get(scene_id, {}))
        for scene_id in sorted(wanted_scenes)
        if any(scope in records.get(scene_id, {}) for scope in wanted_scopes)
    }

