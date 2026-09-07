"""Durable, idempotent, disk-backed job queue (Stage B-III / B7).

Design
------
* **No broker dependency** — stdlib + local disk only (cloud queue adapters are a
  later, additive stage). Durability reuses ``job_store.write_json_atomically``
  (flush + fsync + os.replace) so a message survives a worker crash.
* **Client-supplied ``idempotency_key``** — a duplicate submission returns the
  EXISTING message/job rather than creating a second one, so there are zero
  duplicate jobs and zero duplicate budget reservations.
* **Delivery-ID dedup table** — each claim mints a ``delivery_id``; redelivering an
  already-acked delivery is a no-op (at-least-once delivery, effectively-once work).
* **Visibility timeout** — a claimed (in-flight) message that is not acked within
  the timeout becomes visible again for redelivery (crash recovery).
* **Honest queued state** — ``submit`` reports the message's queue ``position`` and
  the queue ``depth`` so overload is surfaced as "queued at position N of M", not a
  bare 429.

Root resolution honours ``FYF_QUEUE_ROOT`` exactly like ``FYF_JOBS_ROOT`` /
``FYF_PROJECTS_ROOT`` so tests and deployments can relocate the queue.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from backend.job_store import write_json_atomically

logger = logging.getLogger(__name__)

DEFAULT_VISIBILITY_TIMEOUT_SECONDS = 300.0

STATE_QUEUED = "queued"
STATE_IN_FLIGHT = "in_flight"
STATE_COMPLETED = "completed"
STATE_FAILED = "failed"

_PATH_TAG = "__fyf_path__"


def default_queue_root() -> Path:
    """Resolve the queue root from ``FYF_QUEUE_ROOT`` or the gitignored output tree."""
    env_override = os.getenv("FYF_QUEUE_ROOT")
    if env_override and str(env_override).strip():
        return Path(env_override)
    repo_root = Path(__file__).resolve().parent.parent
    return repo_root / "output" / "queue"


def _encode_arg(value: Any) -> Any:
    """JSON-safe encoding that preserves ``Path`` args through a durable round-trip."""
    if isinstance(value, Path):
        return {_PATH_TAG: str(value)}
    if isinstance(value, (list, tuple)):
        return [_encode_arg(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _encode_arg(v) for k, v in value.items()}
    return value


def _decode_arg(value: Any) -> Any:
    if isinstance(value, dict):
        if len(value) == 1 and _PATH_TAG in value:
            return Path(value[_PATH_TAG])
        return {k: _decode_arg(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_decode_arg(v) for v in value]
    return value


def _run_awaitable(awaitable: Any) -> Any:
    """Run an awaitable to completion from a synchronous dispatch context."""
    try:
        asyncio.get_running_loop()
        running = True
    except RuntimeError:
        running = False
    if not running:
        return asyncio.run(awaitable)
    # A loop is already running in this thread; drive the coroutine on a side thread.
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(asyncio.run, awaitable).result()


@dataclass
class QueueMessage:
    """A durable unit of queued work."""

    message_id: str
    idempotency_key: str
    target: str
    job_id: str | None = None
    state: str = STATE_QUEUED
    created: bool = True
    delivery_count: int = 0
    position: int | None = None
    depth: int = 0
    args: list[Any] = field(default_factory=list)
    kwargs: dict[str, Any] = field(default_factory=dict)

    @property
    def is_duplicate(self) -> bool:
        return not self.created

    def queued_state(self) -> dict[str, Any]:
        """Honest queued state for overload surfaces (position + depth)."""
        return {
            "state": "queued",
            "message_id": self.message_id,
            "job_id": self.job_id,
            "idempotency_key": self.idempotency_key,
            "queue_position": self.position,
            "queue_depth": self.depth,
            "duplicate": self.is_duplicate,
        }


@dataclass
class DispatchResult:
    message_id: str
    delivery_id: str | None
    status: str  # dispatched | duplicate | completed | not_visible | not_found | failed
    result: Any = None
    error: str | None = None


class JobQueue:
    """Disk-backed durable queue with idempotent submission and at-least-once delivery."""

    def __init__(
        self,
        root: Path | str | None = None,
        *,
        visibility_timeout: float = DEFAULT_VISIBILITY_TIMEOUT_SECONDS,
        resolver: Callable[[str], Callable[..., Any]] | None = None,
        now_fn: Callable[[], float] = time.time,
    ):
        self.root = Path(root) if root is not None else default_queue_root()
        self.visibility_timeout = float(visibility_timeout)
        self._resolver = resolver or _default_resolver
        self._now = now_fn
        self._lock = threading.Lock()
        self._messages_dir = self.root / "messages"
        self._idempotency_dir = self.root / "idempotency"

    # -- paths -------------------------------------------------------------
    def _message_path(self, message_id: str) -> Path:
        return self._messages_dir / f"{message_id}.json"

    def _dedup_path(self, idempotency_key: str) -> Path:
        digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
        return self._idempotency_dir / f"{digest}.json"

    def _ensure_dirs(self) -> None:
        self._messages_dir.mkdir(parents=True, exist_ok=True)
        self._idempotency_dir.mkdir(parents=True, exist_ok=True)

    # -- persistence -------------------------------------------------------
    def _load_record(self, message_id: str) -> dict[str, Any] | None:
        path = self._message_path(message_id)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else None
        except (OSError, ValueError):
            return None

    def _save_record(self, record: dict[str, Any]) -> None:
        write_json_atomically(self._message_path(record["message_id"]), record)

    def _all_records(self) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        if not self._messages_dir.is_dir():
            return records
        for path in self._messages_dir.iterdir():
            if path.suffix != ".json":
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    records.append(data)
            except (OSError, ValueError):
                continue
        return records

    # -- read-only lookups -------------------------------------------------
    def resolve(self, idempotency_key: str) -> QueueMessage | None:
        """Return the existing message for a key, or None (read-only, no side effects)."""
        dedup_path = self._dedup_path(idempotency_key)
        try:
            entry = json.loads(dedup_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        message_id = entry.get("message_id") if isinstance(entry, dict) else None
        if not message_id:
            return None
        return self.get(message_id)

    def get(self, message_id: str) -> QueueMessage | None:
        record = self._load_record(message_id)
        if record is None:
            return None
        return self._to_message(record, created=True)

    def _to_message(self, record: dict[str, Any], *, created: bool) -> QueueMessage:
        depth = self.depth()
        position = self.position(record["message_id"])
        return QueueMessage(
            message_id=record["message_id"],
            idempotency_key=record.get("idempotency_key", ""),
            target=record.get("target", ""),
            job_id=record.get("job_id"),
            state=record.get("state", STATE_QUEUED),
            created=created,
            delivery_count=int(record.get("delivery_count", 0)),
            position=position,
            depth=depth,
            args=_decode_arg(record.get("args", [])),
            kwargs=_decode_arg(record.get("kwargs", {})),
        )

    def depth(self) -> int:
        """Count of not-yet-finished messages (queued + in-flight)."""
        return sum(
            1
            for record in self._all_records()
            if record.get("state") in (STATE_QUEUED, STATE_IN_FLIGHT)
        )

    def position(self, message_id: str) -> int | None:
        """1-based position of a message among queued work, ordered by creation."""
        queued = sorted(
            (r for r in self._all_records() if r.get("state") in (STATE_QUEUED, STATE_IN_FLIGHT)),
            key=lambda r: (float(r.get("created_at", 0.0)), r.get("message_id", "")),
        )
        for index, record in enumerate(queued, start=1):
            if record.get("message_id") == message_id:
                return index
        return None

    # -- submission --------------------------------------------------------
    def submit(
        self,
        *,
        idempotency_key: str,
        target: str,
        args: list[Any] | None = None,
        kwargs: dict[str, Any] | None = None,
        job_id: str | None = None,
    ) -> QueueMessage:
        """Durably enqueue work, deduplicated by ``idempotency_key``.

        A repeated key returns the EXISTING message with ``created=False`` — no
        second job, and (because callers key the budget reservation off the same
        idempotency identity) no second reservation.
        """
        if not idempotency_key or not str(idempotency_key).strip():
            raise ValueError("idempotency_key is required")
        if not target or not str(target).strip():
            raise ValueError("target is required")

        with self._lock:
            self._ensure_dirs()
            existing = self.resolve(idempotency_key)
            if existing is not None:
                existing.created = False
                return existing

            now = self._now()
            message_id = uuid.uuid4().hex
            record = {
                "message_id": message_id,
                "idempotency_key": idempotency_key,
                "job_id": job_id,
                "target": target,
                "args": _encode_arg(args or []),
                "kwargs": _encode_arg(kwargs or {}),
                "state": STATE_QUEUED,
                "created_at": now,
                "updated_at": now,
                "visible_at": now,
                "delivery_count": 0,
                "deliveries": {},
                "result": None,
                "error": None,
            }
            # Claim the idempotency key atomically (exclusive create) then persist
            # the message. If the claim loses a race, fall back to the winner.
            dedup_path = self._dedup_path(idempotency_key)
            try:
                with open(dedup_path, "x", encoding="utf-8") as handle:
                    json.dump({"message_id": message_id, "idempotency_key": idempotency_key}, handle)
                    handle.flush()
                    os.fsync(handle.fileno())
            except FileExistsError:
                winner = self.resolve(idempotency_key)
                if winner is not None:
                    winner.created = False
                    return winner
                # Stale dedup entry with no message: reclaim it.
                dedup_path.unlink(missing_ok=True)
                with open(dedup_path, "x", encoding="utf-8") as handle:
                    json.dump({"message_id": message_id, "idempotency_key": idempotency_key}, handle)
                    handle.flush()
                    os.fsync(handle.fileno())
            self._save_record(record)
            return self._to_message(record, created=True)

    def abort(self, message_id: str) -> None:
        """Remove a message whose route failed before dispatch (keeps dedup honest)."""
        with self._lock:
            record = self._load_record(message_id)
            if record is None:
                return
            self._message_path(message_id).unlink(missing_ok=True)
            self._dedup_path(record.get("idempotency_key", "")).unlink(missing_ok=True)

    # -- delivery ----------------------------------------------------------
    def claim(self, message_id: str, delivery_id: str | None = None) -> tuple[str | None, str]:
        """Claim a message for delivery respecting visibility timeout + dedup."""
        with self._lock:
            record = self._load_record(message_id)
            if record is None:
                return None, "not_found"
            now = self._now()
            state = record.get("state")
            if state == STATE_COMPLETED:
                return None, "completed"
            if state == STATE_IN_FLIGHT and float(record.get("visible_at", 0.0)) > now:
                return None, "not_visible"
            did = delivery_id or uuid.uuid4().hex
            deliveries = record.setdefault("deliveries", {})
            prior = deliveries.get(did)
            if isinstance(prior, dict) and prior.get("state") == "acked":
                return None, "duplicate"
            record["state"] = STATE_IN_FLIGHT
            record["delivery_count"] = int(record.get("delivery_count", 0)) + 1
            record["visible_at"] = now + self.visibility_timeout
            record["updated_at"] = now
            deliveries[did] = {"claimed_at": now, "state": "in_flight"}
            self._save_record(record)
            return did, "claimed"

    def ack(self, message_id: str, delivery_id: str, *, result: Any = None) -> None:
        with self._lock:
            record = self._load_record(message_id)
            if record is None:
                return
            record["state"] = STATE_COMPLETED
            record["updated_at"] = self._now()
            record["result"] = _encode_arg(result) if _jsonable(result) else None
            deliveries = record.setdefault("deliveries", {})
            entry = deliveries.get(delivery_id, {})
            entry.update({"state": "acked", "acked_at": self._now()})
            deliveries[delivery_id] = entry
            self._save_record(record)

    def nack(self, message_id: str, delivery_id: str, *, error: str | None = None) -> None:
        """Record a failed delivery; the message becomes visible again for retry."""
        with self._lock:
            record = self._load_record(message_id)
            if record is None:
                return
            now = self._now()
            record["state"] = STATE_QUEUED
            record["updated_at"] = now
            record["visible_at"] = now
            record["error"] = error
            deliveries = record.setdefault("deliveries", {})
            entry = deliveries.get(delivery_id, {})
            entry.update({"state": "failed", "failed_at": now, "error": error})
            deliveries[delivery_id] = entry
            self._save_record(record)

    def dispatch(self, message_id: str, delivery_id: str | None = None) -> DispatchResult:
        """Claim, run the resolved target, and ack — the at-least-once worker step."""
        did, claim_state = self.claim(message_id, delivery_id=delivery_id)
        if did is None:
            return DispatchResult(message_id=message_id, delivery_id=None, status=claim_state)
        record = self._load_record(message_id) or {}
        target = record.get("target", "")
        args = _decode_arg(record.get("args", []))
        kwargs = _decode_arg(record.get("kwargs", {}))
        try:
            handler = self._resolver(target)
        except Exception as exc:  # unknown target
            self.nack(message_id, did, error=f"unknown target '{target}': {exc}")
            raise
        try:
            result = handler(*args, **kwargs)
            if inspect.isawaitable(result):
                result = _run_awaitable(result)
        except Exception as exc:
            self.nack(message_id, did, error=str(exc))
            logger.warning("Queue dispatch failed for message %s target %s: %s", message_id, target, exc)
            raise
        self.ack(message_id, did, result=result)
        return DispatchResult(message_id=message_id, delivery_id=did, status="dispatched", result=result)

    def recover_visible(self) -> list[str]:
        """Flip in-flight messages past their visibility timeout back to queued.

        Called on worker restart so a crashed job's message is redelivered
        (at-least-once) instead of being stranded in-flight forever.
        """
        recovered: list[str] = []
        with self._lock:
            now = self._now()
            for record in self._all_records():
                if record.get("state") == STATE_IN_FLIGHT and float(record.get("visible_at", 0.0)) <= now:
                    record["state"] = STATE_QUEUED
                    record["updated_at"] = now
                    self._save_record(record)
                    recovered.append(record["message_id"])
        return recovered


def _jsonable(value: Any) -> bool:
    try:
        json.dumps(value)
        return True
    except (TypeError, ValueError):
        return False


def _default_resolver(target: str) -> Callable[..., Any]:
    """Resolve a target against ``backend.main`` so test patches are honoured."""
    import backend.main as main_module

    try:
        return getattr(main_module, target)
    except AttributeError as exc:
        raise KeyError(f"Unknown queue target: {target}") from exc
