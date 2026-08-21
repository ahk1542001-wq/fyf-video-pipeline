import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.script_pipeline import (
    _script_max_retries,
    _sleep_before_script_retry,
    run_script_pipeline,
)


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
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(_script_max_retries(), 3)
        with patch.dict("os.environ", {"FYF_SCRIPT_MAX_RETRIES": "1"}, clear=True):
            self.assertEqual(_script_max_retries(), 1)
        with patch.dict("os.environ", {"FYF_SCRIPT_MAX_RETRIES": "99"}, clear=True):
            self.assertEqual(_script_max_retries(), 3)

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


if __name__ == "__main__":
    unittest.main()
