import unittest
import tempfile
import asyncio
import os
import json
import hashlib
from unittest.mock import patch, MagicMock
from pathlib import Path
from backend.pipeline import run_pipeline

class TestPipeline(unittest.TestCase):
    def setUp(self):
        self.audio_master_patch = patch(
            "backend.pipeline.master_voice_audio",
            return_value={
                "changed": False,
                "version": 1,
                "after": {"peak_dbfs": -3.0, "full_scale_samples": 0},
            },
        )
        self.audio_master_mock = self.audio_master_patch.start()
        self.relation_patch = patch(
            "backend.pipeline.ensure_relationship_modes",
            side_effect=lambda script, job_dir: script,
        )
        self.relation_mock = self.relation_patch.start()
        self.visual_patch = patch(
            "backend.pipeline.generate_and_verify_visual_evidence",
            side_effect=lambda script, job_dir: script,
        )
        self.visual_mock = self.visual_patch.start()
        self.plan_patch = patch(
            "backend.pipeline.plan_visual_treatments",
            create=True,
            side_effect=lambda script, job_dir, policy=None: script,
        )
        self.plan_mock = self.plan_patch.start()
        self.final_visual_patch = patch(
            "backend.pipeline.verify_final_rendered_meaning",
            return_value={"passed": True, "segments": []},
        )
        self.final_visual_mock = self.final_visual_patch.start()

    def tearDown(self):
        self.audio_master_patch.stop()
        self.relation_patch.stop()
        self.visual_patch.stop()
        self.plan_patch.stop()
        self.final_visual_patch.stop()

    def test_render_checkpoint_v2_records_strategy_and_accepts_legacy_monolithic(self):
        from backend.job_store import initialize_job_status, write_json_atomically
        from backend.pipeline import (
            _render_checkpoint_is_usable,
            _write_render_checkpoint,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            job_dir = Path(temp_dir) / "1234abcd"
            job_dir.mkdir()
            initialize_job_status(job_dir, job_dir.name, "gemini")
            script = {"title": "Checkpoint", "language": "my-MM", "segments": [{"id": "s1", "text": "test"}]}
            audio = job_dir / "voice.wav"
            video = job_dir / "video.mp4"
            audio.write_bytes(b"audio")
            video.write_bytes(b"video")
            (job_dir / "render_input.json").write_text("{}")
            (job_dir / "mouth_cues.json").write_text("[]")
            _write_render_checkpoint(
                job_dir,
                script,
                audio,
                video,
                {"strategy": "monolithic", "total": 0, "rendered": 0, "cache_hits": 0},
            )
            checkpoint_path = job_dir / "render_checkpoint.json"
            checkpoint = json.loads(checkpoint_path.read_text())
            self.assertEqual(checkpoint["version"], 2)
            self.assertEqual(checkpoint["strategy"], "monolithic")
            self.assertTrue(_render_checkpoint_is_usable(job_dir, script, audio))

            legacy = dict(checkpoint)
            legacy.pop("version")
            legacy.pop("strategy")
            write_json_atomically(checkpoint_path, legacy)
            self.assertTrue(_render_checkpoint_is_usable(job_dir, script, audio))

    def test_segmented_render_checkpoint_does_not_bypass_current_asset_validation(self):
        from backend.job_store import initialize_job_status, write_json_atomically
        from backend.pipeline import _render_checkpoint_is_usable, _write_render_checkpoint

        with tempfile.TemporaryDirectory() as temp_dir:
            job_dir = Path(temp_dir) / "1234abcd"
            job_dir.mkdir()
            initialize_job_status(job_dir, job_dir.name, "gemini")
            asset_dir = job_dir / "visuals"
            asset_dir.mkdir()
            asset = asset_dir / "s1.png"
            asset.write_bytes(b"asset-v1")
            script = {
                "title": "Asset checkpoint",
                "language": "my-MM",
                "segments": [
                    {
                        "id": "s1",
                        "text": "test",
                        "visual": {"asset_path": "visuals/s1.png"},
                    }
                ],
            }
            audio = job_dir / "voice.wav"
            video = job_dir / "video.mp4"
            audio.write_bytes(b"audio")
            video.write_bytes(b"video")
            (job_dir / "render_input.json").write_text(
                json.dumps({"audioSrc": "voice.wav", "segments": script["segments"]})
            )
            (job_dir / "mouth_cues.json").write_text("[]")
            _write_render_checkpoint(
                job_dir,
                script,
                audio,
                video,
                {"strategy": "segmented", "manifest_fingerprint": "a" * 64},
            )

            checkpoint = json.loads((job_dir / "render_checkpoint.json").read_text())
            self.assertTrue(checkpoint["complete"])
            self.assertEqual(checkpoint["video_bytes"], video.stat().st_size)
            self.assertEqual(
                checkpoint["video_sha256"], hashlib.sha256(video.read_bytes()).hexdigest()
            )
            asset.write_bytes(b"asset-v2")
            self.assertFalse(_render_checkpoint_is_usable(job_dir, script, audio))

            checkpoint_path = job_dir / "render_checkpoint.json"
            checkpoint = json.loads(checkpoint_path.read_text())
            checkpoint["strategy"] = "monolithic"
            write_json_atomically(checkpoint_path, checkpoint)
            self.assertTrue(_render_checkpoint_is_usable(job_dir, script, audio))

            legacy = dict(checkpoint)
            legacy.pop("version", None)
            legacy.pop("strategy", None)
            write_json_atomically(checkpoint_path, legacy)
            self.assertTrue(_render_checkpoint_is_usable(job_dir, script, audio))

    def test_segment_strategy_is_selected_and_render_progress_is_persisted(self):
        from backend.job_store import initialize_job_status, read_job_status
        from backend.pipeline import _render_with_configured_strategy
        from backend.segment_render_cache import RenderAssemblyReport

        with tempfile.TemporaryDirectory() as temp_dir:
            job_dir = Path(temp_dir) / "1234abcd"
            job_dir.mkdir()
            initialize_job_status(job_dir, job_dir.name, "gemini")
            output = job_dir / "video.mp4"
            output.write_bytes(b"video")
            report = RenderAssemblyReport(output, 3, 1, 2, "a" * 64)
            with patch.dict(os.environ, {"FYF_SEGMENT_RENDER_ENABLED": "1"}), patch(
                "backend.pipeline.render_segments_and_assemble", return_value=report
            ) as segmented, patch("backend.pipeline.render_video_remotion") as monolithic:
                result, progress = _render_with_configured_strategy(job_dir)

            segmented.assert_called_once_with(str(job_dir))
            monolithic.assert_not_called()
            self.assertEqual(result, output)
            self.assertEqual(progress, {
                "strategy": "segmented",
                "total": 3,
                "rendered": 1,
                "cache_hits": 2,
                "manifest_fingerprint": "a" * 64,
            })
            self.assertEqual(read_job_status(job_dir)["render_progress"], progress)

    def test_monolithic_strategy_is_selected_when_segment_flag_is_disabled(self):
        from backend.job_store import initialize_job_status, read_job_status
        from backend.pipeline import _render_with_configured_strategy

        with tempfile.TemporaryDirectory() as temp_dir:
            job_dir = Path(temp_dir) / "1234abcd"
            job_dir.mkdir()
            initialize_job_status(job_dir, job_dir.name, "gemini")
            output = job_dir / "video.mp4"
            output.write_bytes(b"video")
            with patch.dict(os.environ, {"FYF_SEGMENT_RENDER_ENABLED": "0"}), patch(
                "backend.pipeline.render_segments_and_assemble"
            ) as segmented, patch(
                "backend.pipeline.render_video_remotion", return_value=str(output)
            ) as monolithic:
                result, progress = _render_with_configured_strategy(job_dir)

            monolithic.assert_called_once_with(str(job_dir))
            segmented.assert_not_called()
            self.assertEqual(result, output)
            self.assertEqual(progress["strategy"], "monolithic")
            self.assertEqual(read_job_status(job_dir)["render_progress"], progress)

    def test_segment_strategy_is_default_when_segment_flag_is_unset(self):
        from backend.job_store import initialize_job_status, read_job_status
        from backend.pipeline import _render_with_configured_strategy
        from backend.segment_render_cache import RenderAssemblyReport

        with tempfile.TemporaryDirectory() as temp_dir:
            job_dir = Path(temp_dir) / "1234abcd"
            job_dir.mkdir()
            initialize_job_status(job_dir, job_dir.name, "gemini")
            output = job_dir / "video.mp4"
            output.write_bytes(b"video")
            with patch.dict(os.environ, {}, clear=True), patch(
                "backend.pipeline.render_segments_and_assemble",
                return_value=RenderAssemblyReport(output, 3, 3, 0, "b" * 64),
            ) as segmented, patch(
                "backend.pipeline.render_video_remotion", return_value=str(output)
            ) as monolithic:
                result, progress = _render_with_configured_strategy(job_dir)

            segmented.assert_not_called()
            monolithic.assert_called_once_with(str(job_dir))
            self.assertEqual(result, output)
            self.assertEqual(progress["strategy"], "monolithic")
            self.assertEqual(read_job_status(job_dir)["render_progress"], progress)

    def test_segment_strategy_failure_falls_back_to_monolithic_once_and_records_reason(self):
        from backend.job_store import initialize_job_status, read_job_status
        from backend.pipeline import _render_with_configured_strategy

        with tempfile.TemporaryDirectory() as temp_dir:
            job_dir = Path(temp_dir) / "1234abcd"
            job_dir.mkdir()
            initialize_job_status(job_dir, job_dir.name, "gemini")
            output = job_dir / "video.mp4"
            output.write_bytes(b"video")
            with patch.dict(os.environ, {"FYF_SEGMENT_RENDER_ENABLED": "1"}), patch(
                "backend.pipeline.render_segments_and_assemble",
                side_effect=RuntimeError("bad segment assembly"),
            ) as segmented, patch(
                "backend.pipeline.render_video_remotion", return_value=str(output)
            ) as monolithic:
                result, progress = _render_with_configured_strategy(job_dir)

            segmented.assert_called_once_with(str(job_dir))
            monolithic.assert_called_once_with(str(job_dir))
            self.assertEqual(result, output)
            self.assertEqual(progress["strategy"], "monolithic-fallback")
            self.assertIn("bad segment assembly", progress["fallback_reason"])
            self.assertEqual(read_job_status(job_dir)["render_progress"], progress)

    def test_semantic_repair_dispatch_reports_one_render_and_two_cache_hits(self):
        from backend.job_store import initialize_job_status
        from backend.pipeline import _render_with_configured_strategy
        from backend.segment_render_cache import RenderAssemblyReport

        with tempfile.TemporaryDirectory() as temp_dir:
            job_dir = Path(temp_dir) / "1234abcd"
            job_dir.mkdir()
            initialize_job_status(job_dir, job_dir.name, "gemini")
            output = job_dir / "video.mp4"
            output.write_bytes(b"video")
            reports = [
                RenderAssemblyReport(output, 3, 3, 0, "a" * 64),
                RenderAssemblyReport(output, 3, 1, 2, "b" * 64),
            ]
            with patch.dict(os.environ, {"FYF_SEGMENT_RENDER_ENABLED": "1"}), patch(
                "backend.pipeline.render_segments_and_assemble", side_effect=reports
            ) as segmented:
                _render_with_configured_strategy(job_dir)
                _output, repair_progress = _render_with_configured_strategy(job_dir)

            self.assertEqual(repair_progress["rendered"], 1)
            self.assertEqual(repair_progress["cache_hits"], 2)
            self.assertEqual(segmented.call_count, 2)

    def test_creative_repair_dispatch_reports_one_render_and_two_cache_hits(self):
        from backend.job_store import initialize_job_status
        from backend.pipeline import _render_with_configured_strategy
        from backend.segment_render_cache import RenderAssemblyReport

        with tempfile.TemporaryDirectory() as temp_dir:
            job_dir = Path(temp_dir) / "1234abcd"
            job_dir.mkdir()
            initialize_job_status(job_dir, job_dir.name, "gemini")
            output = job_dir / "video.mp4"
            output.write_bytes(b"video")
            reports = [
                RenderAssemblyReport(output, 3, 3, 0, "c" * 64),
                RenderAssemblyReport(output, 3, 1, 2, "d" * 64),
            ]
            with patch.dict(os.environ, {"FYF_SEGMENT_RENDER_ENABLED": "1"}), patch(
                "backend.pipeline.render_segments_and_assemble", side_effect=reports
            ):
                _render_with_configured_strategy(job_dir)
                _output, repair_progress = _render_with_configured_strategy(job_dir)

            self.assertEqual(repair_progress["rendered"], 1)
            self.assertEqual(repair_progress["cache_hits"], 2)

    def test_reused_voice_is_mastered_and_checkpoint_refreshed_before_render(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            job_id = "1234abcd"
            job_dir = root / job_id
            job_dir.mkdir()
            script = {"title": "Test", "language": "my-MM", "segments": [{"id": "1", "text": "test"}]}
            from backend.job_store import initialize_job_status
            from backend.pipeline import _voice_fingerprint
            initialize_job_status(job_dir, job_id, "gemini")
            audio_path = job_dir / "voice.wav"
            audio_path.write_text("old-audio")
            (job_dir / "voice_checkpoint.json").write_text(json.dumps({
                "provider": "gemini",
                "fingerprint": _voice_fingerprint(script, "gemini"),
                "bytes": audio_path.stat().st_size,
            }))

            def master(path):
                Path(path).write_text("mastered-audio")
                return {"changed": True, "version": 1, "after": {"peak_dbfs": -1.5, "full_scale_samples": 0}}

            def render(job_dir_value):
                output = Path(job_dir_value) / "video.mp4"
                output.write_text("fresh-video")
                return str(output)

            self.audio_master_mock.side_effect = master
            with patch("backend.pipeline.generate_voice") as voice, patch(
                "backend.pipeline.build_render_input",
                return_value={"mouthCues": [], "audioSrc": "voice.wav", "script": script},
            ), patch("backend.pipeline.render_video_remotion", side_effect=render) as render_mock, patch(
                "backend.pipeline.qa_job_directory",
                return_value={"passed": True, "checks": [], "failure_codes": [], "metrics": {}},
            ), patch("backend.pipeline.audit_creative_quality", return_value={"passed": True}):
                asyncio.run(run_pipeline(job_id, script, "gemini", root))

            voice.assert_not_called()
            render_mock.assert_called_once()
            checkpoint = json.loads((job_dir / "voice_checkpoint.json").read_text())
            self.assertEqual(checkpoint["bytes"], audio_path.stat().st_size)
            self.assertEqual(checkpoint["audio_master_version"], 1)
            self.assertEqual(checkpoint["audio_peak_dbfs"], -1.5)

    @patch("backend.pipeline.qa_job_directory")
    @patch("backend.pipeline.render_video_remotion")
    @patch("backend.pipeline.generate_voice")
    def test_run_pipeline_success(self, mock_generate_voice, mock_render_video, mock_qa):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)

            script_dict = {
                "title": "Test", "language": "my-MM",
                "segments": [{"id": "1", "text": "test"}]
            }

            for provider in ["gemini"]:
                with self.subTest(provider=provider):
                    mock_generate_voice.reset_mock()
                    mock_render_video.reset_mock()
                    mock_qa.reset_mock()
                    mock_qa.return_value = {"passed": True, "checks": [], "failure_codes": [], "metrics": {}}

                    job_id = "5678abcd"
                    job_dir = temp_path / job_id
                    job_dir.mkdir()

                    # Initialize status
                    from backend.job_store import initialize_job_status
                    initialize_job_status(job_dir, job_id, "gemini")

                    # Setup mocks to create expected output files
                    def mock_voice(*args, **kwargs):
                        with open(kwargs["output_path"], "w") as f:
                            f.write("audio")
                    mock_generate_voice.side_effect = mock_voice

                    def mock_render(*args, **kwargs):
                        out_path = job_dir / "video.mp4"
                        with open(out_path, "w") as f:
                            f.write("video")
                        return str(out_path)
                    mock_render_video.side_effect = mock_render

                    # Mock mouth cues to avoid dependencies on real cue generator
                    with patch("backend.pipeline.build_render_input") as mock_build_cues:
                        mock_build_cues.return_value = {"mouthCues": [], "audioSrc": "voice.wav", "script": script_dict}

                        asyncio.run(run_pipeline(job_id, script_dict, "gemini", temp_path))

                        mock_generate_voice.assert_called_with(
                            script_json=script_dict,
                            provider="gemini",
                            output_path=str(job_dir / "voice.wav")
                        )

                        # Verify status is completed
                        from backend.job_store import read_job_status
                        status = read_job_status(job_dir)
                        self.assertEqual(status["status"], "completed")
                        self.assertEqual(status["video_url"], f"/api/jobs/{job_id}/video")
                        self.assertTrue(status["qa_report"]["passed"])
                        self.assertTrue(status["final_visual_qa"]["passed"])
                        self.assertEqual(status["qa_report"]["attempts"], 1)

                        # Verify files were created
                        self.assertTrue((job_dir / "mouth_cues.json").exists())
                        self.assertTrue((job_dir / "render_input.json").exists())

                        # Verify render_input content
                        with open(job_dir / "render_input.json") as f:
                            render_input = json.load(f)
                            self.assertEqual(render_input["audioSrc"], "voice.wav")
                            self.assertEqual(render_input["mouthCues"], [])
                            self.assertEqual(render_input["script"]["title"], "Test")

    def test_retry_with_materialized_script_reuses_persisted_approved_artifact_key(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            from backend.job_store import initialize_job_status, read_job_status
            from backend.pipeline import _prepare_visual_artifact

            root = Path(temp_dir)
            job_id = "1234abcd"
            job_dir = root / "jobs" / job_id
            artifacts = root / "visual-artifacts"
            job_dir.mkdir(parents=True)
            initialize_job_status(job_dir, job_id, "gemini")
            original = {"title": "Locked", "language": "my-MM", "segments": [{"id": "s1", "text": "same"}]}

            def enrich(script, _artifact_dir, policy=None):
                enriched = json.loads(json.dumps(script))
                enriched["segments"][0]["runtime_visual_metadata"] = {"approved": True}
                return enriched

            self.plan_mock.side_effect = enrich
            produced = _prepare_visual_artifact(job_id, job_dir, original, artifacts)
            original_key = read_job_status(job_dir)["visual_artifact_key"]
            self.assertNotEqual(produced, original)

            reused = _prepare_visual_artifact(job_id, job_dir, produced, artifacts)

            self.assertEqual(read_job_status(job_dir)["visual_artifact_key"], original_key)
            self.assertEqual(reused, produced)
            self.assertEqual(self.plan_mock.call_count, 1)

    def test_visual_producer_migrates_job_local_director_checkpoint(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            job_id = "1234abcd"
            job_dir = root / job_id
            job_dir.mkdir()
            from backend.job_store import initialize_job_status
            initialize_job_status(job_dir, job_id, "gemini")
            local_checkpoint = {"input_fingerprint": "legacy", "complete": False, "script": {}}
            (job_dir / "director_treatment_checkpoint.json").write_text(json.dumps(local_checkpoint))
            observed = {}

            def plan(script, artifact_dir, policy=None):
                checkpoint = Path(artifact_dir) / "director_treatment_checkpoint.json"
                observed["checkpoint"] = json.loads(checkpoint.read_text())
                return script

            with patch("backend.pipeline.plan_visual_treatments", side_effect=plan):
                from backend.pipeline import _prepare_visual_artifact
                _prepare_visual_artifact(job_id, job_dir, {"title": "T", "language": "my-MM", "segments": []}, root / ".visual-artifacts")

            self.assertEqual(observed["checkpoint"], local_checkpoint)

    def test_visual_producer_migrates_richest_matching_job_checkpoint(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            producer_id = "1234abcd"
            sibling_id = "5678abcd"
            producer_dir = root / producer_id
            sibling_dir = root / sibling_id
            producer_dir.mkdir()
            sibling_dir.mkdir()
            from backend.job_store import initialize_job_status
            initialize_job_status(producer_dir, producer_id, "gemini")
            initialize_job_status(sibling_dir, sibling_id, "gemini")

            def checkpoint(completed):
                return {
                    "input_fingerprint": "same-locked-script",
                    "complete": False,
                    "completed_shot_ids": [f"s{i}/shot-{i}" for i in range(completed)],
                    "script": {},
                }

            (producer_dir / "director_treatment_checkpoint.json").write_text(
                json.dumps(checkpoint(5))
            )
            (sibling_dir / "director_treatment_checkpoint.json").write_text(
                json.dumps(checkpoint(22))
            )
            observed = {}

            def plan(script, artifact_dir, policy=None):
                shared = Path(artifact_dir) / "director_treatment_checkpoint.json"
                observed["checkpoint"] = json.loads(shared.read_text())
                return script

            with patch("backend.pipeline.plan_visual_treatments", side_effect=plan):
                from backend.pipeline import _prepare_visual_artifact
                _prepare_visual_artifact(
                    producer_id,
                    producer_dir,
                    {"title": "T", "language": "my-MM", "segments": []},
                    root / ".visual-artifacts",
                )

            self.assertEqual(len(observed["checkpoint"]["completed_shot_ids"]), 22)

    def test_visual_producer_counts_treated_shots_in_legacy_checkpoint(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            producer_id = "1234abcd"
            sibling_id = "5678abcd"
            producer_dir = root / producer_id
            sibling_dir = root / sibling_id
            producer_dir.mkdir()
            sibling_dir.mkdir()
            from backend.job_store import initialize_job_status
            initialize_job_status(producer_dir, producer_id, "gemini")
            initialize_job_status(sibling_dir, sibling_id, "gemini")

            def legacy_checkpoint(completed):
                shots = [
                    {"shot_id": f"shot-{index}", "treatment": "object_action"}
                    for index in range(completed)
                ] + [
                    {"shot_id": f"shot-{index}"}
                    for index in range(completed, 27)
                ]
                return {
                    "input_fingerprint": "same-legacy-script",
                    "script": {
                        "segments": [{"id": "s1", "visual": {"evidence_shots": shots}}],
                    },
                }

            (producer_dir / "director_treatment_checkpoint.json").write_text(
                json.dumps(legacy_checkpoint(6))
            )
            (sibling_dir / "director_treatment_checkpoint.json").write_text(
                json.dumps(legacy_checkpoint(22))
            )
            artifact_dir = root / ".visual-artifacts" / ("a" * 64)
            artifact_dir.mkdir(parents=True)
            from backend.pipeline import _migrate_best_director_checkpoint
            with patch.object(Path, "iterdir", return_value=iter([producer_dir, sibling_dir])):
                _migrate_best_director_checkpoint(producer_dir, artifact_dir)

            selected = json.loads(
                (artifact_dir / "director_treatment_checkpoint.json").read_text()
            )
            selected_shots = selected["script"]["segments"][0]["visual"]["evidence_shots"]
            self.assertEqual(sum(bool(shot.get("treatment")) for shot in selected_shots), 22)

    def test_completed_pipeline_records_stage_timings(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            job_id = "1234abcd"
            job_dir = root / job_id
            job_dir.mkdir()
            script = {
                "title": "Timing",
                "language": "my-MM",
                "segments": [{"id": "s1", "text": "dynamic narration"}],
            }
            from backend.job_store import initialize_job_status, read_job_status
            initialize_job_status(job_dir, job_id, "gemini")

            def voice(*_args, **kwargs):
                Path(kwargs["output_path"]).write_text("audio")

            def render(job_dir_value):
                output = Path(job_dir_value) / "video.mp4"
                output.write_text("video")
                return str(output)

            with patch("backend.pipeline.generate_voice", side_effect=voice), patch(
                "backend.pipeline.build_render_input",
                return_value={"mouthCues": [], "audioSrc": "voice.wav", "script": script},
            ), patch("backend.pipeline.render_video_remotion", side_effect=render), patch(
                "backend.pipeline.qa_job_directory",
                return_value={"passed": True, "checks": [], "failure_codes": [], "metrics": {}},
            ), patch("backend.pipeline.audit_creative_quality", return_value={"passed": True}):
                asyncio.run(run_pipeline(job_id, script, "gemini", root))

            timings = read_job_status(job_dir)["stage_timings"]
            self.assertEqual(set(timings), {"visuals", "voice", "render", "qa"})
            self.assertTrue(all(isinstance(value, float) and value >= 0 for value in timings.values()))

    @patch("backend.pipeline.qa_job_directory", return_value={"passed": True, "checks": [], "failure_codes": [], "metrics": {}})
    @patch("backend.pipeline.render_video_remotion")
    @patch("backend.pipeline.generate_voice")
    def test_run_pipeline_reuses_voice_render_contract_and_video_checkpoints(
        self, mock_generate_voice, mock_render_video, _mock_qa
    ):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            job_id = "1234abcd"
            job_dir = root / job_id
            job_dir.mkdir()
            script = {"title": "Test", "language": "my-MM", "segments": [{"id": "1", "text": "test"}]}
            from backend.job_store import initialize_job_status
            from backend.pipeline import _voice_fingerprint, _write_render_checkpoint
            initialize_job_status(job_dir, job_id, "gemini")
            (job_dir / "voice.wav").write_text("audio")
            (job_dir / "voice_checkpoint.json").write_text(json.dumps({
                "provider": "gemini",
                "fingerprint": _voice_fingerprint(script, "gemini"),
                "bytes": (job_dir / "voice.wav").stat().st_size,
            }))
            (job_dir / "mouth_cues.json").write_text("[]")
            (job_dir / "render_input.json").write_text(json.dumps({"audioSrc": "voice.wav"}))
            (job_dir / "video.mp4").write_text("video")
            _write_render_checkpoint(
                job_dir,
                script,
                job_dir / "voice.wav",
                job_dir / "video.mp4",
                {"strategy": "monolithic", "total": 0, "rendered": 0, "cache_hits": 0},
            )

            with patch("backend.pipeline.build_render_input") as mock_build:
                asyncio.run(run_pipeline(job_id, script, "gemini", root))

            mock_generate_voice.assert_not_called()
            mock_build.assert_not_called()
            mock_render_video.assert_not_called()

    def test_run_pipeline_rerenders_existing_video_when_render_fingerprint_changed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            from backend.job_store import initialize_job_status, write_json_atomically

            root = Path(temp_dir)
            job_id = "1234abcd"
            job_dir = root / job_id
            job_dir.mkdir()
            script = {
                "title": "Changed visual", "language": "my-MM",
                "segments": [{"id": "S1", "text": "တူညီသော စာသား"}],
            }
            initialize_job_status(job_dir, job_id, "gemini")
            (job_dir / "video.mp4").write_text("stale-video")
            write_json_atomically(job_dir / "render_checkpoint.json", {"fingerprint": "stale"})

            def voice(*_args, **kwargs):
                Path(kwargs["output_path"]).write_text("voice")

            def render(job_dir_value):
                output = Path(job_dir_value) / "video.mp4"
                output.write_text("fresh-video")
                return str(output)

            with patch("backend.pipeline.generate_voice", side_effect=voice), patch(
                "backend.pipeline.build_render_input",
                return_value={"mouthCues": [], "audioSrc": "voice.wav", "script": script},
            ), patch("backend.pipeline.render_video_remotion", side_effect=render) as mock_render, patch(
                "backend.pipeline.qa_job_directory",
                return_value={"passed": True, "checks": [], "failure_codes": [], "metrics": {}},
            ), patch("backend.pipeline.audit_creative_quality", return_value={"passed": True}):
                asyncio.run(run_pipeline(job_id, script, "gemini", root))

            mock_render.assert_called_once_with(str(job_dir))
            self.assertEqual((job_dir / "video.mp4").read_text(), "fresh-video")

    def test_run_pipeline_rerenders_when_matching_checkpoint_was_not_sealed_after_render(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            from backend.job_store import initialize_job_status, write_json_atomically
            from backend.pipeline import _render_fingerprint

            root = Path(temp_dir)
            job_id = "1234abcd"
            job_dir = root / job_id
            job_dir.mkdir()
            script = {
                "title": "Interrupted render", "language": "my-MM",
                "segments": [{"id": "S1", "text": "တူညီသော စာသား"}],
            }
            initialize_job_status(job_dir, job_id, "gemini")
            (job_dir / "voice.wav").write_text("voice")
            (job_dir / "video.mp4").write_text("old-video")
            write_json_atomically(job_dir / "render_checkpoint.json", {
                "fingerprint": _render_fingerprint(script, job_dir / "voice.wav"),
            })

            def render(job_dir_value):
                output = Path(job_dir_value) / "video.mp4"
                output.write_text("fresh-video")
                return str(output)

            with patch("backend.pipeline.generate_voice"), patch(
                "backend.pipeline._voice_checkpoint_is_usable", return_value=True
            ), patch(
                "backend.pipeline.build_render_input",
                return_value={"mouthCues": [], "audioSrc": "voice.wav", "script": script},
            ), patch("backend.pipeline.render_video_remotion", side_effect=render) as mock_render, patch(
                "backend.pipeline.qa_job_directory",
                return_value={"passed": True, "checks": [], "failure_codes": [], "metrics": {}},
            ), patch("backend.pipeline.audit_creative_quality", return_value={"passed": True}):
                asyncio.run(run_pipeline(job_id, script, "gemini", root))

            mock_render.assert_called_once_with(str(job_dir))
            checkpoint = json.loads((job_dir / "render_checkpoint.json").read_text())
            self.assertTrue(checkpoint["complete"])
            self.assertEqual(checkpoint["video_bytes"], len("fresh-video"))

    def test_retry_after_final_qa_repairs_failed_scenes_and_reuses_narration_voice(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            job_id = "1234abcd"
            job_dir = root / job_id
            job_dir.mkdir()
            from backend.job_store import initialize_job_status, read_job_status, write_json_atomically
            script = {"title": "Test", "language": "my-MM", "segments": [{"id": "s1", "text": "တူညီတဲ့အသံ"}]}
            repaired = {"title": "Test", "language": "my-MM", "segments": [{"id": "s1", "text": "တူညီတဲ့အသံ", "visual": {"kind": "generic"}}]}
            initialize_job_status(job_dir, job_id, "gemini")
            write_json_atomically(job_dir / "final_visual_qa.json", {
                "passed": False, "segments": [{"segment_id": "s1", "passed": False, "issues": ["unclear"]}],
            })
            (job_dir / "voice.wav").write_text("same-audio")
            write_json_atomically(job_dir / "voice_checkpoint.json", {
                "provider": "gemini", "fingerprint": hashlib.sha256(json.dumps(
                    {"provider": "gemini", "script": script}, ensure_ascii=False,
                    sort_keys=True, separators=(",", ":"),
                ).encode("utf-8")).hexdigest(),
                "bytes": (job_dir / "voice.wav").stat().st_size,
            })
            (job_dir / "rejected-video.mp4").write_text("old")

            def render(_job_dir):
                (job_dir / "video.mp4").write_text("new")
                return str(job_dir / "video.mp4")

            with patch("backend.pipeline.repair_final_visual_failures", return_value=repaired) as repair, patch(
                "backend.pipeline.generate_and_verify_visual_evidence", side_effect=lambda value, _dir: value
            ), patch("backend.pipeline.generate_voice") as voice, patch(
                "backend.pipeline.build_render_input", return_value={"mouthCues": [], "audioSrc": "voice.wav", "script": repaired}
            ) as build, patch("backend.pipeline.render_video_remotion", side_effect=render), patch(
                "backend.pipeline.qa_job_directory", return_value={"passed": True, "checks": [], "failure_codes": [], "metrics": {}}
            ), patch("backend.pipeline.verify_final_rendered_meaning", return_value={"passed": True, "segments": []}):
                asyncio.run(run_pipeline(job_id, script, "gemini", root))

            repair.assert_called_once()
            self.plan_mock.assert_not_called()
            self.visual_mock.assert_not_called()
            voice.assert_not_called()
            build.assert_called_once()
            self.assertEqual(read_job_status(job_dir)["status"], "completed")

    @patch("backend.pipeline.qa_job_directory")
    @patch("backend.pipeline.render_video_remotion")
    @patch("backend.pipeline.generate_voice")
    def test_run_pipeline_retries_render_once_for_retryable_qa_failure(self, mock_generate_voice, mock_render_video, mock_qa):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            job_id = "1234abcd"
            job_dir = temp_path / job_id
            job_dir.mkdir()
            from backend.job_store import initialize_job_status, read_job_status
            initialize_job_status(job_dir, job_id)

            def mock_voice(*args, **kwargs):
                Path(kwargs["output_path"]).write_text("audio")

            def mock_render(*args, **kwargs):
                output = job_dir / "video.mp4"
                output.write_text("video")
                return str(output)

            mock_generate_voice.side_effect = mock_voice
            mock_render_video.side_effect = mock_render
            mock_qa.side_effect = [
                {"passed": False, "checks": [], "failure_codes": ["VIDEO_TOO_SHORT"], "metrics": {}},
                {"passed": True, "checks": [], "failure_codes": [], "metrics": {}},
            ]
            render_input = {"mouthCues": [], "audioSrc": "voice.wav", "script": {}}
            with patch("backend.pipeline.build_render_input", return_value=render_input):
                asyncio.run(run_pipeline(job_id, {}, "gemini", temp_path))

            status = read_job_status(job_dir)
            self.assertEqual(status["status"], "completed")
            self.assertEqual(status["qa_report"]["attempts"], 2)
            self.assertEqual(mock_render_video.call_count, 2)

    @patch("backend.pipeline.qa_job_directory")
    @patch("backend.pipeline.render_video_remotion")
    @patch("backend.pipeline.generate_voice")
    def test_run_pipeline_exposes_nonretryable_qa_report(self, mock_generate_voice, mock_render_video, mock_qa):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            job_id = "1234abcd"
            job_dir = temp_path / job_id
            job_dir.mkdir()
            from backend.job_store import initialize_job_status, read_job_status
            initialize_job_status(job_dir, job_id)

            mock_generate_voice.side_effect = lambda *args, **kwargs: Path(kwargs["output_path"]).write_text("audio")
            mock_render_video.side_effect = lambda *args, **kwargs: str((job_dir / "video.mp4"))
            (job_dir / "video.mp4").write_text("video")
            mock_qa.return_value = {"passed": False, "checks": [], "failure_codes": ["SEGMENTS_MISMATCH"], "metrics": {}}
            render_input = {"mouthCues": [], "audioSrc": "voice.wav", "script": {}}
            with patch("backend.pipeline.build_render_input", return_value=render_input):
                asyncio.run(run_pipeline(job_id, {}, "gemini", temp_path))

            status = read_job_status(job_dir)
            self.assertEqual(status["status"], "failed")
            self.assertEqual(status["qa_report"]["failure_codes"], ["SEGMENTS_MISMATCH"])
            self.assertIn("SEGMENTS_MISMATCH", status["error"])

    @patch("backend.pipeline.generate_voice")
    def test_run_pipeline_voice_failure(self, mock_generate_voice):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            job_id = "1234abcd"
            job_dir = temp_path / job_id
            job_dir.mkdir()

            from backend.job_store import initialize_job_status, read_job_status
            initialize_job_status(job_dir, job_id, "gemini")

            # Setup mock to NOT create the audio file
            mock_generate_voice.side_effect = lambda *args, **kwargs: None

            asyncio.run(run_pipeline(job_id, {}, "gemini", temp_path))

            status = read_job_status(job_dir)
            self.assertEqual(status["status"], "failed")
            self.assertTrue(status["restart_resumable"])
            self.assertIsNotNone(status["error"])

    @patch("backend.pipeline.qa_job_directory", return_value={"passed": True, "checks": [], "failure_codes": [], "metrics": {}})
    @patch("backend.pipeline.render_video_remotion")
    @patch("backend.pipeline.generate_voice")
    def test_failed_final_visual_qa_is_persisted_and_video_is_quarantined(self, mock_generate_voice, mock_render, _mock_qa):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            job_id = "1234abcd"
            job_dir = root / job_id
            job_dir.mkdir()
            from backend.job_store import initialize_job_status, read_job_status
            initialize_job_status(job_dir, job_id)
            mock_generate_voice.side_effect = lambda *args, **kwargs: Path(kwargs["output_path"]).write_text("audio")
            def render(*_args, **_kwargs):
                (job_dir / "video.mp4").write_text("video")
                return str(job_dir / "video.mp4")
            mock_render.side_effect = render
            failed_report = {"passed": False, "segments": [{"segment_id": "s1", "passed": False, "issues": ["wrong count"]}]}
            with patch("backend.pipeline.build_render_input", return_value={"mouthCues": [], "audioSrc": "voice.wav", "script": {}}), patch("backend.pipeline.verify_final_rendered_meaning", return_value=failed_report), patch("backend.pipeline.MAX_FINAL_VISUAL_ATTEMPTS", 1):
                asyncio.run(run_pipeline(job_id, {}, "gemini", root))
            status = read_job_status(job_dir)
            self.assertEqual(status["status"], "failed")
            self.assertEqual(status["final_visual_qa"], failed_report)
            self.assertTrue(status["restart_resumable"])
            self.assertFalse((job_dir / "video.mp4").exists())
            self.assertTrue((job_dir / "rejected-video.attempt-1.mp4").exists())

    @patch("backend.pipeline.plan_visual_treatments")
    @patch("backend.pipeline.generate_and_verify_visual_evidence")
    @patch("backend.pipeline.generate_voice")
    def test_visual_treatment_plan_precedes_evidence_and_reuses_narration_voice(
        self, mock_generate_voice, mock_generate_evidence, mock_plan
    ):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            job_id = "1234abcd"
            job_dir = root / job_id
            job_dir.mkdir()
            from backend.job_store import initialize_job_status, read_job_status
            from backend.pipeline import _voice_fingerprint

            script = {"title": "Test", "language": "my-MM", "segments": [{"id": "s1", "text": "same"}]}
            initialize_job_status(job_dir, job_id, "gemini")
            (job_dir / "voice.wav").write_text("audio")
            (job_dir / "voice_checkpoint.json").write_text(json.dumps({
                "provider": "gemini",
                "fingerprint": _voice_fingerprint(script, "gemini"),
                "bytes": (job_dir / "voice.wav").stat().st_size,
            }))
            mock_plan.return_value = script
            mock_generate_evidence.return_value = script
            calls = []
            mock_plan.side_effect = lambda *args: calls.append("plan") or script
            mock_generate_evidence.side_effect = lambda *args: calls.append("evidence") or script

            def render_video(_job_dir):
                video_path = job_dir / "video.mp4"
                video_path.write_text("video")
                return str(video_path)

            with patch("backend.pipeline.build_render_input", return_value={"mouthCues": [], "audioSrc": "voice.wav", "script": script}), patch(
                "backend.pipeline.render_video_remotion", side_effect=render_video
            ), patch("backend.pipeline.qa_job_directory", return_value={"passed": True, "checks": [], "failure_codes": [], "metrics": {}}):
                asyncio.run(run_pipeline(job_id, script, "gemini", root))

            self.assertLess(calls.index("plan"), calls.index("evidence"))
            mock_generate_voice.assert_not_called()
            self.assertEqual(read_job_status(job_dir)["status"], "completed")

    @patch("backend.pipeline.repair_creative_failures", return_value={})
    @patch("backend.pipeline.audit_creative_quality")
    def test_failed_creative_qa_needs_human_review_after_bounded_retries(self, mock_audit, mock_repair):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            job_id = "1234abcd"
            job_dir = root / job_id
            job_dir.mkdir()
            from backend.job_store import initialize_job_status, read_job_status
            initialize_job_status(job_dir, job_id, "gemini")
            failed_report = {"passed": False, "failure_codes": ["CREATIVE_QUALITY_FAILED"], "issues": ["repetitive"]}
            mock_audit.return_value = failed_report

            with patch("backend.pipeline.plan_visual_treatments", return_value={}), patch(
                "backend.pipeline.generate_and_verify_visual_evidence", return_value={}
            ), patch("backend.pipeline.generate_voice", side_effect=lambda *args, **kwargs: Path(kwargs["output_path"]).write_text("audio")), patch(
                "backend.pipeline.build_render_input", return_value={"mouthCues": [], "audioSrc": "voice.wav", "script": {}}
            ), patch("backend.pipeline.render_video_remotion", side_effect=lambda *_args: (job_dir / "video.mp4").write_text("video") and str(job_dir / "video.mp4")), patch(
                "backend.pipeline.qa_job_directory", return_value={"passed": True, "checks": [], "failure_codes": [], "metrics": {}}
            ), patch("backend.pipeline.verify_final_rendered_meaning", return_value={"passed": True, "segments": []}), patch(
                "backend.pipeline.MAX_CREATIVE_ATTEMPTS", 2
            ):
                asyncio.run(run_pipeline(job_id, {}, "gemini", root))

            status = read_job_status(job_dir)
            self.assertEqual(status["status"], "needs_human_review")
            self.assertIsNone(status["video_url"])
            self.assertEqual(status["creative_qa"], failed_report)
            self.assertTrue(list(job_dir.glob("creative_qa.attempt-*.json")))
            self.assertEqual(mock_audit.call_count, 2)
            self.assertEqual(mock_repair.call_count, 1)

    def test_final_visual_failure_auto_repairs_with_bounded_retry(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            job_id = "1234abcd"
            job_dir = root / job_id
            job_dir.mkdir()
            from backend.job_store import initialize_job_status, read_job_status
            initialize_job_status(job_dir, job_id, "gemini")
            failed = {"passed": False, "segments": [{"segment_id": "s1", "passed": False, "issues": ["unclear"]}]}
            passed = {"passed": True, "segments": [{"segment_id": "s1", "passed": True, "issues": []}]}
            repaired = {"title": "T", "language": "my-MM", "segments": [{"id": "s1", "text": "same", "visual": {"kind": "generic"}}]}
            script = {"title": "T", "language": "my-MM", "segments": [{"id": "s1", "text": "same"}]}

            def voice(*_args, **kwargs):
                Path(kwargs["output_path"]).write_text("audio")
            def render(_job_dir):
                (job_dir / "video.mp4").write_text("video")
                return str(job_dir / "video.mp4")

            with patch("backend.pipeline.generate_voice", side_effect=voice), patch(
                "backend.pipeline.build_render_input", return_value={"mouthCues": [], "audioSrc": "voice.wav", "script": script}
            ), patch("backend.pipeline.render_video_remotion", side_effect=render), patch(
                "backend.pipeline.qa_job_directory", return_value={"passed": True, "checks": [], "failure_codes": [], "metrics": {}}
            ), patch("backend.pipeline.verify_final_rendered_meaning", side_effect=[failed, passed]), patch(
                "backend.pipeline.repair_final_visual_failures", return_value=repaired
            ) as repair:
                asyncio.run(run_pipeline(job_id, script, "gemini", root))

            status = read_job_status(job_dir)
            self.assertEqual(status["status"], "completed")
            self.assertEqual(status["attempt_count"], 1)
            self.assertTrue((job_dir / "final_visual_qa.attempt-1.json").exists())
            self.assertTrue((job_dir / "rejected-video.attempt-1.mp4").exists())
            repair.assert_called_once()

    def test_restart_from_failed_creative_qa_preserves_job_local_script_and_repairs_before_artifact_reuse(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            job_id = "1234abcd"
            job_dir = root / job_id
            job_dir.mkdir()
            from backend.job_store import initialize_job_status
            initialize_job_status(job_dir, job_id, "gemini")
            original = {"title": "T", "language": "my-MM", "segments": [{"id": "s1", "text": "locked", "local_visual": "semantic-repair"}]}
            repaired = {"title": "T", "language": "my-MM", "segments": [{"id": "s1", "text": "locked", "local_visual": "creative-repair"}]}
            (job_dir / "script.json").write_text(json.dumps(original))
            (job_dir / "creative_qa.json").write_text(json.dumps({
                "passed": False,
                "failure_codes": ["TREATMENT_RUN_REPEATED"],
                "failed_clusters": [{"scene_ids": ["s1"]}],
            }))

            def voice(*_args, **kwargs):
                Path(kwargs["output_path"]).write_text("audio")
            def render(_job_dir):
                (job_dir / "video.mp4").write_text("video")
                return str(job_dir / "video.mp4")

            with patch("backend.pipeline._prepare_visual_artifact", side_effect=AssertionError("must not restore base artifact")), patch(
                "backend.pipeline.repair_creative_failures", return_value=repaired
            ) as repair, patch("backend.pipeline.generate_voice", side_effect=voice), patch(
                "backend.pipeline.build_render_input", return_value={"mouthCues": [], "audioSrc": "voice.wav", "script": repaired}
            ), patch("backend.pipeline.render_video_remotion", side_effect=render), patch(
                "backend.pipeline.qa_job_directory", return_value={"passed": True, "checks": [], "failure_codes": [], "metrics": {}}
            ), patch("backend.pipeline.audit_creative_quality", return_value={"passed": True}):
                asyncio.run(run_pipeline(job_id, original, "gemini", root))

            repair.assert_called_once()
            self.assertEqual(json.loads((job_dir / "script.json").read_text()), repaired)

    def test_restart_from_pending_vertex_qa_preserves_verified_render_and_job_local_script(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            job_id = "1234abcd"
            job_dir = root / job_id
            job_dir.mkdir()
            from backend.job_store import initialize_job_status, read_job_status, write_json_atomically
            from backend.pipeline import _voice_fingerprint, _write_render_checkpoint

            script = {
                "title": "T", "language": "my-MM",
                "segments": [{"id": "s1", "text": "locked", "local_visual": "verified"}],
            }
            initialize_job_status(job_dir, job_id, "gemini")
            write_json_atomically(job_dir / "script.json", script)
            (job_dir / "voice.wav").write_text("audio")
            write_json_atomically(job_dir / "voice_checkpoint.json", {
                "provider": "gemini",
                "fingerprint": _voice_fingerprint(script, "gemini"),
                "bytes": (job_dir / "voice.wav").stat().st_size,
            })
            write_json_atomically(job_dir / "render_input.json", {"audioSrc": "voice.wav"})
            write_json_atomically(job_dir / "mouth_cues.json", [])
            (job_dir / "video.mp4").write_text("already-rendered")
            _write_render_checkpoint(
                job_dir,
                script,
                job_dir / "voice.wav",
                job_dir / "video.mp4",
                {"strategy": "monolithic", "total": 0, "rendered": 0, "cache_hits": 0},
            )
            write_json_atomically(job_dir / "qa_report.json", {
                "passed": True, "checks": [], "failure_codes": [], "metrics": {},
            })

            with patch(
                "backend.pipeline._prepare_visual_artifact",
                side_effect=AssertionError("must not restore the base artifact"),
            ), patch("backend.pipeline.generate_voice") as voice, patch(
                "backend.pipeline.build_render_input"
            ) as build, patch("backend.pipeline.render_video_remotion") as render, patch(
                "backend.pipeline.qa_job_directory",
                return_value={"passed": True, "checks": [], "failure_codes": [], "metrics": {}},
            ), patch("backend.pipeline.audit_creative_quality", return_value={"passed": True}):
                asyncio.run(run_pipeline(job_id, script, "gemini", root))

            voice.assert_not_called()
            build.assert_not_called()
            render.assert_not_called()
            self.assertEqual(json.loads((job_dir / "script.json").read_text()), script)
            self.assertEqual(read_job_status(job_dir)["status"], "completed")

    def test_transient_vertex_qa_error_preserves_render_for_resumable_retry(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            job_id = "1234abcd"
            job_dir = root / job_id
            job_dir.mkdir()
            from backend.job_store import initialize_job_status, read_job_status
            initialize_job_status(job_dir, job_id, "gemini")
            script = {"title": "T", "language": "my-MM", "segments": [{"id": "s1", "text": "same"}]}

            def voice(*_args, **kwargs):
                Path(kwargs["output_path"]).write_text("audio")
            def render(_job_dir):
                (job_dir / "video.mp4").write_text("video")
                return str(job_dir / "video.mp4")

            with patch("backend.pipeline.generate_voice", side_effect=voice), patch(
                "backend.pipeline.build_render_input", return_value={"mouthCues": [], "audioSrc": "voice.wav", "script": script}
            ), patch("backend.pipeline.render_video_remotion", side_effect=render), patch(
                "backend.pipeline.qa_job_directory", return_value={"passed": True, "checks": [], "failure_codes": [], "metrics": {}}
            ), patch("backend.pipeline.verify_final_rendered_meaning", side_effect=RuntimeError("temporary Vertex auth unavailable")):
                asyncio.run(run_pipeline(job_id, script, "gemini", root))

            self.assertTrue((job_dir / "video.mp4").is_file())
            status = read_job_status(job_dir)
            self.assertEqual(status["status"], "failed")
            self.assertTrue(status["restart_resumable"])

    def test_render_fingerprint_changes_when_renderer_source_changes(self):
        from backend import pipeline as pipeline_module
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            renderer = root / "src"
            renderer.mkdir()
            source = renderer / "Scene.tsx"
            source.write_text("export const scene = 1")
            audio = root / "voice.wav"
            audio.write_bytes(b"audio")
            script = {"segments": [{"id": "s1", "text": "locked"}]}
            with patch.object(pipeline_module, "REMOTION_SOURCE_ROOT", renderer):
                before = pipeline_module._render_fingerprint(script, audio)
                source.write_text("export const scene = 2")
                after = pipeline_module._render_fingerprint(script, audio)
            self.assertNotEqual(before, after)

    def test_render_fingerprint_changes_when_audio_content_changes_at_same_size(self):
        from backend import pipeline as pipeline_module
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            audio = root / "voice.wav"
            audio.write_bytes(b"audio-a")
            script = {"segments": [{"id": "s1", "text": "locked"}]}
            before = pipeline_module._render_fingerprint(script, audio)
            audio.write_bytes(b"audio-b")
            after = pipeline_module._render_fingerprint(script, audio)
            self.assertNotEqual(before, after)

if __name__ == "__main__":
    unittest.main()
