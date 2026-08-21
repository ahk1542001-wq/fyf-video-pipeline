"""Persisted asynchronous script production with narration and batch checkpoints."""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from backend.job_store import write_json_atomically
from backend.lock_store import create_script_lock
from backend.video_director import apply_director_pass
from backend.vertex_telemetry import telemetry_job_attempt, telemetry_scope

logger = logging.getLogger(__name__)

DEFAULT_SCRIPT_LOCK_BATCH_SIZE = 2
DEFAULT_SCRIPT_RETRY_BASE_SECONDS = 30.0
DEFAULT_SCRIPT_RETRY_MAX_SECONDS = 120.0
DEFAULT_SCRIPT_QUOTA_RETRY_BASE_SECONDS = 60.0
DEFAULT_SCRIPT_QUOTA_RETRY_MAX_SECONDS = 300.0
DEFAULT_SCRIPT_MAX_RETRIES = 4


def _provider_rate_limited(error: BaseException) -> bool:
    code = getattr(error, "code", None) or getattr(error, "status_code", None)
    if code in {429, 500, 502, 503, 504}:
        return True
    text = str(error).upper()
    return any(marker in text for marker in ("429", "RESOURCE_EXHAUSTED", "DEADLINE_EXCEEDED"))


def _script_max_retries() -> int:
    """Keep persisted script retries bounded while allowing quota recovery."""
    try:
        configured = int(os.getenv(
            "FYF_SCRIPT_MAX_RETRIES",
            str(DEFAULT_SCRIPT_MAX_RETRIES),
        ))
    except ValueError:
        configured = DEFAULT_SCRIPT_MAX_RETRIES
    return max(0, min(6, configured))


def _sleep_before_script_retry(attempt: int, *, rate_limited: bool = False) -> None:
    base_name = (
        "FYF_SCRIPT_QUOTA_RETRY_BASE_SECONDS"
        if rate_limited
        else "FYF_SCRIPT_RETRY_BASE_SECONDS"
    )
    max_name = (
        "FYF_SCRIPT_QUOTA_RETRY_MAX_SECONDS"
        if rate_limited
        else "FYF_SCRIPT_RETRY_MAX_SECONDS"
    )
    default_base = (
        DEFAULT_SCRIPT_QUOTA_RETRY_BASE_SECONDS
        if rate_limited
        else DEFAULT_SCRIPT_RETRY_BASE_SECONDS
    )
    default_max = (
        DEFAULT_SCRIPT_QUOTA_RETRY_MAX_SECONDS
        if rate_limited
        else DEFAULT_SCRIPT_RETRY_MAX_SECONDS
    )
    try:
        base_delay = float(os.getenv(
            base_name,
            str(default_base),
        ))
    except ValueError:
        base_delay = default_base
    try:
        max_delay = float(os.getenv(
            max_name,
            str(default_max),
        ))
    except ValueError:
        max_delay = default_max
    delay = min(
        max(0.0, base_delay) * (2 ** max(0, attempt)),
        max(0.0, max_delay),
    )
    if delay:
        time.sleep(delay)


def _read_json(path: Path) -> dict:
    import json
    return json.loads(path.read_text(encoding="utf-8"))


def update_script_status(job_dir: Path, **updates) -> dict:
    status_path = job_dir / "status.json"
    current = _read_json(status_path)
    current.update(updates)
    write_json_atomically(status_path, current)
    return current


def _script_lock_batch_size(job_dir: Path) -> int:
    """Use smaller default batches while preserving older checkpoint shapes."""
    try:
        configured = int(os.getenv(
            "FYF_SCRIPT_LOCK_BATCH_SIZE", str(DEFAULT_SCRIPT_LOCK_BATCH_SIZE)
        ))
    except ValueError:
        configured = DEFAULT_SCRIPT_LOCK_BATCH_SIZE
    configured = max(1, min(8, configured))

    try:
        status_batch_size = int(_read_json(job_dir / "status.json").get("batch_size", 0))
    except (OSError, TypeError, ValueError):
        status_batch_size = 0
    if 1 <= status_batch_size <= 8:
        return status_batch_size

    try:
        first_checkpoint = _read_json(job_dir / "locked-batch-000.json")
        checkpoint_size = len(first_checkpoint.get("segments", []))
    except (OSError, TypeError, ValueError):
        checkpoint_size = 0
    if 1 <= checkpoint_size <= 8:
        return checkpoint_size
    return configured


def run_script_pipeline(job_id: str, script_jobs_root: Path, locks_root: Path) -> None:
    job_dir = script_jobs_root / job_id
    try:
        prior_retries = int(_read_json(job_dir / "status.json").get("retry_count", 0))
    except (OSError, TypeError, ValueError):
        prior_retries = 0
    with telemetry_scope(job_id, "script", job_dir):
        with telemetry_job_attempt(prior_retries + 1):
            _run_script_pipeline(job_id, script_jobs_root, locks_root)


def _run_script_pipeline(job_id: str, script_jobs_root: Path, locks_root: Path) -> None:
    job_dir = script_jobs_root / job_id
    try:
        from writer_agent_vertex import generate_exact_lock, generate_narration_script
        from video_contract import StoryDraftScript, VideoScript

        request = _read_json(job_dir / "request.json")
        use_adk = request.get("use_adk_agent") or os.getenv("FYF_USE_ADK_AGENT", "false").lower() in ("true", "1")
        if use_adk:
            from backend.agent.runner import run_adk_pipeline
            update_script_status(job_dir, status="writing", stage="adk_producer", progress=15)
            adk_result = run_adk_pipeline(
                request["topic"],
                request.get("duration_mode", "short"),
                job_dir=job_dir,
            )
            result = adk_result["script"]
            lock_id = create_script_lock(locks_root, result)
            update_script_status(
                job_dir, status="completed", stage="locked", progress=100,
                lock_id=lock_id, error=None, restart_resumable=True,
            )
            return

        narration_path = job_dir / "narration.json"
        if narration_path.exists():
            draft = StoryDraftScript.model_validate(_read_json(narration_path))
        else:
            update_script_status(job_dir, status="writing", stage="narration", progress=5)
            draft = StoryDraftScript.model_validate(generate_narration_script(
                request["topic"], request.get("duration_mode", "short")
            ))
            write_json_atomically(narration_path, draft.model_dump(mode="json"))

        batch_size = _script_lock_batch_size(job_dir)
        batches = [draft.segments[i:i + batch_size] for i in range(0, len(draft.segments), batch_size)]
        merged: list[dict] = []
        for index, batch in enumerate(batches):
            checkpoint = job_dir / f"locked-batch-{index:03d}.json"
            if checkpoint.exists():
                locked = _read_json(checkpoint)
            else:
                update_script_status(
                    job_dir,
                    status="writing",
                    stage="storyboard",
                    progress=20 + round(70 * index / max(1, len(batches))),
                    batch=index + 1,
                    batch_count=len(batches),
                    batch_size=batch_size,
                )
                locked = generate_exact_lock({
                    "title": draft.title,
                    "approved_segments": [
                        {"id": segment.id, "text": segment.text} for segment in batch
                    ],
                })
                write_json_atomically(checkpoint, locked)
            merged.extend(locked["segments"])

        result = VideoScript.model_validate({
            "title": draft.title,
            "language": "my-MM",
            "segments": merged,
        }).model_dump(mode="json")
        result = apply_director_pass(result)
        lock_id = create_script_lock(locks_root, result)
        write_json_atomically(job_dir / "result.json", result)
        update_script_status(
            job_dir, status="completed", stage="locked", progress=100,
            lock_id=lock_id, error=None, restart_resumable=True,
        )
    except Exception as exc:
        logger.exception("Script job %s failed", job_id)
        current = _read_json(job_dir / "status.json")
        retry_count = int(current.get("retry_count", 0))
        if retry_count < _script_max_retries():
            update_script_status(
                job_dir, status="queued", stage="retrying",
                retry_count=retry_count + 1, error=None, restart_resumable=True,
            )
            _sleep_before_script_retry(retry_count, rate_limited=_provider_rate_limited(exc))
            return run_script_pipeline(job_id, script_jobs_root, locks_root)
        update_script_status(
            job_dir, status="failed", error="Script production failed",
            restart_resumable=(job_dir / "narration.json").exists(),
        )
