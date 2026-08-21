"""Rate Limiting, Concurrency Management, and Budget Enforcement."""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request, status

from backend.budget_store import get_budget_status, is_budget_available, reserve_budget

logger = logging.getLogger(__name__)

DEFAULT_MAX_CONCURRENT_JOBS = 1
DEFAULT_RATE_LIMIT_PER_MINUTE = 10

_LOCK = threading.Lock()
# In-memory sliding window tracking: {ip: [timestamp, ...]}
_RATE_LIMIT_WINDOW: dict[str, list[float]] = defaultdict(list)
_ACTIVE_JOB_IDS: set[str] = set()


def get_client_ip(request: Request | None) -> str:
    """Extract client IP address, guarding against spoofed headers unless explicitly configured."""
    if request is None:
        return "127.0.0.1"

    trust_proxies = os.getenv("FYF_TRUST_PROXY_HEADERS", "false").lower() in ("true", "1")
    if trust_proxies:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
        real_ip = request.headers.get("x-real-ip")
        if real_ip:
            return real_ip.strip()

    if request.client and request.client.host:
        return request.client.host
    return "127.0.0.1"


def _get_max_concurrency() -> int:
    try:
        return int(os.getenv("FYF_MAX_CONCURRENT_JOBS", str(DEFAULT_MAX_CONCURRENT_JOBS)))
    except ValueError:
        return DEFAULT_MAX_CONCURRENT_JOBS


def _get_rate_limit_per_minute() -> int:
    try:
        return int(os.getenv("FYF_RATE_LIMIT_PER_MINUTE", str(DEFAULT_RATE_LIMIT_PER_MINUTE)))
    except ValueError:
        return DEFAULT_RATE_LIMIT_PER_MINUTE


def check_rate_limit(client_ip: str) -> tuple[bool, str | None]:
    """Check sliding window request count for an IP in the last 60 seconds with thread safety."""
    now = time.time()
    limit = _get_rate_limit_per_minute()

    with _LOCK:
        window = _RATE_LIMIT_WINDOW[client_ip]
        # Prune timestamps older than 60 seconds
        _RATE_LIMIT_WINDOW[client_ip] = [ts for ts in window if now - ts < 60.0]
        current_count = len(_RATE_LIMIT_WINDOW[client_ip])

        if current_count >= limit:
            return False, f"Rate limit exceeded ({limit} requests/minute). Please wait before submitting again."

        _RATE_LIMIT_WINDOW[client_ip].append(now)
        return True, None


def count_active_disk_jobs(job_roots: tuple[Path, ...] | None = None, excluding_job_id: str | None = None) -> int:
    """Count active uncompleted jobs across script and video job roots."""
    import json
    if job_roots is None:
        jobs_root = Path(os.getenv("FYF_JOBS_ROOT", "jobs"))
        script_jobs_root = Path(os.getenv("FYF_SCRIPT_JOBS_ROOT", "script-jobs"))
        job_roots = (jobs_root, script_jobs_root)

    active_count = 0
    active_statuses = {
        "queued", "writing", "adk_producer", "retrying",
        "visuals", "voice", "rendering", "qa", "creative_qa",
    }
    for root in job_roots:
        if not root.is_dir():
            continue
        for job_dir in root.iterdir():
            if not job_dir.is_dir() or (excluding_job_id and job_dir.name == excluding_job_id):
                continue
            status_file = job_dir / "status.json"
            if not status_file.is_file():
                continue
            try:
                data = json.loads(status_file.read_text(encoding="utf-8"))
                if data.get("status") in active_statuses:
                    active_count += 1
            except (OSError, json.JSONDecodeError):
                continue
    return active_count


def check_concurrency(job_roots: tuple[Path, ...] | None = None, excluding_job_id: str | None = None) -> tuple[bool, str | None]:
    """Check whether total active jobs (memory + disk) exceed configured limit."""
    max_concurrency = _get_max_concurrency()
    with _LOCK:
        mem_count = len(_ACTIVE_JOB_IDS)
    disk_count = count_active_disk_jobs(job_roots, excluding_job_id=excluding_job_id)
    total_active = max(mem_count, disk_count)

    if total_active >= max_concurrency:
        return False, f"System is currently busy ({total_active}/{max_concurrency} active jobs). Please retry shortly."
    return True, None


def try_acquire_job_slot(job_id: str, job_roots: tuple[Path, ...] | None = None) -> tuple[bool, str | None]:
    """Atomically check and reserve a concurrency slot for a generation job."""
    max_concurrency = _get_max_concurrency()
    with _LOCK:
        if job_id in _ACTIVE_JOB_IDS:
            return True, None
        disk_count = count_active_disk_jobs(job_roots, excluding_job_id=job_id)
        mem_count = len(_ACTIVE_JOB_IDS)
        total_active = max(mem_count, disk_count)
        if total_active >= max_concurrency:
            return False, f"System is currently busy ({total_active}/{max_concurrency} active generation jobs). Please retry shortly."
        _ACTIVE_JOB_IDS.add(job_id)
        return True, None


def register_active_job(job_id: str) -> None:
    """Track an active generation job with thread safety."""
    with _LOCK:
        _ACTIVE_JOB_IDS.add(job_id)


def release_active_job(job_id: str) -> None:
    """Release a completed, failed, or cancelled generation job from concurrency tracker."""
    with _LOCK:
        _ACTIVE_JOB_IDS.discard(job_id)


def get_active_job_count() -> int:
    with _LOCK:
        return len(_ACTIVE_JOB_IDS)


def clear_limits_state() -> None:
    """Clear memory limits state for test isolation."""
    with _LOCK:
        _RATE_LIMIT_WINDOW.clear()
        _ACTIVE_JOB_IDS.clear()


def enforce_generation_guardrails(
    request: Request | None = None,
    client_ip: str | None = None,
    operation_id: str | None = None,
    estimated_charge_usd: float = 0.05,
    root_dir: Path | None = None,
    job_roots: tuple[Path, ...] | None = None,
) -> None:
    """Enforce budget reservations, rate limits, and concurrency guards before queuing work.

    Raises:
        HTTPException with 429 status code if any guardrail is tripped.
    """
    ip = client_ip or get_client_ip(request)

    # 1. Budget reservation check (fail-closed)
    if operation_id:
        reserved, reason = reserve_budget(operation_id, estimated_usd=estimated_charge_usd, root_dir=root_dir)
        if not reserved:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Budget guardrail: {reason}. Contact the operator.",
            )
    else:
        if not is_budget_available(estimated_charge_usd, root_dir=root_dir):
            budget_info = get_budget_status(root_dir=root_dir)
            reason = budget_info.get("reason") or "Generation budget cap reached"
            logger.warning("Generation blocked by budget cap: %s", budget_info)
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Budget guardrail: {reason}. Contact the operator.",
            )

    # 2. Concurrency slot reservation
    if operation_id:
        slot_ok, slot_reason = try_acquire_job_slot(operation_id, job_roots=job_roots)
        if not slot_ok:
            logger.warning("Generation blocked by concurrency slot: %s", slot_reason)
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=slot_reason,
            )
    else:
        concurrency_ok, concurrency_reason = check_concurrency(job_roots=job_roots)
        if not concurrency_ok:
            logger.warning("Generation blocked by concurrency limit: %s", concurrency_reason)
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=concurrency_reason,
            )

    # 3. Rate limit check per client IP
    rate_ok, rate_reason = check_rate_limit(ip)
    if not rate_ok:
        logger.warning("Generation blocked by rate limit for %s: %s", ip, rate_reason)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=rate_reason,
        )
