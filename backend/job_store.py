import os
import json
import uuid
import re
import socket
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, Any, Literal


JOB_LEASE_FILENAME = "pipeline.lease"
_RENDER_PROGRESS_STRATEGIES = {
    "segmented",
    "monolithic",
    "monolithic-fallback",
}
_RENDER_PROGRESS_FIELDS = {
    "strategy",
    "total",
    "rendered",
    "cache_hits",
    "manifest_fingerprint",
    "fallback_reason",
}
_QA_PROGRESS_FIELDS = {"total", "verified", "cache_hits", "batches"}


def _validate_render_progress(progress: Any) -> Dict[str, Any] | None:
    if progress is None:
        return None
    if not isinstance(progress, dict):
        raise ValueError("render_progress must be an object or null")

    unknown = set(progress) - _RENDER_PROGRESS_FIELDS
    if unknown:
        raise ValueError(f"Unknown render_progress fields: {sorted(unknown)}")

    strategy = progress.get("strategy")
    if strategy not in _RENDER_PROGRESS_STRATEGIES:
        raise ValueError(f"Invalid render_progress strategy: {strategy!r}")

    counters: dict[str, int] = {}
    for field in ("total", "rendered", "cache_hits"):
        value = progress.get(field)
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"render_progress.{field} must be a non-negative integer")
        if value < 0:
            raise ValueError(f"render_progress.{field} must be a non-negative integer")
        counters[field] = value

    if counters["rendered"] > counters["total"]:
        raise ValueError("render_progress.rendered cannot exceed total")
    if counters["cache_hits"] > counters["total"]:
        raise ValueError("render_progress.cache_hits cannot exceed total")
    if counters["rendered"] + counters["cache_hits"] > counters["total"]:
        raise ValueError("render_progress.rendered plus cache_hits cannot exceed total")

    manifest_fingerprint = progress.get("manifest_fingerprint")
    if manifest_fingerprint is not None and (
        not isinstance(manifest_fingerprint, str)
        or not re.fullmatch(r"[0-9a-f]{64}", manifest_fingerprint)
    ):
        raise ValueError("render_progress.manifest_fingerprint must be lowercase SHA-256")

    fallback_reason = progress.get("fallback_reason")
    if fallback_reason is not None and not isinstance(fallback_reason, str):
        raise ValueError("render_progress.fallback_reason must be a string")

    return dict(progress)


def _validate_qa_progress(progress: Any) -> Dict[str, Any] | None:
    if progress is None:
        return None
    if not isinstance(progress, dict):
        raise ValueError("qa_progress must be an object or null")
    unknown = set(progress) - _QA_PROGRESS_FIELDS
    if unknown:
        raise ValueError(f"Unknown qa_progress fields: {sorted(unknown)}")

    counters: dict[str, int] = {}
    for field in sorted(_QA_PROGRESS_FIELDS):
        value = progress.get(field)
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"qa_progress.{field} must be a non-negative integer")
        if value < 0:
            raise ValueError(f"qa_progress.{field} must be a non-negative integer")
        counters[field] = value
    if counters["verified"] > counters["total"]:
        raise ValueError("qa_progress.verified cannot exceed total")
    if counters["cache_hits"] > counters["total"]:
        raise ValueError("qa_progress.cache_hits cannot exceed total")
    return dict(progress)


def _visual_progress(job_dir: Path) -> Dict[str, Any] | None:
    try:
        status_data = json.loads((job_dir / "status.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        status_data = {}
    evidence_checkpoints = [job_dir / "visual_evidence_checkpoint.json"]
    director_checkpoints = [job_dir / "director_treatment_checkpoint.json"]
    artifact_key = status_data.get("visual_artifact_key")
    if (
        isinstance(artifact_key, str)
        and re.fullmatch(r"[0-9a-f]{64}", artifact_key)
    ):
        jobs_root = job_dir.parent
        artifact_root = (
            jobs_root.parent / "visual-artifacts"
            if jobs_root.name == "jobs"
            else jobs_root / ".visual-artifacts"
        )
        director_checkpoints.append(
            artifact_root / artifact_key / "director_treatment_checkpoint.json"
        )
        evidence_checkpoints.append(
            artifact_root / artifact_key / "visual_evidence_checkpoint.json"
        )
    has_evidence_progress = any(path.is_file() for path in evidence_checkpoints)
    for director_checkpoint in director_checkpoints:
        if not director_checkpoint.is_file():
            continue
        try:
            checkpoint = json.loads(director_checkpoint.read_text(encoding="utf-8"))
            total = int(checkpoint.get("total_shot_count", 0))
            planned = len(checkpoint.get("completed_shot_ids") or [])
            batch_size = int(checkpoint.get("batch_size", 0))
            completed_batches = int(checkpoint.get("completed_batch_count", 0))
            if total > 0 and batch_size > 0:
                if checkpoint.get("complete") is True and has_evidence_progress:
                    continue
                return {
                    "planned": planned,
                    "total": total,
                    "completed_batches": completed_batches,
                    "total_batches": (total + batch_size - 1) // batch_size,
                    "cache_state": status_data.get("visual_cache_state"),
                    "retry_count": int(checkpoint.get("retry_count", 0)),
                    "current_failed_ids": [
                        value for value in (checkpoint.get("current_failed_ids") or [])
                        if isinstance(value, str)
                    ],
                }
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            continue
    for checkpoint_path in evidence_checkpoints:
        if not checkpoint_path.is_file():
            continue
        try:
            checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            segments = checkpoint.get("script", {}).get("segments", [])
            shots = [
                shot for segment in segments
                for shot in (segment.get("visual") or {}).get("evidence_shots", [])
                if isinstance(shot, dict)
            ]
        except (OSError, json.JSONDecodeError, AttributeError):
            continue
        if not shots:
            continue
        passed = sum(shot.get("verification_status") == "passed" for shot in shots)
        fallbacks = sum(bool(shot.get("fallback_used")) for shot in shots)
        return {
            "passed": passed, "total": len(shots), "fallbacks": fallbacks,
            "percent": round(100 * passed / len(shots)),
        }
    return None

def generate_job_id() -> str:
    """Generate exactly 8 lowercase hex."""
    return uuid.uuid4().hex[:8].lower()

def is_valid_job_id(job_id: str) -> bool:
    """Validates job IDs exactly 8 lowercase hex."""
    return bool(re.fullmatch(r"[0-9a-f]{8}", job_id))

def create_job_dir(jobs_root: Path) -> str:
    """Creates unique job dirs with mkdir exist_ok false and bounded UUID retries."""
    for _ in range(10):
        job_id = generate_job_id()
        job_dir = jobs_root / job_id
        try:
            job_dir.mkdir(parents=True, exist_ok=False)
            return job_id
        except FileExistsError:
            continue
    raise RuntimeError("Failed to create a unique job directory after 10 attempts.")

def write_json_atomically(filepath: Path, data: Any) -> None:
    """Writes any JSON atomically through a sibling temporary file with flush fsync os.replace and cleanup."""
    if not filepath.parent.exists():
        raise FileNotFoundError(f"Parent directory {filepath.parent} does not exist.")
    temp_path = filepath.with_suffix(filepath.suffix + f".tmp.{uuid.uuid4().hex[:8]}")
    try:
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, filepath)
    finally:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass

def read_job_status(job_dir: Path) -> Dict[str, Any]:
    """Reads allowlisted status fields only."""
    status_path = job_dir / "status.json"

    if not status_path.exists():
        raise FileNotFoundError(f"Status file not found in {job_dir}")

    try:
        with open(status_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        result = {
            "job_id": data.get("job_id"),
            "status": data.get("status", "queued"),
            "created_at": data.get("created_at"),
            "updated_at": data.get("updated_at"),
            "video_url": data.get("video_url"),
            "error": data.get("error"),
            "qa_report": data.get("qa_report"),
            "creative_qa": data.get("creative_qa"),
            "final_visual_qa": data.get("final_visual_qa"),
            "voice_provider": data.get("voice_provider"),
            "resume_count": int(data.get("resume_count", 0)),
            "attempt_count": int(data.get("attempt_count", 0)),
            "restart_resumable": bool(data.get("restart_resumable", False)),
            "visual_artifact_key": data.get("visual_artifact_key"),
            "visual_cache_state": data.get("visual_cache_state"),
            "stage_timings": data.get("stage_timings", {}),
            "paired_source_job_id": data.get("paired_source_job_id"),
        }
        raw_render_progress = data.get("render_progress")
        if raw_render_progress is not None:
            result["render_progress"] = _validate_render_progress(raw_render_progress)
        raw_qa_progress = data.get("qa_progress")
        if raw_qa_progress is not None:
            result["qa_progress"] = _validate_qa_progress(raw_qa_progress)
        result["visual_progress"] = _visual_progress(job_dir)
        return result
    except json.JSONDecodeError as e:
        raise ValueError(f"Corrupt status file: {e}")

def initialize_job_status(
    job_dir: Path,
    job_id: str,
    voice_provider: Literal["gemini"] | None = "gemini",
) -> Dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    status_data = {
        "job_id": job_id,
        "status": "queued",
        "created_at": now,
        "updated_at": now,
        "video_url": None,
        "error": None,
        "qa_report": None,
        "creative_qa": None,
        "final_visual_qa": None,
        "voice_provider": voice_provider,
        "resume_count": 0,
        "attempt_count": 0,
        "restart_resumable": True,
        "visual_progress": None,
        "visual_artifact_key": None,
        "visual_cache_state": None,
        "stage_timings": {},
        "paired_source_job_id": None,
    }
    write_json_atomically(job_dir / "status.json", status_data)
    return status_data

def update_job_status(job_dir: Path, updates: Dict[str, Any]) -> Dict[str, Any]:
    """Updates job status and writes it atomically."""
    current = read_job_status(job_dir)

    # Apply allowlisted updates
    allowed_keys = {
        "status", "video_url", "error", "qa_report", "creative_qa", "final_visual_qa",
        "voice_provider", "resume_count", "restart_resumable",
        "attempt_count", "visual_artifact_key", "visual_cache_state", "stage_timings",
        "paired_source_job_id", "render_progress", "qa_progress",
    }
    if "render_progress" in updates:
        _validate_render_progress(updates["render_progress"])
    if "qa_progress" in updates:
        _validate_qa_progress(updates["qa_progress"])
    for k, v in updates.items():
        if k in allowed_keys:
            if k == "final_visual_qa" and v is None and current.get(k) is not None:
                continue
            if k == "render_progress":
                if v is None:
                    current.pop(k, None)
                else:
                    current[k] = dict(v)
                continue
            if k == "qa_progress":
                if v is None:
                    current.pop(k, None)
                else:
                    current[k] = dict(v)
                continue
            current[k] = v

    valid_statuses = {
        "queued", "visuals", "voice", "rendering", "qa", "creative_qa",
        "retrying", "needs_attention", "completed", "failed", "needs_human_review",
    }
    if current["status"] not in valid_statuses:
        raise ValueError(f"Invalid status: {current['status']}")

    current["updated_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    if "restart_resumable" not in updates:
        if current["status"] == "completed":
            current["restart_resumable"] = False
        elif current["status"] in {"needs_attention", "retrying", "needs_human_review", "queued", "visuals", "voice", "rendering", "qa", "creative_qa"}:
            current["restart_resumable"] = True

    # Visual progress is derived from the atomic evidence checkpoint. Do not
    # copy that snapshot into status.json where it can become stale.
    current.pop("visual_progress", None)

    write_json_atomically(job_dir / "status.json", current)
    return read_job_status(job_dir)


def begin_job_attempt(job_dir: Path) -> Dict[str, Any]:
    """Expose a clean current attempt while retaining prior reports on disk."""
    current = read_job_status(job_dir)
    current.update({
        "status": "visuals",
        "video_url": None,
        "error": None,
        "qa_report": None,
        "creative_qa": None,
        "final_visual_qa": None,
        "restart_resumable": True,
        "attempt_count": int(current.get("attempt_count", 0)) + 1,
        "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    })
    current.pop("visual_progress", None)
    current.pop("render_progress", None)
    current.pop("qa_progress", None)
    write_json_atomically(job_dir / "status.json", current)
    return read_job_status(job_dir)


def _process_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def acquire_job_lease(job_dir: Path) -> str | None:
    """Atomically acquire a local cross-process lease for one persisted job."""
    lease_path = job_dir / JOB_LEASE_FILENAME
    token = uuid.uuid4().hex
    payload = {
        "token": token,
        "pid": os.getpid(),
        "host": socket.gethostname(),
        "acquired_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    for _ in range(2):
        try:
            with open(lease_path, "x", encoding="utf-8") as lease_file:
                json.dump(payload, lease_file)
                lease_file.flush()
                os.fsync(lease_file.fileno())
            return token
        except FileExistsError:
            try:
                existing = json.loads(lease_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return None
            same_host = existing.get("host") == socket.gethostname()
            if not same_host or _process_is_alive(int(existing.get("pid", 0))):
                return None
            try:
                lease_path.unlink()
            except FileNotFoundError:
                pass
    return None


def release_job_lease(job_dir: Path, token: str | None) -> None:
    """Release only the lease owned by the supplied opaque token."""
    if not token:
        return
    lease_path = job_dir / JOB_LEASE_FILENAME
    try:
        existing = json.loads(lease_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return
    if existing.get("token") == token:
        lease_path.unlink(missing_ok=True)
