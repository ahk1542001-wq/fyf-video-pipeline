import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.script_pipeline import (
    _is_transient_error,
    _parse_full_script,
    _script_max_retries,
    _sleep_before_script_retry,
    _terminal_error_message,
    run_script_pipeline,
)


class TransientClassificationTests(unittest.TestCase):
    def test_empty_error_message_is_treated_as_transient(self):
        """Regression: opaque failures (empty provider error text) once aborted
        jobs as terminal instead of using a bounded retry."""

        class OpaqueError(RuntimeError):
            def __str__(self) -> str:
                return ""

        self.assertTrue(_is_transient_error(OpaqueError("")))
        self.assertTrue(_is_transient_error(RuntimeError("504 DEADLINE_EXCEEDED")))
        self.assertFalse(_is_transient_error(ValueError("Output segment count does not match input segment count")))


def draft(segment_count: int = 12) -> dict:
    return {
        "title": "ရှည်လျားသော စမ်းသပ်ဗီဒီယို",
        "language": "my-MM",
        "segments": [
            {
                "id": f"s{index:02d}",
                "text": f"စမ်းသပ် စာသား အပိုင်း {index}",
                "visual_action": "အကြောင်းအရာကို ပြပါ",
                "scene_type": "demo",
                "mascot_action": "explain",
                "emotion": "focused",
                "emphasis": [],
            }
            for index in range(1, segment_count + 1)
        ],
    }


def lock_batch(payload: dict) -> dict:
    return {
        "title": payload["title"],
        "language": "my-MM",
        "segments": [
            {
                "id": segment["id"],
                "text": segment["text"],
                "visual_action": "အကြောင်းအရာကို ပြပါ",
                "scene_type": "demo",
                "mascot_action": "explain",
                "emotion": "focused",
                "emphasis": [],
                "visual": {
                    "kind": "generic",
                    "phase": "in_progress",
                    "camera": "wide",
                    "screen_text": [f"အပိုင်း {segment['id']}"],
                    "evidence_claims": [{
                        "claim_id": f"{segment['id']}_C1",
                        "statement": f"Claim {segment['id']}",
                        "evidence_type": "concept",
                        "values": [],
                    }],
                    "evidence_shots": [{
                        "shot_id": f"{segment['id']}_SHOT",
                        "proves_claim_ids": [f"{segment['id']}_C1"],
                        "prompt": f"Show {segment['id']}",
                        "caption": f"Beat {segment['id']}",
                        "hold_fraction": 1.0,
                        "media_type": "motion_graphic" if int(segment["id"][1:]) % 2 == 0 else "generated_image",
                        "motion_preset": "static",
                        "transition": "cut",
                        "composition": "focal_center",
                        "mascot_presence": "none",
                        "motion_spec": ({"layout": "concept", "labels": [f"Beat {segment['id']}"], "values": []} if int(segment["id"][1:]) % 2 == 0 else None),
                        "asset_path": None,
                        "fallback_asset_path": None,
                        "fallback_used": False,
                        "verification_status": "planned",
                    }],
                },
            }
            for segment in payload["approved_segments"]
        ],
    }


class ScriptPipelineTests(unittest.TestCase):
    def make_job(self, root: Path, job_id: str = "abcd1234") -> tuple[Path, Path]:
        jobs = root / "script-jobs"
        locks = root / "locks"
        job = jobs / job_id
        job.mkdir(parents=True)
        locks.mkdir()
        (job / "request.json").write_text(
            json.dumps({"topic": "ရှည်လျားသော စမ်းသပ်မှု", "duration_mode": "short", "use_adk_agent": True}),
            encoding="utf-8",
        )
        (job / "status.json").write_text(
            json.dumps({"job_id": job_id, "status": "queued", "retry_count": 0}),
            encoding="utf-8",
        )
        return jobs, locks

    def test_long_job_is_batched_and_persisted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            jobs, locks = self.make_job(root)
            (jobs / "abcd1234" / "request.json").write_text(
                json.dumps({"topic": "ရှည်လျားသော စမ်းသပ်မှု", "duration_mode": "long", "use_adk_agent": False}),
                encoding="utf-8",
            )
            with (
                patch("writer_agent_vertex.generate_narration_script", return_value=draft(20)) as narration,
                patch("writer_agent_vertex.generate_exact_lock", side_effect=lock_batch) as exact,
            ):
                run_script_pipeline("abcd1234", jobs, locks)

            job = jobs / "abcd1234"
            status = json.loads((job / "status.json").read_text(encoding="utf-8"))
            result = json.loads((job / "result.json").read_text(encoding="utf-8"))
            self.assertEqual(status["status"], "completed")
            self.assertEqual(status["progress"], 100)
            self.assertTrue(status["restart_resumable"])
            self.assertEqual(len(result["segments"]), 20)
            self.assertEqual(exact.call_count, 10)
            self.assertEqual(narration.call_args.args[1], "long")
            self.assertEqual(len(list(job.glob("locked-batch-*.json"))), 10)
            self.assertEqual(status["batch_size"], 2)

    def test_full_script_parser_preserves_explicit_scene_blocks(self):
        supplied = "Opening claim exactly.\n\nSecond scene stays unchanged.\n\nFinal invitation."
        self.assertEqual(
            _parse_full_script(supplied),
            [
                {"id": "s1", "text": "Opening claim exactly."},
                {"id": "s2", "text": "Second scene stays unchanged."},
                {"id": "s3", "text": "Final invitation."},
            ],
        )

    def test_full_script_bypasses_writer_and_plans_all_scenes_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            jobs, locks = self.make_job(root)
            job = jobs / "abcd1234"
            supplied = "Opening claim.\n\nWhat the equation asks.\n\nWhy it matters."
            (job / "request.json").write_text(
                json.dumps({
                    "topic": supplied,
                    "title": "A supplied proof script",
                    "source_mode": "full_script",
                    "duration_mode": "short",
                    "use_adk_agent": True,
                    "language": "en-US",
                    "genre": "cinematic_documentary",
                    "presenter_mode": "voiceover_only",
                }),
                encoding="utf-8",
            )
            planned = lock_batch({
                "title": "A supplied proof script",
                "approved_segments": _parse_full_script(supplied),
            })
            with (
                patch("backend.agent.runner.run_adk_pipeline") as adk,
                patch("writer_agent_vertex.generate_narration_script") as writer,
                patch("writer_agent_vertex.generate_exact_lock", return_value=planned) as planner,
            ):
                run_script_pipeline("abcd1234", jobs, locks)

            self.assertFalse(adk.called)
            self.assertFalse(writer.called)
            self.assertEqual(planner.call_count, 1)
            payload = planner.call_args.args[0]
            self.assertEqual(payload["approved_segments"], _parse_full_script(supplied))
            self.assertTrue(payload["source_is_final_script"])
            result = json.loads((job / "result.json").read_text(encoding="utf-8"))
            self.assertEqual(
                [(segment["id"], segment["text"]) for segment in result["segments"]],
                [(segment["id"], segment["text"]) for segment in _parse_full_script(supplied)],
            )

    def test_direct_writer_path_persists_requested_studio_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            jobs, locks = self.make_job(root)
            job = jobs / "abcd1234"
            (job / "request.json").write_text(
                json.dumps({
                    "topic": "Cinema topic",
                    "duration_mode": "standard",
                    "use_adk_agent": False,
                    "studio_name": "Cinema Lab",
                    "language": "en-US",
                    "genre": "cinematic_documentary",
                    "presenter_mode": "voiceover_only",
                    "voice_actor": "Kore",
                }),
                encoding="utf-8",
            )
            with (
                patch("writer_agent_vertex.generate_narration_script", return_value=draft(12)) as narration,
                patch("writer_agent_vertex.generate_exact_lock", side_effect=lock_batch) as exact,
            ):
                run_script_pipeline("abcd1234", jobs, locks)

            result = json.loads((job / "result.json").read_text(encoding="utf-8"))
            self.assertEqual(result["studio_name"], "Cinema Lab")
            self.assertEqual(result["language"], "en-US")
            self.assertEqual(result["genre"], "cinematic_documentary")
            self.assertEqual(result["presenter_mode"], "voiceover_only")
            self.assertEqual(result["voice_actor"], "Kore")
            narration.assert_called_once_with(
                "Cinema topic", "standard", language="en-US", genre="cinematic_documentary"
            )
            self.assertEqual(exact.call_args.args[0]["language"], "en-US")

    def test_job_retry_has_a_bounded_cooldown(self):
        with (
            patch.dict("os.environ", {}, clear=True),
            patch("backend.script_pipeline.time.sleep") as sleep,
        ):
            _sleep_before_script_retry(0)
            _sleep_before_script_retry(1)
            _sleep_before_script_retry(4)

        self.assertEqual([call.args[0] for call in sleep.call_args_list], [30, 60, 120])

    def test_rate_limited_job_retry_uses_a_longer_cooldown(self):
        with (
            patch.dict("os.environ", {}, clear=True),
            patch("backend.script_pipeline.time.sleep") as sleep,
        ):
            _sleep_before_script_retry(0, rate_limited=True)
            _sleep_before_script_retry(1, rate_limited=True)
            _sleep_before_script_retry(4, rate_limited=True)

        self.assertEqual([call.args[0] for call in sleep.call_args_list], [60, 120, 300])

    def test_script_retry_limit_is_bounded_and_configurable(self):
        # B3 (document line 110): at most TWO automatic transient retries, matching
        # writer_agent_vertex.DEFAULT_MAX_ATTEMPTS = 2, and the value is clamped to
        # the 0-2 range. The previous expectation of 3 asserted the pre-fix defect
        # (a retry ceiling inconsistent with the writer agent and the documented
        # limit), so it is corrected here rather than weakened.
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(_script_max_retries(), 2)
        with patch.dict("os.environ", {"FYF_SCRIPT_MAX_RETRIES": "1"}, clear=True):
            self.assertEqual(_script_max_retries(), 1)
        with patch.dict("os.environ", {"FYF_SCRIPT_MAX_RETRIES": "0"}, clear=True):
            self.assertEqual(_script_max_retries(), 0)
        with patch.dict("os.environ", {"FYF_SCRIPT_MAX_RETRIES": "99"}, clear=True):
            self.assertEqual(_script_max_retries(), 2)
        with patch.dict("os.environ", {"FYF_SCRIPT_MAX_RETRIES": "-5"}, clear=True):
            self.assertEqual(_script_max_retries(), 0)

    def test_transient_retry_is_withheld_when_budget_unavailable(self):
        """B3: a transient failure must NOT dispatch an automatic retry that the
        approved budget does not allow; the checkpoint stays resumable instead."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            jobs, locks = self.make_job(root)
            job = jobs / "abcd1234"
            with (
                patch.dict("os.environ", {"FYF_BUDGET_LEDGER_PATH": str(root / ".budget_ledger.json")}),
                patch("backend.agent.runner.run_adk_pipeline", side_effect=TimeoutError("Connection timed out")) as adk,
                patch("backend.script_pipeline.time.sleep") as sleep,
                patch("backend.budget_store.is_budget_available", return_value=False),
            ):
                run_script_pipeline("abcd1234", jobs, locks)

            self.assertEqual(adk.call_count, 1, "No automatic retry when the approved budget is unavailable")
            self.assertFalse(sleep.called, "No retry cooldown is incurred when the retry is withheld")
            status = json.loads((job / "status.json").read_text(encoding="utf-8"))
            self.assertEqual(status["status"], "needs_attention")
            self.assertTrue(status["restart_resumable"])
            self.assertIn("approved budget", status["error"])

    def test_transient_retry_proceeds_when_budget_available(self):
        """B3: when the approved budget allows it, a transient failure retries with
        the bounded cooldown and the job can still complete."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            jobs, locks = self.make_job(root)
            job = jobs / "abcd1234"
            completed = {
                "script": {
                    "title": "\u1015\u103c\u1014\u103a\u101c\u100a\u103a\u1000\u103c\u102d\u102f\u1038\u101b\u103e\u1014\u103e\u102c\u1019\u103e\u102f",
                    "language": "my-MM",
                    "segments": [
                        {
                            "id": "s1", "text": "\u1005\u102c\u101e\u102c\u1038", "visual_action": "explain",
                            "scene_type": "whiteboard", "mascot_action": "explain",
                            "emotion": "focused", "emphasis": [],
                        }
                    ],
                },
                "draft": {},
                "audit": {"passed": True},
            }
            with (
                patch.dict("os.environ", {"FYF_BUDGET_LEDGER_PATH": str(root / ".budget_ledger.json")}),
                patch("backend.agent.runner.run_adk_pipeline", side_effect=[TimeoutError("Connection timed out"), completed]) as adk,
                patch("backend.script_pipeline.time.sleep") as sleep,
                patch("backend.budget_store.is_budget_available", return_value=True),
            ):
                run_script_pipeline("abcd1234", jobs, locks)

            self.assertEqual(adk.call_count, 2, "Transient failure retries once when the budget allows it")
            self.assertEqual(sleep.call_count, 1, "Bounded cooldown is applied before the retry")
            status = json.loads((job / "status.json").read_text(encoding="utf-8"))
            self.assertEqual(status["status"], "completed")

    def test_transient_error_sets_needs_attention_when_retries_exhausted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            jobs, locks = self.make_job(root)
            job = jobs / "abcd1234"
            (job / "status.json").write_text(
                json.dumps({"job_id": "abcd1234", "status": "queued", "retry_count": 3}),
                encoding="utf-8",
            )
            with patch("backend.agent.runner.run_adk_pipeline", side_effect=TimeoutError("Connection timed out")):
                run_script_pipeline("abcd1234", jobs, locks)

            status = json.loads((job / "status.json").read_text(encoding="utf-8"))
            self.assertEqual(status["status"], "needs_attention")
            self.assertTrue(status["restart_resumable"])
            self.assertIn("temporarily unavailable", status["error"])

    def test_non_transient_error_fails_closed_immediately(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            jobs, locks = self.make_job(root)
            job = jobs / "abcd1234"
            with patch("backend.agent.runner.run_adk_pipeline", side_effect=ValueError("Invalid script contract")):
                run_script_pipeline("abcd1234", jobs, locks)

            status = json.loads((job / "status.json").read_text(encoding="utf-8"))
            self.assertEqual(status["status"], "failed")
            self.assertFalse(status["restart_resumable"])

    def test_authorization_failure_has_safe_operator_action(self):
        self.assertEqual(
            _terminal_error_message(RuntimeError("Vertex returned 403 PERMISSION_DENIED")),
            "Vertex authorization was rejected. The operator must configure approved provider credentials.",
        )

    def test_restart_uses_existing_narration_and_batch_checkpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            jobs, locks = self.make_job(root)
            job = jobs / "abcd1234"
            (job / "request.json").write_text(
                json.dumps({"topic": "ရှည်လျားသော စမ်းသပ်မှု", "duration_mode": "long", "use_adk_agent": False}),
                encoding="utf-8",
            )
            source = draft(12)
            (job / "narration.json").write_text(json.dumps(source, ensure_ascii=False), encoding="utf-8")
            (job / "locked-batch-000.json").write_text(
                json.dumps(lock_batch({"title": source["title"], "approved_segments": [
                    {"id": item["id"], "text": item["text"]} for item in source["segments"][:5]
                ]}), ensure_ascii=False),
                encoding="utf-8",
            )
            with (
                patch("writer_agent_vertex.generate_narration_script") as narration,
                patch("writer_agent_vertex.generate_exact_lock", side_effect=lock_batch) as exact,
            ):
                run_script_pipeline("abcd1234", jobs, locks)

            self.assertFalse(narration.called)
            self.assertEqual(exact.call_count, 2)
            result = json.loads((job / "result.json").read_text(encoding="utf-8"))
            self.assertEqual(len(result["segments"]), 12)

    def test_script_pipeline_routes_through_adk_agent_when_configured(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            jobs, locks = self.make_job(root)
            job = jobs / "abcd1234"
            (job / "request.json").write_text(
                json.dumps({"topic": "စိုက်ပျိုးရေး", "duration_mode": "short", "use_adk_agent": True}),
                encoding="utf-8",
            )
            mock_video_script = {
                "title": "လယ်ယာကဏ္ဍ",
                "language": "my-MM",
                "segments": [
                    {
                        "id": f"s{i}",
                        "text": f"စာသား {i}",
                        "visual_action": "explain",
                        "scene_type": "whiteboard",
                        "mascot_action": "explain",
                        "emotion": "focused",
                        "emphasis": [],
                    }
                    for i in range(1, 6)
                ],
            }
            with patch(
                "backend.agent.runner.run_adk_pipeline",
                return_value={"script": mock_video_script, "draft": {}, "audit": {"passed": True}},
            ) as mock_adk:
                run_script_pipeline("abcd1234", jobs, locks)

            mock_adk.assert_called_once_with("စိုက်ပျိုးရေး", "short", job_dir=job)
            status = json.loads((job / "status.json").read_text(encoding="utf-8"))
            self.assertEqual(status["status"], "completed")
            self.assertEqual(status["stage"], "locked")
            self.assertIsNotNone(status.get("lock_id"))
            self.assertEqual(
                json.loads((job / "result.json").read_text(encoding="utf-8")),
                mock_video_script,
            )


if __name__ == "__main__":
    unittest.main()
