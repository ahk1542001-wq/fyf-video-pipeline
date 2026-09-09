"""Focused Task 5 core tests (budget, telemetry, outbox and reconciliation).

These tests deliberately exercise the additive core APIs without touching the
FastAPI/UI integration surface.  They are hermetic: no provider or live
ClickHouse calls are made.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import backend.clickhouse_telemetry as clickhouse
from backend.budget_store import (
    ApprovalError,
    begin_approval,
    consume_approval,
    get_budget_status,
    get_project_budget,
    record_approval,
    reconcile_budget,
    revoke_approval,
    rollback_approval,
    reserve_budget,
    set_project_budget,
)
from backend.telemetry_outbox import TelemetryOutbox
from backend.telemetry_reconcile import build_reconciliation_report
from backend.telemetry_store import record_job_telemetry


class EngineClient:
    def __init__(self, engine: str) -> None:
        self.engine = engine
        self.commands: list[str] = []

    def command(self, sql: str):
        self.commands.append(sql)

    def query(self, sql: str, **kwargs):
        result = type("Result", (), {})()
        result.column_names = ["engine", "create_table_query"]
        result.result_rows = [
            (self.engine, "CREATE TABLE ... ENGINE = " + self.engine + " ORDER BY (event_id)")
        ]
        return result


def test_project_budget_persists_and_is_bounded_by_account_caps(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("FYF_DAILY_BUDGET_CAP_USD", "4")
    monkeypatch.setenv("FYF_TOTAL_BUDGET_CAP_USD", "5")
    monkeypatch.setenv("FYF_PROJECT_DEFAULT_BUDGET_USD", "3")

    configured = set_project_budget("project-a", 10, actor="victor", root_dir=tmp_path)
    assert configured["project_id"] == "project-a"
    assert configured["budget_usd"] == 10.0
    assert get_project_budget("project-a", root_dir=tmp_path)["budget_usd"] == 10.0

    status = get_budget_status(root_dir=tmp_path, project_id="project-a")
    assert status["project_budget_usd"] == 10.0
    assert status["effective_project_cap_usd"] == 5.0
    assert status["project_budget_available"] is True


def test_project_reservation_enforces_project_ceiling_without_cross_project_leak(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("FYF_DAILY_BUDGET_CAP_USD", "20")
    monkeypatch.setenv("FYF_TOTAL_BUDGET_CAP_USD", "20")
    set_project_budget("project-a", 1, root_dir=tmp_path)

    from backend.budget_store import reserve_budget

    assert reserve_budget("a-1", 0.75, project_id="project-a", root_dir=tmp_path)[0]
    denied, reason = reserve_budget("a-2", 0.5, project_id="project-a", root_dir=tmp_path)
    assert denied is False
    assert "Project" in (reason or "")
    assert reserve_budget("b-1", 0.5, project_id="project-b", root_dir=tmp_path)[0]


def test_approval_is_exact_target_expiring_revocable_and_one_shot(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("FYF_DAILY_BUDGET_CAP_USD", "10")
    monkeypatch.setenv("FYF_TOTAL_BUDGET_CAP_USD", "10")
    now = datetime(2026, 9, 8, tzinfo=timezone.utc)
    record_approval(
        "render",
        approved_spend_usd=1.0,
        decision="approved",
        actor="victor",
        project_id="project-a",
        target_ref="project-a@v2",
        approval_id="approval-1",
        expires_at=(now + timedelta(minutes=5)).isoformat(),
        root_dir=tmp_path,
    )

    consumed = consume_approval(
        "approval-1",
        operation="render",
        project_id="project-a",
        target_ref="project-a@v2",
        now=now,
        root_dir=tmp_path,
    )
    assert consumed["consumed_at"]
    with pytest.raises(Exception):
        consume_approval(
            "approval-1",
            operation="render",
            project_id="project-a",
            target_ref="project-a@v2",
            now=now,
            root_dir=tmp_path,
        )

    record_approval(
        "render",
        approved_spend_usd=1.0,
        decision="approved",
        actor="victor",
        project_id="project-a",
        target_ref="project-a@v3",
        approval_id="approval-2",
        expires_at=(now - timedelta(seconds=1)).isoformat(),
        root_dir=tmp_path,
    )
    with pytest.raises(Exception):
        consume_approval(
            "approval-2",
            operation="render",
            project_id="project-a",
            target_ref="project-a@v3",
            now=now,
            root_dir=tmp_path,
        )

    record_approval(
        "render",
        approved_spend_usd=1.0,
        decision="approved",
        actor="victor",
        project_id="project-a",
        target_ref="project-a@v4",
        approval_id="approval-3",
        root_dir=tmp_path,
    )
    assert revoke_approval("approval-3", actor="victor", root_dir=tmp_path)["status"] == "revoked"
    with pytest.raises(Exception):
        consume_approval(
            "approval-3",
            operation="render",
            project_id="project-a",
            target_ref="project-a@v4",
            now=now,
            root_dir=tmp_path,
        )


def test_approval_claim_failure_is_terminal_and_reservation_identity_is_bound(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("FYF_DAILY_BUDGET_CAP_USD", "10")
    monkeypatch.setenv("FYF_TOTAL_BUDGET_CAP_USD", "10")
    record_approval(
        "render",
        approved_spend_usd=1.0,
        decision="approved",
        actor="victor",
        project_id="project-a",
        target_ref="project-a@v2",
        approval_id="approval-rollback",
        root_dir=tmp_path,
    )
    begin_approval(
        "approval-rollback",
        operation="render",
        project_id="project-a",
        target_ref="project-a@v2",
        root_dir=tmp_path,
    )
    assert rollback_approval("approval-rollback", root_dir=tmp_path)["status"] == "failed"
    with pytest.raises(ApprovalError):
        consume_approval(
            "approval-rollback",
            operation="render",
            project_id="project-a",
            target_ref="project-a@v2",
            root_dir=tmp_path,
        )

    set_project_budget("project-a", 2.0, root_dir=tmp_path)
    assert reserve_budget("same-op", 0.5, project_id="project-a", root_dir=tmp_path)[0]
    allowed, reason = reserve_budget("same-op", 0.5, project_id="project-b", root_dir=tmp_path)
    assert allowed is False
    assert reason and "identity" in reason.lower()


def test_approval_idempotency_key_does_not_create_a_second_decision(tmp_path: Path):
    first = record_approval(
        "render",
        approved_spend_usd=1.0,
        decision="approved",
        actor="victor",
        project_id="project-a",
        target_ref="project-a@v5",
        idempotency_key="approval-request-5",
        root_dir=tmp_path,
    )
    second = record_approval(
        "render",
        approved_spend_usd=1.0,
        decision="approved",
        actor="victor",
        project_id="project-a",
        target_ref="project-a@v5",
        idempotency_key="approval-request-5",
        root_dir=tmp_path,
    )
    assert second["approval_id"] == first["approval_id"]


def test_unknown_budget_reconciliation_never_releases_or_debits_as_zero(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("FYF_DAILY_BUDGET_CAP_USD", "10")
    monkeypatch.setenv("FYF_TOTAL_BUDGET_CAP_USD", "10")
    assert reserve_budget("unknown-charge", 0.5, project_id="project-a", root_dir=tmp_path)[0]
    ledger = reconcile_budget("unknown-charge", None, project_id="project-a", root_dir=tmp_path)
    assert ledger["total_spend_usd"] == 0.0
    assert "unknown-charge" in ledger["unknown_reconciliations"]
    assert get_budget_status(root_dir=tmp_path, project_id="project-a")["project_reserved_usd"] == 0.5


def test_unknown_job_telemetry_is_none_and_records_all_event_kinds(tmp_path: Path):
    record_job_telemetry(
        "abcd1234",
        {
            "status": "completed",
            "scenes": [{"scene_id": "S1", "render_time_ms": None}],
            "qa_records": [{"check_name": "captions", "passed": None}],
            "calls": [{"call_id": "call-1", "stage": "script", "status": "succeeded", "usage": {}}],
        },
        base_dir=tmp_path,
    )
    record = json.loads((tmp_path / "abcd1234.json").read_text(encoding="utf-8"))
    assert record["input_tokens"] is None
    assert record["estimated_cost_usd"] is None
    outbox = TelemetryOutbox(tmp_path / "outbox")
    tables = [event.table for event in outbox.events()]
    assert tables.count("video_pipeline_jobs") == 1
    assert tables.count("video_scene_telemetry") == 1
    assert tables.count("video_qa_records") == 1
    assert tables.count("video_vertex_calls") == 1
    vertex_event = next(event for event in outbox.events() if event.table == "video_vertex_calls")
    usage_values = dict(zip(vertex_event.column_names, vertex_event.row))
    assert usage_values["input_tokens"] is None
    assert usage_values["output_tokens"] is None
    assert usage_values["total_tokens"] is None


def test_legacy_clickhouse_engine_never_claims_replay_safe_ready(monkeypatch):
    monkeypatch.setattr(clickhouse, "_SCHEMA_REPLAY_SAFE_READY", False)
    assert clickhouse._init_clickhouse_schema(EngineClient("MergeTree")) is False
    assert clickhouse.is_replay_safe_schema_ready() is False
    assert clickhouse._init_clickhouse_schema(EngineClient("ReplacingMergeTree")) is True
    assert clickhouse.is_replay_safe_schema_ready() is True


def test_corrupt_outbox_fails_closed_without_reset_or_duplicate_sequence(tmp_path: Path):
    root = tmp_path / "outbox"
    outbox = TelemetryOutbox(root)
    outbox.enqueue("video_pipeline_jobs", ["job_id"], ["abcd1234"])
    (root / "state.json").write_text("{not-json", encoding="utf-8")

    recovered = TelemetryOutbox(root)
    status = recovered.status()
    assert status["corrupted"] is False
    assert status["reconstructed"] is True
    assert recovered.enqueue("video_pipeline_jobs", ["job_id"], ["efgh5678"]).sequence == 1

    (root / "events.jsonl").write_text(
        (root / "events.jsonl").read_text(encoding="utf-8") +
        (root / "events.jsonl").read_text(encoding="utf-8"), encoding="utf-8"
    )
    broken = TelemetryOutbox(root)
    assert broken.status()["corrupted"] is True
    report = broken.drain_once(lambda: pytest.fail("corrupt outbox must not call client"), schema_ready=True)
    assert report.blocked_reason == "corrupt_outbox_state"


def test_reconciliation_exposes_completeness_and_unknown_cost(tmp_path: Path):
    report = build_reconciliation_report(
        [
            {"event_id": "a", "sequence": 0},
            {"event_id": "b", "sequence": 1},
        ],
        source_events={
            "canonical": [{"event_id": "a"}, {"event_id": "b"}],
            "outbox": [{"event_id": "a"}],
            "clickhouse": [],
        },
        job_records=[{"total_tokens_used": None, "cost_usd": None}],
    )
    assert report["completeness"]["status"] == "incomplete"
    assert report["completeness"]["missing"]["outbox"] == ["b"]
    assert report["reconciliation"]["sources"]["canonical_jobs"]["total_tokens"] is None
    assert report["reconciliation"]["sources"]["canonical_jobs"]["total_cost_usd"] is None
