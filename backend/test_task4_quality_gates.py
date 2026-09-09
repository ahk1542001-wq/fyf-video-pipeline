import json
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.lock_store import (
    final_acceptance_readiness,
    set_human_acceptance,
)
from backend.output_qa import audit_output_manifest, persist_qa_report, qa_report_fingerprint
from backend.pipeline import _is_transient_render_failure, _render_with_configured_strategy
from backend.pipeline import run_pipeline
from backend.final_visual_qa_vertex import verify_final_rendered_meaning
from backend.creative_quality import route_creative_quality
from backend.render_contract import (
    audit_sound_off_comprehension,
    route_caption_audio_qa,
)
from visual_evidence_vertex import (
    _deterministic_motion_graphic_fallback,
    _passed_shot_is_usable,
)


class Task4QualityGateTests(unittest.TestCase):
    def test_caption_audio_routing_blocks_technical_failures_and_bounds_repairs(self):
        technical_failure = {
            "technical_gate": {"passed": False, "failure_codes": ["caption_reading_speed"]},
            "creative_gate": {"passed": True, "route": "approved_for_review"},
            "overall": {"passed": False},
        }
        self.assertEqual(
            route_caption_audio_qa(technical_failure, attempt=0, max_repairs=1)["route"],
            "blocked",
        )

        creative_failure = {
            "technical_gate": {"passed": True, "failure_codes": []},
            "creative_gate": {"passed": False, "route": "repair", "failure_codes": ["creative_caption_dwell"]},
            "overall": {"passed": False},
        }
        self.assertEqual(
            route_caption_audio_qa(creative_failure, attempt=0, max_repairs=1)["route"],
            "repair",
        )
        self.assertEqual(
            route_caption_audio_qa(creative_failure, attempt=1, max_repairs=1)["route"],
            "needs_human_review",
        )
        self.assertEqual(route_creative_quality(creative_failure, attempt=0, max_repairs=1)["route"], "repair")
        self.assertEqual(route_creative_quality(creative_failure, attempt=1, max_repairs=1)["route"], "needs_human_review")
        self.assertEqual(
            route_creative_quality(
                {"passed": False, "route": "needs_human_review", "failure_codes": ["unjudged"]},
                attempt=0,
                max_repairs=1,
            )["route"],
            "needs_human_review",
        )

    def test_sound_off_comprehension_reads_nested_visual_screen_text(self):
        render_input = {
            "segments": [
                {
                    "id": "s1",
                    "text": "Inventory has 12 items.",
                    "visual": {"screen_text": ["Inventory: 12"]},
                }
            ]
        }
        cues = [{"segment_id": "s1", "start": 0.0, "end": 2.0, "text": "Inventory has 12 items."}]
        checks = audit_sound_off_comprehension(render_input, cues=cues)
        by_id = {check["id"]: check for check in checks}
        self.assertTrue(by_id["sound_off_screen_text_present"]["passed"])
        self.assertTrue(by_id["sound_off_numbers_visible"]["passed"])

    def test_segment_fallback_is_transient_only(self):
        self.assertTrue(_is_transient_render_failure(TimeoutError("renderer timed out")))
        self.assertTrue(_is_transient_render_failure(RuntimeError("HTTP 503 unavailable")))
        self.assertFalse(_is_transient_render_failure(ValueError("invalid segment manifest")))
        self.assertFalse(_is_transient_render_failure(RuntimeError("bad segment assembly")))

    def test_strict_segmented_job_does_not_hide_deterministic_failure(self):
        from backend.job_store import initialize_job_status

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            job_dir = root / "1234abcd"
            job_dir.mkdir()
            initialize_job_status(job_dir, job_dir.name, "gemini")
            (job_dir / "render_input.json").write_text(json.dumps({"segments": [{"id": "s1"}]}))
            with patch.dict("os.environ", {"FYF_SEGMENT_RENDER_ENABLED": "1"}), patch(
                "backend.pipeline.render_segments_and_assemble",
                side_effect=RuntimeError("bad segment assembly"),
            ), patch("backend.pipeline.render_video_remotion") as monolithic:
                with self.assertRaisesRegex(RuntimeError, "bad segment assembly"):
                    _render_with_configured_strategy(job_dir)
            monolithic.assert_not_called()

    def test_deterministic_motion_fallback_is_not_semantically_verified(self):
        shot = {
            "shot_id": "count",
            "caption": "ပစ္စည်း ၅ ခု",
            "proves_claim_ids": ["c1"],
        }
        required = [{
            "claim_id": "c1",
            "statement": "There are five items",
            "evidence_type": "count",
            "values": ["5"],
        }]
        result = _deterministic_motion_graphic_fallback(required, shot)
        self.assertEqual(result["verification_status"], "passed")
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "visuals").mkdir()
            (root / "visual_evidence_unverified.json").write_text(
                json.dumps({"version": 1, "shots": [{"segment_id": "s1", "shot_id": "count"}]})
            )
            self.assertFalse(_passed_shot_is_usable(result, root / "visuals", segment_id="s1"))

    def test_corrupt_unverified_fallback_registry_does_not_look_absent(self):
        """A broken fallback sidecar must fail closed instead of restoring semantic trust."""
        shot = {
            "shot_id": "count",
            "verification_status": "passed",
            "media_type": "motion_graphic",
            "motion_spec": {"layout": "count", "labels": ["5"], "values": ["5"]},
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            visuals = root / "visuals"
            visuals.mkdir()
            (root / "visual_evidence_unverified.json").symlink_to("missing-sidecar.json")
            with self.assertRaisesRegex(ValueError, "fallback registry"):
                _passed_shot_is_usable(shot, visuals, segment_id="s1")

    def test_final_visual_qa_reports_provider_unverified_fallback_without_calling_provider(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "script.json").write_text("{}")
            (root / "visual_evidence_unverified.json").write_text(
                json.dumps({"version": 1, "shots": [{"segment_id": "s1", "shot_id": "count"}]})
            )
            with patch("backend.final_visual_qa_vertex._client") as client:
                report = verify_final_rendered_meaning(temp_dir)
            client.assert_not_called()
            self.assertFalse(report["passed"])
            self.assertEqual(report["provider_status"], "unavailable")
            self.assertIn("UNVERIFIED_DETERMINISTIC_FALLBACK", report["failure_codes"])

    def test_manifest_integrity_report_and_qa_persistence_are_durable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            persisted = persist_qa_report(root, {"passed": False, "failure_codes": ["x"]}, attempt=2)
            self.assertEqual(persisted.name, "qa_report.attempt-2.json")
            self.assertEqual(json.loads(persisted.read_text())["failure_codes"], ["x"])
            self.assertEqual(json.loads((root / "qa_report.json").read_text())["failure_codes"], ["x"])

            report = audit_output_manifest(root, require_manifest=True)
            self.assertFalse(report["passed"])
            self.assertIn("MISSING_RENDER_MANIFEST", report["failure_codes"])

    def test_output_qa_rejects_measurable_video_contract_mismatch(self):
        from backend.output_qa import qa_job_directory

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            for name, contents in (
                ("voice.wav", "audio"),
                ("video.mp4", "video"),
                ("mouth_cues.json", [{"start": 0.0, "end": 2.0, "value": "A"}]),
                ("script.json", {"segments": [{"id": "s1", "text": "hello"}]}),
                (
                    "render_input.json",
                    {
                        "fps": 30,
                        "durationInFrames": 60,
                        "width": 1080,
                        "height": 1920,
                        "segments": [{"id": "s1", "text": "hello"}],
                    },
                ),
            ):
                path = root / name
                if isinstance(contents, (dict, list)):
                    path.write_text(json.dumps(contents), encoding="utf-8")
                else:
                    path.write_text(contents, encoding="utf-8")

            def probe(path):
                if str(path).endswith("voice.wav"):
                    return {"format": {"duration": "2.0"}, "streams": [{"codec_type": "audio"}]}
                return {
                    "format": {"duration": "2.0"},
                    "streams": [
                        {
                            "codec_type": "video",
                            "codec_name": "h264",
                            "width": 1080,
                            "height": 1920,
                            "pix_fmt": "yuv444p",
                            "avg_frame_rate": "30/1",
                            "nb_frames": "60",
                        },
                        {"codec_type": "audio", "codec_name": "aac"},
                    ],
                }

            with patch("backend.output_qa._get_ffprobe_info", side_effect=probe), patch(
                "backend.output_qa.analyze_pcm16_wav",
                return_value={"peak_dbfs": -3.0, "full_scale_samples": 0},
            ):
                report = qa_job_directory(root)

            self.assertFalse(report["passed"])
            self.assertIn("VIDEO_PIXEL_FORMAT_MISMATCH", report["failure_codes"])

    def test_human_acceptance_is_project_version_bound_and_required_for_readiness(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project_id = "1234abcd"
            persisted = persist_qa_report(
                root,
                {
                    "status": "available",
                    "passed": True,
                    "checks": [{"id": "deterministic_contract", "passed": True}],
                    "failure_codes": [],
                    "metrics": {},
                    "warnings": [],
                },
                attempt=1,
            )
            qa = json.loads(persisted.read_text(encoding="utf-8"))
            self.assertRegex(qa["fingerprint"], r"^[0-9a-f]{64}$")
            accepted = set_human_acceptance(
                root,
                project_id,
                7,
                actor="owner@example.com",
                accepted=True,
                automated_qa=qa,
            )
            self.assertTrue(accepted["accepted"])
            self.assertTrue(final_acceptance_readiness(root, project_id, 7)["download_ready"])
            self.assertFalse(final_acceptance_readiness(root, project_id, 8)["download_ready"])
            with self.assertRaisesRegex(ValueError, "passing automated QA"):
                set_human_acceptance(
                    root,
                    project_id,
                    11,
                    actor="owner@example.com",
                    accepted=True,
                    automated_qa={"passed": True},
                )
            tampered = dict(qa)
            tampered["checks"] = [{"id": "tampered", "passed": True}]
            with self.assertRaisesRegex(ValueError, "passing automated QA"):
                set_human_acceptance(
                    root,
                    project_id,
                    12,
                    actor="owner@example.com",
                    accepted=True,
                    automated_qa=tampered,
                )
            inconsistent_status = dict(qa)
            inconsistent_status["status"] = "failed"
            inconsistent_status["fingerprint"] = qa_report_fingerprint(inconsistent_status)
            with self.assertRaisesRegex(ValueError, "passing automated QA"):
                set_human_acceptance(
                    root,
                    project_id,
                    13,
                    actor="owner@example.com",
                    accepted=True,
                    automated_qa=inconsistent_status,
                )
            with self.assertRaisesRegex(ValueError, "passing automated QA"):
                set_human_acceptance(
                    root,
                    project_id,
                    9,
                    actor="owner@example.com",
                    accepted=True,
                    automated_qa={"passed": False},
                )
            with self.assertRaisesRegex(ValueError, "passing automated QA"):
                set_human_acceptance(
                    root,
                    project_id,
                    10,
                    actor="owner@example.com",
                    accepted=True,
                    automated_qa={"passed": True, "provider_status": "unavailable"},
                )

    def test_automated_qa_rejects_bare_passing_gate_objects_without_evidence(self):
        """A fingerprint must bind complete persisted evidence, not just gate flags."""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            persisted = persist_qa_report(
                root,
                {
                    "passed": True,
                    "technical_gate": {"passed": True},
                    "creative_gate": {"passed": True},
                    "overall": {"passed": True},
                    "failure_codes": [],
                },
                attempt=1,
            )
            qa = json.loads(persisted.read_text(encoding="utf-8"))
            with self.assertRaisesRegex(ValueError, "passing automated QA"):
                set_human_acceptance(
                    root,
                    "1234abcd",
                    1,
                    actor="owner@example.com",
                    accepted=True,
                    automated_qa=qa,
                )

    def test_automated_qa_rejects_conflicting_evidence_sections(self):
        """A passing evidence branch cannot hide a failed section in the same report."""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            persisted = persist_qa_report(
                root,
                {
                    "passed": True,
                    "checks": [{"name": "deterministic_contract", "passed": True}],
                    "segments": [{"segment_id": "s1", "passed": False}],
                    "failure_codes": [],
                },
                attempt=1,
            )
            qa = json.loads(persisted.read_text(encoding="utf-8"))
            with self.assertRaisesRegex(ValueError, "passing automated QA"):
                set_human_acceptance(
                    root,
                    "1234abcd",
                    1,
                    actor="owner@example.com",
                    accepted=True,
                    automated_qa=qa,
                )

    def test_automated_qa_rejects_anonymous_passing_evidence_entries(self):
        """Evidence rows need stable identifiers so acceptance is auditable."""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            persisted = persist_qa_report(
                root,
                {
                    "passed": True,
                    "checks": [{"passed": True}],
                    "failure_codes": [],
                },
                attempt=1,
            )
            qa = json.loads(persisted.read_text(encoding="utf-8"))
            with self.assertRaisesRegex(ValueError, "passing automated QA"):
                set_human_acceptance(
                    root,
                    "1234abcd",
                    1,
                    actor="owner@example.com",
                    accepted=True,
                    automated_qa=qa,
                )

    def test_caption_audio_technical_failure_stops_pipeline_before_render(self):
        from backend.job_store import initialize_job_status, read_job_status

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            job_id = "1234abcd"
            job_dir = root / job_id
            job_dir.mkdir()
            initialize_job_status(job_dir, job_id, "gemini")
            script = {"title": "T", "language": "my-MM", "segments": [{"id": "s1", "text": "locked"}]}
            render_input = {
                "audioSrc": "voice.wav",
                "mouthCues": [],
                "segments": [{"id": "s1", "startFrame": 0, "endFrame": 30, "text": "locked"}],
            }
            failed_report = {
                "technical_gate": {"passed": False, "failure_codes": ["caption_reading_speed"]},
                "creative_gate": {"passed": True, "failure_codes": []},
                "overall": {"passed": False},
            }
            with patch("backend.pipeline._prepare_visual_artifact", return_value=script), patch(
                "backend.pipeline.generate_voice",
                side_effect=lambda *args, **kwargs: Path(kwargs["output_path"]).write_text("audio"),
            ), patch(
                "backend.pipeline.master_voice_audio",
                return_value={"version": 1, "after": {"peak_dbfs": -3.0, "full_scale_samples": 0}},
            ), patch("backend.pipeline.build_render_input", return_value=render_input), patch(
                "backend.pipeline._write_timed_animatic", return_value={"status": "planned"}
            ), patch("backend.pipeline._run_caption_audio_quality", return_value=failed_report), patch(
                "backend.pipeline.render_video_remotion"
            ) as render:
                asyncio.run(run_pipeline(job_id, script, "gemini", root))

            render.assert_not_called()
            status = read_job_status(job_dir)
            self.assertEqual(status["status"], "failed")
            self.assertFalse(status["restart_resumable"])
            self.assertTrue((job_dir / "caption_audio_qa.attempt-1.json").is_file())

    def test_caption_audio_repair_changes_persisted_animatic_plan(self):
        from backend.pipeline import _repair_caption_audio_quality

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            render_input = {
                "language": "en-US",
                "fps": 30,
                "segments": [{
                    "id": "s1",
                    "startFrame": 0,
                    "endFrame": 30,
                    "text": "Hello",
                    "screen_text": ["Hello"],
                    "visual": {"screen_text": ["Hello"]},
                }],
            }
            (root / "animatic.json").write_text(json.dumps({
                "captions": [{
                    "segment_id": "s1",
                    "start": 0.0,
                    "end": 0.2,
                    "text": "Hello",
                    "lines": ["Hello"],
                    "overflowed": False,
                    "characters_per_second": 25.0,
                    "readable": True,
                }],
                "mix": {"music_present": True, "ducking_enabled": False},
            }), encoding="utf-8")
            report = {
                "technical_gate": {"passed": True, "failure_codes": []},
                "creative_gate": {
                    "passed": False,
                    "route": "repair",
                    "failure_codes": ["creative_caption_dwell", "creative_voice_music_balance"],
                },
                "overall": {"passed": False},
            }

            self.assertTrue(_repair_caption_audio_quality(root, render_input, report))
            repaired = json.loads((root / "animatic.json").read_text(encoding="utf-8"))
            self.assertGreaterEqual(repaired["captions"][0]["end"], 0.6)
            self.assertTrue(repaired["mix"]["ducking_enabled"])
            self.assertEqual(repaired["repair_history"][0]["failure_codes"], [
                "creative_caption_dwell",
                "creative_voice_music_balance",
            ])

    def test_caption_audio_repair_route_escalates_without_rechecking_identical_report(self):
        from backend.job_store import initialize_job_status, read_job_status

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            job_id = "1234abcd"
            job_dir = root / job_id
            job_dir.mkdir()
            initialize_job_status(job_dir, job_id, "gemini")
            script = {"title": "T", "language": "my-MM", "segments": [{"id": "s1", "text": "locked"}]}
            render_input = {
                "audioSrc": "voice.wav",
                "mouthCues": [],
                "segments": [{"id": "s1", "startFrame": 0, "endFrame": 30, "text": "locked"}],
            }
            creative_failure = {
                "technical_gate": {"passed": True, "failure_codes": []},
                "creative_gate": {
                    "passed": False,
                    "route": "repair",
                    "failure_codes": ["unsupported_creative_repair"],
                },
                "overall": {"passed": False},
            }
            with patch("backend.pipeline._prepare_visual_artifact", return_value=script), patch(
                "backend.pipeline.generate_voice",
                side_effect=lambda *args, **kwargs: Path(kwargs["output_path"]).write_text("audio"),
            ), patch(
                "backend.pipeline.master_voice_audio",
                return_value={"version": 1, "after": {"peak_dbfs": -3.0, "full_scale_samples": 0}},
            ), patch("backend.pipeline.build_render_input", return_value=render_input), patch(
                "backend.pipeline._write_timed_animatic", return_value={"status": "planned"}
            ), patch("backend.pipeline._run_caption_audio_quality", return_value=creative_failure) as quality, patch(
                "backend.pipeline._repair_caption_audio_quality", return_value=False
            ) as repair, patch("backend.pipeline.render_video_remotion") as render:
                asyncio.run(run_pipeline(job_id, script, "gemini", root))

            self.assertEqual(quality.call_count, 1)
            repair.assert_called_once()
            render.assert_not_called()
            self.assertEqual(read_job_status(job_dir)["status"], "needs_human_review")

    def test_caption_audio_preflight_does_not_recheck_when_repair_claims_no_observable_change(self):
        """A repair route must not loop over the same persisted plan/report."""
        from backend.pipeline import _run_caption_audio_preflight

        report = {
            "technical_gate": {"passed": True, "failure_codes": []},
            "creative_gate": {
                "passed": False,
                "route": "repair",
                "failure_codes": ["unsupported_creative_repair"],
            },
            "overall": {"passed": False},
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            render_input = {"segments": [{"id": "s1"}]}
            with patch(
                "backend.pipeline._run_caption_audio_quality",
                side_effect=[report, report],
            ) as quality, patch(
                "backend.pipeline._repair_caption_audio_quality", return_value=True
            ) as repair:
                final_report, route = _run_caption_audio_preflight(
                    root, render_input, max_repairs=1
                )

            self.assertIs(final_report, report)
            self.assertEqual(quality.call_count, 1)
            repair.assert_called_once()
            self.assertEqual(route["route"], "needs_human_review")
            self.assertEqual(route["reason"], "caption_audio_repair_noop")
            self.assertFalse(route["repair_applied"])

    def test_caption_audio_preflight_rejects_invalid_repair_budget(self):
        from backend.pipeline import _run_caption_audio_preflight

        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaisesRegex(ValueError, "max_repairs"):
                _run_caption_audio_preflight(
                    Path(temp_dir), {"segments": [{"id": "s1"}]}, max_repairs=-1
                )


if __name__ == "__main__":
    unittest.main()
