"""Tests for the durable, replay-safe telemetry outbox (Stage E2)."""

from __future__ import annotations

import json
import multiprocessing
import os
import stat
import threading
from concurrent.futures import TimeoutError as FutureTimeoutError
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from backend.telemetry_outbox import (
    MAX_DELIVERY_ATTEMPTS,
    SCHEMA_VERSION,
    TelemetryOutbox,
    compute_backoff,
    get_outbox,
    stable_event_id,
)
import backend.telemetry_outbox as telemetry_outbox_module

JOB_COLUMNS = ["job_id", "status", "cost_usd"]


class RecordingClient:
    """Fake ClickHouse client that records inserts (or fails on demand)."""

    def __init__(self, fail: bool = False) -> None:
        self.inserted: list[tuple] = []
        self.fail = fail
        self.insert_calls = 0

    def insert(self, table, rows, column_names=None):
        self.insert_calls += 1
        if self.fail:
            raise RuntimeError("simulated ClickHouse outage")
        for row in rows:
            self.inserted.append((table, tuple(column_names), tuple(row)))


def _enqueue_jobs(outbox: TelemetryOutbox, count: int) -> list[str]:
    ids = []
    for index in range(count):
        event = outbox.enqueue(
            "video_pipeline_jobs", JOB_COLUMNS, [f"job{index}", "completed", float(index)]
        )
        ids.append(event.event_id)
    return ids


class _BlockingProcessClient:
    """Coordinate two drain workers so a duplicate insert is observable."""

    def __init__(self, calls, first_insert, second_insert, release):
        self.calls = calls
        self.first_insert = first_insert
        self.second_insert = second_insert
        self.release = release

    def insert(self, table, rows, column_names=None):
        del table, rows, column_names
        with self.calls.get_lock():
            self.calls.value += 1
            call_number = self.calls.value
        if call_number == 1:
            self.first_insert.set()
            assert self.release.wait(timeout=5)
        else:
            self.second_insert.set()


def _drain_worker(root, start, calls, first_insert, second_insert, release, results):
    start.wait(timeout=5)
    try:
        outbox = TelemetryOutbox(Path(root))
        report = outbox.drain_once(
            lambda: _BlockingProcessClient(calls, first_insert, second_insert, release),
            schema_ready=True,
        )
        results.put(("ok", report.delivered, report.pending))
    except Exception as exc:  # pragma: no cover - failure is asserted in parent
        results.put(("error", type(exc).__name__, str(exc)))


def test_stable_event_id_is_deterministic_and_content_addressed():
    payload = {"job_id": "abcd1234", "cost_usd": 1.5}
    first = stable_event_id("video_pipeline_jobs", payload)
    second = stable_event_id("video_pipeline_jobs", dict(reversed(list(payload.items()))))
    assert first == second  # order-independent / stable across replays
    assert first != stable_event_id("video_pipeline_jobs", {"job_id": "abcd1234", "cost_usd": 2.5})


def test_enqueue_is_idempotent_on_event_id(tmp_path: Path):
    outbox = get_outbox(tmp_path)
    _enqueue_jobs(outbox, 1)
    # Re-record the exact same logical event (a replay).
    _enqueue_jobs(outbox, 1)
    status = outbox.status()
    assert status["total"] == 1
    assert status["pending"] == 1
    # The durable log holds exactly one physical record too.
    assert len(outbox.events()) == 1


def test_enqueue_injects_replay_safe_envelope_columns(tmp_path: Path):
    outbox = get_outbox(tmp_path)
    event = outbox.enqueue("video_pipeline_jobs", JOB_COLUMNS, ["abcd1234", "completed", 1.0])
    assert event.column_names[:5] == [
        "event_id", "schema_version", "event_timestamp", "ingestion_timestamp", "sequence",
    ]
    assert event.row[0] == event.event_id
    assert event.row[1] == SCHEMA_VERSION
    assert event.schema_version == SCHEMA_VERSION
    assert event.sequence == 0


def test_sequence_is_monotonic(tmp_path: Path):
    outbox = get_outbox(tmp_path)
    seqs = []
    for index in range(4):
        seqs.append(outbox.enqueue("video_pipeline_jobs", JOB_COLUMNS, [f"j{index}", "x", 0.0]).sequence)
    assert seqs == [0, 1, 2, 3]


def test_atomic_state_writes_do_not_share_a_temporary_path(tmp_path: Path, monkeypatch):
    """Independent workers must not race on one fixed ``state.json.tmp`` file."""
    first = TelemetryOutbox(tmp_path)
    second = TelemetryOutbox(tmp_path)
    barrier = threading.Barrier(2)
    real_replace = os.replace

    def synchronized_replace(source, destination):
        barrier.wait(timeout=2)
        real_replace(source, destination)

    monkeypatch.setattr(telemetry_outbox_module.os, "replace", synchronized_replace)
    states = [
        {"events": {}, "next_sequence": value, "last_drain": None}
        for value in (1, 2)
    ]
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(outbox._save_state, state)
            for outbox, state in zip((first, second), states)
        ]
        for future in futures:
            future.result()

    assert json.loads((tmp_path / "state.json").read_text(encoding="utf-8")) in states


def test_concurrent_process_drains_do_not_insert_the_same_event_twice(tmp_path: Path):
    """A process lock must cover drain read, delivery, and metadata save."""
    if telemetry_outbox_module.fcntl is None:
        return

    outbox = TelemetryOutbox(tmp_path)
    _enqueue_jobs(outbox, 1)
    context = multiprocessing.get_context("fork")
    start = context.Event()
    first_insert = context.Event()
    second_insert = context.Event()
    release = context.Event()
    calls = context.Value("i", 0)
    results = context.Queue()
    processes = [
        context.Process(
            target=_drain_worker,
            args=(tmp_path, start, calls, first_insert, second_insert, release, results),
        )
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    start.set()

    assert first_insert.wait(timeout=5)
    assert not second_insert.wait(timeout=0.5)
    release.set()

    worker_results = [results.get(timeout=5) for _ in processes]
    for process in processes:
        process.join(timeout=5)
        assert process.exitcode == 0

    assert worker_results.count(("ok", 1, 0)) == 1
    assert worker_results.count(("ok", 0, 0)) == 1
    assert calls.value == 1


def test_enqueue_completes_while_drain_client_is_blocked(tmp_path: Path):
    """A stalled sink must not hold the shared enqueue/state lock."""
    if telemetry_outbox_module.fcntl is None:
        return

    outbox = TelemetryOutbox(tmp_path)
    _enqueue_jobs(outbox, 1)
    context = multiprocessing.get_context("fork")
    start = context.Event()
    first_insert = context.Event()
    second_insert = context.Event()
    release = context.Event()
    calls = context.Value("i", 0)
    results = context.Queue()
    process = context.Process(
        target=_drain_worker,
        args=(tmp_path, start, calls, first_insert, second_insert, release, results),
    )
    process.start()
    start.set()
    assert first_insert.wait(timeout=5)

    pool = ThreadPoolExecutor(max_workers=1)
    future = pool.submit(
        outbox.enqueue,
        "video_pipeline_jobs",
        JOB_COLUMNS,
        ["new-job", "completed", 1.0],
    )
    enqueue_error = None
    new_event = None
    try:
        try:
            new_event = future.result(timeout=1.5)
        except FutureTimeoutError as exc:
            enqueue_error = exc
        except Exception as exc:
            enqueue_error = exc
    finally:
        release.set()
        pool.shutdown(wait=True)

    worker_result = results.get(timeout=5)
    process.join(timeout=5)
    assert process.exitcode == 0
    assert enqueue_error is None, enqueue_error
    assert new_event is not None
    assert worker_result[0] == "ok"
    assert calls.value == 1
    assert outbox.status()["total"] == 2
    assert outbox.status()["pending"] == 1


def test_rehydrate_count_does_not_use_concurrent_total_snapshots(
    tmp_path: Path, monkeypatch
):
    """An unrelated writer must not make an idempotent rehydrate look new."""
    outbox = TelemetryOutbox(tmp_path / "outbox")
    mirror_dir = tmp_path / "mirror"
    mirror_dir.mkdir()
    (mirror_dir / "job_existing.json").write_text(
        json.dumps({"job_id": "existing"}), encoding="utf-8"
    )
    outbox.enqueue("video_pipeline_jobs", ["job_id"], ["existing"])

    real_status = outbox.status
    status_calls = 0

    def status_with_unrelated_writer():
        nonlocal status_calls
        result = real_status()
        status_calls += 1
        if status_calls == 1:
            TelemetryOutbox(outbox.root).enqueue(
                "video_pipeline_jobs",
                ["job_id"],
                ["unrelated"],
                event_id="unrelated-writer-event",
            )
        return result

    monkeypatch.setattr(outbox, "status", status_with_unrelated_writer)

    assert outbox.rehydrate_from_mirror(mirror_dir) == 0


def test_state_replace_fsyncs_parent_directory(tmp_path: Path, monkeypatch):
    """Replacing state must durably persist the directory entry as well."""
    outbox = TelemetryOutbox(tmp_path)
    fsynced_modes = []
    real_fsync = os.fsync

    def recording_fsync(descriptor):
        fsynced_modes.append(os.fstat(descriptor).st_mode)
        return real_fsync(descriptor)

    monkeypatch.setattr(telemetry_outbox_module.os, "fsync", recording_fsync)
    outbox._save_state({"events": {}, "next_sequence": 0, "last_drain": None})

    assert any(stat.S_ISDIR(mode) for mode in fsynced_modes)


def test_backoff_is_exponential_and_bounded():
    assert compute_backoff(1) == 1.0
    assert compute_backoff(2) == 2.0
    assert compute_backoff(3) == 4.0
    assert compute_backoff(50) == 300.0  # ceiling, never unbounded


def test_outage_then_recovery_delivers_all_without_duplicates(tmp_path: Path):
    outbox = get_outbox(tmp_path)
    ids = _enqueue_jobs(outbox, 5)

    # Outage: no client configured -> nothing delivered, nothing lost, honest reason.
    outage = outbox.drain_once(lambda: None, schema_ready=True)
    assert outage.delivered == 0
    assert outage.pending == 5
    assert outage.blocked_reason == "clickhouse_unavailable_local_mirror_only"
    assert outbox.status()["pending"] == 5

    # Recovery: a real client drains the durable backlog exactly once each.
    client = RecordingClient()
    recovered = outbox.drain_once(lambda: client, schema_ready=True)
    assert recovered.delivered == 5
    assert recovered.pending == 0
    status = outbox.status()
    assert status["pending"] == 0
    assert status["delivered"] == 5
    assert status["failed"] == 0
    delivered_ids = [row[2][0] for row in client.inserted]  # event_id is column 0
    assert sorted(delivered_ids) == sorted(ids)
    assert len(set(delivered_ids)) == 5  # no duplicates


def test_drain_is_gated_on_replay_safe_schema(tmp_path: Path):
    outbox = get_outbox(tmp_path)
    _enqueue_jobs(outbox, 3)
    client = RecordingClient()
    report = outbox.drain_once(lambda: client, schema_ready=False)
    assert report.delivered == 0
    assert report.blocked_reason == "replay_safe_schema_not_confirmed"
    assert client.insert_calls == 0  # never writes when dedup schema is unconfirmed
    assert outbox.status()["pending"] == 3  # events preserved, not lost


def test_delivery_failure_is_visible_bounded_and_lossless(tmp_path: Path):
    outbox = get_outbox(tmp_path)
    _enqueue_jobs(outbox, 1)
    client = RecordingClient(fail=True)
    now = datetime(2026, 9, 1, tzinfo=timezone.utc)

    attempts_seen = []
    for attempt in range(MAX_DELIVERY_ATTEMPTS + 3):
        report = outbox.drain_once(lambda: client, schema_ready=True, now=now)
        attempts_seen.append(report.attempted)
        status = outbox.status()
        if status["failed"] == 1:
            break
        # Advance beyond the exponential backoff so the retry becomes due.
        now = now + timedelta(seconds=compute_backoff(status_pending_attempts(attempt)) + 1)

    status = outbox.status()
    assert status["failed"] == 1          # visible failed counter
    assert status["pending"] == 0
    assert status["delivered"] == 0
    # Bounded: exactly MAX_DELIVERY_ATTEMPTS insert attempts, no infinite retry.
    assert client.insert_calls == MAX_DELIVERY_ATTEMPTS
    # Lossless: the event is still durably present in the log.
    assert len(outbox.events()) == 1
    assert outbox.events()[0].state == "failed"
    assert outbox.events()[0].attempts == MAX_DELIVERY_ATTEMPTS


def status_pending_attempts(_attempt: int) -> int:
    # Helper kept simple: backoff for the next attempt index (1-based).
    return _attempt + 1


def test_successful_delivery_after_transient_failures(tmp_path: Path):
    outbox = get_outbox(tmp_path)
    _enqueue_jobs(outbox, 1)
    now = datetime(2026, 9, 1, tzinfo=timezone.utc)

    flaky = RecordingClient(fail=True)
    outbox.drain_once(lambda: flaky, schema_ready=True, now=now)
    assert outbox.status()["pending"] == 1
    assert outbox.status()["failed"] == 0

    now = now + timedelta(seconds=compute_backoff(1) + 1)
    healthy = RecordingClient()
    report = outbox.drain_once(lambda: healthy, schema_ready=True, now=now)
    assert report.delivered == 1
    assert outbox.status()["delivered"] == 1
    assert outbox.status()["pending"] == 0


def test_ingestion_lag_is_none_until_first_delivery(tmp_path: Path):
    outbox = get_outbox(tmp_path)
    _enqueue_jobs(outbox, 1)
    assert outbox.ingestion_lag_seconds() is None  # unknown, never 0
    now = datetime(2026, 9, 1, tzinfo=timezone.utc)
    outbox.drain_once(lambda: RecordingClient(), schema_ready=True, now=now)
    lag = outbox.ingestion_lag_seconds(now=now)
    assert lag == 0.0  # delivered exactly at `now`


def test_rehydrate_from_mirror_replays_local_json(tmp_path: Path):
    local_dir = tmp_path
    (local_dir / "job_abcd1234.json").write_text(
        json.dumps(
            {
                "job_id": "abcd1234", "title": "T", "duration_sec": 1.0,
                "voice_mode": "gemini", "status": "completed",
                "total_render_time_ms": 10, "total_tokens_used": 5,
                "cost_usd": 0.1, "qa_passed": 1, "studio_name": "S",
                "language": "my-MM", "genre": "g",
            }
        ),
        encoding="utf-8",
    )
    outbox = TelemetryOutbox(tmp_path / "recovered")
    assert outbox.rehydrate_from_mirror(local_dir) == 1
    # Idempotent: a second recovery pass adds nothing.
    assert outbox.rehydrate_from_mirror(local_dir) == 0
    assert outbox.status()["total"] == 1
    # And the recovered event is deliverable.
    report = outbox.drain_once(lambda: RecordingClient(), schema_ready=True)
    assert report.delivered == 1


def test_maybe_start_background_drain_is_inert_without_a_host(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("CLICKHOUSE_HOST", raising=False)
    outbox = get_outbox(tmp_path)
    started = outbox.maybe_start_background_drain(lambda: None, lambda: True)
    assert started is False  # no thread, no delivery off a hermetic test
