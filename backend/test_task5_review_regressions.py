"""Regression tests for the independent Task 5 core review findings.

These tests stay on the additive budget/telemetry seams.  They do not start a
provider or a live ClickHouse client; the process tests only exercise the local
durable files under a temporary directory.
"""

from __future__ import annotations

import json
import multiprocessing
from pathlib import Path

import pytest

import backend.clickhouse_telemetry as clickhouse
from backend.budget_store import (
    get_budget_status,
    reconcile_budget,
    record_cost,
    reserve_budget,
    set_project_budget,
)
from backend.telemetry_outbox import TelemetryOutbox
from backend.telemetry_reconcile import reconcile_sources
from backend.telemetry_store import record_job_telemetry


def _reserve_worker(root: str, start: object, results: object, index: int) -> None:
    """Run one budget reservation in a separate process."""
    start.wait()  # type: ignore[attr-defined]
    result = reserve_budget(f"cross-process-{index}", 0.6, root_dir=Path(root))
    results.put(result)  # type: ignore[attr-defined]


def _enqueue_worker(root: str, start: object, results: object, index: int) -> None:
    """Run one outbox enqueue in a separate process."""
    start.wait()  # type: ignore[attr-defined]
    try:
        event = TelemetryOutbox(Path(root)).enqueue(
            "video_pipeline_jobs",
            ["job_id"],
            [f"job-{index}"],
            event_id=f"cross-process-event-{index}",
        )
        results.put(("ok", event.sequence))  # type: ignore[attr-defined]
    except Exception as exc:  # pragma: no cover - failure is asserted in parent
        results.put(("error", type(exc).__name__, str(exc)))  # type: ignore[attr-defined]


def _run_processes(worker, root: Path, count: int = 8) -> list[object]:
    """Release a group of forked workers together and collect their results."""
    context = multiprocessing.get_context("fork")
    start = context.Event()
    results = context.Queue()
    processes = [
        context.Process(target=worker, args=(str(root), start, results, index))
        for index in range(count)
    ]
    for process in processes:
        process.start()
    start.set()
    values = [results.get(timeout=10) for _ in processes]
    for process in processes:
        process.join(timeout=10)
        assert process.exitcode == 0
    return values


def test_budget_reservation_is_atomic_across_processes(tmp_path: Path, monkeypatch):
    """Two processes must not both spend the same remaining account budget."""
    monkeypatch.setenv("FYF_DAILY_BUDGET_CAP_USD", "1.0")
    monkeypatch.setenv("FYF_TOTAL_BUDGET_CAP_USD", "1.0")

    results = _run_processes(_reserve_worker, tmp_path, count=8)

    assert sum(result[0] is True for result in results) == 1
    assert sum(result[0] is False for result in results) == 7
    status = get_budget_status(root_dir=tmp_path)
    assert status["active_reserved_usd"] == 0.6


def test_outbox_sequence_is_atomic_across_processes(tmp_path: Path):
    """A shared outbox must never reuse or lose a sequence under process load."""
    root = tmp_path / "outbox"

    results = _run_processes(_enqueue_worker, root)

    assert all(result[0] == "ok" for result in results), results
    sequences = sorted(result[1] for result in results)
    assert sequences == list(range(len(results)))
    events = TelemetryOutbox(root).events()
    assert sorted(event.sequence for event in events) == sequences
    assert len({event.event_id for event in events}) == len(results)


def test_reconcile_rejects_a_reservation_bound_to_another_project(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("FYF_DAILY_BUDGET_CAP_USD", "10.0")
    monkeypatch.setenv("FYF_TOTAL_BUDGET_CAP_USD", "10.0")
    assert reserve_budget(
        "project-bound-operation",
        0.5,
        project_id="project-a",
        root_dir=tmp_path,
    )[0]

    with pytest.raises(ValueError, match="identity"):
        reconcile_budget(
            "project-bound-operation",
            0.25,
            project_id="project-b",
            root_dir=tmp_path,
        )

    status_a = get_budget_status(root_dir=tmp_path, project_id="project-a")
    status_b = get_budget_status(root_dir=tmp_path, project_id="project-b")
    assert status_a["active_reserved_usd"] == 0.5
    assert status_a["total_spend_usd"] == 0.0
    assert status_b["project_spend_usd"] == 0.0


class _SchemaIdentityClient:
    def __init__(self, create_sql: str):
        self.create_sql = create_sql

    def query(self, sql: str, **kwargs):
        del sql, kwargs
        result = type("Result", (), {})()
        result.result_rows = [
            ("ReplacingMergeTree", self.create_sql),
        ]
        return result


@pytest.mark.parametrize(
    "order_clause",
    ["(job_id, event_id)", "(occurred_at)"],
)
def test_clickhouse_requires_stable_event_id_order_key(order_clause: str):
    """Mentioning event_id in a column/query is not enough for replay safety."""
    ddl = f"CREATE TABLE video_pipeline_jobs (event_id String) ENGINE = ReplacingMergeTree ORDER BY {order_clause}"
    assert clickhouse.verify_replay_safe_schema(_SchemaIdentityClient(ddl)) is False


def test_project_budget_availability_includes_account_caps(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("FYF_DAILY_BUDGET_CAP_USD", "1.0")
    monkeypatch.setenv("FYF_TOTAL_BUDGET_CAP_USD", "10.0")
    set_project_budget("project-a", 5.0, root_dir=tmp_path)
    record_cost(1.0, root_dir=tmp_path, project_id="project-a")

    status = get_budget_status(root_dir=tmp_path, project_id="project-a")
    assert status["project_remaining_usd"] == 4.0
    assert status["project_budget_exceeded"] is False
    assert status["project_budget_available"] is False


def test_nan_and_inf_are_unavailable_in_telemetry_clickhouse_and_reconcile(
    tmp_path: Path,
):
    record_job_telemetry(
        "abcd1234",
        {
            "model_name": "gemini-3.7-flash",
            "input_tokens": float("inf"),
            "output_tokens": float("nan"),
            "total_duration_ms": float("inf"),
            "render_duration_ms": float("nan"),
            "stage_duration_ms": {"script": float("inf")},
            "calls": [
                {"duration_ms": float("inf"), "usage": {"input_tokens": float("nan")}}
            ],
        },
        base_dir=tmp_path,
    )
    saved = json.loads((tmp_path / "abcd1234.json").read_text(encoding="utf-8"))
    assert saved["input_tokens"] is None
    assert saved["output_tokens"] is None
    assert saved["total_duration_ms"] is None
    assert saved["render_duration_ms"] is None
    assert saved["stage_duration_ms"]["script"] is None
    assert saved["calls"][0]["duration_ms"] is None
    assert saved["calls"][0]["usage"]["input_tokens"] is None

    event = next(iter(TelemetryOutbox(tmp_path / "outbox").events()))
    row = dict(zip(event.column_names, event.row))
    assert row["total_render_time_ms"] is None
    assert row["total_tokens_used"] is None
    assert row["cost_usd"] is None

    report = reconcile_sources(
        job_records=[{"total_tokens_used": float("inf"), "cost_usd": float("nan")}],
        provider_usage=[{"total_tokens": 10, "cost_usd": 0.1}],
        clickhouse_aggregates=[{"total_tokens_used": 10, "cost_usd": 0.1}],
    )
    assert report["sources"]["canonical_jobs"]["total_tokens"] is None
    assert report["sources"]["canonical_jobs"]["total_cost_usd"] is None
    assert report["status"] == "ok"
