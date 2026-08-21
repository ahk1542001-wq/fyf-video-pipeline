import os
import tempfile
import json
import unittest
from unittest.mock import patch, MagicMock, AsyncMock
from pathlib import Path
from fastapi.testclient import TestClient

from backend.main import app
from backend.render_video import (
    REMOTION_COMPOSITION_ID,
    render_video_remotion,
    render_video_segment,
)

client = TestClient(app)

class TestRenderVideo(unittest.TestCase):
    def setUp(self):
        from backend.runtime_limits import clear_limits_state
        clear_limits_state()

    def tearDown(self):
        from backend.runtime_limits import clear_limits_state
        clear_limits_state()

    def test_local_cors_is_reserved_for_video_pipeline_port_3001(self):
        cors = next(
            middleware
            for middleware in app.user_middleware
            if middleware.cls.__name__ == "CORSMiddleware"
        )
        self.assertEqual(
            cors.kwargs["allow_origins"],
            ["http://localhost:3001", "http://127.0.0.1:3001"],
        )

    def _segment_render_input(self):
        return {
            "fps": 30,
            "durationInFrames": 60,
            "audioSrc": "voice.wav",
            "segments": [
                {
                    "id": "s1",
                    "startFrame": 0,
                    "endFrame": 30,
                    "text": "First",
                    "visual": {"kind": "generic", "phase": "setup", "screen_text": ["First"]},
                },
                {
                    "id": "s2",
                    "startFrame": 30,
                    "endFrame": 60,
                    "text": "Second",
                    "visual": {"kind": "generic", "phase": "setup", "screen_text": ["Second"]},
                },
            ],
            "mouthCues": [],
        }

    @patch("backend.render_video.validate_render_input")
    @patch("backend.render_video.subprocess.run")
    def test_render_video_segment_uses_safe_video_only_frame_range_command(self, mock_run, mock_validate):
        with tempfile.TemporaryDirectory() as temp_dir:
            job_dir = Path(temp_dir)
            (job_dir / "voice.wav").write_bytes(b"mock audio")
            (job_dir / "render_input.json").write_text(json.dumps(self._segment_render_input()))
            output_path = job_dir / "render-segments" / "s1-abcdef.mp4"

            def side_effect(*args, **kwargs):
                command = args[0]
                output = Path(command[command.index(REMOTION_COMPOSITION_ID) + 1])
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(b"mock segment")

            mock_run.side_effect = side_effect
            result = render_video_segment(
                str(job_dir),
                segment_id="s1",
                start_frame=0,
                end_frame=30,
                output_path=str(output_path),
            )

            self.assertEqual(result, str(output_path.resolve()))
            mock_validate.assert_called_once()
            mock_run.assert_called_once()
            command, kwargs = mock_run.call_args
            command = command[0]
            self.assertIn("--frames=0-29", command)
            self.assertIn("--muted", command)
            self.assertIn("--codec=h264", command)
            self.assertIn("--pixel-format=yuv420p", command)
            self.assertIn("--color-space=bt709", command)
            self.assertFalse(kwargs["shell"])
            self.assertTrue(kwargs["check"])
            self.assertGreaterEqual(kwargs["timeout"], 60)
            self.assertLessEqual(kwargs["timeout"], 7200)
            self.assertTrue(Path(result).is_relative_to((job_dir / "render-segments").resolve()))

    @patch("backend.render_video.subprocess.run")
    def test_render_video_segment_rejects_invalid_range_unknown_segment_and_external_output(self, mock_run):
        with tempfile.TemporaryDirectory() as temp_dir:
            job_dir = Path(temp_dir)
            (job_dir / "voice.wav").write_bytes(b"mock audio")
            (job_dir / "render_input.json").write_text(json.dumps(self._segment_render_input()))
            output_path = job_dir / "render-segments" / "s1.mp4"

            with self.assertRaisesRegex(ValueError, "end_frame"):
                render_video_segment(
                    str(job_dir), segment_id="s1", start_frame=30, end_frame=30,
                    output_path=str(output_path),
                )
            with self.assertRaisesRegex(ValueError, "segment"):
                render_video_segment(
                    str(job_dir), segment_id="missing", start_frame=0, end_frame=30,
                    output_path=str(output_path),
                )
            with self.assertRaisesRegex(ValueError, "render-segments"):
                render_video_segment(
                    str(job_dir), segment_id="s1", start_frame=0, end_frame=30,
                    output_path=str(job_dir / "outside.mp4"),
                )
            mock_run.assert_not_called()

    @patch("backend.render_video.approved_visual_preset")
    @patch("backend.render_video.validate_render_input")
    @patch("backend.render_video.subprocess.run")
    def test_full_and_segment_renderers_share_staged_props_and_assets(
        self, mock_run, mock_validate, mock_preset
    ):
        mock_preset.return_value = {
            "v3SceneAssets": [["fyf-v2/scene-a1.png"]],
            "v3MascotSegments": [0],
        }
        captures = []
        with tempfile.TemporaryDirectory() as temp_dir:
            job_dir = Path(temp_dir)
            (job_dir / "voice.wav").write_bytes(b"mock audio")
            (job_dir / "render_input.json").write_text(json.dumps(self._segment_render_input()))
            visuals = job_dir / "visuals"
            visuals.mkdir()
            (visuals / "verified.png").write_bytes(b"verified")

            def side_effect(*args, **kwargs):
                command = args[0]
                props_path = Path(command[command.index("--props") + 1])
                public_dir = Path(command[command.index("--public-dir") + 1])
                captures.append({
                    "props": json.loads(props_path.read_text()),
                    "public": sorted(
                        str(path.relative_to(public_dir))
                        for path in public_dir.rglob("*")
                        if path.is_file()
                    ),
                })
                output = Path(command[command.index(REMOTION_COMPOSITION_ID) + 1])
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(b"mock video")

            mock_run.side_effect = side_effect
            render_video_remotion(str(job_dir))
            render_video_segment(
                str(job_dir), segment_id="s1", start_frame=0, end_frame=30,
                output_path=str(job_dir / "render-segments" / "s1.mp4"),
            )

            self.assertEqual(len(captures), 2)
            self.assertEqual(captures[0], captures[1])
            self.assertEqual(captures[0]["props"]["audioSrc"], "voice.wav")
            self.assertEqual(captures[0]["props"]["v3MascotSegments"], [0])
            self.assertIn("fyf-v2/scene-a1.png", captures[0]["public"])
            self.assertIn("job-visuals/verified.png", captures[0]["public"])

    def test_startup_resume_counts_active_job_and_enforces_limit(self):
        import backend.main as main_module
        from backend.job_store import initialize_job_status, update_job_status, read_job_status

        with tempfile.TemporaryDirectory() as jobs_dir, tempfile.TemporaryDirectory() as script_jobs, tempfile.TemporaryDirectory() as locks:
            root = Path(jobs_dir)
            resumable = root / "1234abcd"
            resumable.mkdir()
            initialize_job_status(resumable, "1234abcd", "gemini")
            update_job_status(resumable, {"status": "visuals", "resume_count": 2})
            (resumable / "script.json").write_text(json.dumps({"segments": []}))

            creative = root / "90abcdef"
            creative.mkdir()
            initialize_job_status(creative, "90abcdef", "gemini")
            update_job_status(creative, {"status": "creative_qa", "resume_count": 1})
            (creative / "script.json").write_text(json.dumps({"segments": []}))

            exhausted = root / "5678abcd"
            exhausted.mkdir()
            initialize_job_status(exhausted, "5678abcd", "gemini")
            update_job_status(exhausted, {"status": "voice", "resume_count": 3})
            (exhausted / "script.json").write_text(json.dumps({"segments": []}))

            async def exercise():
                with patch.object(main_module, "JOBS_ROOT", root), patch.object(
                    main_module, "SCRIPT_JOBS_ROOT", Path(script_jobs)
                ), patch.object(main_module, "LOCKS_ROOT", Path(locks)), patch.object(
                    main_module, "run_pipeline", new_callable=AsyncMock
                ) as pipeline:
                    await main_module.resume_interrupted_script_jobs()
                    await __import__("asyncio").sleep(0)
                    return pipeline

            pipeline = __import__("asyncio").run(exercise())
            self.assertEqual(pipeline.await_count, 2)
            self.assertEqual(read_job_status(resumable)["resume_count"], 3)
            self.assertEqual(read_job_status(creative)["resume_count"], 2)
            exhausted_status = read_job_status(exhausted)
            self.assertEqual(exhausted_status["status"], "failed")
            self.assertFalse(exhausted_status["restart_resumable"])

    @patch("backend.render_video.validate_render_input")
    @patch("backend.render_video.subprocess.run")
    def test_render_video_remotion(self, mock_run, mock_validate):
        with tempfile.TemporaryDirectory() as temp_dir:
            job_dir = temp_dir

            # Create mock inputs
            audio_path = os.path.join(job_dir, "voice.wav")
            with open(audio_path, "w") as f:
                f.write("mock audio")

            render_input_path = os.path.join(job_dir, "render_input.json")
            with open(render_input_path, "w") as f:
                json.dump({"test": "data"}, f)
            visuals_dir = os.path.join(job_dir, "visuals")
            os.makedirs(visuals_dir)
            with open(os.path.join(visuals_dir, "verified.png"), "w") as f:
                f.write("verified visual")

            # Mock the output file creation and capture props file
            def side_effect(*args, **kwargs):
                cmd = args[0]
                # Read staged props during execution before temp cleanup
                props_path = cmd[cmd.index("--props") + 1]
                with open(props_path, "r") as f:
                    self.staged_props = json.load(f)

                public_dir = cmd[cmd.index("--public-dir") + 1]
                self.public_files = set(os.listdir(public_dir))
                self.job_visual_files = set(os.listdir(os.path.join(public_dir, "job-visuals")))
                self.has_legacy_cinematic_dir = os.path.exists(os.path.join(public_dir, "fyf-v2"))

                out_mp4 = os.path.join(job_dir, "video.mp4")
                with open(out_mp4, "w") as f:
                    f.write("mock video")
                return MagicMock(returncode=0)

            mock_run.side_effect = side_effect

            repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

            # Execute
            result = render_video_remotion(job_dir)

            # Verify
            self.assertEqual(result, os.path.join(job_dir, "video.mp4"))
            mock_validate.assert_called_once()
            mock_run.assert_called_once()
            args, kwargs = mock_run.call_args
            self.assertFalse(kwargs["shell"])
            self.assertTrue(kwargs["check"])
            self.assertIn("timeout", kwargs)
            self.assertTrue(kwargs.get("cwd", "").endswith("remotion"))

            cmd = args[0]

            # Verify positional arguments are exactly as specified before flags
            idx_render = cmd.index("render")
            idx_entry = cmd.index(os.path.join(repo_root, "remotion", "src", "index.ts"))
            idx_comp = cmd.index(REMOTION_COMPOSITION_ID)
            idx_out = cmd.index(os.path.join(job_dir, "video.mp4"))

            self.assertEqual(idx_entry, idx_render + 1)
            self.assertEqual(idx_comp, idx_entry + 1)
            self.assertEqual(idx_out, idx_comp + 1)

            # Verify public dir contents
            self.assertEqual(
                self.public_files,
                {
                    "fyf-mascot-presenting.png",
                    "voice.wav",
                    "fyf-mascot-talking-atlas.png",
                    "fyf-cut-paper-world.png",
                    "job-visuals",
                },
            )
            self.assertEqual(self.job_visual_files, {"verified.png"})
            self.assertFalse(self.has_legacy_cinematic_dir)
            # Check staged props audioSrc captured during mock execution
            self.assertEqual(self.staged_props.get("audioSrc"), "voice.wav")

    @patch("backend.render_video.subprocess.run")
    def test_render_video_remotion_zero_byte_atlas(self, mock_run):
        with tempfile.TemporaryDirectory() as temp_dir:
            job_dir = temp_dir

            # Create mock inputs
            audio_path = os.path.join(job_dir, "voice.wav")
            with open(audio_path, "w") as f:
                f.write("mock audio")

            render_input_path = os.path.join(job_dir, "render_input.json")
            with open(render_input_path, "w") as f:
                json.dump({"test": "data"}, f)

            # mock atlas file check to return size 0
            real_getsize = os.path.getsize
            with patch("backend.render_video.os.path.getsize", side_effect=lambda x: 0 if x.endswith("fyf-mascot-talking-atlas.png") else real_getsize(x)):

                with self.assertRaises(FileNotFoundError) as context:
                    render_video_remotion(job_dir)

                self.assertIn("Missing or empty talking mascot atlas", str(context.exception))

            mock_run.assert_not_called()

    @patch("backend.render_video.approved_visual_preset")
    @patch("backend.render_video.validate_render_input")
    @patch("backend.render_video.subprocess.run")
    def test_render_video_stages_exact_approved_v3_assets(self, mock_run, mock_validate, mock_preset):
        mock_preset.return_value = {
            "v3SceneAssets": [["fyf-v2/scene-a1.png"]],
            "v3MascotSegments": [0],
        }
        with tempfile.TemporaryDirectory() as job_dir:
            with open(os.path.join(job_dir, "voice.wav"), "w") as f:
                f.write("mock audio")
            with open(os.path.join(job_dir, "render_input.json"), "w") as f:
                json.dump({"test": "data"}, f)

            def side_effect(*args, **kwargs):
                cmd = args[0]
                props_path = cmd[cmd.index("--props") + 1]
                with open(props_path) as f:
                    staged_props = json.load(f)
                self.assertEqual(staged_props["v3SceneAssets"], [["fyf-v2/scene-a1.png"]])
                public_dir = cmd[cmd.index("--public-dir") + 1]
                self.assertTrue(os.path.isfile(os.path.join(public_dir, "fyf-v2", "scene-a1.png")))
                with open(os.path.join(job_dir, "video.mp4"), "w") as f:
                    f.write("mock video")

            mock_run.side_effect = side_effect
            self.assertEqual(render_video_remotion(job_dir), os.path.join(job_dir, "video.mp4"))

    def test_get_video_endpoint_success(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch("backend.main.JOBS_ROOT", __import__("pathlib").Path(temp_dir)):
                job_id = "1234abcd"
                job_dir = os.path.join(temp_dir, job_id)
                os.makedirs(job_dir, exist_ok=True)
                video_file = os.path.join(job_dir, "video.mp4")
                with open(video_file, "w") as f:
                    f.write("mock video")
                from backend.job_store import initialize_job_status, update_job_status
                path = __import__("pathlib").Path(job_dir)
                initialize_job_status(path, job_id)
                update_job_status(path, {"status": "completed", "qa_report": {"passed": True}, "final_visual_qa": {"passed": True}})

                response = client.get(f"/api/jobs/{job_id}/video")
                self.assertEqual(response.status_code, 200)

    def test_get_video_endpoint_rejects_unapproved_render(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch("backend.main.JOBS_ROOT", __import__("pathlib").Path(temp_dir)):
                job_id = "1234abcd"
                job_dir = __import__("pathlib").Path(temp_dir) / job_id
                job_dir.mkdir()
                (job_dir / "video.mp4").write_text("unapproved")
                from backend.job_store import initialize_job_status
                initialize_job_status(job_dir, job_id)
                self.assertEqual(client.get(f"/api/jobs/{job_id}/video").status_code, 404)

    def test_get_video_endpoint_invalid_id(self):
        response = client.get("/api/jobs/invalid_id/video")
        self.assertEqual(response.status_code, 400)

    def test_get_video_endpoint_not_found(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch("backend.main.JOBS_ROOT", __import__("pathlib").Path(temp_dir)):
                response = client.get("/api/jobs/1234abcd/video")
                self.assertEqual(response.status_code, 404)

    @patch("backend.main.run_pipeline", new_callable=AsyncMock)
    def test_generate_video_endpoint(self, mock_pipeline):
        with tempfile.TemporaryDirectory() as temp_dir, tempfile.TemporaryDirectory() as locks_dir:
            locked_script = {
                "title": "Test Video", "language": "my-MM",
                "segments": [{"id": "s1", "text": "Burmese", "visual_action": "explain", "scene_type": "whiteboard", "mascot_action": "explain", "emotion": "focused", "emphasis": []}],
            }
            lock_id = "abc12345"
            lock_path = __import__("pathlib").Path(locks_dir) / lock_id
            lock_path.mkdir()
            (lock_path / "script.json").write_text(json.dumps(locked_script))
            with patch("backend.main.JOBS_ROOT", __import__("pathlib").Path(temp_dir)), patch("backend.main.LOCKS_ROOT", __import__("pathlib").Path(locks_dir)):
                req_data = {
                    "lock_id": lock_id,
                    "voice_provider": "gemini"
                }

                response = client.post("/api/generate-video", json=req_data)
                self.assertEqual(response.status_code, 202)

                data = response.json()
                self.assertTrue(data["success"])
                job_id = data.get("job_id")
                self.assertIsNotNone(job_id)
                self.assertEqual(data.get("status_url"), f"/api/jobs/{job_id}/status")
                self.assertTrue(data.get("restart_resumable"))

                # Check status was initialized
                status_file = os.path.join(temp_dir, job_id, "status.json")
                self.assertTrue(os.path.exists(status_file))
                with open(status_file, "r") as f:
                    status_data = json.load(f)
                    self.assertEqual(status_data["status"], "queued")
                    self.assertEqual(status_data["voice_provider"], "gemini")
                    self.assertTrue(status_data["restart_resumable"])

                # Check script was saved
                script_file = os.path.join(temp_dir, job_id, "script.json")
                self.assertTrue(os.path.exists(script_file))
                with open(script_file, "r") as f:
                    script_data = json.load(f)
                    self.assertEqual(script_data["title"], "Test Video")

                # Check unknown provider rejected
                req_data["voice_provider"] = "unknown"
                response = client.post("/api/generate-video", json=req_data)
                self.assertEqual(response.status_code, 422)

    def test_get_status_endpoints(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch("backend.main.JOBS_ROOT", __import__("pathlib").Path(temp_dir)):
                job_id = "1234abcd"
                job_dir = os.path.join(temp_dir, job_id)
                os.makedirs(job_dir, exist_ok=True)

                status_file = os.path.join(job_dir, "status.json")
                with open(status_file, "w") as f:
                    json.dump({"status": "completed", "job_id": job_id, "created_at": "2024-01-01T00:00:00.000Z", "updated_at": None, "video_url": None, "error": None, "restart_resumable": False}, f)

                # Success
                response = client.get(f"/api/jobs/{job_id}/status")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json(), {"status": "completed", "job_id": job_id, "created_at": "2024-01-01T00:00:00.000Z", "updated_at": None, "video_url": None, "error": None, "qa_report": None, "final_visual_qa": None, "creative_qa": None, "voice_provider": None, "resume_count": 0, "attempt_count": 0, "restart_resumable": False, "visual_artifact_key": None, "visual_cache_state": None, "stage_timings": {}, "paired_source_job_id": None, "visual_progress": None})

                # Invalid ID
                response = client.get("/api/jobs/invalid_id/status")
                self.assertEqual(response.status_code, 400)

                # Missing
                response = client.get("/api/jobs/9999aaaa/status")
                self.assertEqual(response.status_code, 404)

                # Corrupt
                corrupt_id = "8888bbbb"
                corrupt_dir = os.path.join(temp_dir, corrupt_id)
                os.makedirs(corrupt_dir, exist_ok=True)
                with open(os.path.join(corrupt_dir, "status.json"), "w") as f:
                    f.write("invalid json")

                response = client.get(f"/api/jobs/{corrupt_id}/status")
                self.assertEqual(response.status_code, 500)

if __name__ == "__main__":
    unittest.main()
