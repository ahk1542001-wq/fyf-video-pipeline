"""Long-running script job persistence and resume pipeline for Vertex AI."""

from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

_repo_root = Path(__file__).resolve().parent.parent
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))
_backend_root = _repo_root / "backend"
if str(_backend_root) not in sys.path:
    sys.path.insert(0, str(_backend_root))

from backend.job_store import write_json_atomically
from backend.lock_store import create_script_lock
from backend.vertex_telemetry import (
    telemetry_job_attempt,
    telemetry_scope,
)

logger = logging.getLogger(__name__)

DEFAULT_SCRIPT_MAX_RETRIES = 3
DEFAULT_SCRIPT_RETRY_BASE_SECONDS = 30.0
DEFAULT_SCRIPT_RETRY_MAX_SECONDS = 120.0
DEFAULT_SCRIPT_QUOTA_RETRY_BASE_SECONDS = 60.0
DEFAULT_SCRIPT_QUOTA_RETRY_MAX_SECONDS = 300.0
DEFAULT_SCRIPT_LOCK_BATCH_SIZE = 2


def _is_transient_error(error: BaseException) -> bool:
    """Classify transient provider/network issues vs terminal validation/contract failures."""
    code = getattr(error, "code", None) or getattr(error, "status_code", None)
    if code in (429, 500, 502, 503, 504):
        return True
    if isinstance(error, (TimeoutError, ConnectionError, OSError)):
        return True
    text = str(error).upper()
    transient_markers = (
        "429", "500", "502", "503", "504",
        "RESOURCE_EXHAUSTED", "DEADLINE_EXCEEDED", "UNAVAILABLE",
        "TIMEOUT", "TIMED OUT", "CONNECTION RESET", "SERVICE UNAVAILABLE",
    )
    return any(marker in text for marker in transient_markers)


def _provider_rate_limited(error: BaseException) -> bool:
    code = getattr(error, "code", None) or getattr(error, "status_code", None)
    if code == 429:
        return True
    text = str(error).upper()
    return any(marker in text for marker in ("429", "RESOURCE_EXHAUSTED"))


def _terminal_error_message(error: BaseException) -> str:
    """Expose a safe, actionable terminal state without returning raw provider errors."""
    code = getattr(error, "code", None) or getattr(error, "status_code", None)
    message = str(error).upper()
    if code in (401, 403) or "401" in message or "403" in message or "PERMISSION_DENIED" in message:
        return "Vertex authorization was rejected. The operator must configure approved provider credentials."
    return "Script validation or contract failure."


def _script_max_retries() -> int:
    """Keep script retries bounded to 0-3 (never permit above 3)."""
    try:
        configured = int(os.getenv(
            "FYF_SCRIPT_MAX_RETRIES",
            str(DEFAULT_SCRIPT_MAX_RETRIES),
        ))
    except ValueError:
        configured = DEFAULT_SCRIPT_MAX_RETRIES
    return max(0, min(3, configured))


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

    from backend.runtime_limits import register_active_job, release_active_job
    register_active_job(job_id)
    try:
        with telemetry_scope(job_id, "script", job_dir):
            with telemetry_job_attempt(prior_retries + 1):
                _run_script_pipeline(job_id, script_jobs_root, locks_root)
    finally:
        release_active_job(job_id)
        from backend.budget_store import release_reservation
        release_reservation(job_id)


def _run_script_pipeline(job_id: str, script_jobs_root: Path, locks_root: Path) -> None:
    job_dir = script_jobs_root / job_id
    try:
        from writer_agent_vertex import generate_exact_lock, generate_narration_script
        from video_contract import StoryDraftScript, VideoScript

        request = _read_json(job_dir / "request.json")
        use_adk = request.get("use_adk_agent", True) and os.getenv("FYF_USE_ADK_AGENT", "true").lower() in ("true", "1")
        if use_adk:
            from backend.agent.runner import run_adk_pipeline
            update_script_status(job_dir, status="writing", stage="adk_producer", progress=15)
            adk_result = run_adk_pipeline(
                request["topic"],
                request.get("duration_mode", "short"),
                job_dir=job_dir,
            )
            result = VideoScript.model_validate(adk_result["script"]).model_dump(
                mode="json", exclude_none=True
            )
            lock_id = create_script_lock(locks_root, result)
            write_json_atomically(job_dir / "result.json", result)
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

        segments = draft.segments
        batch_size = _script_lock_batch_size(job_dir)
        total_batches = max(1, (len(segments) + batch_size - 1) // batch_size)
        locked_segments = []

        for batch_index in range(total_batches):
            checkpoint_path = job_dir / f"locked-batch-{batch_index:03d}.json"
            if checkpoint_path.exists():
                batch_data = VideoScript.model_validate(_read_json(checkpoint_path))
                locked_segments.extend(batch_data.segments)
                continue

            start = batch_index * batch_size
            batch_slice = segments[start:start + batch_size]
            progress = 10 + int(85 * ((batch_index + 1) / total_batches))
            update_script_status(
                job_dir,
                status="writing",
                stage="visual_lock",
                progress=progress,
                batch=batch_index + 1,
                batch_count=total_batches,
                batch_size=batch_size,
            )
            batch_script = VideoScript.model_validate(generate_exact_lock({
                "title": draft.title,
                "approved_segments": [
                    {"id": item.id, "text": item.text}
                    for item in batch_slice
                ],
            }))
            write_json_atomically(checkpoint_path, batch_script.model_dump(mode="json"))
            locked_segments.extend(batch_script.segments)

        final_script = VideoScript(
            title=draft.title,
            language=draft.language,
            segments=locked_segments,
        ).model_dump(mode="json")

        lock_id = create_script_lock(locks_root, final_script)
        write_json_atomically(job_dir / "result.json", final_script)
        update_script_status(
            job_dir,
            status="completed",
            stage="locked",
            progress=100,
            batch=total_batches,
            batch_count=total_batches,
            lock_id=lock_id,
            error=None,
            restart_resumable=True,
        )

    except Exception as exc:
        status_info = _read_json(job_dir / "status.json")
        retry_count = int(status_info.get("retry_count", 0))
        max_retries = _script_max_retries()
        is_transient = _is_transient_error(exc)

        logger.warning(
            "Script pipeline job %s attempt %s encountered %s error: %s",
            job_id,
            retry_count + 1,
            "transient" if is_transient else "terminal non-transient",
            exc,
        )

        if is_transient and retry_count < max_retries:
            update_script_status(
                job_dir,
                status="retrying",
                stage="retrying",
                retry_count=retry_count + 1,
                error="Script generation encountered a transient provider issue. Retrying...",
                restart_resumable=True,
            )
            _sleep_before_script_retry(retry_count, rate_limited=_provider_rate_limited(exc))
            _run_script_pipeline(job_id, script_jobs_root, locks_root)
            return

        if is_transient:
            update_script_status(
                job_dir,
                status="needs_attention",
                stage="needs_attention",
                retry_count=retry_count,
                error="Provider temporarily unavailable. Retries exhausted. Checkpoint preserved for manual resume.",
                restart_resumable=True,
            )
        else:
            update_script_status(
                job_dir,
                status="failed",
                stage="failed",
                retry_count=retry_count,
                error=_terminal_error_message(exc),
                restart_resumable=False,
            )
