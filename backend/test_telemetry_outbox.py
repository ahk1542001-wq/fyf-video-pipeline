"""Tests for the durable, replay-safe telemetry outbox (Stage E2)."""

from __future__ import annotations

import json
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
