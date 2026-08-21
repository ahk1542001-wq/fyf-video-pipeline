"""Rate Limiting, Concurrency Management, and Budget Enforcement with Transactional Leases."""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request, status

from backend.budget_store import (
    get_budget_status,
    is_budget_available,
    reconcile_budget,
    release_reservation,
    reserve_budget,
)

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
        headers = getattr(request, "headers", {})
        forwarded_for = headers.get("x-forwarded-for") or headers.get("X-Forwarded-For")
        if forwarded_for:
            # Take the leftmost untrusted client IP
            client_ip = forwarded_for.split(",")[0].strip()
            if client_ip:
                return client_ip
        real_ip = headers.get("x-real-ip") or headers.get("X-Real-IP")
        if real_ip:
            client_ip = real_ip.strip()
            if client_ip:
                return client_ip

    if getattr(request, "client", None) and getattr(request.client, "host", None):
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
    """Check sliding window request count for an IP in the last 60 seconds."""
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
    """Check whether total active jobs in memory exceed configured limit."""
    max_concurrency = _get_max_concurrency()
    with _LOCK:
        mem_count = len(_ACTIVE_JOB_IDS)
        if mem_count >= max_concurrency:
            return False, f"System is currently busy ({mem_count}/{max_concurrency} active jobs). Please retry shortly."
        return True, None


def try_acquire_job_slot(job_id: str, job_roots: tuple[Path, ...] | None = None) -> tuple[bool, str | None]:
    """Atomically check and reserve a concurrency slot for a generation job."""
    max_concurrency = _get_max_concurrency()
    with _LOCK:
        if job_id in _ACTIVE_JOB_IDS:
            return True, None
        mem_count = len(_ACTIVE_JOB_IDS)
        if mem_count >= max_concurrency:
            return False, f"System is currently busy ({mem_count}/{max_concurrency} active generation jobs). Please retry shortly."
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


class GuardrailLease:
    """Transactional lease guaranteeing clean acquisition, reconciliation, and release."""

    def __init__(
        self,
        operation_id: str,
        estimated_usd: float = 0.05,
        slot_acquired: bool = False,
        budget_reserved: bool = False,
        root_dir: Path | None = None,
    ):
        self.operation_id = operation_id
        self.estimated_usd = estimated_usd
        self.slot_acquired = slot_acquired
        self.budget_reserved = budget_reserved
        self.root_dir = root_dir
        self.reconciled = False
        self.released = False

    def reconcile(self, actual_usd: float, outcome: str = "completed") -> dict[str, Any]:
        with _LOCK:
            if not self.reconciled:
                self.reconciled = True
                if self.slot_acquired:
                    _ACTIVE_JOB_IDS.discard(self.operation_id)
                    self.slot_acquired = False
                return reconcile_budget(
                    self.operation_id,
                    actual_usd,
                    outcome=outcome,
                    root_dir=self.root_dir,
                )
            return get_budget_status(root_dir=self.root_dir)

    def release(self) -> None:
        """Idempotently release all reserved slots and budget reservations."""
        with _LOCK:
            if not self.released and not self.reconciled:
                self.released = True
                if self.slot_acquired:
                    _ACTIVE_JOB_IDS.discard(self.operation_id)
                    self.slot_acquired = False
                if self.budget_reserved:
                    release_reservation(self.operation_id, root_dir=self.root_dir)
                    self.budget_reserved = False

    def __enter__(self) -> GuardrailLease:
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if not self.reconciled:
            self.release()


def acquire_guardrail_lease(
    operation_id: str,
    request: Request | None = None,
    client_ip: str | None = None,
    estimated_charge_usd: float = 0.05,
    root_dir: Path | None = None,
    job_roots: tuple[Path, ...] | None = None,
) -> GuardrailLease:
    """Transactionally acquire budget reservation and concurrency slot.

    Guarantees rollback of all acquired resources if any subsequent check fails.
    """
    ip = client_ip or get_client_ip(request)
    lease = GuardrailLease(
        operation_id=operation_id,
        estimated_usd=estimated_charge_usd,
        root_dir=root_dir,
    )

    # 1. Rate limit check first (zero state held on failure)
    rate_ok, rate_reason = check_rate_limit(ip)
    if not rate_ok:
        logger.warning("Generation blocked by rate limit for %s: %s", ip, rate_reason)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=rate_reason,
        )

    # 2. Budget reservation
    reserved, budget_reason = reserve_budget(
        operation_id,
        estimated_usd=estimated_charge_usd,
        root_dir=root_dir,
    )
    if not reserved:
        lease.release()
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Budget guardrail: {budget_reason}. Contact the operator.",
        )
    lease.budget_reserved = True

    # 3. Concurrency slot acquisition
    slot_ok, slot_reason = try_acquire_job_slot(operation_id, job_roots=job_roots)
    if not slot_ok:
        lease.release()  # Transactional rollback of budget reservation
        logger.warning("Generation blocked by concurrency slot: %s", slot_reason)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=slot_reason,
        )
    lease.slot_acquired = True

    return lease


def enforce_generation_guardrails(
    request: Request | None = None,
    client_ip: str | None = None,
    operation_id: str | None = None,
    estimated_charge_usd: float = 0.05,
    root_dir: Path | None = None,
    job_roots: tuple[Path, ...] | None = None,
) -> GuardrailLease | None:
    """Enforce guardrails transactionally, returning a lease when operation_id is provided."""
    if operation_id:
        return acquire_guardrail_lease(
            operation_id=operation_id,
            request=request,
            client_ip=client_ip,
            estimated_charge_usd=estimated_charge_usd,
            root_dir=root_dir,
            job_roots=job_roots,
        )

    ip = client_ip or get_client_ip(request)
    if not is_budget_available(estimated_charge_usd, root_dir=root_dir):
        budget_info = get_budget_status(root_dir=root_dir)
        reason = budget_info.get("reason") or "Generation budget cap reached"
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Budget guardrail: {reason}. Contact the operator.",
        )

    concurrency_ok, concurrency_reason = check_concurrency(job_roots=job_roots)
    if not concurrency_ok:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=concurrency_reason,
        )

    rate_ok, rate_reason = check_rate_limit(ip)
    if not rate_ok:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=rate_reason,
        )
    return None
