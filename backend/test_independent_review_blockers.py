"""Adversarial regression tests for independent review blockers."""

import asyncio
import json
import math
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend.budget_store import (
    _read_budget_ledger,
    get_budget_status,
    is_reservation_active,
    reconcile_budget,
    release_reservation,
    reserve_budget,
)
from backend.main import app
from backend.runtime_limits import (
    acquire_guardrail_lease,
    clear_limits_state,
    get_active_job_count,
    register_active_job,
)
from backend.telemetry_store import get_all_telemetry_summary, record_job_telemetry
from vertex_model_routing import model_for

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
            {"name": f"Variant {i}", "script": {"title": f"Title {i}", "language": "my-MM", "segments": [make_seg(j) for j in range(1, 6)]}}
            for i in range(1, 4)
        ],
        "model_used": "gemini-3.7-flash"
    }


class TestIndependentReviewBlockers(unittest.TestCase):
    def setUp(self):
        clear_limits_state()

    def tearDown(self):
        clear_limits_state()

    # BLOCKER 1: Lease cleanup after acquisition failure
    def test_generate_script_write_failure_releases_lease_and_removes_orphan(self):
        """When disk writing fails during generate-script, lease must be released and no orphan directory remains."""
        with tempfile.TemporaryDirectory() as script_jobs_dir, tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with patch.dict("os.environ", {"FYF_BUDGET_LEDGER_PATH": str(root / ".budget_ledger.json")}), \
                 patch("backend.main.SCRIPT_JOBS_ROOT", Path(script_jobs_dir)), \
                 patch("backend.main.write_json_atomically", side_effect=OSError("Disk full")):

                resp = client.post("/api/generate-script", json={"topic": "Test topic"})
                self.assertEqual(resp.status_code, 500)

                # Assert zero resource leaks
                self.assertEqual(get_active_job_count(), 0, "Active slots must be 0 after write failure")
                self.assertEqual(get_budget_status(root)["active_reserved_usd"], 0.0, "Reserved budget must be 0")
                # Assert no orphan directory
                dirs = list(Path(script_jobs_dir).iterdir())
                self.assertEqual(len(dirs), 0, "Partial job directory must be cleaned up on failure")

    def test_generate_video_create_failure_releases_lease_and_removes_orphan(self):
        """When _create_video_job fails, lease must be released and no orphan directory remains."""
        with tempfile.TemporaryDirectory() as jobs_dir, tempfile.TemporaryDirectory() as locks_dir, tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            lock_id = "a1b2c3d4"
            lock_path = Path(locks_dir) / lock_id
            lock_path.mkdir()
            (lock_path / "script.json").write_text(json.dumps(_valid_video_script_data()))

            with patch.dict("os.environ", {"FYF_BUDGET_LEDGER_PATH": str(root / ".budget_ledger.json")}), \
                 patch("backend.main.JOBS_ROOT", Path(jobs_dir)), \
                 patch("backend.main.LOCKS_ROOT", Path(locks_dir)), \
                 patch("backend.main._create_video_job", side_effect=OSError("Disk permission error")):

                resp = client.post("/api/generate-video", json={
                    "lock_id": lock_id,
                    "voice_provider": "gemini",
                    "style": "fyf_explainer",
                })
                self.assertEqual(resp.status_code, 500)

                self.assertEqual(get_active_job_count(), 0, "Active slots must be 0 after video job creation failure")
                self.assertEqual(get_budget_status(root)["active_reserved_usd"], 0.0, "Reserved budget must be 0")
                dirs = list(Path(jobs_dir).iterdir())
                self.assertEqual(len(dirs), 0, "Partial video job directory must be cleaned up on failure")

    def test_script_resume_failure_releases_lease_and_preserves_safe_state(self):
        """When resume_script_job fails during update/queue, lease must release and job remains in safe resumable state."""
        with tempfile.TemporaryDirectory() as script_jobs_dir, tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            job_id = "c0000001"
            job_dir = Path(script_jobs_dir) / job_id
            job_dir.mkdir()
            (job_dir / "status.json").write_text(json.dumps({
                "status": "needs_attention",
                "restart_resumable": True,
                "resume_count": 0,
            }))
            (job_dir / "request.json").write_text(json.dumps({"topic": "T"}))

            with patch.dict("os.environ", {"FYF_BUDGET_LEDGER_PATH": str(root / ".budget_ledger.json")}), \
                 patch("backend.main.SCRIPT_JOBS_ROOT", Path(script_jobs_dir)), \
                 patch("backend.main.update_script_status", side_effect=[OSError("State write error"), None]):

                resp = client.post(f"/api/script-jobs/{job_id}/resume")
                self.assertEqual(resp.status_code, 500)

                self.assertEqual(get_active_job_count(), 0, "Active slots must be 0 after resume failure")
                self.assertEqual(get_budget_status(root)["active_reserved_usd"], 0.0, "Reserved budget must be 0")
                # Existing job directory must still exist!
                self.assertTrue(job_dir.exists(), "Existing job must be preserved")

    # BLOCKER 2: Restart auto-resume guardrails
    def test_startup_recovery_fails_safely_when_guardrail_acquisition_rejected(self):
        """Startup recovery must atomically acquire lease; if rejected, update to safe needs_attention."""
        with tempfile.TemporaryDirectory() as script_jobs_dir, tempfile.TemporaryDirectory() as jobs_dir, tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            s_job_id = "a0000001"
            s_dir = Path(script_jobs_dir) / s_job_id
            s_dir.mkdir()
            (s_dir / "status.json").write_text(json.dumps({
                "status": "writing",
                "restart_resumable": True,
                "resume_count": 0,
            }))

            with patch.dict("os.environ", {
                "FYF_BUDGET_LEDGER_PATH": str(root / ".budget_ledger.json"),
                "FYF_MAX_CONCURRENT_JOBS": "0",  # Guardrails reject acquisition
            }), \
                 patch("backend.main.SCRIPT_JOBS_ROOT", Path(script_jobs_dir)), \
                 patch("backend.main.JOBS_ROOT", Path(jobs_dir)):

                from backend.main import resume_interrupted_script_jobs
                import asyncio
                asyncio.run(resume_interrupted_script_jobs())

                # Assert that the job status was safely updated to needs_attention with restart_resumable=True
                status_data = json.loads((s_dir / "status.json").read_text(encoding="utf-8"))
                self.assertEqual(status_data["status"], "needs_attention")
                self.assertTrue(status_data["restart_resumable"])
                self.assertIn("guardrail", status_data["error"].lower())

    def test_startup_video_completion_releases_slot_and_reservation(self):
        """A successfully completed startup video task must release its exact lease."""
        import backend.main as main_module
        from backend.job_store import initialize_job_status, update_job_status

        with tempfile.TemporaryDirectory() as jobs_dir, \
             tempfile.TemporaryDirectory() as script_jobs_dir, \
             tempfile.TemporaryDirectory() as locks_dir, \
             tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            job_id = "1234abcd"
            job_dir = Path(jobs_dir) / job_id
            job_dir.mkdir()
            initialize_job_status(job_dir, job_id, "gemini")
            update_job_status(job_dir, {"status": "visuals", "resume_count": 0})
            (job_dir / "script.json").write_text(json.dumps({"segments": []}))

            async def exercise():
                with patch.dict("os.environ", {
                    "FYF_BUDGET_LEDGER_PATH": str(root / ".budget_ledger.json"),
                    "FYF_MAX_CONCURRENT_JOBS": "1",
                }), patch.object(main_module, "JOBS_ROOT", Path(jobs_dir)), \
                     patch.object(main_module, "SCRIPT_JOBS_ROOT", Path(script_jobs_dir)), \
                     patch.object(main_module, "LOCKS_ROOT", Path(locks_dir)), \
                     patch.object(main_module, "run_pipeline", new_callable=AsyncMock) as pipeline:
                    await main_module.resume_interrupted_script_jobs()
                    await asyncio.sleep(0)
                    await asyncio.sleep(0)
                    return pipeline.await_count

            self.assertEqual(asyncio.run(exercise()), 1)
            self.assertEqual(get_active_job_count(), 0)
            self.assertEqual(get_budget_status(root)["active_reserved_usd"], 0.0)

    def test_startup_video_status_failure_releases_lease_and_marks_attention(self):
        """A startup status-write failure must not leak paid-work resources."""
        import backend.main as main_module
        from backend.job_store import initialize_job_status, update_job_status as real_update_job_status

        with tempfile.TemporaryDirectory() as jobs_dir, \
             tempfile.TemporaryDirectory() as script_jobs_dir, \
             tempfile.TemporaryDirectory() as locks_dir, \
             tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            job_id = "5678abcd"
            job_dir = Path(jobs_dir) / job_id
            job_dir.mkdir()
            initialize_job_status(job_dir, job_id, "gemini")
            real_update_job_status(job_dir, {"status": "visuals", "resume_count": 0})
            (job_dir / "script.json").write_text(json.dumps({"segments": []}))
            call_count = 0

            def flaky_update(target_dir, updates):
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    raise OSError("disk full")
                return real_update_job_status(target_dir, updates)

            async def exercise():
                with patch.dict("os.environ", {
                    "FYF_BUDGET_LEDGER_PATH": str(root / ".budget_ledger.json"),
                    "FYF_MAX_CONCURRENT_JOBS": "1",
                }), patch.object(main_module, "JOBS_ROOT", Path(jobs_dir)), \
                     patch.object(main_module, "SCRIPT_JOBS_ROOT", Path(script_jobs_dir)), \
                     patch.object(main_module, "LOCKS_ROOT", Path(locks_dir)), \
                     patch.object(main_module, "update_job_status", side_effect=flaky_update), \
                     patch.object(main_module, "run_pipeline", new_callable=AsyncMock):
                    await main_module.resume_interrupted_script_jobs()

            asyncio.run(exercise())
            status_data = json.loads((job_dir / "status.json").read_text(encoding="utf-8"))
            self.assertEqual(status_data["status"], "needs_attention")
            self.assertTrue(status_data["restart_resumable"])
            self.assertEqual(get_active_job_count(), 0)
            self.assertEqual(get_budget_status(root)["active_reserved_usd"], 0.0)

    def test_disk_active_job_blocks_second_slot_without_double_counting(self):
        """Persisted active jobs remain part of the concurrency boundary after restart."""
        from backend.runtime_limits import try_acquire_job_slot

        with tempfile.TemporaryDirectory() as jobs_dir, tempfile.TemporaryDirectory() as script_jobs_dir:
            existing = Path(jobs_dir) / "aaaabbbb"
            existing.mkdir()
            (existing / "status.json").write_text(json.dumps({"status": "queued"}))
            with patch.dict("os.environ", {"FYF_MAX_CONCURRENT_JOBS": "1"}):
                accepted, reason = try_acquire_job_slot(
                    "ccccdddd",
                    job_roots=(Path(jobs_dir), Path(script_jobs_dir)),
                )
            self.assertFalse(accepted)
            self.assertIn("busy", reason or "")

    def test_adk_event_usage_is_recorded_in_current_job_telemetry(self):
        """ADK orchestration usage must be recorded in addition to wrapped tool calls."""
        from backend.agent.runner import run_adk_pipeline
        from backend.vertex_telemetry import telemetry_scope

        event = SimpleNamespace(
            id="adk-event-1",
            model_version="gemini-3.7-flash",
            error_code=None,
            error_message=None,
            usage_metadata=SimpleNamespace(
                prompt_token_count=111,
                candidates_token_count=22,
                total_token_count=133,
                cached_content_token_count=0,
                thoughts_token_count=0,
            ),
            get_function_responses=lambda: [
                SimpleNamespace(response=_valid_video_script_data())
            ],
        )

        async def fake_events(*args, **kwargs):
            yield event
            yield event  # Repeated delivery of the same ADK event must not double count.

        with tempfile.TemporaryDirectory() as temp_dir, patch.dict("os.environ", {
            "FYF_BUDGET_LEDGER_PATH": str(Path(temp_dir) / ".budget_ledger.json"),
        }):
            with telemetry_scope("abcd1234", "script", Path(temp_dir)) as collector, \
                 patch("google.adk.Runner.run_async", side_effect=fake_events):
                run_adk_pipeline("diagnostic")
                summary = collector.summary()
                self.assertEqual(summary["total_calls"], 1)
                self.assertEqual(summary["total_input_tokens"], 111)
                self.assertEqual(summary["total_output_tokens"], 22)
                self.assertEqual(summary["total_tokens"], 133)
                self.assertEqual(collector.calls[0]["stage"], "adk_orchestration")
                self.assertEqual(collector.calls[0]["model"], "gemini-3.7-flash")

    def test_story_operation_telemetry_is_visible_with_valid_job_id(self):
        """Story operation telemetry must be discoverable by the dashboard collector."""
        from backend.job_store import is_valid_job_id

        with tempfile.TemporaryDirectory() as jobs_dir, \
             tempfile.TemporaryDirectory() as script_jobs_dir, \
             tempfile.TemporaryDirectory() as locks_dir, \
             tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with patch.dict("os.environ", {
                "FYF_BUDGET_LEDGER_PATH": str(root / ".budget_ledger.json"),
            }), patch("backend.main.JOBS_ROOT", Path(jobs_dir)), \
                 patch("backend.main.SCRIPT_JOBS_ROOT", Path(script_jobs_dir)), \
                 patch("backend.main.LOCKS_ROOT", Path(locks_dir)), \
                 patch("writer_agent_vertex.generate_story_modes", return_value=_valid_story_modes_data()):
                response = client.post("/api/story-polish", json={"topic_or_draft": "Topic"})
                self.assertEqual(response.status_code, 200)

            summary = get_all_telemetry_summary(
                base_dir=root / "telemetry",
                job_roots=(Path(jobs_dir), Path(script_jobs_dir)),
                budget_root=root,
            )
            self.assertEqual(summary["total_jobs"], 1)
            self.assertEqual(summary["jobs"][0]["job_kind"], "story_polish")
            self.assertTrue(is_valid_job_id(summary["jobs"][0]["job_id"]))

    # BLOCKER 3: Real cost telemetry only (no hardcoded spend)
    def test_story_polish_and_lock_use_real_telemetry_without_hardcoded_costs(self):
        """Story polish and lock must not hardcode 0.015 / 0.025 USD; if unavailable, record 0 spend and release reservation."""
        with tempfile.TemporaryDirectory() as temp_dir, tempfile.TemporaryDirectory() as locks_dir:
            root = Path(temp_dir)
            with patch.dict("os.environ", {"FYF_BUDGET_LEDGER_PATH": str(root / ".budget_ledger.json")}), \
                 patch("backend.main.LOCKS_ROOT", Path(locks_dir)), \
                 patch("writer_agent_vertex.generate_story_modes", return_value=_valid_story_modes_data()), \
                 patch("writer_agent_vertex.generate_exact_lock", return_value=_valid_video_script_data()):

                # Execute polish
                resp1 = client.post("/api/story-polish", json={"topic_or_draft": "Topic"})
                self.assertEqual(resp1.status_code, 200)

                # Execute lock
                resp2 = client.post("/api/story-lock", json={"title": "Title", "approved_segments": [{"id": "s1", "text": "Text"}]})
                self.assertEqual(resp2.status_code, 200)

                budget_info = get_budget_status(root)
                # Since mock had no actual Vertex usage metadata recorded, spend must be 0.0, NOT invented 0.015/0.025!
                self.assertEqual(budget_info["total_spend_usd"], 0.0, "Never invent spend when actual telemetry is unavailable")
                self.assertEqual(budget_info["active_reserved_usd"], 0.0, "Reservations must still be released")

    # BLOCKER 4: ADK model routing and telemetry
    def test_adk_producer_uses_central_model_routing_defaulting_to_script_model(self):
        """ADK Producer Agent must default to model_for('script') (gemini-3.7-flash), not gemini-2.5-flash."""
        from backend.agent.fyf_producer import create_fyf_producer_agent
        agent = create_fyf_producer_agent()
        expected_model = model_for("script")
        self.assertEqual(agent.model.model, expected_model, f"Expected {expected_model} but got {agent.model}")

    # BLOCKER 5: Budget fail-closed validation
    def test_budget_fail_closed_on_nan_inf_negative_or_malformed_reservations(self):
        """Budget store must fail closed on NaN, Inf, negative values, and malformed nested reservations."""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            ledger_file = root / ".budget_ledger.json"

            # 1. NaN total spend in ledger
            ledger_file.write_text(json.dumps({"total_spend_usd": "invalid_number"}))
            status = get_budget_status(root)
            self.assertTrue(status["budget_exceeded"], "Malformed total_spend must fail closed")

            # 2. Malformed active_reservations containing negative amount
            ledger_file.write_text(json.dumps({
                "total_spend_usd": 1.0,
                "active_reservations": {"bad_op": {"amount_usd": -50.0, "date": "2026-08-21"}},
            }))
            status = get_budget_status(root)
            self.assertTrue(status["budget_exceeded"], "Negative reservation must fail closed")

            # 3. reserve_budget rejects NaN, Inf, and negative values
            ok, reason = reserve_budget("op1", float("nan"), root_dir=root)
            self.assertFalse(ok)
            ok, reason = reserve_budget("op2", float("inf"), root_dir=root)
            self.assertFalse(ok)
            ok, reason = reserve_budget("op3", -1.0, root_dir=root)
            self.assertFalse(ok)

            # 4. Configured cap env vars with NaN/negative must fail closed
            with patch.dict("os.environ", {"FYF_DAILY_BUDGET_CAP_USD": "invalid_cap"}):
                status = get_budget_status(root)
                self.assertTrue(status["budget_exceeded"], "Invalid configured cap must fail closed")

    # BLOCKER 6: Telemetry consistency
    def test_telemetry_summary_total_calls_zero_when_call_count_missing_and_real_budget_status(self):
        """When model_call_count is missing/zero, total_calls must be 0; budget_status reflects real ledger state."""
        with tempfile.TemporaryDirectory() as tmp_path, tempfile.TemporaryDirectory() as root_temp:
            root = Path(root_temp)
            # Record a job with no model_call_count
            record_job_telemetry(
                "99998888",
                {"model_name": "gemini-3.7-flash", "input_tokens": 0, "output_tokens": 0},
                base_dir=Path(tmp_path),
            )

            # 1. Healthy budget
            with patch.dict("os.environ", {"FYF_BUDGET_LEDGER_PATH": str(root / ".budget_ledger.json")}):
                summary = get_all_telemetry_summary(base_dir=Path(tmp_path))
                self.assertEqual(summary["jobs"][0]["summary"]["total_calls"], 0)
                self.assertEqual(summary["budget_status"], "healthy")

            # 2. Corrupt budget ledger -> budget_status must NOT claim 'healthy'
            (root / ".budget_ledger.json").write_text("invalid json content")
            with patch.dict("os.environ", {"FYF_BUDGET_LEDGER_PATH": str(root / ".budget_ledger.json")}):
                summary = get_all_telemetry_summary(base_dir=Path(tmp_path))
                self.assertIn(summary["budget_status"], ["corrupted", "cap_exceeded"])
                self.assertNotEqual(summary["budget_status"], "healthy", "Corrupted ledger must never be reported healthy")


if __name__ == "__main__":
    unittest.main()
