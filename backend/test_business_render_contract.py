import json
import os
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend.main import VideoRequest, app
from backend.mouth_cues import build_render_input


def _write_wav(path: Path, seconds: float = 0.4, rate: int = 8_000) -> None:
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(rate)
        wav_file.writeframes(b"\x00\x00" * round(seconds * rate))


def _legacy_script() -> dict:
    return {
        "title": "မြန်မာ စမ်းသပ်မှု",
        "language": "my-MM",
        "segments": [
            {
                "id": "s1",
                "text": "စမ်းသပ်ချက်",
                "visual_action": "Show result",
                "scene_type": "demo",
                "mascot_action": "present",
                "emotion": "neutral",
                "emphasis": [],
            }
        ],
    }


class BusinessRenderContractTests(unittest.TestCase):
    def test_video_request_defaults_preserve_legacy_render_behavior(self):
        request = VideoRequest(lock_id="deadbeef")

        self.assertEqual(request.cta_text, "")
        self.assertTrue(request.retention_progress_bar)
        self.assertTrue(request.animated_lower_thirds)
        self.assertEqual(request.aspect_ratio, "9:16")

    def test_video_request_rejects_blank_or_overlong_cta(self):
        with self.assertRaises(ValueError):
            VideoRequest(lock_id="deadbeef", cta_text="   ")
        with self.assertRaises(ValueError):
            VideoRequest(lock_id="deadbeef", cta_text="x" * 81)

    def test_video_request_rejects_unknown_aspect_ratio(self):
        with self.assertRaises(ValueError):
            VideoRequest(lock_id="deadbeef", aspect_ratio="4:3")

    def test_video_request_rejects_non_boolean_render_controls(self):
        with self.assertRaises(ValueError):
            VideoRequest(lock_id="deadbeef", retention_progress_bar=1)
        with self.assertRaises(ValueError):
            VideoRequest(lock_id="deadbeef", animated_lower_thirds="false")

    def test_build_render_input_persists_controls_and_dimensions(self):
        with tempfile.TemporaryDirectory() as directory:
            wav_path = Path(directory) / "voice.wav"
            _write_wav(wav_path)
            script = {
                **_legacy_script(),
                "render_controls": {
                    "cta_text": "Learn More",
                    "retention_progress_bar": False,
                    "animated_lower_thirds": False,
                    "aspect_ratio": "1:1",
                },
            }

            render_input = build_render_input(script, wav_path)

        self.assertEqual(render_input["cta_text"], "Learn More")
        self.assertFalse(render_input["retention_progress_bar"])
        self.assertFalse(render_input["animated_lower_thirds"])
        self.assertEqual(render_input["aspect_ratio"], "1:1")
        self.assertEqual((render_input["width"], render_input["height"]), (1080, 1080))

    def test_build_render_input_persists_studio_metadata_for_remotion(self):
        with tempfile.TemporaryDirectory() as directory:
            wav_path = Path(directory) / "voice.wav"
            _write_wav(wav_path)
            script = {
                **_legacy_script(),
                "studio_name": "Cinema Lab",
                "genre": "cinematic_documentary",
                "presenter_mode": "voiceover_only",
                "voice_actor": "Fenrir",
            }

            render_input = build_render_input(script, wav_path)

        self.assertEqual(render_input["studio_name"], "Cinema Lab")
        self.assertEqual(render_input["genre"], "cinematic_documentary")
        self.assertEqual(render_input["presenter_mode"], "voiceover_only")
        self.assertEqual(render_input["voice_actor"], "Fenrir")

    def test_generate_video_persists_controls_in_job_script_and_passes_them_to_pipeline(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lock_path = root / "locks" / "deadbeef.json"
            lock_path.parent.mkdir()
            lock_path.write_text(json.dumps(_legacy_script()), encoding="utf-8")
            jobs_root = root / "jobs"

            with patch.dict("os.environ", {"FYF_DAILY_BUDGET_CAP_USD": "10.0", "FYF_TOTAL_BUDGET_CAP_USD": "50.0", "FYF_BUDGET_LEDGER_PATH": str(root / ".budget_ledger.json")}), patch("backend.main.LOCKS_ROOT", lock_path.parent), patch(
                "backend.main.JOBS_ROOT", jobs_root
            ), patch("backend.main.SCRIPT_JOBS_ROOT", root / "script-jobs"), patch(
                "backend.main.apply_video_style", side_effect=lambda script, _style: script
            ), patch(
                "backend.main.read_script_lock", return_value=_legacy_script()
            ), patch("backend.main._run_video_pipeline_tracked"):
                with TestClient(app) as client:
                    response = client.post(
                        "/api/generate-video",
                        json={
                            "lock_id": "deadbeef",
                            "cta_text": "Shop Now",
                            "retention_progress_bar": False,
                            "animated_lower_thirds": True,
                            "aspect_ratio": "16:9",
                        },
                    )

            self.assertEqual(response.status_code, 202)
            job_id = response.json()["job_id"]
            saved = json.loads((jobs_root / job_id / "script.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["render_controls"], {
                "cta_text": "Shop Now",
                "retention_progress_bar": False,
                "animated_lower_thirds": True,
                "aspect_ratio": "16:9",
            })

    def test_public_archive_requires_generation_access_before_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            jobs_root = Path(directory) / "jobs"
            job_dir = jobs_root / "deadbeef"
            job_dir.mkdir(parents=True)
            (job_dir / "status.json").write_text(
                json.dumps({"job_id": "deadbeef", "status": "completed"}),
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {
                    "FYF_PUBLIC_DEPLOYMENT": "true",
                    "FYF_PUBLIC_GENERATION_ENABLED": "true",
                    "FYF_GENERATION_ACCESS_TOKEN": "operator-only",
                    "FYF_VERTEX_API_KEY": "configured",
                },
                clear=True,
            ), patch("backend.main.JOBS_ROOT", jobs_root), patch(
                "backend.main.SCRIPT_JOBS_ROOT", Path(directory) / "script-jobs"
            ):
                with TestClient(app) as client:
                    response = client.delete("/api/jobs/deadbeef")

            self.assertEqual(response.status_code, 401)
            self.assertEqual(
                json.loads((job_dir / "status.json").read_text(encoding="utf-8"))["status"],
                "completed",
            )

    def test_clickhouse_query_accepts_only_server_owned_query_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            jobs_root = Path(directory) / "jobs"
            with patch("backend.main.JOBS_ROOT", jobs_root), patch(
                "backend.main.SCRIPT_JOBS_ROOT", Path(directory) / "script-jobs"
            ), patch("backend.main.REPO_ROOT", Path(directory)):
                with TestClient(app) as client:
                    missing_id = client.post("/api/clickhouse/query", json={"query": "SELECT 1"})
                    unknown_id = client.post("/api/clickhouse/query", json={"query_id": "users"})
                    valid_id = client.post("/api/clickhouse/query", json={"query_id": "jobs_overview"})

            self.assertEqual(missing_id.status_code, 400)
            self.assertEqual(unknown_id.status_code, 400)
            self.assertEqual(valid_id.status_code, 200)
            self.assertEqual(valid_id.json()["source"], "local_mirror")


if __name__ == "__main__":
    unittest.main()
