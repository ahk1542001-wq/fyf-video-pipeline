"""Tests for production Vertex/Gemini TTS usage telemetry."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from backend.main import app
from backend.vertex_telemetry import (
    telemetry_job_attempt,
    telemetry_retry_attempt,
    telemetry_scope,
    track_client,
)
from backend.telemetry_store import get_job_telemetry


def _usage(*, prompt: int = 1000, output: int = 2000, total: int = 3000):
    return SimpleNamespace(
        prompt_token_count=prompt,
        candidates_token_count=output,
        total_token_count=total,
        cached_content_token_count=0,
        thoughts_token_count=0,
    )


def _response(*, usage=None):
    return SimpleNamespace(usage_metadata=usage or _usage(), candidates=[])


class VertexTelemetryTests(unittest.TestCase):
    def test_tracks_usage_model_latency_and_known_cost_without_persisting_prompt(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            delegate = MagicMock()
            delegate.models.generate_content.return_value = _response()

            with telemetry_scope("a1b2c3d4", "script", root) as collector:
                tracked = track_client(delegate, stage="script")
                result = tracked.models.generate_content(
                    model="gemini-3.1-pro-preview",
                    contents="private prompt that must not be written",
                )

            self.assertIsNotNone(result)
            payload = json.loads((root / "telemetry.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["job_id"], "a1b2c3d4")
            self.assertEqual(payload["job_kind"], "script")
            self.assertEqual(payload["summary"]["total_calls"], 1)
            self.assertEqual(payload["summary"]["total_input_tokens"], 1000)
            self.assertEqual(payload["summary"]["total_output_tokens"], 2000)
            self.assertEqual(payload["summary"]["total_tokens"], 3000)
            self.assertEqual(payload["summary"]["cost_status"], "exact")
            self.assertAlmostEqual(payload["summary"]["estimated_cost_usd"], 0.026, places=6)
            self.assertEqual(payload["calls"][0]["model"], "gemini-3.1-pro-preview")
            self.assertEqual(payload["calls"][0]["operation"], "generate_content")
            self.assertEqual(payload["calls"][0]["attempt"], 1)
            self.assertEqual(payload["calls"][0]["status"], "succeeded")
            self.assertNotIn("private prompt", json.dumps(payload))
            self.assertEqual(collector.summary()["total_calls"], 1)

    def test_records_failed_attempt_and_retry_attempt_separately(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            delegate = MagicMock()
            delegate.models.generate_content.side_effect = [
                RuntimeError("provider detail must not be persisted"),
                _response(),
            ]

            with telemetry_scope("deadbeef", "video", root) as tracked_collector:
                tracked = track_client(delegate, stage="visual")
                with telemetry_retry_attempt("visual retry", 1):
                    with self.assertRaises(RuntimeError):
                        tracked.models.generate_content(
                            model="unknown-model",
                            contents="not persisted",
                        )
                with telemetry_retry_attempt("visual retry", 2):
                    tracked.models.generate_content(
                        model="unknown-model",
                        contents="not persisted",
                    )

            payload = json.loads((root / "telemetry.json").read_text(encoding="utf-8"))
            self.assertEqual([call["attempt"] for call in payload["calls"]], [1, 2])
            self.assertEqual(payload["summary"]["failed_calls"], 1)
            self.assertEqual(payload["summary"]["retry_calls"], 1)
            self.assertEqual(payload["calls"][1]["retry_group"], "visual retry")
            self.assertEqual(payload["summary"]["cost_status"], "unpriced")
            self.assertIsNone(payload["summary"]["estimated_cost_usd"])
            self.assertEqual(payload["calls"][0]["error_type"], "RuntimeError")
            self.assertNotIn("provider detail", json.dumps(payload))
            self.assertIs(tracked_collector, tracked_collector)

    def test_records_outer_job_retry_attempts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            delegate = MagicMock()
            delegate.models.generate_content.return_value = _response()

            with telemetry_scope("jobretry1", "script", root):
                with telemetry_job_attempt(2):
                    track_client(delegate, stage="script").models.generate_content(
                        model="gemini-3.1-pro-preview",
                        contents="not persisted",
                    )

            payload = json.loads((root / "telemetry.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["calls"][0]["job_attempt"], 2)
            self.assertEqual(payload["summary"]["job_retry_count"], 1)
            self.assertEqual(payload["summary"]["retry_calls"], 1)

    def test_nested_scopes_share_one_collector_and_tts_records_characters(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            delegate = MagicMock()
            delegate.models.generate_content.return_value = _response(
                usage=_usage(prompt=20, output=30, total=50)
            )

            with telemetry_scope("11223344", "video", root) as outer:
                with telemetry_scope("different", "script", Path(temp_dir) / "other") as inner:
                    self.assertIs(outer, inner)
                    track_client(delegate, stage="tts").models.generate_content(
                        model="gemini-2.5-flash-preview-tts",
                        contents="မြန်မာစာ စာလုံးရေ",
                    )

            self.assertTrue((root / "telemetry.json").exists())
            self.assertFalse((Path(temp_dir) / "other" / "telemetry.json").exists())
            payload = json.loads((root / "telemetry.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["calls"][0]["stage"], "tts")
            self.assertEqual(payload["calls"][0]["input_characters"], len("မြန်မာစာ စာလုံးရေ"))

    def test_tracks_video_polling_as_non_billable_operation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            delegate = MagicMock()
            delegate.models.generate_videos.return_value = SimpleNamespace(done=False)
            delegate.operations.get.return_value = SimpleNamespace(done=True)

            with telemetry_scope("55667788", "video", root):
                tracked = track_client(delegate, stage="visual")
                operation = tracked.models.generate_videos(
                    model="veo-3.1-generate-001",
                    source=object(),
                )
                tracked.operations.get(operation)

            payload = json.loads((root / "telemetry.json").read_text(encoding="utf-8"))
            self.assertEqual(
                [call["operation"] for call in payload["calls"]],
                ["generate_videos", "operation_poll"],
            )
            self.assertEqual(payload["summary"]["billable_calls"], 1)
            self.assertEqual(payload["summary"]["operation_poll_calls"], 1)

    def test_job_reader_prefers_detailed_job_local_telemetry_and_keeps_legacy_scenes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            jobs_root = root / "jobs"
            job_dir = jobs_root / "abcdef12"
            job_dir.mkdir(parents=True)
            (job_dir / "telemetry.json").write_text(
                json.dumps({"job_id": "abcdef12", "calls": [{"model": "x"}]}),
                encoding="utf-8",
            )
            legacy_dir = root / "legacy"
            legacy_dir.mkdir()
            (legacy_dir / "job_abcdef12.json").write_text(
                json.dumps({"job_id": "abcdef12", "total_tokens_used": 7}),
                encoding="utf-8",
            )

            details = get_job_telemetry(
                "abcdef12",
                base_dir=legacy_dir,
                job_roots=(jobs_root,),
            )

            self.assertEqual(details["job"]["calls"][0]["model"], "x")
            self.assertEqual(details["job"]["job_id"], "abcdef12")

    def test_api_returns_job_local_telemetry_for_dashboard_consumers(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            jobs_root = root / "jobs"
            script_jobs_root = root / "script-jobs"
            (jobs_root / "abcdef12").mkdir(parents=True)
            (jobs_root / "abcdef12" / "telemetry.json").write_text(
                json.dumps({"job_id": "abcdef12", "summary": {"total_calls": 3}}),
                encoding="utf-8",
            )
            with patch("backend.main.REPO_ROOT", root), patch(
                "backend.main.JOBS_ROOT", jobs_root
            ), patch("backend.main.SCRIPT_JOBS_ROOT", script_jobs_root):
                with TestClient(app) as client:
                    response = client.get("/api/jobs/abcdef12/telemetry")

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["job"]["summary"]["total_calls"], 3)


if __name__ == "__main__":
    unittest.main()
