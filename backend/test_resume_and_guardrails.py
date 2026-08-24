"""Tests for transactional guardrail acquisition, story polish/lock resource lifecycle, resume validation, and orphan prevention."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend.budget_store import get_budget_status, record_cost
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


class TestResumeAndGuardrails(unittest.TestCase):
    def setUp(self):
        clear_limits_state()

    def tearDown(self):
        clear_limits_state()

    def test_rejected_video_generation_creates_no_orphan_on_disk(self):
        """When guardrails reject video generation, no job folder or status file must be created on disk."""
        with tempfile.TemporaryDirectory() as jobs_dir, tempfile.TemporaryDirectory() as locks_dir:
            lock_id = "a1b2c3d4"
            lock_path = Path(locks_dir) / lock_id
            lock_path.mkdir()
            (lock_path / "script.json").write_text(json.dumps(_valid_video_script_data()))

            with patch("backend.main.JOBS_ROOT", Path(jobs_dir)), \
                 patch("backend.main.LOCKS_ROOT", Path(locks_dir)), \
                 patch.dict("os.environ", {"FYF_MAX_CONCURRENT_JOBS": "1"}):

                # Occupy the concurrency slot
                register_active_job("occupying_job")

                response = client.post("/api/generate-video", json={
                    "lock_id": lock_id,
                    "voice_provider": "gemini",
                    "style": "fyf_explainer",
                })

                self.assertEqual(response.status_code, 429)

                # Verify NO job directory or status file was created in JOBS_ROOT!
                created_dirs = [d for d in Path(jobs_dir).iterdir() if d.is_dir()]
                self.assertEqual(len(created_dirs), 0, "Rejected video generation must not leave orphan job dirs")

    def test_rejected_script_generation_creates_no_orphan_on_disk(self):
        """When guardrails reject script generation, no script job folder must be created on disk."""
        with tempfile.TemporaryDirectory() as script_jobs_dir, tempfile.TemporaryDirectory() as locks_dir:
            with patch("backend.main.SCRIPT_JOBS_ROOT", Path(script_jobs_dir)), \
                 patch("backend.main.LOCKS_ROOT", Path(locks_dir)), \
                 patch.dict("os.environ", {"FYF_MAX_CONCURRENT_JOBS": "1"}):

                register_active_job("occupying_job")

                response = client.post("/api/generate-script", json={
                    "topic": "စမ်းသပ်ချက် အကြောင်းအရာ",
                    "duration_mode": "short",
                })

                self.assertEqual(response.status_code, 429)

                created_dirs = [d for d in Path(script_jobs_dir).iterdir() if d.is_dir()]
                self.assertEqual(len(created_dirs), 0, "Rejected script generation must not leave orphan script job dirs")

    def test_story_polish_releases_resources_in_finally_on_success_and_failure(self):
        """Story polish must reconcile/release budget and release concurrency slot on both success and failure."""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with patch.dict("os.environ", {"FYF_BUDGET_LEDGER_PATH": str(root / ".budget_ledger.json")}), \
                 patch("writer_agent_vertex.generate_story_modes", return_value=_valid_story_modes_data()):

                # 1. Successful polish
                resp = client.post("/api/story-polish", json={"topic_or_draft": "စမ်းသပ်ချက်"})
                self.assertEqual(resp.status_code, 200)
                status_info = get_budget_status(root)
                self.assertEqual(status_info["active_reserved_usd"], 0.0, "Budget reservation must be released after polish")
                self.assertEqual(get_active_job_count(), 0, "Active slot must be released after polish")

            # 2. Failing polish
            with patch.dict("os.environ", {"FYF_BUDGET_LEDGER_PATH": str(root / ".budget_ledger.json")}), \
                 patch("writer_agent_vertex.generate_story_modes", side_effect=RuntimeError("Vertex API Error")):

                resp = client.post("/api/story-polish", json={"topic_or_draft": "စမ်းသပ်ချက်"})
                self.assertEqual(resp.status_code, 500)
                status_info = get_budget_status(root)
                self.assertEqual(status_info["active_reserved_usd"], 0.0, "Budget reservation must be released on failure")
                self.assertEqual(get_active_job_count(), 0, "Active slot must be released on failure")

    def test_story_lock_releases_resources_in_finally_on_success_and_failure(self):
        """Story lock must reconcile/release budget and release concurrency slot on both success and failure."""
        with tempfile.TemporaryDirectory() as temp_dir, tempfile.TemporaryDirectory() as locks_dir:
            root = Path(temp_dir)
            exact_lock_payload = {
                "title": "Title",
                "approved_segments": [{"id": "s1", "text": "Segment text"}],
            }
            with patch.dict("os.environ", {"FYF_BUDGET_LEDGER_PATH": str(root / ".budget_ledger.json")}), \
                 patch("backend.main.LOCKS_ROOT", Path(locks_dir)), \
                 patch("writer_agent_vertex.generate_exact_lock", return_value=_valid_video_script_data()):

                # 1. Successful lock
                resp = client.post("/api/story-lock", json=exact_lock_payload)
                self.assertEqual(resp.status_code, 200)
                status_info = get_budget_status(root)
                self.assertEqual(status_info["active_reserved_usd"], 0.0, "Budget reservation must be released after lock")
                self.assertEqual(get_active_job_count(), 0, "Active slot must be released after lock")

            # 2. Failing lock
            with patch.dict("os.environ", {"FYF_BUDGET_LEDGER_PATH": str(root / ".budget_ledger.json")}), \
                 patch("backend.main.LOCKS_ROOT", Path(locks_dir)), \
                 patch("writer_agent_vertex.generate_exact_lock", side_effect=RuntimeError("Vertex lock failure")):

                resp = client.post("/api/story-lock", json=exact_lock_payload)
                self.assertEqual(resp.status_code, 500)
                status_info = get_budget_status(root)
                self.assertEqual(status_info["active_reserved_usd"], 0.0, "Budget reservation must be released on failure")
                self.assertEqual(get_active_job_count(), 0, "Active slot must be released on failure")

    def test_resume_script_job_rejects_completed_queued_and_nonresumable_jobs(self):
        """Only needs_attention jobs with restart_resumable=True can be resumed."""
        with tempfile.TemporaryDirectory() as script_jobs_dir, tempfile.TemporaryDirectory() as locks_dir:
            script_root = Path(script_jobs_dir)
            with patch("backend.main.SCRIPT_JOBS_ROOT", script_root), \
                 patch("backend.main.LOCKS_ROOT", Path(locks_dir)), \
                 patch.dict("os.environ", {
                     "FYF_MAX_CONCURRENT_JOBS": "5",
                     "FYF_BUDGET_LEDGER_PATH": str(Path(script_jobs_dir) / ".budget_ledger.json"),
                 }):

                # Case A: Completed job -> rejected
                j_completed = "c0000001"
                d_completed = script_root / j_completed
                d_completed.mkdir()
                (d_completed / "status.json").write_text(json.dumps({"status": "completed", "restart_resumable": True}))
                (d_completed / "request.json").write_text(json.dumps({"topic": "T"}))
                resp = client.post(f"/api/script-jobs/{j_completed}/resume")
                self.assertEqual(resp.status_code, 400)

                # Case B: In-progress 'writing' job -> rejected
                j_writing = "w0000001"
                d_writing = script_root / j_writing
                d_writing.mkdir()
                (d_writing / "status.json").write_text(json.dumps({"status": "writing", "restart_resumable": True}))
                (d_writing / "request.json").write_text(json.dumps({"topic": "T"}))
                resp = client.post(f"/api/script-jobs/{j_writing}/resume")
                self.assertEqual(resp.status_code, 400)

                # Case C: Failed with restart_resumable=False -> rejected
                j_failed = "f0000001"
                d_failed = script_root / j_failed
                d_failed.mkdir()
                (d_failed / "status.json").write_text(json.dumps({"status": "failed", "restart_resumable": False}))
                (d_failed / "request.json").write_text(json.dumps({"topic": "T"}))
                resp = client.post(f"/api/script-jobs/{j_failed}/resume")
                self.assertEqual(resp.status_code, 400)

                # Case D: needs_attention with restart_resumable=True -> accepted
                j_ok = "a0000001"
                d_ok = script_root / j_ok
                d_ok.mkdir()
                (d_ok / "status.json").write_text(json.dumps({
                    "status": "needs_attention",
                    "restart_resumable": True,
                    "resume_count": 0,
                }))
                (d_ok / "request.json").write_text(json.dumps({"topic": "T"}))
                with patch("backend.main.run_script_pipeline"):
                    resp = client.post(f"/api/script-jobs/{j_ok}/resume")
                    self.assertEqual(resp.status_code, 202)


if __name__ == "__main__":
    unittest.main()
