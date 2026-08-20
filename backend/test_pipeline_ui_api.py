import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend.main import _should_resume_script_job, app


class PipelineUIAPITests(unittest.TestCase):
    def test_startup_does_not_requeue_terminal_script_failures(self):
        self.assertFalse(_should_resume_script_job({
            "status": "failed",
            "retry_count": 3,
            "restart_resumable": True,
        }))
        self.assertFalse(_should_resume_script_job({
            "status": "failed",
            "retry_count": 4,
            "restart_resumable": True,
        }))
        self.assertFalse(_should_resume_script_job({
            "status": "failed",
            "retry_count": 0,
            "restart_resumable": False,
        }))
        self.assertTrue(_should_resume_script_job({"status": "queued"}))
        self.assertTrue(_should_resume_script_job({"status": "writing"}))

    def _write_job(
        self,
        jobs_root: Path,
        job_id: str,
        *,
        title: str = "Approved title",
        provider: str = "gemini",
        updated_at: str = "2026-08-19T12:00:00Z",
        status: str = "completed",
        qa_passed: bool = True,
        final_visual_qa_passed: bool = True,
        video: bool = True,
        corrupt_status: bool = False,
    ) -> Path:
        job_dir = jobs_root / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        if corrupt_status:
            (job_dir / "status.json").write_text("{not-json", encoding="utf-8")
        else:
            (job_dir / "status.json").write_text(
                json.dumps(
                    {
                        "job_id": job_id,
                        "status": status,
                        "updated_at": updated_at,
                        "voice_provider": provider,
                        "qa_report": {"passed": qa_passed},
                        "final_visual_qa": {"passed": final_visual_qa_passed},
                    }
                ),
                encoding="utf-8",
            )
        (job_dir / "script.json").write_text(
            json.dumps({"title": title}),
            encoding="utf-8",
        )
        if video:
            (job_dir / "video.mp4").write_bytes(b"approved-video")
        return job_dir

    def test_runtime_hackathon_exposes_only_gemini_and_routed_models(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            os.environ,
            {
                "FYF_RUNTIME_MODE": "hackathon",
                "FYF_VERTEX_SCRIPT_MODEL": "script-override",
                "FYF_VERTEX_STORY_FALLBACK_MODEL": "fallback-override",
            },
        ), patch("backend.main.SCRIPT_JOBS_ROOT", Path(temp_dir) / "script-jobs"):
            with TestClient(app) as client:
                response = client.get("/api/runtime")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "runtime_mode": "hackathon",
                "allowed_voice_providers": ["gemini"],
                "script_model": "script-override",
                "fallback_model": "fallback-override",
            },
        )

    def test_runtime_product_exposes_all_voice_modes_and_model_overrides(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            os.environ,
            {
                "FYF_RUNTIME_MODE": "product",
                "FYF_VERTEX_SCRIPT_MODEL": "product-script",
                "FYF_VERTEX_STORY_FALLBACK_MODEL": "product-fallback",
            },
        ), patch("backend.main.SCRIPT_JOBS_ROOT", Path(temp_dir) / "script-jobs"):
            with TestClient(app) as client:
                response = client.get("/api/runtime")

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["runtime_mode"], "product")
        self.assertEqual(data["allowed_voice_providers"], ["kaggle", "gemini", "dual"])
        self.assertEqual(data["script_model"], "product-script")
        self.assertEqual(data["fallback_model"], "product-fallback")

    def test_recent_returns_only_newest_six_completed_approved_jobs_with_safe_fields(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            jobs_root = Path(temp_dir) / "jobs"
            for index in range(1, 8):
                self._write_job(
                    jobs_root,
                    f"0000000{index}",
                    title=f"Canonical title {index}",
                    updated_at=f"2026-08-19T12:0{index}:00Z",
                )
            self._write_job(jobs_root, "deadbeef", corrupt_status=True)
            self._write_job(jobs_root, "abcdef12", qa_passed=False)
            self._write_job(jobs_root, "feedface", video=False)
            self._write_job(jobs_root, "not-valid")

            outside_root = Path(temp_dir) / "outside"
            outside_job = self._write_job(outside_root, "11223344", title="Outside title")
            (jobs_root / "11223344").symlink_to(outside_job, target_is_directory=True)

            with patch("backend.main.JOBS_ROOT", jobs_root), patch(
                "backend.main.SCRIPT_JOBS_ROOT", Path(temp_dir) / "script-jobs"
            ):
                with TestClient(app) as client:
                    response = client.get("/api/jobs/recent")

        self.assertEqual(response.status_code, 200)
        recent = response.json()
        self.assertEqual(
            [item["job_id"] for item in recent],
            ["00000007", "00000006", "00000005", "00000004", "00000003", "00000002"],
        )
        self.assertEqual(
            set(recent[0]),
            {"job_id", "title", "voice_provider", "updated_at", "video_url"},
        )
        self.assertEqual(recent[0]["title"], "Canonical title 7")
        self.assertEqual(recent[0]["voice_provider"], "gemini")
        self.assertEqual(recent[0]["video_url"], "/api/jobs/00000007/video")
        self.assertNotIn(temp_dir, json.dumps(recent))


if __name__ == "__main__":
    unittest.main()
