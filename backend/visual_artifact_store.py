"""Content-addressed, integrity-checked shared visual artifacts."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from backend.job_store import write_json_atomically


_KEY_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_LEASE_NAME = "producer.lease"
_MANIFEST_NAME = "manifest.json"


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def visual_artifact_key(
    script: dict,
    policy_version: str,
    model_routes: dict[str, str],
) -> str:
    payload = {
        "script": script,
        "policy_version": policy_version,
        "model_routes": model_routes,
    }
    return hashlib.sha256(_canonical_json(payload)).hexdigest()


def _artifact_dir(root: Path, key: str) -> Path:
    if not _KEY_PATTERN.fullmatch(key):
        raise ValueError("Visual artifact key must be exactly 64 lowercase hex characters")
    root = Path(root).resolve()
    artifact = (root / key).resolve()
    artifact.relative_to(root)
    return artifact


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_file(base: Path, relative: str, *, must_exist: bool) -> Path:
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        raise ValueError("Artifact manifest contains an invalid file path")
    base = base.resolve()
    candidate = base / relative
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(base)
    except ValueError as exc:
        raise ValueError("Artifact manifest path escapes its root") from exc
    current = candidate
    while current != base:
        if current.is_symlink():
            raise ValueError("Artifact manifest paths must not contain symlinks")
        current = current.parent
    if must_exist and (not candidate.is_file() or candidate.is_symlink()):
        raise ValueError(f"Artifact file is missing or unsafe: {relative}")
    return candidate


def _load_manifest(artifact: Path, *, strict: bool) -> dict | None:
    path = artifact / _MANIFEST_NAME
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict) or manifest.get("state") != "approved":
            raise ValueError("Visual artifact is not approved")
        files = manifest.get("files")
        if not isinstance(files, list) or not files:
            raise ValueError("Approved visual artifact has no files")
        for item in files:
            if not isinstance(item, dict):
                raise ValueError("Artifact manifest file entry is invalid")
            source = _safe_file(artifact, item.get("path"), must_exist=True)
            if item.get("bytes") != source.stat().st_size:
                raise ValueError(f"Artifact size mismatch: {item.get('path')}")
            if item.get("sha256") != _sha256(source):
                raise ValueError(f"Artifact digest mismatch: {item.get('path')}")
        return manifest
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        if strict:
            raise
        return None


def claim_artifact(
    root: Path,
    key: str,
    owner_id: str,
    stale_after_seconds: int = 900,
) -> Literal["producer", "waiting", "hit"]:
    if not owner_id:
        raise ValueError("Artifact producer owner_id is required")
    if stale_after_seconds < 1:
        raise ValueError("stale_after_seconds must be positive")
    artifact = _artifact_dir(root, key)
    artifact.mkdir(parents=True, exist_ok=True)
    if _load_manifest(artifact, strict=False) is not None:
        return "hit"

    lease = artifact / _LEASE_NAME
    for _ in range(2):
        try:
            descriptor = os.open(lease, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            try:
                data = json.loads(lease.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                data = {}
            if data.get("owner_id") == owner_id:
                return "producer"
            try:
                age = time.time() - lease.stat().st_mtime
            except OSError:
                continue
            if age <= stale_after_seconds:
                return "waiting"
            try:
                lease.unlink()
            except FileNotFoundError:
                pass
            continue
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump({
                "owner_id": owner_id,
                "acquired_at": datetime.now(timezone.utc).isoformat(),
            }, handle)
            handle.flush()
            os.fsync(handle.fileno())
        return "producer"
    return "waiting"


def seal_artifact(root: Path, key: str, owner_id: str, manifest: dict) -> None:
    artifact = _artifact_dir(root, key)
    lease = artifact / _LEASE_NAME
    try:
        lease_data = json.loads(lease.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("Visual artifact producer lease is missing or corrupt") from exc
    if lease_data.get("owner_id") != owner_id:
        raise ValueError("Only the current visual artifact producer can seal it")

    requested_files = manifest.get("files")
    if not isinstance(requested_files, list) or not requested_files:
        raise ValueError("Visual artifact manifest requires at least one file")
    files = []
    for relative in requested_files:
        source = _safe_file(artifact, relative, must_exist=True)
        files.append({
            "path": relative,
            "bytes": source.stat().st_size,
            "sha256": _sha256(source),
        })
    approved = {
        **{key: value for key, value in manifest.items() if key not in {"state", "files"}},
        "state": "approved",
        "approved_at": datetime.now(timezone.utc).isoformat(),
        "files": files,
    }
    write_json_atomically(artifact / _MANIFEST_NAME, approved)
    lease.unlink(missing_ok=True)


def fail_artifact(root: Path, key: str, owner_id: str, failure_code: str) -> None:
    artifact = _artifact_dir(root, key)
    lease = artifact / _LEASE_NAME
    try:
        lease_data = json.loads(lease.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("Visual artifact producer lease is missing or corrupt") from exc
    if lease_data.get("owner_id") != owner_id:
        raise ValueError("Only the current visual artifact producer can fail it")
    write_json_atomically(artifact / _MANIFEST_NAME, {
        "state": "failed",
        "failure_code": str(failure_code),
        "failed_at": datetime.now(timezone.utc).isoformat(),
    })
    lease.unlink(missing_ok=True)


def materialize_artifact(root: Path, key: str, job_dir: Path) -> dict:
    artifact = _artifact_dir(root, key)
    manifest = _load_manifest(artifact, strict=True)
    if manifest is None:
        raise ValueError("Visual artifact is not approved")
    job_dir = Path(job_dir)
    job_dir.mkdir(parents=True, exist_ok=True)
    for item in manifest["files"]:
        relative = item["path"]
        source = _safe_file(artifact, relative, must_exist=True)
        target = _safe_file(job_dir, relative, must_exist=False)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_symlink():
            raise ValueError("Job artifact destination must not be a symlink")
        temporary = target.with_name(target.name + ".artifact-tmp")
        shutil.copyfile(source, temporary)
        if temporary.stat().st_size != item["bytes"] or _sha256(temporary) != item["sha256"]:
            temporary.unlink(missing_ok=True)
            raise ValueError(f"Materialized artifact failed integrity check: {relative}")
        os.replace(temporary, target)
    return manifest
