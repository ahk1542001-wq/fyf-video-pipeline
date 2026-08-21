"""Rate Limiting, Concurrency Management, and Budget Enforcement."""

from __future__ import annotations

import logging
import os
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from fastapi import HTTPException, status

from backend.budget_store import get_budget_status, is_budget_available

logger = logging.getLogger(__name__)

DEFAULT_MAX_CONCURRENT_JOBS = 2
DEFAULT_RATE_LIMIT_PER_MINUTE = 10

# In-memory sliding window tracking: {ip: [timestamp, ...]}
_RATE_LIMIT_WINDOW: dict[str, list[float]] = defaultdict(list)
_ACTIVE_JOB_IDS: set[str] = set()


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
    """Check sliding window request count for an IP in the last 60 seconds."""
    now = time.time()
    limit = _get_rate_limit_per_minute()
    window = _RATE_LIMIT_WINDOW[client_ip]

    # Prune timestamps older than 60 seconds
    _RATE_LIMIT_WINDOW[client_ip] = [ts for ts in window if now - ts < 60.0]
    current_count = len(_RATE_LIMIT_WINDOW[client_ip])

    if current_count >= limit:
        return False, f"Rate limit exceeded ({limit} requests/minute). Please wait before submitting again."

    _RATE_LIMIT_WINDOW[client_ip].append(now)
    return True, None


def check_concurrency() -> tuple[bool, str | None]:
    """Check whether concurrent generation jobs exceed configured limit."""
    max_concurrency = _get_max_concurrency()
    if len(_ACTIVE_JOB_IDS) >= max_concurrency:
        return False, f"System is currently busy ({len(_ACTIVE_JOB_IDS)}/{max_concurrency} active jobs). Please retry shortly."
    return True, None


def register_active_job(job_id: str) -> None:
    """Track an active generation job."""
    _ACTIVE_JOB_IDS.add(job_id)


def release_active_job(job_id: str) -> None:
    """Release a completed or failed generation job from concurrency tracker."""
    _ACTIVE_JOB_IDS.discard(job_id)


def get_active_job_count() -> int:
    return len(_ACTIVE_JOB_IDS)


def clear_limits_state() -> None:
    """Clear memory limits state for test isolation."""
    _RATE_LIMIT_WINDOW.clear()
    _ACTIVE_JOB_IDS.clear()


def enforce_generation_guardrails(
    client_ip: str = "127.0.0.1",
    estimated_charge_usd: float = 0.05,
    root_dir: Path | None = None,
) -> None:
    """Enforce budget caps, rate limits, and concurrency guards before queuing work.
    
    Raises:
        HTTPException with 429 status code if any guardrail is tripped.
    """
    # 1. Budget check
    if not is_budget_available(estimated_charge_usd, root_dir=root_dir):
        budget_info = get_budget_status(root_dir=root_dir)
        reason = budget_info.get("reason") or "Generation budget cap reached"
        logger.warning("Generation blocked by budget cap: %s", budget_info)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Budget guardrail: {reason}. Contact the operator.",
        )

    # 2. Concurrency check
    concurrency_ok, concurrency_reason = check_concurrency()
    if not concurrency_ok:
        logger.warning("Generation blocked by concurrency limit: %s", concurrency_reason)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=concurrency_reason,
        )

    # 3. Rate limit check
    rate_ok, rate_reason = check_rate_limit(client_ip)
    if not rate_ok:
        logger.warning("Generation blocked by rate limit for %s: %s", client_ip, rate_reason)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=rate_reason,
        )
