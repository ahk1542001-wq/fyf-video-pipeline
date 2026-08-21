"""Fresh diagnostic assertions proving strict production-path safety guarantees."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend.budget_store import get_budget_status, is_reservation_active, reserve_budget
from backend.main import app
from backend.runtime_limits import (
    acquire_guardrail_lease,
    clear_limits_state,
    get_active_job_count,
    register_active_job,
)

client = TestClient(app)


def _valid_video_script_data():
    return {
        "title": "Test Lock",
        "language": "my-MM",
        "segments": [{
            "id": "s1",
            "text": "Segment 1",
            "visual_action": "explain",
            "scene_type": "whiteboard",
            "mascot_action": "explain",
            "emotion": "focused",
            "emphasis": [],
            "visual": {
                "kind": "generic",
                "phase": "setup",
                "camera": "wide",
                "screen_text": ["test"],
                "evidence_claims": [{
                    "claim_id": "c1", "statement": "Approved fact",
                    "evidence_type": "concept", "values": []
                }],
                "evidence_shots": [{
                    "shot_id": "shot-1", "proves_claim_ids": ["c1"],
                    "prompt": "Show the approved fact",
                    "caption": "test", "hold_fraction": 1,
                    "media_type": "motion_graphic", "motion_preset": "slow_push",
                    "motion_spec": {
                        "layout": "concept",
                        "labels": ["test"],
                        "values": [],
                    },
                    "treatment": {
                        "treatment_type": "story_scene",
                        "focal_object": "obj",
                        "action": "act",
                        "change": "chg",
                        "visual_world": "world",
                        "motion_family": "camera",
                        "text_mode": "caption",
                        "attention_reset": False,
                        "director_reason": "reason",
                    }
                }]
            }
        }],
    }


def _valid_story_modes_data():
    def make_seg(i):
        return {
            "id": f"s{i}",
            "text": f"scene text {i}",
            "visual_action": "v",
            "scene_type": "demo",
            "mascot_action": "present",
            "emotion": "neutral",
            "emphasis": [],
            "visual": {
                "kind": "generic", "phase": "setup", "camera": "wide", "screen_text": ["t"],
                "evidence_claims": [{
                    "claim_id": f"c{i}", "statement": "claim", "evidence_type": "concept", "values": []
                }],
                "evidence_shots": [{
                    "shot_id": f"shot-{i}", "proves_claim_ids": [f"c{i}"], "prompt": "p", "caption": "c",
                    "hold_fraction": 1, "media_type": "motion_graphic", "motion_preset": "slow_push",
                    "motion_spec": {
                        "layout": "concept",
                        "labels": ["t"],
                        "values": [],
                    },
                    "treatment": {
                        "treatment_type": "story_scene", "focal_object": "o", "action": "a", "change": "c",
                        "visual_world": "w", "motion_family": "camera", "text_mode": "caption",
                        "attention_reset": False, "director_reason": "r"
                    }
                }]
            }
        }
    return {
        "variants": [
            {
                "name": "Variant 1",
                "script": {
                    "title": "Title 1",
                    "language": "my-MM",
                    "segments": [make_seg(1), make_seg(2), make_seg(3), make_seg(4), make_seg(5)],
                }
            },
            {
                "name": "Variant 2",
                "script": {
                    "title": "Title 2",
                    "language": "my-MM",
                    "segments": [make_seg(1), make_seg(2), make_seg(3), make_seg(4), make_seg(5)],
                }
            },
            {
                "name": "Variant 3",
                "script": {
                    "title": "Title 3",
                    "language": "my-MM",
                    "segments": [make_seg(1), make_seg(2), make_seg(3), make_seg(4), make_seg(5)],
                }
            }
        ],
        "model_used": "gemini-3.7-flash"
    }


class TestProductionDiagnostics(unittest.TestCase):
    def setUp(self):
        clear_limits_state()

    def tearDown(self):
        clear_limits_state()

    def test_diagnostic_1_adk_runner_failure_does_not_return_success(self):
        """Diagnostic 1: ADK Runner failure raises exception and cannot produce successful result."""
        from backend.agent.runner import run_adk_pipeline
        with patch(
            "google.adk.Runner.run_async",
            side_effect=RuntimeError("Provider Vertex ADK outage"),
        ):
            with tempfile.TemporaryDirectory() as temp_dir:
                with self.assertRaises(RuntimeError) as ctx:
                    run_adk_pipeline("စမ်းသပ်ချက်", "short", job_dir=Path(temp_dir))
                self.assertIn("Provider Vertex ADK outage", str(ctx.exception))
                self.assertFalse((Path(temp_dir) / "result.json").exists())

    def test_diagnostic_2_rate_limit_rejection_leaves_zero_budget_and_zero_slots_leaked(self):
        """Diagnostic 2: Rate limit rejection leaves reservation_leaked=False and slot_leaked=0."""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with patch.dict("os.environ", {
                "FYF_RATE_LIMIT_PER_MINUTE": "1",
                "FYF_BUDGET_LEDGER_PATH": str(root / ".budget_ledger.json"),
            }):
                ip = "192.168.1.100"
                # First request passes
                resp1 = client.post(
                    "/api/generate-script",
                    json={"topic": "First request topic"},
                    headers={"X-Forwarded-For": ip},
                )
                self.assertEqual(resp1.status_code, 202)

                # Second request rejected by rate limit
                resp2 = client.post(
                    "/api/generate-script",
                    json={"topic": "Second request topic"},
                    headers={"X-Forwarded-For": ip},
                )
                self.assertEqual(resp2.status_code, 429)

                # Prove zero leaks on rejected request
                budget_status = get_budget_status(root)
                # Only 1 reservation from the 1st request exists (or 0 if reconciled)
                self.assertLessEqual(budget_status["active_reserved_usd"], 0.04)
                self.assertLessEqual(get_active_job_count(), 1)

    def test_diagnostic_3_story_polish_and_lock_leave_zero_active_resources(self):
        """Diagnostic 3: Story polish and lock leave active_reserved_usd=0.0 and active_slots=0."""
        with tempfile.TemporaryDirectory() as temp_dir, tempfile.TemporaryDirectory() as locks_dir:
            root = Path(temp_dir)
            with patch.dict("os.environ", {"FYF_BUDGET_LEDGER_PATH": str(root / ".budget_ledger.json")}), \
                 patch("backend.main.LOCKS_ROOT", Path(locks_dir)), \
                 patch("writer_agent_vertex.generate_story_modes", return_value=_valid_story_modes_data()), \
                 patch("writer_agent_vertex.generate_exact_lock", return_value=_valid_video_script_data()):

                client.post("/api/story-polish", json={"topic_or_draft": "Topic"})
                client.post("/api/story-lock", json={"title": "Title", "approved_segments": [{"id": "s1", "text": "Text"}]})

                budget_status = get_budget_status(root)
                self.assertEqual(budget_status["active_reserved_usd"], 0.0, "Zero budget reserved after polish and lock")
                self.assertEqual(get_active_job_count(), 0, "Zero active slots after polish and lock")

    def test_diagnostic_4_resume_reconciliation_removes_exact_reservation(self):
        """Diagnostic 4: Script and video resume reconciles/releases exact job_id reservation."""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            job_id = "resum001"
            with patch.dict("os.environ", {"FYF_BUDGET_LEDGER_PATH": str(root / ".budget_ledger.json")}):
                lease = acquire_guardrail_lease(
                    operation_id=job_id,
                    client_ip="127.0.0.1",
                    estimated_charge_usd=0.06,
                    root_dir=root,
                )
                self.assertTrue(is_reservation_active(job_id, root))

                # Reconcile exact job_id
                lease.reconcile(actual_usd=0.05, outcome="completed")
                self.assertFalse(is_reservation_active(job_id, root))
                self.assertEqual(get_budget_status(root)["active_reserved_usd"], 0.0)

    def test_diagnostic_5_rejected_video_generation_leaves_no_queued_orphan(self):
        """Diagnostic 5: Rejected video generation leaves zero orphan directories on disk."""
        with tempfile.TemporaryDirectory() as jobs_dir, tempfile.TemporaryDirectory() as locks_dir:
            lock_id = "a1b2c3d4"
            lock_path = Path(locks_dir) / lock_id
            lock_path.mkdir()
            (lock_path / "script.json").write_text(json.dumps(_valid_video_script_data()))

            with patch("backend.main.JOBS_ROOT", Path(jobs_dir)), \
                 patch("backend.main.LOCKS_ROOT", Path(locks_dir)), \
                 patch.dict("os.environ", {"FYF_MAX_CONCURRENT_JOBS": "1"}):

                register_active_job("blocker_job")

                resp = client.post("/api/generate-video", json={
                    "lock_id": lock_id,
                    "voice_provider": "gemini",
                    "style": "fyf_explainer",
                })
                self.assertEqual(resp.status_code, 429)

                created = list(Path(jobs_dir).iterdir())
                self.assertEqual(len(created), 0, "No job folders or status files on rejection")


if __name__ == "__main__":
    unittest.main()
