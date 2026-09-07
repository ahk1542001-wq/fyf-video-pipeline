"""Unit tests for ClickHouse telemetry integration and local mirror."""

import shutil
import tempfile
import unittest
from pathlib import Path

from backend.clickhouse_telemetry import (
    get_all_telemetry_summary,
    get_job_telemetry,
    record_job_telemetry,
    record_scene_telemetry,
)


class TestClickHouseTelemetry(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_record_and_retrieve_job_telemetry(self):
        job_id = "test1234"
        record = record_job_telemetry(
            job_id=job_id,
            title="Test Video Script",
            duration_sec=120.5,
            voice_mode="gemini",
            status="completed",
            total_render_time_ms=95000,
            total_tokens_used=8500,
            cost_usd=0.0125,
            qa_passed=True,
            base_dir=self.temp_dir,
        )
        self.assertEqual(record["job_id"], job_id)
        self.assertEqual(record["cost_usd"], 0.0125)

        # Record scenes
        record_scene_telemetry(
            job_id=job_id,
            scene_id="S1",
            treatment_type="diorama",
            render_time_ms=3500,
            vertex_latency_ms=1100,
            evidence_claim_count=2,
            base_dir=self.temp_dir,
        )

        record_scene_telemetry(
            job_id=job_id,
            scene_id="S2",
            treatment_type="motion_diagram",
            render_time_ms=4200,
            vertex_latency_ms=900,
            evidence_claim_count=1,
            base_dir=self.temp_dir,
        )

        details = get_job_telemetry(job_id, base_dir=self.temp_dir)
        self.assertEqual(details["job"]["job_id"], job_id)
        self.assertEqual(details["scene_count"], 2)
        self.assertEqual(details["scenes"][0]["scene_id"], "S1")
        self.assertEqual(details["scenes"][1]["scene_id"], "S2")

    def test_all_telemetry_summary_aggregation(self):
        record_job_telemetry(
            job_id="job1",
            title="Job 1",
            duration_sec=60.0,
            voice_mode="gemini",
            status="completed",
            total_render_time_ms=50000,
            total_tokens_used=5000,
            cost_usd=0.01,
            qa_passed=True,
            base_dir=self.temp_dir,
        )
        record_job_telemetry(
            job_id="job2",
            title="Job 2",
            duration_sec=120.0,
            voice_mode="partner",
            status="completed",
            total_render_time_ms=100000,
            total_tokens_used=10000,
            cost_usd=0.02,
            qa_passed=True,
            base_dir=self.temp_dir,
        )

        summary = get_all_telemetry_summary(base_dir=self.temp_dir)
        self.assertEqual(summary["total_jobs"], 2)
        self.assertEqual(summary["total_tokens_used"], 15000)
        self.assertEqual(summary["total_cost_usd"], 0.03)
        self.assertEqual(summary["avg_render_time_sec"], 75.0)

    def test_record_job_telemetry_with_studio_parameters(self):
        job_id = "cinema99"
        record = record_job_telemetry(
            job_id=job_id,
            title="Global AI Tech Trailer",
            duration_sec=45.0,
            voice_mode="gemini",
            status="completed",
            total_render_time_ms=38000,
            total_tokens_used=4200,
            cost_usd=0.009,
            qa_passed=True,
            studio_name="Agentic Cinema Studio",
            language="en-US",
            genre="tech_explainer",
            base_dir=self.temp_dir,
        )
        self.assertEqual(record["studio_name"], "Agentic Cinema Studio")
        self.assertEqual(record["language"], "en-US")
        self.assertEqual(record["genre"], "tech_explainer")

        details = get_job_telemetry(job_id, base_dir=self.temp_dir)
        self.assertEqual(details["job"]["studio_name"], "Agentic Cinema Studio")
        self.assertEqual(details["job"]["language"], "en-US")
        self.assertEqual(details["job"]["genre"], "tech_explainer")

    def test_telemetry_special_characters_and_emojis(self):
        job_id = "unicode_test_123"
        record = record_job_telemetry(
            job_id=job_id,
            title="AI's Impact: မြန်မာနိုင်ငံ 🇲🇲 & O'Reilly",
            duration_sec=30.0,
            voice_mode="gemini",
            status="completed",
            total_render_time_ms=25000,
            total_tokens_used=3000,
            cost_usd=0.005,
            qa_passed=True,
            studio_name="L'Étoile Studio 🌟",
            language="my-MM",
            genre="cinematic_documentary",
            base_dir=self.temp_dir,
        )
        self.assertEqual(record["studio_name"], "L'Étoile Studio 🌟")
        self.assertEqual(record["title"], "AI's Impact: မြန်မာနိုင်ငံ 🇲🇲 & O'Reilly")

        details = get_job_telemetry(job_id, base_dir=self.temp_dir)
        self.assertEqual(details["job"]["studio_name"], "L'Étoile Studio 🌟")
        self.assertEqual(details["job"]["title"], "AI's Impact: မြန်မာနိုင်ငံ 🇲🇲 & O'Reilly")


# ===========================================================================
# Stage E1 (replay-safe schema) + E2 (durable outbox integration) tests.
# Appended without altering the original mirror/summary coverage above.
# ===========================================================================

from datetime import datetime, timedelta, timezone  # noqa: E402

from backend.telemetry_outbox import (  # noqa: E402
    MAX_BACKOFF_SECONDS,
    MAX_DELIVERY_ATTEMPTS,
)

import backend.clickhouse_telemetry as ch  # noqa: E402
from backend.clickhouse_telemetry import (  # noqa: E402
    _CREATE_DDL,
    _EXPAND_COLUMN_DDL,
    TABLE_COLUMNS,
    drain_outbox,
    get_telemetry_delivery_status,
    is_replay_safe_schema_ready,
    plan_engine_migration,
    record_vertex_call_telemetry,
)

EVENT_COLUMNS = [
    "event_id", "schema_version", "event_timestamp", "ingestion_timestamp", "sequence",
]
ALL_TABLES = [
    "video_pipeline_jobs", "video_scene_telemetry", "video_qa_records", "video_vertex_calls",
]


class FakeReplacingMergeTree:
    """Sink that mimics ReplacingMergeTree(ingestion_timestamp) ORDER BY event_id.

    Rows sharing an event_id collapse to the one with the newest
    ingestion_timestamp, exactly like the ClickHouse engine on merge/FINAL.
    """

    def __init__(self):
        self.rows: dict[str, tuple[str, list]] = {}
        self.insert_calls = 0

    def insert(self, table, rows, column_names=None):
        self.insert_calls += 1
        cols = list(column_names)
        eid_idx = cols.index("event_id")
        ing_idx = cols.index("ingestion_timestamp")
        for row in rows:
            eid = row[eid_idx]
            existing = self.rows.get(eid)
            if existing is None or str(row[ing_idx]) >= str(existing[1][ing_idx]):
                self.rows[eid] = (table, list(row))

    def count(self, table):
        return sum(1 for t, _ in self.rows.values() if t == table)


class RecordingClient:
    def __init__(self, fail=False):
        self.inserted = []
        self.insert_calls = 0
        self.fail = fail

    def insert(self, table, rows, column_names=None):
        self.insert_calls += 1
        if self.fail:
            raise RuntimeError("simulated outage")
        for row in rows:
            self.inserted.append((table, tuple(column_names), tuple(row)))


class CommandClient:
    """Fake ClickHouse client that only records DDL commands."""

    def __init__(self, fail=False):
        self.commands = []
        self.fail = fail

    def command(self, sql):
        self.commands.append(sql)
        if self.fail:
            raise RuntimeError("ddl rejected")
        return None


# ---------------------------------------------------------------------------
# E1: replay-safe schema DDL
# ---------------------------------------------------------------------------

def test_every_table_uses_replacing_merge_tree_ordered_by_event_id():
    for table in ALL_TABLES:
        ddl = _CREATE_DDL[table]
        assert "ENGINE = ReplacingMergeTree(ingestion_timestamp)" in ddl
        assert "ORDER BY (event_id)" in ddl


def test_every_table_declares_the_five_event_columns():
    for table in ALL_TABLES:
        ddl = _CREATE_DDL[table]
        assert "event_id String" in ddl
        assert "schema_version UInt8" in ddl
        assert "event_timestamp DateTime64" in ddl
        assert "ingestion_timestamp DateTime64" in ddl
        assert "sequence UInt64" in ddl


def test_create_ddl_is_idempotent_expand_only():
    for table in ALL_TABLES:
        assert "CREATE TABLE IF NOT EXISTS" in _CREATE_DDL[table]
    for column_ddl in _EXPAND_COLUMN_DDL.values():
        assert column_ddl.startswith("ADD COLUMN IF NOT EXISTS")


def test_no_active_destructive_ddl_is_ever_issued():
    # The applied DDL (create + expand) must contain no destructive statements.
    applied = list(_CREATE_DDL.values()) + list(_EXPAND_COLUMN_DDL.values())
    joined = "\n".join(applied).upper()
    for forbidden in ("DROP TABLE", "DROP COLUMN", "TRUNCATE", "RENAME TABLE", "ALTER"):
        assert forbidden not in joined


def test_migration_plan_defers_the_contract_step_behind_owner_approval():
    plan = plan_engine_migration()
    assert plan["strategy"] == "expand_then_migrate_then_contract"
    assert plan["expand_applied_now"] is True
    assert plan["contract_deferred"] is True
    contract_steps = [s for s in plan["steps"] if s["phase"] == "contract"]
    assert contract_steps  # a documented, deferred contract step exists
    for step in contract_steps:
        # The destructive rename/drop is commented out and gated on approval.
        assert "REQUIRES OWNER APPROVAL" in step["sql"]
        for line in step["sql"].splitlines():
            stripped = line.strip()
            if "DROP TABLE" in stripped or "RENAME TABLE" in stripped:
                assert stripped.startswith("--")  # never executed


def test_init_schema_marks_ready_on_success_and_gates_on_failure():
    original = ch._SCHEMA_REPLAY_SAFE_READY
    try:
        ok_client = CommandClient()
        assert ch._init_clickhouse_schema(ok_client) is True
        assert is_replay_safe_schema_ready() is True
        # Idempotent expand: CREATE ran once per table, plus ADD COLUMN alters.
        assert sum(1 for c in ok_client.commands if "CREATE TABLE IF NOT EXISTS" in c) == len(ALL_TABLES)

        bad_client = CommandClient(fail=True)
        assert ch._init_clickhouse_schema(bad_client) is False
        # On failure the drain stays gated -- never claims cloud success.
        assert is_replay_safe_schema_ready() is False
    finally:
        ch._SCHEMA_REPLAY_SAFE_READY = original


# ---------------------------------------------------------------------------
# E2: delivery is off the request path + durable replay
# ---------------------------------------------------------------------------

def test_record_does_not_deliver_synchronously_off_request_path(tmp_path, monkeypatch):
    monkeypatch.delenv("CLICKHOUSE_HOST", raising=False)
    sink = RecordingClient()
    monkeypatch.setattr(ch, "get_clickhouse_client", lambda: sink)
    original = ch._SCHEMA_REPLAY_SAFE_READY
    ch._SCHEMA_REPLAY_SAFE_READY = True
    try:
        record_job_telemetry(
            job_id="offpath1", title="T", duration_sec=1.0, voice_mode="gemini",
            status="completed", total_render_time_ms=10, total_tokens_used=5,
            cost_usd=0.1, qa_passed=True, base_dir=tmp_path,
        )
        # The record (request) path never touches the sink synchronously.
        assert sink.insert_calls == 0
        # Delivery happens only via the explicit/background drain step.
        report = drain_outbox(tmp_path, client=sink, schema_ready=True)
        assert report["delivered"] == 1
        assert sink.insert_calls == 1
    finally:
        ch._SCHEMA_REPLAY_SAFE_READY = original


def test_outage_then_recovery_delivers_all_with_no_duplicates(tmp_path):
    for index in range(5):
        record_job_telemetry(
            job_id=f"job{index:05d}", title=f"T{index}", duration_sec=1.0,
            voice_mode="gemini", status="completed", total_render_time_ms=10,
            total_tokens_used=5, cost_usd=0.1, qa_passed=True, base_dir=tmp_path,
        )
    # Outage: client forced to None -> nothing delivered, nothing lost.
    outage = drain_outbox(tmp_path, client=None, schema_ready=True)
    assert outage["delivered"] == 0
    assert outage["blocked_reason"] == "clickhouse_unavailable_local_mirror_only"
    status = get_telemetry_delivery_status(tmp_path)
    assert status["pending"] == 5

    # Recovery: drain to a dedup sink.
    sink = FakeReplacingMergeTree()
    recovered = drain_outbox(tmp_path, client=sink, schema_ready=True)
    assert recovered["delivered"] == 5
    after = get_telemetry_delivery_status(tmp_path)
    assert after["pending"] == 0
    assert after["delivered"] == 5
    assert sink.count("video_pipeline_jobs") == 5
    event_ids = [row[0] for _, row in sink.rows.values()]
    assert len(set(event_ids)) == 5  # no duplicates


def test_duplicate_insert_collapses_to_identical_aggregates(tmp_path):
    record_job_telemetry(
        job_id="dupjob01", title="T", duration_sec=1.0, voice_mode="gemini",
        status="completed", total_render_time_ms=10, total_tokens_used=5,
        cost_usd=0.25, qa_passed=True, base_dir=tmp_path,
    )
    sink = FakeReplacingMergeTree()
    drain_outbox(tmp_path, client=sink, schema_ready=True)
    # Recompute total_cost_usd cleanly: cost_usd is a business column.
    def _cost_sum():
        total = 0.0
        for table, row in sink.rows.values():
            cols = EVENT_COLUMNS + TABLE_COLUMNS[table]
            total += row[cols.index("cost_usd")]
        return total

    baseline = _cost_sum()
    assert baseline == 0.25

    # Replay the exact same durable rows again (as a recovery replay would).
    for table, row in list(sink.rows.values()):
        cols = EVENT_COLUMNS + TABLE_COLUMNS[table]
        sink.insert(table, [row], column_names=cols)
    # ReplacingMergeTree dedup: still one row, identical aggregate.
    assert sink.count("video_pipeline_jobs") == 1
    assert _cost_sum() == baseline


def test_drain_gated_when_replay_safe_schema_not_confirmed(tmp_path):
    record_job_telemetry(
        job_id="gated001", title="T", duration_sec=1.0, voice_mode="gemini",
        status="completed", total_render_time_ms=10, total_tokens_used=5,
        cost_usd=0.1, qa_passed=True, base_dir=tmp_path,
    )
    sink = FakeReplacingMergeTree()
    report = drain_outbox(tmp_path, client=sink, schema_ready=False)
    assert report["delivered"] == 0
    assert report["blocked_reason"] == "replay_safe_schema_not_confirmed"
    assert sink.insert_calls == 0
    assert get_telemetry_delivery_status(tmp_path)["pending"] == 1


def test_delivery_failure_is_visible_and_not_silent_loss(tmp_path):
    record_job_telemetry(
        job_id="failjob1", title="T", duration_sec=1.0, voice_mode="gemini",
        status="completed", total_render_time_ms=10, total_tokens_used=5,
        cost_usd=0.1, qa_passed=True, base_dir=tmp_path,
    )
    failing = RecordingClient(fail=True)
    now = datetime(2026, 9, 1, tzinfo=timezone.utc)
    for _ in range(MAX_DELIVERY_ATTEMPTS + 2):
        drain_outbox(tmp_path, client=failing, schema_ready=True, now=now)
        status = get_telemetry_delivery_status(tmp_path)
        if status["failed"] == 1:
            break
        # Advance past the exponential backoff ceiling so the retry is due.
        now = now + timedelta(seconds=MAX_BACKOFF_SECONDS + 1)
    status = get_telemetry_delivery_status(tmp_path)
    assert status["failed"] == 1          # visible failed counter
    assert status["delivered"] == 0
    # Bounded retry: exactly MAX_DELIVERY_ATTEMPTS insert attempts, never infinite.
    assert failing.insert_calls == MAX_DELIVERY_ATTEMPTS


def test_scene_and_vertex_calls_flow_through_the_outbox(tmp_path):
    record_scene_telemetry(
        job_id="scenejob", scene_id="S1", treatment_type="diorama",
        render_time_ms=100, vertex_latency_ms=50, evidence_claim_count=2,
        base_dir=tmp_path,
    )
    enqueued = record_vertex_call_telemetry(
        job_id="scenejob", job_kind="video",
        calls=[{"call_id": "c1", "stage": "script", "status": "succeeded",
                "attempt": 1, "billable": True, "duration_ms": 10.0,
                "usage": {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3}}],
        base_dir=tmp_path,
    )
    assert enqueued == 1
    sink = FakeReplacingMergeTree()
    drain_outbox(tmp_path, client=sink, schema_ready=True)
    assert sink.count("video_scene_telemetry") == 1
    assert sink.count("video_vertex_calls") == 1


def test_delivery_status_reports_schema_and_cloud_honestly(tmp_path):
    status = get_telemetry_delivery_status(tmp_path)
    assert "schema_ready" in status
    assert "cloud_connected" in status
    assert status["cloud_connected"] is False  # no live cloud in tests
    # Ingestion lag is None (unknown) until something is delivered -- never 0.
    assert status["ingestion_lag_seconds"] is None


if __name__ == "__main__":
    unittest.main()
