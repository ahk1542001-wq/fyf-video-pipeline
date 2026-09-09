"""Durable, replay-safe telemetry outbox.

Design goals (Stage E2)
-----------------------
* **No silent loss.** Every telemetry event is appended to a durable local
  outbox *before* any attempt to deliver it to ClickHouse Cloud.  The previous
  behaviour wrote a local JSON mirror that was never replayed, so an outage
  permanently lost the remote copy.  The outbox is now the replay source.
* **Replay-safe.** Each event carries a *stable* ``event_id`` (a deterministic
  hash of its business payload) plus a ``schema_version`` and a monotonic
  ``sequence``.  Replaying the same logical event after recovery produces the
  same ``event_id`` so the ``ReplacingMergeTree(ingestion_timestamp)`` sink
  collapses it instead of double counting.
* **Off the request path.** Callers only append to the durable outbox (a fast
  local write).  The ClickHouse ``insert`` happens in
  :meth:`TelemetryOutbox.drain_once`, driven by a lazily-started background
  daemon thread -- never inline inside a user-facing request handler.  This
  matters because the client is configured with ``send_receive_timeout=300``
  and a synchronous insert could otherwise stall a request for minutes.
* **Bounded, honest retry.** Delivery uses exponential backoff with a hard
  attempt ceiling.  An event that exhausts its attempts becomes ``failed`` and
  is counted; it is never dropped and never retried blindly forever.
* **Gated on the replay-safe schema.** The drain refuses to deliver until the
  E1 replay-safe schema is confirmed.  Degrading honestly (events stay
  ``pending``) is preferred over silently duplicating rows.

This module has no third-party dependencies beyond the standard library and
never imports ClickHouse at module load time; the client is injected as a
factory so the whole module is hermetically testable.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

try:  # pragma: no cover - the production/runtime target is POSIX
    import fcntl
except ImportError:  # pragma: no cover - retained for importability on Windows
    fcntl = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Bumped only when the on-the-wire event contract changes incompatibly.
SCHEMA_VERSION = 1

#: Columns injected at the head of every delivered row (Stage E1 contract).
EVENT_COLUMNS: List[str] = [
    "event_id",
    "schema_version",
    "event_timestamp",
    "ingestion_timestamp",
    "sequence",
]

#: Hard ceiling on delivery attempts before an event is marked ``failed``.
MAX_DELIVERY_ATTEMPTS = 5

#: Exponential backoff bounds (seconds).  Bounded so a stuck sink cannot push
#: the next retry beyond a sane horizon, and cannot spin in a tight loop.
BASE_BACKOFF_SECONDS = 1.0
MAX_BACKOFF_SECONDS = 300.0

#: Maximum number of events delivered in a single drain pass.
DEFAULT_DRAIN_BATCH = 100

_PENDING = "pending"
_DELIVERED = "delivered"
_FAILED = "failed"


@contextmanager
def _outbox_process_lock(lock_path: Path):
    """Serialize shared outbox read/append/state transactions across workers."""
    if lock_path.is_symlink():
        raise OutboxCorruptedError("outbox lock file is a symlink")
    with lock_path.open("a+") as handle:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class OutboxCorruptedError(RuntimeError):
    """Raised when durable outbox state cannot be trusted safely."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat()


def _parse_iso(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _sequence_or_none(value: Any) -> int | None:
    """Parse a JSON sequence only when it is a finite, integral, non-negative value."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed != value or parsed < 0:
        return None
    return parsed


def stable_event_id(table: str, payload: Dict[str, Any]) -> str:
    """Return a deterministic event id for a business payload.

    The same logical event always hashes to the same id, across retries and
    across process restarts, which is what makes a replay deduplicate at the
    ``ReplacingMergeTree`` sink instead of inserting a second row.
    """
    canonical = json.dumps(
        {"table": table, "payload": payload},
        sort_keys=True,
        ensure_ascii=False,
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


def compute_backoff(attempts: int) -> float:
    """Exponential backoff with a ceiling.  ``attempts`` is 1-based."""
    attempt = max(1, int(attempts))
    return min(BASE_BACKOFF_SECONDS * (2 ** (attempt - 1)), MAX_BACKOFF_SECONDS)


@dataclass
class OutboxEvent:
    """One durable telemetry event awaiting (or finished) delivery."""

    event_id: str
    table: str
    schema_version: int
    sequence: int
    event_timestamp: str
    ingestion_timestamp: str
    column_names: List[str]
    row: List[Any]
    state: str = _PENDING
    attempts: int = 0
    next_retry_at: Optional[str] = None
    last_error: Optional[str] = None
    enqueued_at: str = field(default_factory=lambda: _iso(_utcnow()))
    delivered_at: Optional[str] = None

    def due(self, now: datetime) -> bool:
        if self.state != _PENDING:
            return False
        retry = _parse_iso(self.next_retry_at)
        return retry is None or retry <= now

    def to_log_record(self) -> Dict[str, Any]:
        return {
            "event_id": self.event_id,
            "table": self.table,
            "schema_version": self.schema_version,
            "sequence": self.sequence,
            "event_timestamp": self.event_timestamp,
            "ingestion_timestamp": self.ingestion_timestamp,
            "column_names": self.column_names,
            "row": self.row,
            "enqueued_at": self.enqueued_at,
        }

    @staticmethod
    def from_log_record(record: Dict[str, Any]) -> "OutboxEvent":
        event_id = record.get("event_id")
        table = record.get("table")
        if not isinstance(event_id, str) or not event_id.strip():
            raise ValueError("event_id must be a non-empty string")
        if not isinstance(table, str) or not table.strip():
            raise ValueError("table must be a non-empty string")
        sequence = _sequence_or_none(record.get("sequence", 0))
        if sequence is None:
            raise ValueError("sequence must be a non-negative integer")
        column_names = record.get("column_names")
        row = record.get("row")
        if not isinstance(column_names, list) or not isinstance(row, list):
            raise ValueError("column_names and row must be lists")
        if len(column_names) != len(row):
            raise ValueError("column_names and row lengths differ")
        return OutboxEvent(
            event_id=event_id,
            table=table,
            schema_version=int(record.get("schema_version", SCHEMA_VERSION)),
            sequence=sequence,
            event_timestamp=str(record.get("event_timestamp", "")),
            ingestion_timestamp=str(record.get("ingestion_timestamp", "")),
            column_names=list(column_names),
            row=list(row),
            enqueued_at=str(record.get("enqueued_at") or _iso(_utcnow())),
        )


@dataclass
class DrainReport:
    """Outcome of a single bounded drain pass."""

    attempted: int = 0
    delivered: int = 0
    failed: int = 0
    pending: Optional[int] = 0
    blocked_reason: Optional[str] = None
    schema_ready: bool = False
    client_available: bool = False
    corrupted: bool = False
    reconstructed: bool = False

    def as_dict(self) -> Dict[str, Any]:
        return {
            "attempted": self.attempted,
            "delivered": self.delivered,
            "failed": self.failed,
            "pending": self.pending,
            "blocked_reason": self.blocked_reason,
            "schema_ready": self.schema_ready,
            "client_available": self.client_available,
            "corrupted": self.corrupted,
            "reconstructed": self.reconstructed,
        }


class TelemetryOutbox:
    """A durable, replay-safe outbox backed by an append-only JSONL log.

    Two files make up the durable state:

    * ``events.jsonl`` -- append-only payload log (the replay source).
    * ``state.json``   -- delivery metadata keyed by ``event_id`` plus the
      monotonic sequence counter and the last drain summary.

    Both writes are durable (append + ``flush``/``fsync`` for the log, atomic
    replace for the state file) so a crash mid-run cannot lose an enqueued
    event.
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._log_path = self.root / "events.jsonl"
        self._state_path = self.root / "state.json"
        self._process_lock_path = self.root / ".outbox.lock"
        self._lock = threading.RLock()
        self._drain_thread: Optional[threading.Thread] = None
        self._corrupted = False
        self._reconstructed = False
        self._corruption_reason: Optional[str] = None

    # -- state persistence --------------------------------------------------

    def _load_state(self) -> Dict[str, Any]:
        """Load and validate state, reconstructing only from a valid log.

        A malformed state file must never be treated as an empty outbox: doing
        that would reset the sequence and replay already-recorded events.  A
        valid append-only log is sufficient to reconstruct delivery metadata;
        an invalid log or an inconsistent state/log pair is marked corrupted
        and all delivery is blocked until an operator repairs it.
        """
        self._corrupted = False
        self._reconstructed = False
        self._corruption_reason = None

        raw_state: Dict[str, Any] | None = None
        state_error: str | None = None
        if self._state_path.is_symlink():
            state_error = "state file is a symlink"
        elif self._state_path.is_file():
            try:
                payload = json.loads(self._state_path.read_text(encoding="utf-8"))
                if not isinstance(payload, dict):
                    state_error = "state payload is not an object"
                else:
                    raw_state = payload
            except (OSError, json.JSONDecodeError) as exc:
                state_error = f"state file unreadable: {exc}"

        try:
            logged = self._read_log(strict=True)
        except OutboxCorruptedError as exc:
            return self._mark_corrupted(str(exc))

        if raw_state is not None and bool(raw_state.get("corrupted")):
            return self._mark_corrupted(
                str(raw_state.get("corruption_reason") or "outbox marked corrupted")
            )

        if state_error is not None:
            if not logged:
                return self._mark_corrupted(state_error)
            reconstructed = self._state_from_log(logged)
            reconstructed["reconstructed"] = True
            self._reconstructed = True
            self._save_state(reconstructed)
            logger.warning("Reconstructed outbox state at %s from valid log", self.root)
            return reconstructed

        if raw_state is None:
            if not logged:
                return {
                    "events": {},
                    "next_sequence": 0,
                    "last_drain": None,
                    "corrupted": False,
                    "reconstructed": False,
                    "corruption_reason": None,
                }
            reconstructed = self._state_from_log(logged)
            reconstructed["reconstructed"] = True
            self._reconstructed = True
            self._save_state(reconstructed)
            return reconstructed

        state = dict(raw_state)
        events = state.get("events")
        if not isinstance(events, dict):
            return self._mark_corrupted("state events is not an object")
        next_sequence = _sequence_or_none(state.get("next_sequence", 0))
        if next_sequence is None:
            return self._mark_corrupted("state next_sequence is invalid")

        logged_by_id = {event.event_id: event for event in logged}
        if set(events) != set(logged_by_id):
            return self._mark_corrupted("state and event log identities differ")
        sequences: set[int] = set()
        for event_id, meta in events.items():
            if not isinstance(meta, dict):
                return self._mark_corrupted(f"state metadata for {event_id} is invalid")
            sequence = _sequence_or_none(meta.get("sequence"))
            if sequence is None:
                return self._mark_corrupted(f"state sequence for {event_id} is invalid")
            if sequence in sequences:
                return self._mark_corrupted("duplicate sequence in state metadata")
            sequences.add(sequence)
            if sequence != logged_by_id[event_id].sequence:
                return self._mark_corrupted(f"sequence mismatch for {event_id}")
            state_value = meta.get("state", _PENDING)
            if state_value not in {_PENDING, _DELIVERED, _FAILED}:
                return self._mark_corrupted(f"invalid delivery state for {event_id}")
        if sequences and next_sequence <= max(sequences):
            return self._mark_corrupted("next_sequence would reuse an existing sequence")
        state.setdefault("last_drain", None)
        state["corrupted"] = False
        state.setdefault("reconstructed", False)
        state.setdefault("corruption_reason", None)
        self._reconstructed = bool(state.get("reconstructed"))
        return state

    def _state_from_log(self, logged: List[OutboxEvent]) -> Dict[str, Any]:
        """Build pending metadata from a validated event log."""
        events: Dict[str, Any] = {}
        for event in logged:
            events[event.event_id] = {
                "state": _PENDING,
                "attempts": 0,
                "next_retry_at": None,
                "last_error": None,
                "enqueued_at": event.enqueued_at,
                "delivered_at": None,
                "table": event.table,
                "sequence": event.sequence,
                "schema_version": event.schema_version,
                "event_timestamp": event.event_timestamp,
                "ingestion_timestamp": event.ingestion_timestamp,
            }
        return {
            "events": events,
            "next_sequence": max((event.sequence for event in logged), default=-1) + 1,
            "last_drain": None,
            "corrupted": False,
            "reconstructed": False,
            "corruption_reason": None,
        }

    def _mark_corrupted(self, reason: str) -> Dict[str, Any]:
        """Persist a corruption marker without discarding the append-only log."""
        self._corrupted = True
        self._reconstructed = False
        self._corruption_reason = reason
        state = {
            "events": {},
            "next_sequence": 0,
            "last_drain": None,
            "corrupted": True,
            "reconstructed": False,
            "corruption_reason": reason,
        }
        if not self._state_path.is_symlink():
            try:
                self._save_state(state)
            except OSError:
                logger.error("Could not persist corruption marker at %s", self._state_path)
        logger.error("Outbox marked corrupted at %s: %s", self.root, reason)
        return state

    def _save_state(self, state: Dict[str, Any]) -> None:
        tmp = self._state_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self._state_path)

    def _append_log(self, event: OutboxEvent) -> None:
        with self._log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event.to_log_record(), ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def _read_log(self, *, strict: bool = False) -> List[OutboxEvent]:
        events: List[OutboxEvent] = []
        if self._log_path.is_symlink():
            if strict:
                raise OutboxCorruptedError("event log is a symlink")
            return events
        if not self._log_path.is_file():
            return events
        try:
            lines = self._log_path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            if strict:
                raise OutboxCorruptedError(f"event log unreadable: {exc}") from exc
            return events
        seen_ids: set[str] = set()
        seen_sequences: set[int] = set()
        for line in lines:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                if strict:
                    raise OutboxCorruptedError(f"event log contains invalid JSON: {exc}") from exc
                continue
            if isinstance(record, dict) and "event_id" in record:
                try:
                    event = OutboxEvent.from_log_record(record)
                except (KeyError, TypeError, ValueError) as exc:
                    if strict:
                        raise OutboxCorruptedError(f"event log record is invalid: {exc}") from exc
                    continue
                if event.event_id in seen_ids:
                    raise OutboxCorruptedError(f"duplicate event identity in log: {event.event_id}")
                if event.sequence in seen_sequences:
                    raise OutboxCorruptedError(
                        f"duplicate sequence in log: {event.sequence}"
                    )
                seen_ids.add(event.event_id)
                seen_sequences.add(event.sequence)
                events.append(event)
            elif strict:
                raise OutboxCorruptedError("event log contains a record without event_id")
        return events

    # -- write path ---------------------------------------------------------

    def enqueue(
        self,
        table: str,
        column_names: List[str],
        row: List[Any],
        *,
        event_id: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> OutboxEvent:
        """Durably enqueue one business event.

        The E1 envelope columns are prepended to ``row`` here so every sink
        receives a uniform, replay-safe contract.  Enqueueing is idempotent on
        ``event_id``: re-recording the same logical event returns the existing
        entry instead of appending a duplicate.
        """
        moment = now or _utcnow()
        payload = dict(zip(column_names, row))
        resolved_id = event_id or stable_event_id(table, payload)

        with self._lock, _outbox_process_lock(self._process_lock_path):
            state = self._load_state()
            if state.get("corrupted"):
                raise OutboxCorruptedError(
                    str(state.get("corruption_reason") or "outbox state is corrupted")
                )
            known = state["events"].get(resolved_id)
            if known is not None:
                return self._materialise(resolved_id, known, table, column_names, row, moment)

            sequence = int(state.get("next_sequence", 0))
            state["next_sequence"] = sequence + 1
            iso_now = _iso(moment)
            meta = {
                "state": _PENDING,
                "attempts": 0,
                "next_retry_at": None,
                "last_error": None,
                "enqueued_at": iso_now,
                "delivered_at": None,
                "table": table,
                "sequence": sequence,
                "schema_version": SCHEMA_VERSION,
                "event_timestamp": iso_now,
                "ingestion_timestamp": iso_now,
            }
            state["events"][resolved_id] = meta
            event = OutboxEvent(
                event_id=resolved_id,
                table=table,
                schema_version=SCHEMA_VERSION,
                sequence=sequence,
                event_timestamp=iso_now,
                ingestion_timestamp=iso_now,
                column_names=list(EVENT_COLUMNS) + list(column_names),
                row=[resolved_id, SCHEMA_VERSION, iso_now, iso_now, sequence] + list(row),
                state=_PENDING,
                attempts=0,
                enqueued_at=iso_now,
            )
            self._append_log(event)
            self._save_state(state)
            return event

    def _materialise(
        self,
        event_id: str,
        meta: Dict[str, Any],
        table: str,
        column_names: List[str],
        row: List[Any],
        moment: datetime,
    ) -> OutboxEvent:
        iso_now = _iso(moment)
        return OutboxEvent(
            event_id=event_id,
            table=str(meta.get("table", table)),
            schema_version=int(meta.get("schema_version", SCHEMA_VERSION)),
            sequence=int(meta.get("sequence", 0)),
            event_timestamp=str(meta.get("event_timestamp", iso_now)),
            ingestion_timestamp=str(meta.get("ingestion_timestamp", iso_now)),
            column_names=list(EVENT_COLUMNS) + list(column_names),
            row=[
                event_id,
                int(meta.get("schema_version", SCHEMA_VERSION)),
                str(meta.get("event_timestamp", iso_now)),
                str(meta.get("ingestion_timestamp", iso_now)),
                int(meta.get("sequence", 0)),
            ] + list(row),
            state=str(meta.get("state", _PENDING)),
            attempts=int(meta.get("attempts", 0)),
            next_retry_at=meta.get("next_retry_at"),
            last_error=meta.get("last_error"),
            enqueued_at=str(meta.get("enqueued_at", iso_now)),
            delivered_at=meta.get("delivered_at"),
        )

    # -- read path ----------------------------------------------------------

    def events(self) -> List[OutboxEvent]:
        """All durable events merged with their current delivery metadata."""
        with self._lock:
            state = self._load_state()
            if state.get("corrupted"):
                return []
            metas = state.get("events", {})
            merged: List[OutboxEvent] = []
            for logged in self._read_log(strict=True):
                meta = metas.get(logged.event_id)
                if meta:
                    logged.state = str(meta.get("state", logged.state))
                    logged.attempts = int(meta.get("attempts", logged.attempts))
                    logged.next_retry_at = meta.get("next_retry_at")
                    logged.last_error = meta.get("last_error")
                    logged.delivered_at = meta.get("delivered_at")
                merged.append(logged)
            return merged

    def status(self) -> Dict[str, Any]:
        """Visible delivery counters (Stage E2 / document line 149 & 151)."""
        with self._lock:
            state = self._load_state()
            if state.get("corrupted"):
                return {
                    "pending": None,
                    "failed": None,
                    "delivered": None,
                    "total": None,
                    "schema_version": SCHEMA_VERSION,
                    "last_enqueued_at": None,
                    "last_delivered_at": None,
                    "last_drain": None,
                    "drain_blocked_reason": "corrupt_outbox_state",
                    "corrupted": True,
                    "reconstructed": False,
                    "corruption_reason": state.get("corruption_reason"),
                }
            metas = state.get("events", {})
            pending = sum(1 for m in metas.values() if m.get("state") == _PENDING)
            failed = sum(1 for m in metas.values() if m.get("state") == _FAILED)
            delivered = sum(1 for m in metas.values() if m.get("state") == _DELIVERED)
            last_delivered = [
                m.get("delivered_at") for m in metas.values() if m.get("delivered_at")
            ]
            last_enqueued = [
                m.get("enqueued_at") for m in metas.values() if m.get("enqueued_at")
            ]
            last_drain = state.get("last_drain")
            return {
                "pending": pending,
                "failed": failed,
                "delivered": delivered,
                "total": len(metas),
                "schema_version": SCHEMA_VERSION,
                "last_enqueued_at": max(last_enqueued) if last_enqueued else None,
                "last_delivered_at": max(last_delivered) if last_delivered else None,
                "last_drain": last_drain,
                "drain_blocked_reason": (last_drain or {}).get("blocked_reason"),
                "corrupted": False,
                "reconstructed": bool(state.get("reconstructed")),
                "corruption_reason": None,
            }

    def ingestion_lag_seconds(self, now: Optional[datetime] = None) -> Optional[float]:
        """Seconds since the most recent successful delivery.

        Returns ``None`` (never ``0``) when nothing has been delivered yet, so
        an unknown lag is surfaced honestly rather than implied to be fresh.
        """
        moment = now or _utcnow()
        status = self.status()
        last = _parse_iso(status.get("last_delivered_at"))
        if last is None:
            return None
        return round((moment - last).total_seconds(), 3)

    # -- delivery path (off the request path) -------------------------------

    def drain_once(
        self,
        client_factory: Callable[[], Any],
        *,
        schema_ready: bool,
        now: Optional[datetime] = None,
        max_batch: int = DEFAULT_DRAIN_BATCH,
    ) -> DrainReport:
        """Perform one bounded delivery pass.  Never raises for sink errors.

        Delivery is skipped -- and honestly reported -- when the replay-safe
        schema is not confirmed or when no ClickHouse client is configured.  In
        both cases events stay ``pending``; nothing is lost and nothing is
        silently duplicated.
        """
        moment = now or _utcnow()
        report = DrainReport(schema_ready=bool(schema_ready))

        with self._lock:
            state = self._load_state()
            if state.get("corrupted"):
                report.corrupted = True
                report.pending = None
                report.blocked_reason = "corrupt_outbox_state"
                return report
            report.reconstructed = bool(state.get("reconstructed"))
            metas = state.get("events", {})
            report.pending = sum(1 for m in metas.values() if m.get("state") == _PENDING)

            if not schema_ready:
                report.blocked_reason = "replay_safe_schema_not_confirmed"
                self._record_drain(state, report, moment)
                return report

            due_events: List[OutboxEvent] = []
            logged_by_id = {e.event_id: e for e in self._read_log(strict=True)}
            for event_id, meta in metas.items():
                candidate = logged_by_id.get(event_id)
                if candidate is None:
                    continue
                candidate.state = str(meta.get("state", _PENDING))
                candidate.attempts = int(meta.get("attempts", 0))
                candidate.next_retry_at = meta.get("next_retry_at")
                if candidate.due(moment) and len(due_events) < max_batch:
                    due_events.append(candidate)
            due_events.sort(key=lambda e: e.sequence)

            if not due_events:
                self._record_drain(state, report, moment)
                return report

            client = None
            try:
                client = client_factory()
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("Outbox client factory failed: %s", exc)
            report.client_available = client is not None
            if client is None:
                report.blocked_reason = "clickhouse_unavailable_local_mirror_only"
                self._record_drain(state, report, moment)
                return report

            for event in due_events:
                report.attempted += 1
                meta = metas[event.event_id]
                try:
                    row_to_insert = []
                    for col, val in zip(event.column_names, event.row):
                        if (
                            col in ("event_timestamp", "ingestion_timestamp", "created_at")
                            or col.endswith(("_timestamp", "_at"))
                        ) and isinstance(val, str):
                            try:
                                val = datetime.fromisoformat(val)
                            except Exception:
                                pass
                        row_to_insert.append(val)
                    client.insert(event.table, [row_to_insert], column_names=event.column_names)
                except Exception as exc:
                    attempts = int(meta.get("attempts", 0)) + 1
                    meta["attempts"] = attempts
                    meta["last_error"] = str(exc)[:200]
                    if attempts >= MAX_DELIVERY_ATTEMPTS:
                        meta["state"] = _FAILED
                        report.failed += 1
                        report.pending -= 1
                    else:
                        meta["state"] = _PENDING
                        backoff = compute_backoff(attempts)
                        retry_at = datetime.fromtimestamp(
                            moment.timestamp() + backoff, tz=timezone.utc
                        )
                        meta["next_retry_at"] = _iso(retry_at)
                    logger.warning(
                        "Outbox delivery failed for %s (attempt %s): %s",
                        event.event_id, attempts, exc,
                    )
                else:
                    meta["state"] = _DELIVERED
                    meta["delivered_at"] = _iso(moment)
                    meta["attempts"] = int(meta.get("attempts", 0)) + 1
                    meta["last_error"] = None
                    report.delivered += 1
                    report.pending -= 1

            self._record_drain(state, report, moment)
            return report

    def _record_drain(self, state: Dict[str, Any], report: DrainReport, moment: datetime) -> None:
        summary = report.as_dict()
        summary["at"] = _iso(moment)
        state["last_drain"] = summary
        self._save_state(state)

    # -- recovery -----------------------------------------------------------

    def rehydrate_from_mirror(self, local_dir: Path) -> int:
        """Re-enqueue durable mirror files that are missing from the outbox.

        This is the recovery path that turns the historical local JSON mirror
        from a dead end into a replay source: after an outage (or if the outbox
        log was lost), the ``job_*.json`` / ``scenes_*.jsonl`` mirror files are
        re-enqueued so nothing recorded locally is permanently undelivered.
        Idempotent -- events already tracked by ``event_id`` are not duplicated.
        Returns the number of newly enqueued events.
        """
        local_dir = Path(local_dir)
        if not local_dir.is_dir() or local_dir.is_symlink():
            return 0
        with self._lock:
            state = self._load_state()
            if state.get("corrupted"):
                raise OutboxCorruptedError(
                    str(state.get("corruption_reason") or "outbox state is corrupted")
                )
        enqueued = 0
        for job_file in sorted(local_dir.glob("job_*.json")):
            if job_file.is_symlink():
                continue
            try:
                record = json.loads(job_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(record, dict):
                continue
            columns, row = _job_mirror_columns(record)
            before = self.status()["total"]
            self.enqueue(
                "video_pipeline_jobs",
                columns,
                row,
                event_id=stable_event_id(
                    "video_pipeline_jobs", {"job_id": record.get("job_id")}
                ),
            )
            if self.status()["total"] > before:
                enqueued += 1
        for scenes_file in sorted(local_dir.glob("scenes_*.jsonl")):
            if scenes_file.is_symlink():
                continue
            try:
                lines = scenes_file.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            for line in lines:
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict):
                    continue
                columns, row = _scene_mirror_columns(record)
                before = self.status()["total"]
                self.enqueue(
                    "video_scene_telemetry",
                    columns,
                    row,
                    event_id=stable_event_id(
                        "video_scene_telemetry",
                        {
                            "job_id": record.get("job_id"),
                            "scene_id": record.get("scene_id"),
                        },
                    ),
                )
                if self.status()["total"] > before:
                    enqueued += 1
        for qa_file in sorted(local_dir.glob("qa_*.jsonl")):
            if qa_file.is_symlink():
                continue
            try:
                lines = qa_file.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            for line in lines:
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict):
                    continue
                columns, row = _qa_mirror_columns(record)
                before = self.status()["total"]
                self.enqueue(
                    "video_qa_records",
                    columns,
                    row,
                    event_id=stable_event_id(
                        "video_qa_records",
                        {
                            "job_id": record.get("job_id"),
                            "check_name": record.get("check_name"),
                        },
                    ),
                )
                if self.status()["total"] > before:
                    enqueued += 1
        return enqueued

    # -- background drain (production only) ---------------------------------

    def maybe_start_background_drain(
        self,
        client_factory: Callable[[], Any],
        schema_ready: Callable[[], bool],
        *,
        interval_seconds: float = 15.0,
    ) -> bool:
        """Lazily start a single daemon drain thread.

        Only starts when a ClickHouse host is configured and the drain is not
        explicitly disabled, so hermetic tests (no host) never spawn threads.
        The thread does the ClickHouse I/O, keeping delivery off the request
        path entirely.
        """
        if not os.getenv("CLICKHOUSE_HOST"):
            return False
        if os.getenv("TELEMETRY_OUTBOX_DRAIN_DISABLED", "").lower() in ("1", "true", "yes"):
            return False
        with self._lock:
            if self._drain_thread is not None and self._drain_thread.is_alive():
                return False
            stop = threading.Event()

            def _loop() -> None:
                while not stop.is_set():
                    try:
                        self.drain_once(client_factory, schema_ready=schema_ready())
                    except Exception as exc:  # pragma: no cover - defensive
                        logger.warning("Background outbox drain error: %s", exc)
                    stop.wait(interval_seconds)

            thread = threading.Thread(
                target=_loop, name="telemetry-outbox-drain", daemon=True
            )
            self._drain_thread = thread
            thread.start()
            return True


# ---------------------------------------------------------------------------
# Mirror -> outbox column mapping (recovery helper)
# ---------------------------------------------------------------------------

_JOB_MIRROR_COLUMNS = [
    "job_id", "title", "duration_sec", "voice_mode", "status",
    "total_render_time_ms", "total_tokens_used", "cost_usd", "qa_passed",
    "studio_name", "language", "genre",
]

_SCENE_MIRROR_COLUMNS = [
    "job_id", "scene_id", "treatment_type", "render_time_ms",
    "vertex_latency_ms", "evidence_claim_count", "segment_hash",
]

_QA_MIRROR_COLUMNS = ["job_id", "check_name", "passed", "detail"]


def _job_mirror_columns(record: Dict[str, Any]) -> "tuple[List[str], List[Any]]":
    row = [record.get(col) for col in _JOB_MIRROR_COLUMNS]
    return list(_JOB_MIRROR_COLUMNS), row


def _scene_mirror_columns(record: Dict[str, Any]) -> "tuple[List[str], List[Any]]":
    row = [record.get(col) for col in _SCENE_MIRROR_COLUMNS]
    return list(_SCENE_MIRROR_COLUMNS), row


def _qa_mirror_columns(record: Dict[str, Any]) -> "tuple[List[str], List[Any]]":
    row = [record.get(col) for col in _QA_MIRROR_COLUMNS]
    return list(_QA_MIRROR_COLUMNS), row


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_outbox_singleton: Optional[TelemetryOutbox] = None
_singleton_lock = threading.Lock()


def get_outbox(base_dir: Optional[Path] = None) -> TelemetryOutbox:
    """Return the process outbox (rooted at ``base_dir`` when provided).

    When ``base_dir`` is supplied a scoped instance is returned (used by tests
    and by callers that record into a job-local directory); otherwise the
    shared singleton rooted at ``output/telemetry/outbox`` is used.
    """
    global _outbox_singleton
    if base_dir is not None:
        return TelemetryOutbox(Path(base_dir) / "outbox")
    with _singleton_lock:
        if _outbox_singleton is None:
            _outbox_singleton = TelemetryOutbox(Path("output/telemetry/outbox"))
        return _outbox_singleton


def reset_outbox_singleton() -> None:
    """Test helper: drop the cached singleton."""
    global _outbox_singleton
    with _singleton_lock:
        _outbox_singleton = None


__all__ = [
    "SCHEMA_VERSION",
    "EVENT_COLUMNS",
    "MAX_DELIVERY_ATTEMPTS",
    "BASE_BACKOFF_SECONDS",
    "MAX_BACKOFF_SECONDS",
    "OutboxEvent",
    "OutboxCorruptedError",
    "DrainReport",
    "TelemetryOutbox",
    "compute_backoff",
    "stable_event_id",
    "get_outbox",
    "reset_outbox_singleton",
]
