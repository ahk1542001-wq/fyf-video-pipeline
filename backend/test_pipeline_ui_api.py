import json
import os
import tempfile
import unittest
import asyncio
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from backend.main import _should_resume_script_job, app, resume_interrupted_script_jobs


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

    def test_health_and_same_origin_api_health_alias_return_ok(self):
        # Hermetic: startup lifespan must never scan or resume real paid jobs.
        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "backend.main.JOBS_ROOT", Path(temp_dir) / "jobs"
        ), patch("backend.main.SCRIPT_JOBS_ROOT", Path(temp_dir) / "script-jobs"):
            with TestClient(app) as client:
                for route in ("/health", "/api/health"):
                    response = client.get(route)
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(
                        response.json(),
                        {"status": "ok", "service": "fyf-video-pipeline"},
                    )

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

    def test_runtime_exposes_only_gemini_and_routed_models(self):
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
                "generation_available": True,
                "generation_access_required": False,
                "generation_status": "ready",
                "generation_message": "Local generation controls are available.",
            },
        )

    def test_public_runtime_fails_closed_without_vertex_credential(self):
        with patch.dict(os.environ, {"FYF_PUBLIC_DEPLOYMENT": "true"}, clear=True), patch(
            "backend.main._vertex_credentials_configured", return_value=False
        ), tempfile.TemporaryDirectory() as temp_dir, patch(
            "backend.main.JOBS_ROOT", Path(temp_dir) / "jobs"
        ), patch("backend.main.SCRIPT_JOBS_ROOT", Path(temp_dir) / "script-jobs"):
            with TestClient(app) as client:
                response = client.get("/api/runtime")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertFalse(payload["generation_available"])
        self.assertFalse(payload["generation_access_required"])
        self.assertEqual(payload["generation_status"], "credential_required")

    def test_public_generation_rejects_missing_access_token_before_job_initialization(self):
        lease_factory = MagicMock()
        with patch.dict(
            os.environ,
            {
                "FYF_PUBLIC_DEPLOYMENT": "true",
                "FYF_PUBLIC_GENERATION_ENABLED": "true",
                "FYF_GENERATION_ACCESS_TOKEN": "operator-only",
                "FYF_VERTEX_API_KEY": "configured-but-never-used-in-test",
            },
            clear=True,
        ), patch("backend.main.acquire_guardrail_lease", lease_factory), tempfile.TemporaryDirectory() as temp_dir, patch(
            "backend.main.JOBS_ROOT", Path(temp_dir) / "jobs"
        ), patch("backend.main.SCRIPT_JOBS_ROOT", Path(temp_dir) / "script-jobs"):
            with TestClient(app) as client:
                response = client.post("/api/generate-script", json={"topic": "Test topic"})

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["detail"], "Private generation access is required.")
        lease_factory.assert_not_called()

    def test_public_startup_never_auto_resumes_paid_jobs(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            script_jobs_root = root / "script-jobs"
            jobs_root = root / "jobs"
            interrupted_script = script_jobs_root / "abcd1234"
            interrupted_script.mkdir(parents=True)
            (interrupted_script / "status.json").write_text(json.dumps({
                "job_id": "abcd1234",
                "status": "writing",
                "stage": "storyboard",
                "restart_resumable": True,
            }), encoding="utf-8")
            interrupted_video = jobs_root / "deadbeef"
            interrupted_video.mkdir(parents=True)
            (interrupted_video / "status.json").write_text(json.dumps({
                "job_id": "deadbeef",
                "status": "rendering",
                "restart_resumable": True,
            }), encoding="utf-8")
            completed_script = script_jobs_root / "feedface"
            completed_script.mkdir(parents=True)
            (completed_script / "status.json").write_text(json.dumps({
                "job_id": "feedface",
                "status": "completed",
                "stage": "completed",
                "restart_resumable": False,
            }), encoding="utf-8")
            failed_video = jobs_root / "facefeed"
            failed_video.mkdir(parents=True)
            (failed_video / "status.json").write_text(json.dumps({
                "job_id": "facefeed",
                "status": "failed",
                "restart_resumable": True,
            }), encoding="utf-8")
            malformed_job = script_jobs_root / "c0ffee00"
            malformed_job.mkdir(parents=True)
            (malformed_job / "status.json").write_text("[]", encoding="utf-8")
            outside_job = root / "outside" / "11223344"
            outside_job.mkdir(parents=True)
            outside_status = outside_job / "status.json"
            outside_status.write_text(json.dumps({
                "job_id": "11223344",
                "status": "writing",
                "restart_resumable": True,
            }), encoding="utf-8")
            (script_jobs_root / "11223344").symlink_to(outside_job, target_is_directory=True)

            with patch.dict(os.environ, {"FYF_PUBLIC_DEPLOYMENT": "true"}, clear=True), patch(
                "backend.main.SCRIPT_JOBS_ROOT", script_jobs_root
            ), patch("backend.main.JOBS_ROOT", jobs_root), patch(
                "backend.main.run_script_pipeline"
            ) as script_pipeline, patch(
                "backend.main._run_video_pipeline_tracked"
            ) as video_pipeline, patch(
                "backend.main.release_reservation"
            ) as release_reservation:
                asyncio.run(resume_interrupted_script_jobs())

            script_pipeline.assert_not_called()
            video_pipeline.assert_not_called()
            script_status = json.loads((interrupted_script / "status.json").read_text(encoding="utf-8"))
            video_status = json.loads((interrupted_video / "status.json").read_text(encoding="utf-8"))
            completed_status = json.loads((completed_script / "status.json").read_text(encoding="utf-8"))
            failed_status = json.loads((failed_video / "status.json").read_text(encoding="utf-8"))
            outside_data = json.loads(outside_status.read_text(encoding="utf-8"))
            self.assertEqual(script_status["status"], "needs_attention")
            self.assertEqual(video_status["status"], "needs_attention")
            self.assertEqual(completed_status["status"], "completed")
            self.assertEqual(failed_status["status"], "failed")
            self.assertEqual(outside_data["status"], "writing")
            self.assertEqual((malformed_job / "status.json").read_text(encoding="utf-8"), "[]")
            self.assertTrue(script_status["restart_resumable"])
            self.assertTrue(video_status["restart_resumable"])
            self.assertIn("Public deployment restart", script_status["error"])
            self.assertIn("Public deployment restart", video_status["error"])
            self.assertEqual(
                {call.args[0] for call in release_reservation.call_args_list},
                {"abcd1234", "deadbeef"},
            )

    def test_public_startup_retries_quarantine_when_reservation_release_fails(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            script_jobs_root = root / "script-jobs"
            jobs_root = root / "jobs"
            interrupted = script_jobs_root / "abcd1234"
            interrupted.mkdir(parents=True)
            status_path = interrupted / "status.json"
            status_path.write_text(json.dumps({
                "job_id": "abcd1234",
                "status": "writing",
                "restart_resumable": True,
            }), encoding="utf-8")

            common_patches = (
                patch.dict(os.environ, {"FYF_PUBLIC_DEPLOYMENT": "true"}, clear=True),
                patch("backend.main.SCRIPT_JOBS_ROOT", script_jobs_root),
                patch("backend.main.JOBS_ROOT", jobs_root),
            )
            with common_patches[0], common_patches[1], common_patches[2], patch(
                "backend.main.release_reservation", side_effect=OSError("ledger unavailable")
            ):
                asyncio.run(resume_interrupted_script_jobs())

            first_status = json.loads(status_path.read_text(encoding="utf-8"))
            self.assertEqual(first_status["status"], "writing")

            with patch.dict(os.environ, {"FYF_PUBLIC_DEPLOYMENT": "true"}, clear=True), patch(
                "backend.main.SCRIPT_JOBS_ROOT", script_jobs_root
            ), patch("backend.main.JOBS_ROOT", jobs_root), patch(
                "backend.main.release_reservation"
            ) as release_reservation:
                asyncio.run(resume_interrupted_script_jobs())

            second_status = json.loads(status_path.read_text(encoding="utf-8"))
            self.assertEqual(second_status["status"], "needs_attention")
            release_reservation.assert_called_once_with("abcd1234")

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

    def test_resume_job_endpoint_queues_resumable_job(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            jobs_root = Path(temp_dir) / "jobs"
            job_id = "00000099"
            self._write_job(
                jobs_root,
                job_id,
                status="needs_attention",
                video=False,
            )
            # Ensure status is restart_resumable
            (jobs_root / job_id / "status.json").write_text(
                json.dumps({
                    "job_id": job_id,
                    "status": "needs_attention",
                    "voice_provider": "gemini",
                    "restart_resumable": True,
                    "resume_count": 1,
                    "updated_at": "2026-08-20T00:00:00Z",
                }),
                encoding="utf-8",
            )
            with patch("backend.main.JOBS_ROOT", jobs_root), patch(
                "backend.main.SCRIPT_JOBS_ROOT", Path(temp_dir) / "script-jobs"
            ), patch("backend.main.run_pipeline", return_value=None):
                with TestClient(app) as client:
                    response = client.post(f"/api/jobs/{job_id}/resume")

            self.assertEqual(response.status_code, 202)
            data = response.json()
            self.assertTrue(data["success"])
            self.assertEqual(data["job_id"], job_id)

            status = json.loads((jobs_root / job_id / "status.json").read_text())
            self.assertEqual(status["status"], "queued")
            self.assertEqual(status["resume_count"], 2)


if __name__ == "__main__":
    unittest.main()
