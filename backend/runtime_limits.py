"""Rate Limiting, Concurrency Management, Budget Enforcement, and Provider-Operation Gating.

Stage B-III additions
---------------------
* Capacity limits (concurrency, rate) now delegate to ``backend.capacity_config``
  so ``FYF_MAX_CONCURRENT_JOBS`` / ``FYF_RATE_LIMIT_PER_MINUTE`` have one owner.
* Every paid-dispatch refusal raises an HTTPException carrying an explicit,
  machine-readable reason code in the ``X-FYF-Rejection-Reason`` response header
  so a caller can distinguish: paid production *disabled* (no explicit ceiling)
  vs budget *exceeded* (ceiling reached) vs *insufficient* (remaining < estimate)
  vs *rate limited* vs *queue full* (concurrency slot busy => honest queued state).
* ``ProviderOperationRegistry`` (B8) records a provider operation ID for every
  paid dispatch and gates paid retries: a re-dispatch of an operation the provider
  already reported as succeeded/billed is refused, so no second paid call is issued.
"""

from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass
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
from backend.capacity_config import load_capacity_config
from backend.job_store import write_json_atomically

logger = logging.getLogger(__name__)

# Retained for backward compatibility; capacity_config owns the real defaults.
DEFAULT_MAX_CONCURRENT_JOBS = 1
DEFAULT_RATE_LIMIT_PER_MINUTE = 10

# ---------------------------------------------------------------------------
# Rejection-reason taxonomy (B11 + fail-closed budget wiring).
# Surfaced in the ``X-FYF-Rejection-Reason`` response header of every refusal so
# a caller can tell WHY paid work was refused instead of guessing from a string.
# ---------------------------------------------------------------------------
REJECTION_HEADER = "X-FYF-Rejection-Reason"
QUEUE_DEPTH_HEADER = "X-FYF-Queue-Depth"
QUEUE_POSITION_HEADER = "X-FYF-Queue-Position"

REASON_RATE_LIMITED = "rate_limited"
REASON_PAID_DISABLED = "paid_production_disabled"
REASON_BUDGET_EXCEEDED = "budget_exceeded"
REASON_BUDGET_INSUFFICIENT = "budget_insufficient"
REASON_QUEUE_FULL = "queue_full"
REASON_CAPACITY_LIMIT = "capacity_limit_exceeded"

REJECTION_REASON_CODES = frozenset({
    REASON_RATE_LIMITED,
    REASON_PAID_DISABLED,
    REASON_BUDGET_EXCEEDED,
    REASON_BUDGET_INSUFFICIENT,
    REASON_QUEUE_FULL,
    REASON_CAPACITY_LIMIT,
})

_LOCK = threading.Lock()
# In-memory sliding window tracking: {ip: [timestamp, ...]}
_RATE_LIMIT_WINDOW: dict[str, list[float]] = defaultdict(list)
_ACTIVE_JOB_IDS: set[str] = set()


def _rejection(
    reason_code: str,
    detail: str,
    *,
    status_code: int = status.HTTP_429_TOO_MANY_REQUESTS,
    extra_headers: dict[str, str] | None = None,
) -> None:
    """Raise an HTTPException whose body names the exact guardrail that fired.

    The human-readable ``detail`` is preserved verbatim for existing callers; the
    machine-readable ``reason_code`` is added as a response header so a client can
    distinguish disabled / exceeded / insufficient / rate-limited / queue-full.
    """
    headers = {REJECTION_HEADER: reason_code}
    if extra_headers:
        headers.update({k: str(v) for k, v in extra_headers.items()})
    raise HTTPException(status_code=status_code, detail=detail, headers=headers)


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
    """Global concurrency, delegated to the single validated capacity config."""
    return load_capacity_config().max_concurrent_jobs


def _get_rate_limit_per_minute() -> int:
    """Per-IP request rate limit, delegated to the single validated capacity config."""
    return load_capacity_config().rate_limit_per_minute


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


def active_disk_job_ids(
    job_roots: tuple[Path, ...] | None = None,
    excluding_job_id: str | None = None,
) -> set[str]:
    """Return persisted active job IDs across script and video job roots."""
    import json
    if job_roots is None:
        jobs_root = Path(os.getenv("FYF_JOBS_ROOT", "jobs"))
        script_jobs_root = Path(os.getenv("FYF_SCRIPT_JOBS_ROOT", "script-jobs"))
        job_roots = (jobs_root, script_jobs_root)

    active_ids: set[str] = set()
    active_statuses = {
        "queued", "writing", "adk_producer", "retrying",
        "visuals", "voice", "rendering", "qa", "creative_qa",
        "cancelling",
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
                    active_ids.add(job_dir.name)
            except (OSError, json.JSONDecodeError):
                continue
    return active_ids


def count_active_disk_jobs(job_roots: tuple[Path, ...] | None = None, excluding_job_id: str | None = None) -> int:
    """Count active uncompleted jobs across script and video job roots."""
    return len(active_disk_job_ids(job_roots, excluding_job_id=excluding_job_id))


def check_concurrency(job_roots: tuple[Path, ...] | None = None, excluding_job_id: str | None = None) -> tuple[bool, str | None]:
    """Check the union of in-memory and persisted active jobs."""
    max_concurrency = _get_max_concurrency()
    with _LOCK:
        active_ids = set(_ACTIVE_JOB_IDS)
        active_ids.update(active_disk_job_ids(job_roots, excluding_job_id=excluding_job_id))
        active_count = len(active_ids)
        if active_count >= max_concurrency:
            return False, f"System is currently busy ({active_count}/{max_concurrency} active jobs). Please retry shortly."
        return True, None


def try_acquire_job_slot(job_id: str, job_roots: tuple[Path, ...] | None = None) -> tuple[bool, str | None]:
    """Atomically check and reserve a concurrency slot for a generation job."""
    max_concurrency = _get_max_concurrency()
    with _LOCK:
        if job_id in _ACTIVE_JOB_IDS:
            return True, None
        active_ids = set(_ACTIVE_JOB_IDS)
        active_ids.update(active_disk_job_ids(job_roots, excluding_job_id=job_id))
        active_count = len(active_ids)
        if active_count >= max_concurrency:
            return False, f"System is currently busy ({active_count}/{max_concurrency} active generation jobs). Please retry shortly."
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
    _PROVIDER_OP_REGISTRY.clear()


# ---------------------------------------------------------------------------
# B8: Provider operation ID registry — gates paid retries.
# ---------------------------------------------------------------------------
@dataclass
class ProviderOperation:
    """One paid provider dispatch attempt.

    ``blocked`` is True when the SAME ``operation_key`` was already settled as
    succeeded/billed; the caller MUST NOT issue another paid call and should reuse
    the prior ``provider_operation_id`` / result.
    """

    operation_key: str
    provider_operation_id: str
    attempt: int = 0
    blocked: bool = False
    prior_outcome: str | None = None
    prior_billed: bool = False
    # Serialized result of the already-settled paid operation, so a blocked
    # re-dispatch can reuse the prior outcome instead of paying twice (B8).
    prior_result_json: str | None = None


class ProviderOperationRegistry:
    """Records a provider operation ID for every paid dispatch and gates retries.

    Durability scope: records persist to the active telemetry collector's job_dir
    (``provider_operations.json``) when a telemetry scope is present — i.e. on the
    real paid production path. With no active scope the registry is a pure
    passthrough (never blocks, never persists) so unit tests that assert provider
    call counts are completely unaffected.
    """

    _SETTLED_PAID_OUTCOMES = frozenset({"succeeded", "completed", "billed"})

    def __init__(self) -> None:
        self._lock = threading.Lock()

    def _store_path(self) -> Path | None:
        try:
            from backend.vertex_telemetry import current_collector
            collector = current_collector()
        except Exception:  # pragma: no cover - telemetry optional
            return None
        if collector is None:
            return None
        job_dir = getattr(collector, "job_dir", None)
        if job_dir is None:
            return None
        path = Path(job_dir)
        try:
            if not path.is_dir():
                return None
        except OSError:
            return None
        return path / "provider_operations.json"

    def _load(self, path: Path) -> dict[str, Any]:
        import json
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def begin(self, operation_key: str, *, attempt: int = 0, estimated_usd: float = 0.0) -> ProviderOperation:
        """Reserve a paid dispatch. Returns a blocked op if already settled paid."""
        provider_operation_id = uuid.uuid4().hex
        path = self._store_path()
        if path is None:
            # No durable scope: pure passthrough so unit tests are unaffected.
            return ProviderOperation(operation_key, provider_operation_id, attempt, blocked=False)
        with self._lock:
            data = self._load(path)
            prior = data.get(operation_key)
            if isinstance(prior, dict) and (
                prior.get("billed") is True
                or prior.get("outcome") in self._SETTLED_PAID_OUTCOMES
            ):
                return ProviderOperation(
                    operation_key,
                    prior.get("provider_operation_id") or provider_operation_id,
                    attempt,
                    blocked=True,
                    prior_outcome=prior.get("outcome"),
                    prior_billed=bool(prior.get("billed")),
                    prior_result_json=prior.get("result_json"),
                )
            data[operation_key] = {
                "provider_operation_id": provider_operation_id,
                "attempt": attempt,
                "state": "in_flight",
                "billed": False,
                "outcome": None,
                "estimated_usd": estimated_usd,
                "started_at": time.time(),
            }
            try:
                write_json_atomically(path, data)
            except OSError:  # pragma: no cover - best effort durability
                logger.warning("Could not persist provider operation registry at %s", path)
            return ProviderOperation(operation_key, provider_operation_id, attempt, blocked=False)

    def settle(
        self,
        operation_key: str,
        *,
        outcome: str,
        billed: bool = False,
        actual_usd: float | None = None,
        provider_reported_state: str | None = None,
        provider_operation_id: str | None = None,
        result_json: str | None = None,
    ) -> dict[str, Any]:
        """Reconcile a dispatched provider operation with its reported outcome."""
        record = {
            "operation_key": operation_key,
            "provider_operation_id": provider_operation_id,
            "outcome": outcome,
            "billed": bool(billed),
            "actual_usd": actual_usd,
            "provider_reported_state": provider_reported_state,
            "result_json": result_json,
            "state": "settled",
            "settled_at": time.time(),
        }
        path = self._store_path()
        if path is None:
            record["persisted"] = False
            return record
        with self._lock:
            data = self._load(path)
            stored = dict(record)
            if provider_operation_id is None and isinstance(data.get(operation_key), dict):
                stored["provider_operation_id"] = data[operation_key].get("provider_operation_id")
            data[operation_key] = stored
            try:
                write_json_atomically(path, data)
                record["persisted"] = True
            except OSError:  # pragma: no cover - best effort durability
                logger.warning("Could not persist provider operation registry at %s", path)
                record["persisted"] = False
        return record

    def is_settled_paid(self, operation_key: str) -> bool:
        """True when the operation was already reported succeeded/billed (gate closed)."""
        path = self._store_path()
        if path is None:
            return False
        with self._lock:
            prior = self._load(path).get(operation_key)
        return bool(
            isinstance(prior, dict)
            and (prior.get("billed") is True or prior.get("outcome") in self._SETTLED_PAID_OUTCOMES)
        )

    def clear(self) -> None:
        """No in-memory state to clear (durability is per job_dir); kept for symmetry."""
        return None


_PROVIDER_OP_REGISTRY = ProviderOperationRegistry()


def provider_operation_registry() -> ProviderOperationRegistry:
    """Accessor for the process-wide provider operation registry."""
    return _PROVIDER_OP_REGISTRY


def begin_provider_operation(operation_key: str, *, attempt: int = 0, estimated_usd: float = 0.0) -> ProviderOperation:
    """Convenience wrapper around :class:`ProviderOperationRegistry`."""
    return _PROVIDER_OP_REGISTRY.begin(operation_key, attempt=attempt, estimated_usd=estimated_usd)


def settle_provider_operation(
    operation_key: str,
    *,
    outcome: str,
    billed: bool = False,
    actual_usd: float | None = None,
    provider_reported_state: str | None = None,
    provider_operation_id: str | None = None,
    result_json: str | None = None,
) -> dict[str, Any]:
    """Convenience wrapper around :class:`ProviderOperationRegistry`."""
    return _PROVIDER_OP_REGISTRY.settle(
        operation_key,
        outcome=outcome,
        billed=billed,
        actual_usd=actual_usd,
        provider_reported_state=provider_reported_state,
        provider_operation_id=provider_operation_id,
        result_json=result_json,
    )


def provider_operation_key(stage: str, *discriminators: Any) -> str:
    """Stable key for one logical paid operation (B8).

    The key is content-addressed so it is identical across retry attempts AND
    across a queue redelivery / resume of the same work within the same job dir,
    which is exactly when the already-billed gate must fire. A short sha256 keeps
    the on-disk registry key bounded regardless of prompt size.
    """
    import hashlib

    hasher = hashlib.sha256()
    hasher.update(stage.encode("utf-8"))
    for part in discriminators:
        hasher.update(b"\x00")
        hasher.update(str(part).encode("utf-8"))
    return f"{stage}:{hasher.hexdigest()[:24]}"


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
        # B8: the provider operation ID for the paid dispatch this lease guards.
        self.provider_operation_id: str | None = None

    def attach_provider_operation(self, provider_operation_id: str | None) -> None:
        """Record the provider operation ID that this lease's paid dispatch produced."""
        self.provider_operation_id = provider_operation_id

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


def _budget_rejection_code(budget_info: dict[str, Any], estimated_charge_usd: float) -> str:
    """Classify a budget refusal into the explicit taxonomy code."""
    if not budget_info.get("paid_production_enabled"):
        return REASON_PAID_DISABLED
    if budget_info.get("budget_exceeded"):
        return REASON_BUDGET_EXCEEDED
    remaining = budget_info.get("remaining_usd")
    if remaining is not None and estimated_charge_usd > float(remaining):
        return REASON_BUDGET_INSUFFICIENT
    return REASON_BUDGET_EXCEEDED


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
    Every refusal names the exact guardrail via ``X-FYF-Rejection-Reason``.
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
        _rejection(REASON_RATE_LIMITED, rate_reason or "Rate limit exceeded.")

    # 2. Fail-closed budget gate. Explicitly consult paid_production_enabled so a
    #    disabled ceiling is refused with its honest reason BEFORE any reservation.
    if estimated_charge_usd and estimated_charge_usd > 0.0:
        budget_info = get_budget_status(root_dir=root_dir)
        if not budget_info.get("paid_production_enabled"):
            _rejection(
                REASON_PAID_DISABLED,
                f"Budget guardrail: {budget_info.get('reason')}. Contact the operator.",
            )

    reserved, budget_reason = reserve_budget(
        operation_id,
        estimated_usd=estimated_charge_usd,
        root_dir=root_dir,
    )
    if not reserved:
        lease.release()
        budget_info = get_budget_status(root_dir=root_dir)
        code = _budget_rejection_code(budget_info, estimated_charge_usd)
        _rejection(code, f"Budget guardrail: {budget_reason}. Contact the operator.")
    lease.budget_reserved = True

    # 3. Concurrency slot acquisition — overload is reported as an honest queued
    #    state (queue_full) carrying depth/position, not a bare 429.
    slot_ok, slot_reason = try_acquire_job_slot(operation_id, job_roots=job_roots)
    if not slot_ok:
        lease.release()  # Transactional rollback of budget reservation
        logger.warning("Generation blocked by concurrency slot: %s", slot_reason)
        depth = get_active_job_count()
        _rejection(
            REASON_QUEUE_FULL,
            slot_reason or "System is currently busy.",
            extra_headers={
                QUEUE_DEPTH_HEADER: depth,
                QUEUE_POSITION_HEADER: depth + 1,
            },
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
    budget_info = get_budget_status(root_dir=root_dir)
    if estimated_charge_usd and estimated_charge_usd > 0.0:
        if not budget_info.get("paid_production_enabled"):
            _rejection(
                REASON_PAID_DISABLED,
                f"Budget guardrail: {budget_info.get('reason')}. Contact the operator.",
            )
        if not is_budget_available(estimated_charge_usd, root_dir=root_dir):
            code = _budget_rejection_code(budget_info, estimated_charge_usd)
            reason = budget_info.get("reason") or "Generation budget cap reached"
            _rejection(code, f"Budget guardrail: {reason}. Contact the operator.")

    concurrency_ok, concurrency_reason = check_concurrency(job_roots=job_roots)
    if not concurrency_ok:
        depth = get_active_job_count()
        _rejection(
            REASON_QUEUE_FULL,
            concurrency_reason or "System is currently busy.",
            extra_headers={
                QUEUE_DEPTH_HEADER: depth,
                QUEUE_POSITION_HEADER: depth + 1,
            },
        )

    rate_ok, rate_reason = check_rate_limit(ip)
    if not rate_ok:
        _rejection(REASON_RATE_LIMITED, rate_reason or "Rate limit exceeded.")
    return None
