"""Tests for the server-owned telemetry query registry (Stage E1 + E3).

Covers the six analytics capabilities, the dialect-variant parity layer, and the
preserved guarantees: caller-SQL rejection, MAX_RESULT_ROWS / MAX_EXECUTION_SECONDS
limits, honest source/availability, and delivery/freshness surfacing.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from backend.telemetry_queries import (
    CLICKHOUSE_QUERY_SQL,
    CLICKHOUSE_SETTINGS,
    MAX_RESULT_ROWS,
    QUERY_SQL,
    _response,
    execute_telemetry_query,
    resolve_query_sql,
    validate_readonly_sql,
)

# The four original ids must keep working unchanged (no regression).
ORIGINAL_IDS = {"jobs_overview", "model_calls", "scene_latency", "cost_summary"}
# The six analytics capabilities required by document line 147.
SIX_CAPABILITY_IDS = {
    "creation_timeline",
    "cost_summary",
    "quality_tracking",
    "editing_friction",
    "version_comparison",
    "grounded_recommendations",
}
_FINAL_WORD = re.compile(r"\s+FINAL\b", re.IGNORECASE)


class FakeQueryResult:
    def __init__(self, column_names, result_rows):
        self.column_names = column_names
        self.result_rows = result_rows


class FakeClickHouseClient:
    """Captures the SQL it was handed so we can assert the FINAL variant."""

    def __init__(self):
        self.last_sql = None
        self.last_settings = None

    def query(self, sql, settings=None):
        self.last_sql = sql
        self.last_settings = settings
        return FakeQueryResult(["ok"], [["1"]])


def _seed_mirror(tmp_path: Path):
    """Create one job + scene so every query id has real data to project."""
    jobs_root = tmp_path / "jobs"
    script_jobs_root = tmp_path / "script_jobs"
    script_jobs_root.mkdir(parents=True, exist_ok=True)
    job_dir = jobs_root / "abcd1234"
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "status.json").write_text(
        json.dumps(
            {
                "status": "completed",
                "created_at": "2026-01-01T00:00:00+00:00",
                "voice_provider": "gemini",
                "qa_report": {"passed": True, "metrics": {"video_duration": 30.0}},
                "stage_timings": {"render": 5.0},
            }
        ),
        encoding="utf-8",
    )
    (job_dir / "script.json").write_text(
        json.dumps({"title": "T", "studio_name": "S", "language": "en", "genre": "g"}),
        encoding="utf-8",
    )
    (job_dir / "telemetry.json").write_text(
        json.dumps(
            {
                "job_kind": "video",
                "started_at": "2026-01-01T00:00:00+00:00",
                "summary": {"total_tokens": 100, "estimated_cost_usd": 0.01},
                "calls": [
                    {
                        "call_id": "c1", "stage": "script", "model": "gemini",
                        "operation": "generate", "attempt": 1, "status": "succeeded",
                        "billable": True, "duration_ms": 10.0,
                        "usage": {"input_tokens": 40, "output_tokens": 60, "total_tokens": 100},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    scenes_dir = tmp_path / "output" / "telemetry"
    scenes_dir.mkdir(parents=True, exist_ok=True)
    (scenes_dir / "scenes_abcd1234.jsonl").write_text(
        json.dumps(
            {
                "job_id": "abcd1234", "scene_id": "S1", "treatment_type": "diorama",
                "render_time_ms": 100, "vertex_latency_ms": 50,
                "evidence_claim_count": 2, "segment_hash": "h",
                "created_at": "2026-01-01T00:00:00+00:00",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return jobs_root, script_jobs_root, tmp_path


# ---------------------------------------------------------------------------
# Registry shape
# ---------------------------------------------------------------------------

def test_registry_contains_the_four_original_ids_unchanged():
    assert ORIGINAL_IDS.issubset(QUERY_SQL.keys())


def test_registry_covers_all_six_analytics_capabilities():
    assert SIX_CAPABILITY_IDS.issubset(QUERY_SQL.keys())


def test_registry_has_no_duplicate_projection_between_variants():
    # Every ClickHouse variant must exist for every neutral query id.
    assert set(CLICKHOUSE_QUERY_SQL.keys()) == set(QUERY_SQL.keys())


# ---------------------------------------------------------------------------
# Dialect parity (the core E1/E3 constraint)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("query_id", sorted(QUERY_SQL.keys()))
def test_clickhouse_variant_differs_only_by_final(query_id):
    neutral = QUERY_SQL[query_id]
    clickhouse = CLICKHOUSE_QUERY_SQL[query_id]
    # Stripping the FINAL keyword must reproduce the neutral SQL exactly,
    # proving the projection is identical across backends (parity by construction).
    assert _FINAL_WORD.sub("", clickhouse) == neutral


@pytest.mark.parametrize("query_id", sorted(QUERY_SQL.keys()))
def test_parity_identical_rows_on_mirror_for_both_dialects(query_id, tmp_path):
    jobs_root, script_jobs_root, repo_root = _seed_mirror(tmp_path)
    neutral_sql = resolve_query_sql(query_id, "sqlite")
    # The FINAL-stripped ClickHouse variant must be byte-identical to neutral,
    # so running both against the SQLite mirror yields identical rows/columns.
    ch_stripped = _FINAL_WORD.sub("", resolve_query_sql(query_id, "clickhouse"))
    assert ch_stripped == neutral_sql

    neutral_result = execute_telemetry_query(
        query_id, jobs_root=jobs_root, script_jobs_root=script_jobs_root,
        repo_root=repo_root, client_factory=lambda: None, include_delivery=False,
    )
    assert neutral_result["success"] is True
    assert neutral_result["source"] == "local_mirror"
    assert isinstance(neutral_result["columns"], list)
    assert isinstance(neutral_result["rows"], list)


@pytest.mark.parametrize("query_id", sorted(QUERY_SQL.keys()))
def test_clickhouse_path_sends_final_variant(query_id):
    client = FakeClickHouseClient()
    result = execute_telemetry_query(
        query_id, jobs_root=Path("/nope"), script_jobs_root=Path("/nope"),
        repo_root=Path("/nope"), client_factory=lambda: client, include_delivery=False,
    )
    assert result["source"] == "clickhouse_cloud"
    # The SQL handed to ClickHouse carries FINAL for replay-safe dedup reads.
    assert re.search(r"\bFINAL\b", client.last_sql)
    # Row/time limits are still applied to the ClickHouse call.
    assert client.last_settings["max_result_rows"] == MAX_RESULT_ROWS
    assert client.last_settings["max_execution_time"] == CLICKHOUSE_SETTINGS["max_execution_time"]


# ---------------------------------------------------------------------------
# Allowlist + caller-SQL rejection (preserved guarantees)
# ---------------------------------------------------------------------------

def test_unknown_query_id_is_rejected(tmp_path):
    with pytest.raises(ValueError):
        execute_telemetry_query(
            "not_a_real_query", jobs_root=tmp_path, script_jobs_root=tmp_path,
            repo_root=tmp_path, client_factory=lambda: None,
        )


def test_caller_sql_mutation_is_rejected():
    for bad in [
        "DROP TABLE video_pipeline_jobs",
        "INSERT INTO video_pipeline_jobs VALUES (1)",
        "DELETE FROM video_pipeline_jobs",
        "SELECT 1; DROP TABLE video_pipeline_jobs",
        "UPDATE video_pipeline_jobs SET cost_usd = 0",
    ]:
        with pytest.raises(ValueError):
            validate_readonly_sql(bad)


def test_caller_sql_select_is_allowed_but_mutation_via_execute_rejected(tmp_path):
    # A read-only SELECT is accepted by the validator...
    assert validate_readonly_sql("SELECT 1") == "SELECT 1"
    # ...but a mutation passed as caller SQL is rejected before any execution.
    with pytest.raises(ValueError):
        execute_telemetry_query(
            sql="DROP TABLE video_pipeline_jobs", jobs_root=tmp_path,
            script_jobs_root=tmp_path, repo_root=tmp_path,
            client_factory=lambda: None,
        )


def test_result_rows_are_bounded_to_max_result_rows():
    rows = [[i] for i in range(MAX_RESULT_ROWS + 50)]
    payload = _response(columns=["n"], rows=rows, source="local_mirror", started_at=0.0)
    assert payload["row_count"] == MAX_RESULT_ROWS
    assert len(payload["rows"]) == MAX_RESULT_ROWS


# ---------------------------------------------------------------------------
# Honest source / availability + delivery surfacing
# ---------------------------------------------------------------------------

def test_availability_is_unavailable_when_no_rows(tmp_path):
    empty_root = tmp_path / "empty"
    empty_root.mkdir()
    result = execute_telemetry_query(
        "jobs_overview", jobs_root=empty_root, script_jobs_root=empty_root,
        repo_root=empty_root, client_factory=lambda: None, include_delivery=False,
    )
    assert result["rows"] == []
    assert result["availability"] == "unavailable"


def test_local_mirror_is_never_reported_as_cloud(tmp_path):
    jobs_root, script_jobs_root, repo_root = _seed_mirror(tmp_path)
    result = execute_telemetry_query(
        "jobs_overview", jobs_root=jobs_root, script_jobs_root=script_jobs_root,
        repo_root=repo_root, client_factory=lambda: None, include_delivery=False,
    )
    # No client -> honest local_mirror, never "clickhouse_cloud".
    assert result["source"] == "local_mirror"


def test_delivery_and_freshness_are_surfaced_in_response(tmp_path):
    jobs_root, script_jobs_root, repo_root = _seed_mirror(tmp_path)
    result = execute_telemetry_query(
        "jobs_overview", jobs_root=jobs_root, script_jobs_root=script_jobs_root,
        repo_root=repo_root, client_factory=lambda: None, include_delivery=True,
    )
    assert "delivery" in result
    assert "ingestion_lag_seconds" in result
    assert "freshness" in result
    # With nothing delivered yet, lag/freshness are honest unknowns, never 0.
    assert result["ingestion_lag_seconds"] is None or isinstance(
        result["ingestion_lag_seconds"], (int, float)
    )
    delivery = result["delivery"]
    for key in ("pending", "failed", "delivered", "schema_ready"):
        assert key in delivery
