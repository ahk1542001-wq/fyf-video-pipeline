"""Cooperative job cancellation (Stage B-III / B9).

Cancellation is COOPERATIVE, never preemptive. A cancel request:

  * records a durable marker (``.cancel_requested.json``) inside the job dir so it
    survives a worker restart, and
  * flips an in-process flag so a running worker stops promptly.

Long-running work calls :func:`checkpoint` at SAFE BOUNDARIES (for the renderer,
between fully-written segments). A segment that is mid-write is never killed: the
worker finishes the current unit, reaches the next boundary, and stops there.

State is keyed by the RESOLVED job directory (falling back to the job id) so two
jobs that share an id in different roots never collide.

Guarantees the callers in ``backend.main`` wire on top of this mechanism:
  * queued work halts before dispatch (checkpoint at ``pre_dispatch``),
  * in-flight work stops at the next safe boundary (checkpoint between segments),
  * supported provider operations get a best-effort cancel,
  * late-arriving results/costs are still reconciled into the ledger, and
  * a stale result cannot overwrite a newer draft.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.job_store import write_json_atomically

logger = logging.getLogger(__name__)

CANCEL_MARKER_FILENAME = ".cancel_requested.json"

STATE_NONE = "none"
STATE_CANCELLING = "cancelling"
STATE_CANCELLED = "cancelled"


class JobCancelledError(RuntimeError):
    """Raised at a safe boundary when cancellation was requested for a job."""

    def __init__(self, job_id: str, reason: str | None = None, boundary: str | None = None):
        self.job_id = job_id
        self.reason = reason
        self.boundary = boundary
        super().__init__(
            f"Job {job_id} cancelled at boundary '{boundary or 'unknown'}': {reason or 'no reason given'}"
        )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class CancellationStatus:
    """Snapshot of a job's cancellation state."""

    job_id: str
    state: str = STATE_NONE
    reason: str | None = None
    requested_at: str | None = None
    boundary: str | None = None

    @property
    def requested(self) -> bool:
        return self.state in (STATE_CANCELLING, STATE_CANCELLED)

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "state": self.state,
            "reason": self.reason,
            "requested_at": self.requested_at,
            "boundary": self.boundary,
        }


def _registry_key(job_id: str, job_dir: Path | str | None) -> str:
    if job_dir is not None:
        try:
            return str(Path(job_dir).resolve())
        except OSError:
            return str(job_dir)
    return job_id


class CancellationRegistry:
    """In-process cancellation flags backed by a durable per-job marker file."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._flags: dict[str, dict[str, Any]] = {}

    @staticmethod
    def _marker_path(job_dir: Path | str | None) -> Path | None:
        if job_dir is None:
            return None
        return Path(job_dir) / CANCEL_MARKER_FILENAME

    def _read_marker(self, job_dir: Path | str | None) -> dict[str, Any] | None:
        path = self._marker_path(job_dir)
        if path is None:
            return None
        try:
            import json

            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else None
        except (OSError, ValueError):
            return None

    def request(
        self,
        job_id: str,
        *,
        job_dir: Path | str | None = None,
        reason: str | None = None,
    ) -> CancellationStatus:
        """Record a cancellation request (durable marker + in-process flag)."""
        key = _registry_key(job_id, job_dir)
        record = {
            "job_id": job_id,
            "state": STATE_CANCELLING,
            "reason": reason,
            "requested_at": _now_iso(),
            "boundary": None,
        }
        with self._lock:
            existing = self._flags.get(key)
            if existing and existing.get("state") == STATE_CANCELLED:
                # Already terminal; do not downgrade to cancelling.
                return CancellationStatus(**{k: existing.get(k) for k in record})
            self._flags[key] = dict(record)
        path = self._marker_path(job_dir)
        if path is not None:
            try:
                write_json_atomically(path, record)
            except (OSError, FileNotFoundError):
                logger.warning("Could not persist cancellation marker for job %s at %s", job_id, path)
        return CancellationStatus(**record)

    def is_requested(self, job_id: str, *, job_dir: Path | str | None = None) -> bool:
        key = _registry_key(job_id, job_dir)
        with self._lock:
            flag = self._flags.get(key)
            if flag and flag.get("state") in (STATE_CANCELLING, STATE_CANCELLED):
                return True
        marker = self._read_marker(job_dir)
        return bool(marker and marker.get("state") in (STATE_CANCELLING, STATE_CANCELLED))

    def status(self, job_id: str, *, job_dir: Path | str | None = None) -> CancellationStatus:
        key = _registry_key(job_id, job_dir)
        with self._lock:
            flag = self._flags.get(key)
        if flag:
            return CancellationStatus(**{k: flag.get(k) for k in ("job_id", "state", "reason", "requested_at", "boundary")})
        marker = self._read_marker(job_dir)
        if marker:
            return CancellationStatus(
                job_id=marker.get("job_id", job_id),
                state=marker.get("state", STATE_NONE),
                reason=marker.get("reason"),
                requested_at=marker.get("requested_at"),
                boundary=marker.get("boundary"),
            )
        return CancellationStatus(job_id=job_id, state=STATE_NONE)

    def mark_cancelled(
        self,
        job_id: str,
        *,
        job_dir: Path | str | None = None,
        boundary: str | None = None,
        reason: str | None = None,
    ) -> CancellationStatus:
        """Move a job to the terminal ``cancelled`` state (work stopped at boundary)."""
        key = _registry_key(job_id, job_dir)
        with self._lock:
            prior = self._flags.get(key, {})
            record = {
                "job_id": job_id,
                "state": STATE_CANCELLED,
                "reason": reason if reason is not None else prior.get("reason"),
                "requested_at": prior.get("requested_at") or _now_iso(),
                "boundary": boundary,
            }
            self._flags[key] = dict(record)
        path = self._marker_path(job_dir)
        if path is not None:
            try:
                write_json_atomically(path, record)
            except (OSError, FileNotFoundError):
                logger.warning("Could not persist cancellation marker for job %s at %s", job_id, path)
        return CancellationStatus(**record)

    def clear(self, job_id: str, *, job_dir: Path | str | None = None) -> None:
        """Remove all cancellation state (used after a job fully restarts)."""
        key = _registry_key(job_id, job_dir)
        with self._lock:
            self._flags.pop(key, None)
        path = self._marker_path(job_dir)
        if path is not None:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

    def checkpoint(
        self,
        job_id: str,
        *,
        job_dir: Path | str | None = None,
        boundary: str | None = None,
    ) -> None:
        """Raise :class:`JobCancelledError` if cancellation was requested.

        Call at a safe boundary only. Cheap when nothing is cancelled.
        """
        if self.is_requested(job_id, job_dir=job_dir):
            status = self.status(job_id, job_dir=job_dir)
            raise JobCancelledError(job_id, reason=status.reason, boundary=boundary)

    def reset(self) -> None:
        """Clear all in-process flags (test isolation)."""
        with self._lock:
            self._flags.clear()


_REGISTRY = CancellationRegistry()


def request_cancellation(job_id: str, *, job_dir: Path | str | None = None, reason: str | None = None) -> CancellationStatus:
    return _REGISTRY.request(job_id, job_dir=job_dir, reason=reason)


def is_cancellation_requested(job_id: str, *, job_dir: Path | str | None = None) -> bool:
    return _REGISTRY.is_requested(job_id, job_dir=job_dir)


def cancellation_status(job_id: str, *, job_dir: Path | str | None = None) -> CancellationStatus:
    return _REGISTRY.status(job_id, job_dir=job_dir)


def mark_cancelled(job_id: str, *, job_dir: Path | str | None = None, boundary: str | None = None, reason: str | None = None) -> CancellationStatus:
    return _REGISTRY.mark_cancelled(job_id, job_dir=job_dir, boundary=boundary, reason=reason)


def clear_cancellation(job_id: str, *, job_dir: Path | str | None = None) -> None:
    return _REGISTRY.clear(job_id, job_dir=job_dir)


def checkpoint(job_id: str, *, job_dir: Path | str | None = None, boundary: str | None = None) -> None:
    return _REGISTRY.checkpoint(job_id, job_dir=job_dir, boundary=boundary)


def reset_cancellation_state() -> None:
    return _REGISTRY.reset()


def best_effort_provider_cancel(provider_operation_id: str | None, *, job_id: str | None = None) -> bool:
    """Best-effort cancellation of a supported provider operation.

    Gemini ``generate_content`` / Vertex TTS calls are synchronous and expose no
    server-side cancel handle, so there is nothing to abort mid-flight; the honest
    behaviour is to acknowledge the request, record it, and let the cooperative
    boundary stop any *further* paid dispatch. Returns True when the request was
    acknowledged. Never raises — a cancel must not mask the original outcome, and
    late costs are still reconciled by the ledger.
    """
    if not provider_operation_id:
        return False
    logger.info(
        "Best-effort provider cancel acknowledged for job=%s provider_operation_id=%s "
        "(synchronous provider call; no server-side abort available)",
        job_id,
        provider_operation_id,
    )
    return True
