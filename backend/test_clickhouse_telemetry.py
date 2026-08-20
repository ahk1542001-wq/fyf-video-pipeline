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


if __name__ == "__main__":
    unittest.main()
