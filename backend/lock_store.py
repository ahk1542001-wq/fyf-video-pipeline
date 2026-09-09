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

3. **Project/version human acceptance** — a separate owner decision persisted
   under ``human-acceptance/{project_id}/v{version_no}.json``.  Automated QA
   can make a render eligible for review, but it cannot create this record or
   make download/export readiness true on its own.

The granular registry is stored atomically (``write_json_atomically``) under
``{locks_root}/granular/{project_id}.json`` and is path-escape safe: project ids
reuse ``is_valid_job_id`` (``[0-9a-f]{8}``) and every path is ``resolve()``-checked
against the root, mirroring ``backend.projects.store``.
"""

from __future__ import annotations

import json
from os import stat_result
import re
import stat
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from backend.job_store import create_job_dir, is_valid_job_id, write_json_atomically
from backend.output_qa import OUTPUT_QA_REPORT_VERSION, qa_report_fingerprint
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
    "human_acceptance_path",
    "read_human_acceptance",
    "set_human_acceptance",
    "revoke_human_acceptance",
    "is_human_accepted",
    "final_acceptance_readiness",
]


# The whole-script lock (existing production-render behaviour) is the "story"
# scope; the granular project scopes match backend.projects.models.CommandScope.
WHOLE_SCRIPT_LOCK_SCOPE = "story"
GRANULAR_LOCK_SCOPES: tuple[str, ...] = ("content", "visual", "timing")
LOCK_SCOPES: tuple[str, ...] = (WHOLE_SCRIPT_LOCK_SCOPE,) + GRANULAR_LOCK_SCOPES

_GRANULAR_DIRNAME = "granular"
_HUMAN_ACCEPTANCE_DIRNAME = "human-acceptance"
_GRANULAR_LOCK = threading.Lock()
_HUMAN_ACCEPTANCE_LOCK = threading.Lock()


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


def _lstat_or_none(path: Path) -> stat_result | None:
    """Read one registry entry without turning I/O errors into absence."""

    try:
        return path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ValueError(f"granular lock registry is unreadable: {path}") from exc


def _granular_path(locks_root: Path, project_id: str) -> Path:
    _validate_project_id(project_id)
    root = Path(locks_root)
    registry_dir = root / _GRANULAR_DIRNAME
    raw_path = registry_dir / f"{project_id}.json"
    # ``Path.resolve()`` hides broken links by returning their would-be target.
    # Inspect the on-disk entry first so a damaged registry cannot look absent.
    registry_stat = _lstat_or_none(registry_dir)
    if registry_stat is not None:
        if stat.S_ISLNK(registry_stat.st_mode) or not stat.S_ISDIR(registry_stat.st_mode):
            raise ValueError(f"granular lock registry is corrupt: {registry_dir}")
    raw_stat = _lstat_or_none(raw_path)
    if raw_stat is not None and stat.S_ISLNK(raw_stat.st_mode):
        raise ValueError(f"granular lock registry is corrupt: {raw_path}")
    root_resolved = root.resolve()
    try:
        path = raw_path.resolve()
    except OSError as exc:
        raise ValueError(f"granular lock registry is unreadable: {raw_path}") from exc
    try:
        path.relative_to(root_resolved)
    except ValueError as exc:  # pragma: no cover - defense in depth
        raise ValueError("Forbidden granular lock path") from exc
    return path


def read_scope_locks(locks_root: Path, project_id: str) -> dict[str, dict]:
    """Return the persisted granular scope locks for one project.

    An absent registry means that no granular lock has been persisted yet.  A
    corrupt registry is different: it may conceal an active lock, so every
    caller fails closed with ``ValueError`` rather than silently treating the
    project as unlocked.
    """
    path = _granular_path(locks_root, project_id)
    registry_stat = _lstat_or_none(path)
    if registry_stat is None:
        return {}
    if not stat.S_ISREG(registry_stat.st_mode) or registry_stat.st_size == 0:
        raise ValueError(f"granular lock registry is corrupt: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"granular lock registry is corrupt: {path}") from exc
    if not isinstance(payload, dict) or payload.get("project_id") != project_id:
        raise ValueError(f"granular lock registry is corrupt: {path}")
    scopes = payload.get("scopes")
    if not isinstance(scopes, dict):
        raise ValueError(f"granular lock registry is corrupt: {path}")

    validated: dict[str, dict] = {}
    for scope, record in scopes.items():
        if scope not in GRANULAR_LOCK_SCOPES or not isinstance(record, dict):
            raise ValueError(f"granular lock registry is corrupt: {path}")
        if record.get("scope") != scope or record.get("locked") is not True:
            raise ValueError(f"granular lock registry is corrupt: {path}")
        validated[scope] = dict(record)
    return validated


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


# ---------------------------------------------------------------------------
# Project/version human acceptance (download/export gate)
# ---------------------------------------------------------------------------


def _validate_version_no(version_no: int) -> int:
    if isinstance(version_no, bool) or not isinstance(version_no, int) or version_no <= 0:
        raise ValueError("version_no must be a positive integer")
    return version_no


def human_acceptance_path(locks_root: Path, project_id: str, version_no: int) -> Path:
    """Return the path for one project/version acceptance decision safely."""

    _validate_project_id(project_id)
    _validate_version_no(version_no)
    root_resolved = Path(locks_root).resolve()
    path = (
        Path(locks_root)
        / _HUMAN_ACCEPTANCE_DIRNAME
        / project_id
        / f"v{version_no}.json"
    ).resolve()
    try:
        path.relative_to(root_resolved)
    except ValueError as exc:  # pragma: no cover - defence in depth
        raise ValueError("Forbidden human acceptance path") from exc
    return path


def read_human_acceptance(
    locks_root: Path, project_id: str, version_no: int
) -> dict | None:
    """Read one acceptance decision, failing closed on malformed state."""

    path = human_acceptance_path(locks_root, project_id, version_no)
    if not path.is_file() or path.stat().st_size == 0:
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("Human acceptance record is unreadable") from exc
    if not isinstance(payload, dict):
        raise ValueError("Human acceptance record must be an object")
    if payload.get("project_id") != project_id or payload.get("version_no") != version_no:
        raise ValueError("Human acceptance record does not match project/version")
    if payload.get("accepted") not in (True, False):
        raise ValueError("Human acceptance record has an invalid accepted flag")
    if not isinstance(payload.get("actor"), str) or not payload["actor"].strip():
        raise ValueError("Human acceptance record is missing actor")
    return payload


def set_human_acceptance(
    locks_root: Path,
    project_id: str,
    version_no: int,
    *,
    actor: str,
    accepted: bool,
    automated_qa: dict | None = None,
    qa_fingerprint: str | None = None,
    note: str | None = None,
    now_fn: Callable[[], str] = _utc_now_iso,
) -> dict:
    """Persist an explicit owner acceptance or rejection for one version.

    ``accepted=True`` is deliberately refused unless the latest deterministic
    QA evidence says ``passed``.  The evidence is recorded for auditability,
    while the acceptance decision remains a distinct actor-owned field.
    """

    _validate_project_id(project_id)
    _validate_version_no(version_no)
    if not isinstance(actor, str) or not actor.strip():
        raise ValueError("actor is required for human acceptance")
    if not isinstance(accepted, bool):
        raise ValueError("accepted must be a boolean")
    if automated_qa is not None and not isinstance(automated_qa, dict):
        raise ValueError("automated_qa must be an object or None")
    qa = dict(automated_qa) if isinstance(automated_qa, dict) else None
    if accepted:
        if not _automated_qa_passed(qa):
            raise ValueError("Human acceptance requires a passing automated QA report")
        if qa.get("unverified") is True:
            raise ValueError("Human acceptance cannot approve unverified automated evidence")
    if qa_fingerprint is not None and (
        not isinstance(qa_fingerprint, str) or not qa_fingerprint.strip()
    ):
        raise ValueError("qa_fingerprint must be a non-empty string when supplied")
    if qa_fingerprint is None and isinstance(qa, dict) and isinstance(qa.get("fingerprint"), str):
        qa_fingerprint = qa["fingerprint"]

    record = {
        "schema_version": 1,
        "project_id": project_id,
        "version_no": version_no,
        "accepted": accepted,
        "status": "accepted" if accepted else "rejected",
        "actor": actor.strip(),
        "decided_at": now_fn(),
        "qa_fingerprint": qa_fingerprint.strip() if isinstance(qa_fingerprint, str) else None,
        "automated_qa": qa,
        "note": note.strip() if isinstance(note, str) and note.strip() else None,
    }
    path = human_acceptance_path(locks_root, project_id, version_no)
    with _HUMAN_ACCEPTANCE_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        write_json_atomically(path, record)
    return record


def revoke_human_acceptance(
    locks_root: Path,
    project_id: str,
    version_no: int,
    *,
    actor: str,
    reason: str | None = None,
    now_fn: Callable[[], str] = _utc_now_iso,
) -> dict:
    """Record an explicit rejection that revokes prior download readiness."""

    return set_human_acceptance(
        locks_root,
        project_id,
        version_no,
        actor=actor,
        accepted=False,
        note=reason,
        now_fn=now_fn,
    )


def _automated_qa_passed(qa: dict | None) -> bool:
    """Accept only an internally consistent, available automated QA pass."""

    if not isinstance(qa, dict) or qa.get("passed") is not True:
        return False
    if qa.get("report_version") != OUTPUT_QA_REPORT_VERSION:
        return False
    if qa.get("persisted_at_source") != "local_deterministic_qa":
        return False
    status = qa.get("status")
    if not isinstance(status, str) or status.strip().lower() not in {
        "available",
        "passed",
        "verified",
    }:
        return False
    fingerprint = qa.get("fingerprint")
    if not isinstance(fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
        return False
    identity = qa.get("qa_identity")
    if not isinstance(identity, dict):
        return False
    if (
        not isinstance(identity.get("job_id"), str)
        or not identity["job_id"].strip()
        or identity.get("report_name") != qa.get("report_name")
        or identity.get("attempt") != qa.get("attempt")
        or qa.get("job_id") != identity.get("job_id")
        or not isinstance(qa.get("report_name"), str)
        or not qa["report_name"].strip()
        or isinstance(qa.get("attempt"), bool)
        or not isinstance(qa.get("attempt"), int)
        or qa["attempt"] < 1
    ):
        return False
    try:
        if qa_report_fingerprint(qa) != fingerprint:
            return False
    except (TypeError, ValueError):
        return False

    failure_codes = qa.get("failure_codes")
    if not isinstance(failure_codes, list) or failure_codes:
        return False
    def _passing_evidence_rows(value: object, identity_keys: tuple[str, ...]) -> bool:
        if not isinstance(value, list) or not value:
            return False
        for row in value:
            if not isinstance(row, dict) or row.get("passed") is not True:
                return False
            identity = next(
                (row.get(key) for key in identity_keys if isinstance(row.get(key), str)),
                None,
            )
            if not isinstance(identity, str) or not identity.strip():
                return False
        return True

    # A persisted pass needs concrete, identifiable evidence rows.  Gate-level
    # booleans alone are not evidence, and a report carrying both ``checks`` and
    # ``segments`` must have both sections internally consistent rather than
    # letting one passing branch hide a failed one.
    evidence_sections = (
        ("checks", ("id", "name", "check_id")),
        ("segments", ("segment_id", "id", "scene_id")),
    )
    evidence_present = False
    for section, identity_keys in evidence_sections:
        if section not in qa:
            continue
        if not _passing_evidence_rows(qa.get(section), identity_keys):
            return False
        evidence_present = True
    if not evidence_present:
        return False
    for key in ("semantic_verification_status", "provider_status", "status"):
        value = qa.get(key)
        if isinstance(value, str) and value.strip().lower() in {
            "unverified",
            "unavailable",
            "not_called",
            "provider_unavailable",
            "invalid_provider_fallback_registry",
        }:
            return False
    for gate_name in ("technical_gate", "creative_gate", "overall"):
        gate = qa.get(gate_name)
        if isinstance(gate, dict) and gate.get("passed") is not True:
            return False
    nested_final = qa.get("final_visual_qa")
    if isinstance(nested_final, dict) and not _automated_qa_passed(nested_final):
        return False
    return True


def is_human_accepted(locks_root: Path, project_id: str, version_no: int) -> bool:
    record = read_human_acceptance(locks_root, project_id, version_no)
    return bool(record and record.get("accepted") is True)


def final_acceptance_readiness(
    locks_root: Path,
    project_id: str,
    version_no: int,
    *,
    automated_qa: dict | None = None,
) -> dict:
    """Return the project/version download/export readiness decision.

    This function is intentionally conservative: missing acceptance is pending,
    a rejected decision is blocked, and any supplied QA evidence that is not a
    deterministic pass keeps the output unavailable.
    """

    record = read_human_acceptance(locks_root, project_id, version_no)
    qa = automated_qa if isinstance(automated_qa, dict) else (
        record.get("automated_qa") if isinstance(record, dict) else None
    )
    qa_passed = _automated_qa_passed(qa) and not bool(qa.get("unverified") is True)
    if not qa_passed:
        status = "blocked_automated_qa" if qa is not None else "pending_automated_qa"
        reason = "a passing deterministic QA report is required"
        return {
            "project_id": project_id,
            "version_no": version_no,
            "automated_qa_passed": False,
            "human_accepted": bool(record and record.get("accepted") is True),
            "download_ready": False,
            "status": status,
            "reason": reason,
        }
    human_accepted = bool(record and record.get("accepted") is True)
    if not record:
        return {
            "project_id": project_id,
            "version_no": version_no,
            "automated_qa_passed": True,
            "human_accepted": False,
            "download_ready": False,
            "status": "pending_human_acceptance",
            "reason": "explicit owner acceptance is required",
        }
    if not human_accepted:
        return {
            "project_id": project_id,
            "version_no": version_no,
            "automated_qa_passed": True,
            "human_accepted": False,
            "download_ready": False,
            "status": "rejected",
            "reason": "human acceptance was rejected or revoked",
        }
    stored_fingerprint = record.get("qa_fingerprint")
    supplied_fingerprint = automated_qa.get("fingerprint") if isinstance(automated_qa, dict) else None
    if stored_fingerprint and automated_qa is not None and supplied_fingerprint != stored_fingerprint:
        return {
            "project_id": project_id,
            "version_no": version_no,
            "automated_qa_passed": True,
            "human_accepted": True,
            "download_ready": False,
            "status": "qa_fingerprint_mismatch",
            "reason": "acceptance is bound to a different automated QA result",
        }
    return {
        "project_id": project_id,
        "version_no": version_no,
        "automated_qa_passed": True,
        "human_accepted": True,
        "download_ready": True,
        "status": "ready",
        "reason": None,
    }
