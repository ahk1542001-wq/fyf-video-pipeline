import json
import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from pydantic import ValidationError

from backend.final_visual_qa_vertex import verify_final_rendered_meaning
from visual_evidence_vertex import EvidenceVerification


def script_fixture():
    return {
        "title": "Final QA", "language": "my-MM",
        "segments": [{
            "id": "s1", "text": "ပစ္စည်း ၅ ခုရှိတယ်။", "visual_action": "count",
            "scene_type": "demo", "mascot_action": "explain", "emotion": "focused", "emphasis": [],
            "visual": {
                "kind": "generic", "phase": "setup", "camera": "wide", "screen_text": ["၅ ခု"],
                "evidence_claims": [{"claim_id": "c1", "statement": "ပစ္စည်း ၅ ခု", "evidence_type": "count", "values": ["5"]}],
                "evidence_shots": [{
                    "shot_id": "count", "proves_claim_ids": ["c1"], "prompt": "five boxes",
                    "caption": "၅ ခု", "hold_fraction": 1, "media_type": "motion_graphic",
                    "motion_preset": "static", "motion_spec": {"layout": "count", "labels": ["ပစ္စည်း"], "values": ["5"], "object_count": 5},
                    "verification_status": "passed",
                }],
            },
        }],
    }


class FinalVisualQATests(unittest.TestCase):
    def prepare(self, root: Path):
        (root / "script.json").write_text(json.dumps(script_fixture(), ensure_ascii=False), encoding="utf-8")
        (root / "render_input.json").write_text(json.dumps({
            "fps": 30, "segments": [{"id": "s1", "startFrame": 0, "endFrame": 300}],
        }), encoding="utf-8")
        (root / "video.mp4").write_bytes(b"video")

    def prepare_two_scene_job(self, root: Path):
        script = script_fixture()
        second = json.loads(json.dumps(script["segments"][0]))
        second["id"] = "s2"
        second["text"] = "လူသားက စစ်ဆေးတယ်။"
        second["visual"]["evidence_claims"][0]["claim_id"] = "c2"
        second["visual"]["evidence_claims"][0]["statement"] = "လူသား စစ်ဆေးမှု"
        second["visual"]["evidence_shots"][0]["shot_id"] = "check"
        second["visual"]["evidence_shots"][0]["proves_claim_ids"] = ["c2"]
        script["segments"].append(second)
        (root / "script.json").write_text(json.dumps(script, ensure_ascii=False), encoding="utf-8")
        (root / "render_input.json").write_text(json.dumps({
            "fps": 30,
            "segments": [
                {"id": "s1", "startFrame": 0, "endFrame": 300},
                {"id": "s2", "startFrame": 300, "endFrame": 600},
            ],
        }), encoding="utf-8")
        (root / "video.mp4").write_bytes(b"video")

    def prepare_many_scene_job(self, root: Path, count: int):
        script = {"title": "Final QA", "language": "my-MM", "segments": []}
        timings = []
        for index in range(count):
            segment = json.loads(json.dumps(script_fixture()["segments"][0]))
            segment_id = f"s{index + 1}"
            claim_id = f"c{index + 1}"
            segment["id"] = segment_id
            segment["visual"]["evidence_claims"][0]["claim_id"] = claim_id
            segment["visual"]["evidence_claims"][0]["statement"] = f"အထောက်အထား {index + 1}"
            segment["visual"]["evidence_shots"][0]["shot_id"] = f"shot-{index + 1}"
            segment["visual"]["evidence_shots"][0]["proves_claim_ids"] = [claim_id]
            script["segments"].append(segment)
            timings.append({"id": segment_id, "startFrame": index * 300, "endFrame": (index + 1) * 300})
        (root / "script.json").write_text(json.dumps(script, ensure_ascii=False), encoding="utf-8")
        (root / "render_input.json").write_text(
            json.dumps({"fps": 30, "segments": timings}), encoding="utf-8"
        )
        (root / "video.mp4").write_bytes(b"video")

    @staticmethod
    def passing_response(claim_id: str):
        return SimpleNamespace(
            text=json.dumps({
                "passed": True,
                "proved_claim_ids": [claim_id],
                "observed_values": [],
                "issues": [],
            })
        )

    @staticmethod
    def passing_batch_response(count: int):
        return SimpleNamespace(text=json.dumps({
            "items": [
                {
                    "segment_id": f"s{index}",
                    "passed": True,
                    "proved_claim_ids": [f"c{index}"],
                    "observed_values": [],
                    "issues": [],
                }
                for index in range(1, count + 1)
            ]
        }))

    @staticmethod
    def passing_batch_response_for(*segment_ids: str):
        return SimpleNamespace(text=json.dumps({
            "items": [
                {
                    "segment_id": segment_id,
                    "passed": True,
                    "proved_claim_ids": [f"c{segment_id[1:]}"],
                    "observed_values": [],
                    "issues": [],
                }
                for segment_id in segment_ids
            ]
        }))

    def test_final_qa_batch_size_defaults_to_four_and_accepts_only_one_to_six(self):
        from backend.final_visual_qa_vertex import _final_qa_batch_size

        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(_final_qa_batch_size(), 4)
        for value in ("1", "2", "4", "6"):
            with self.subTest(value=value), patch.dict("os.environ", {"FYF_FINAL_QA_BATCH_SIZE": value}):
                self.assertEqual(_final_qa_batch_size(), int(value))
        for value in ("0", "7", "four", ""):
            with self.subTest(value=value), patch.dict("os.environ", {"FYF_FINAL_QA_BATCH_SIZE": value}):
                with self.assertRaises(ValueError):
                    _final_qa_batch_size()

    def test_render_progress_strategy_defaults_off_and_honors_explicit_overrides(self):
        from backend.final_visual_qa_vertex import _render_progress_strategy

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with patch.dict("os.environ", {}, clear=True):
                self.assertEqual(_render_progress_strategy(root), "monolithic")
            with patch.dict("os.environ", {"FYF_SEGMENT_RENDER_ENABLED": "1"}, clear=True):
                self.assertEqual(_render_progress_strategy(root), "segmented")
            with patch.dict("os.environ", {"FYF_SEGMENT_RENDER_ENABLED": "0"}, clear=True):
                self.assertEqual(_render_progress_strategy(root), "monolithic")

    def test_vertex_qa_config_omits_deprecated_sampling_parameters(self):
        from backend.final_visual_qa_vertex import _request_batch_verification

        client = MagicMock()
        client.models.generate_content.return_value = self.passing_batch_response_for("s1")
        _request_batch_verification(client, ["frames"], ["s1"])

        config = client.models.generate_content.call_args.kwargs["config"]
        self.assertIsNone(getattr(config, "temperature", None))
        self.assertIsNone(getattr(config, "top_p", None))
        self.assertIsNone(getattr(config, "top_k", None))

    def test_missing_or_invalid_segment_checkpoint_uses_full_video_not_globbed_variant(self):
        from backend.final_visual_qa_vertex import _qa_source

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            cache_root = root / "render-segments"
            cache_root.mkdir()
            (cache_root / "s1-1111.mp4").write_bytes(b"old-cache")
            current_variant = cache_root / "s1-9999.mp4"
            current_variant.write_bytes(b"current-looking-cache")
            (root / "video.mp4").write_bytes(b"authoritative-full-video")

            with patch.dict("os.environ", {"FYF_SEGMENT_RENDER_ENABLED": "1"}):
                media_path, media_source = _qa_source(root, "s1")
                self.assertEqual(media_path.resolve(), (root / "video.mp4").resolve())
                self.assertEqual(media_source, "full-video")

                current_bytes = current_variant.read_bytes()
                (root / "segment_render_checkpoint.json").write_text(json.dumps({
                    "version": 1,
                    "complete": True,
                    "segment_ids": ["s1"],
                    "segments": [{
                        "segment_id": "s1",
                        "path": "render-segments/s1-9999.mp4",
                        "size_bytes": len(current_bytes),
                        "sha256": hashlib.sha256(b"wrong-sha").hexdigest(),
                        "complete": True,
                    }],
                }))
                media_path, media_source = _qa_source(root, "s1")

            self.assertEqual(media_path.resolve(), (root / "video.mp4").resolve())
            self.assertEqual(media_source, "full-video")

    def test_batch_models_forbid_extra_fields_and_limit_six_items(self):
        from backend.final_visual_qa_vertex import FinalVisualBatchItem, FinalVisualBatchResponse

        item = {
            "segment_id": "s1",
            "passed": True,
            "proved_claim_ids": ["c1"],
            "observed_values": [],
            "issues": [],
        }
        self.assertEqual(FinalVisualBatchResponse.model_validate({"items": [item]}).items[0].segment_id, "s1")
        with self.assertRaises(ValidationError):
            FinalVisualBatchItem.model_validate({**item, "unexpected": True})
        with self.assertRaises(ValidationError):
            FinalVisualBatchResponse.model_validate({"items": [item] * 7})

    def test_four_scene_batch_uses_one_flash_call_without_pro(self):
        client = MagicMock()
        client.models.generate_content.return_value = self.passing_batch_response(4)
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self.prepare_many_scene_job(root, 4)
            with patch("backend.final_visual_qa_vertex._client", return_value=client), patch(
                "backend.final_visual_qa_vertex._extract_frame",
                side_effect=lambda video, seconds, output: output.write_bytes(b"jpg"),
            ) as extract:
                report = verify_final_rendered_meaning(str(root))

        self.assertTrue(report["passed"])
        self.assertEqual([item["segment_id"] for item in report["segments"]], ["s1", "s2", "s3", "s4"])
        self.assertEqual(client.models.generate_content.call_count, 1)
        self.assertEqual(extract.call_count, 12)
        self.assertEqual(
            client.models.generate_content.call_args.kwargs["model"],
            "gemini-3.7-flash",
        )
        prompt = client.models.generate_content.call_args.kwargs["contents"][0]
        self.assertIn("SEGMENT s1", prompt)
        self.assertIn("SEGMENT s4", prompt)

    def test_verify_persists_qa_progress_counters_after_batch(self):
        from backend.job_store import initialize_job_status, read_job_status

        client = MagicMock()
        client.models.generate_content.return_value = self.passing_batch_response(4)
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self.prepare_many_scene_job(root, 4)
            initialize_job_status(root, root.name, "gemini")
            with patch("backend.final_visual_qa_vertex._client", return_value=client), patch(
                "backend.final_visual_qa_vertex._extract_frame",
                side_effect=lambda video, seconds, output: output.write_bytes(b"jpg"),
            ):
                report = verify_final_rendered_meaning(str(root))
            progress = read_job_status(root)["qa_progress"]

        self.assertTrue(report["passed"])
        self.assertEqual(progress, {"total": 4, "verified": 4, "cache_hits": 0, "batches": 1})

    def test_failed_batch_item_gets_only_individual_flash_adjudication(self):
        client = MagicMock()
        client.models.generate_content.side_effect = [
            SimpleNamespace(text=json.dumps({
                "items": [
                    {
                        "segment_id": f"s{index}",
                        "passed": index != 2,
                        "proved_claim_ids": [f"c{index}"] if index != 2 else [],
                        "observed_values": [],
                        "issues": [] if index != 2 else ["unclear"],
                    }
                    for index in range(1, 5)
                ]
            })),
            self.passing_response("c2"),
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self.prepare_many_scene_job(root, 4)
            with patch("backend.final_visual_qa_vertex._client", return_value=client), patch(
                "backend.final_visual_qa_vertex._extract_frame",
                side_effect=lambda video, seconds, output: output.write_bytes(b"jpg"),
            ):
                report = verify_final_rendered_meaning(str(root))

        self.assertTrue(report["passed"])
        self.assertEqual(client.models.generate_content.call_count, 2)
        self.assertEqual(
            [call.kwargs["model"] for call in client.models.generate_content.call_args_list],
            ["gemini-3.7-flash", "gemini-3.7-flash"],
        )

    def test_invalid_batch_response_uses_individual_fallback_for_only_that_batch(self):
        from backend.final_visual_qa_vertex import FinalVisualBatchResponse

        invalid_responses = [
            {"items": [{"segment_id": "s1", "passed": True, "proved_claim_ids": ["c1"], "observed_values": [], "issues": []}]},
            {"items": [
                {"segment_id": "s1", "passed": True, "proved_claim_ids": ["c1"], "observed_values": [], "issues": []},
                {"segment_id": "s1", "passed": True, "proved_claim_ids": ["c1"], "observed_values": [], "issues": []},
                {"segment_id": "s3", "passed": True, "proved_claim_ids": ["c3"], "observed_values": [], "issues": []},
                {"segment_id": "s4", "passed": True, "proved_claim_ids": ["c4"], "observed_values": [], "issues": []},
            ]},
            {"items": "malformed"},
        ]
        for invalid in invalid_responses:
            with self.subTest(invalid=invalid), tempfile.TemporaryDirectory() as temp_dir:
                client = MagicMock()
                client.models.generate_content.side_effect = [
                    SimpleNamespace(text=json.dumps(invalid)),
                    self.passing_response("c1"),
                    self.passing_response("c2"),
                    self.passing_response("c3"),
                    self.passing_response("c4"),
                ]
                root = Path(temp_dir)
                self.prepare_many_scene_job(root, 4)
                with patch("backend.final_visual_qa_vertex._client", return_value=client), patch(
                    "backend.final_visual_qa_vertex._extract_frame",
                    side_effect=lambda video, seconds, output: output.write_bytes(b"jpg"),
                ):
                    report = verify_final_rendered_meaning(str(root))
                self.assertTrue(report["passed"])
                self.assertEqual(client.models.generate_content.call_count, 5)

    def test_partial_batch_checkpoint_restarts_only_unfinished_batch(self):
        from backend.final_visual_qa_vertex import FinalVisualBatchResponse

        first_batch = FinalVisualBatchResponse.model_validate_json(self.passing_batch_response(2).text)
        second_batch = FinalVisualBatchResponse.model_validate({
            "items": [
                {"segment_id": "s3", "passed": True, "proved_claim_ids": ["c3"], "observed_values": [], "issues": []},
                {"segment_id": "s4", "passed": True, "proved_claim_ids": ["c4"], "observed_values": [], "issues": []},
            ]
        })
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self.prepare_many_scene_job(root, 4)
            first_client = MagicMock()
            with patch.dict("os.environ", {"FYF_FINAL_QA_BATCH_SIZE": "2"}), patch(
                "backend.final_visual_qa_vertex._client", return_value=first_client
            ), patch("backend.final_visual_qa_vertex._extract_frame", side_effect=lambda video, seconds, output: output.write_bytes(b"jpg")), patch(
                "backend.final_visual_qa_vertex._request_batch_verification",
                side_effect=[first_batch, RuntimeError("interrupted")],
            ), patch(
                "backend.final_visual_qa_vertex._request_verification",
                side_effect=RuntimeError("interrupted"),
            ):
                with self.assertRaisesRegex(RuntimeError, "interrupted"):
                    verify_final_rendered_meaning(str(root))

            checkpoint = json.loads((root / "final_visual_qa_checkpoint.json").read_text(encoding="utf-8"))
            self.assertEqual(checkpoint["completed_segment_ids"], ["s1", "s2"])

            second_client = MagicMock()
            with patch.dict("os.environ", {"FYF_FINAL_QA_BATCH_SIZE": "2"}), patch(
                "backend.final_visual_qa_vertex._client", return_value=second_client
            ), patch("backend.final_visual_qa_vertex._extract_frame", side_effect=lambda video, seconds, output: output.write_bytes(b"jpg")), patch(
                "backend.final_visual_qa_vertex._request_batch_verification", return_value=second_batch
            ) as request:
                report = verify_final_rendered_meaning(str(root))

        self.assertTrue(report["passed"])
        request.assert_called_once()

    def test_all_sampled_frames_pass(self):
        client = MagicMock()
        client.models.generate_content.return_value = self.passing_batch_response_for("s1")
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir); self.prepare(root)
            with patch("backend.final_visual_qa_vertex._client", return_value=client), patch(
                "backend.final_visual_qa_vertex._extract_frame",
                side_effect=lambda video, seconds, output: output.write_bytes(b"jpg"),
            ) as extract:
                report = verify_final_rendered_meaning(str(root))
        self.assertTrue(report["passed"])
        self.assertEqual(extract.call_count, 3)

    def test_missing_final_claim_fails_closed(self):
        client = MagicMock()
        client.models.generate_content.side_effect = [
            SimpleNamespace(text=json.dumps({
                "items": [{
                    "segment_id": "s1", "passed": False, "proved_claim_ids": [],
                    "observed_values": [], "issues": ["five boxes not visible"],
                }]
            })),
            SimpleNamespace(text='{"passed":false,"proved_claim_ids":[],"observed_values":[],"issues":["five boxes not visible"]}'),
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir); self.prepare(root)
            with patch("backend.final_visual_qa_vertex._client", return_value=client), patch(
                "backend.final_visual_qa_vertex._extract_frame",
                side_effect=lambda video, seconds, output: output.write_bytes(b"jpg"),
            ):
                report = verify_final_rendered_meaning(str(root))
        self.assertFalse(report["passed"])
        self.assertEqual(report["segments"][0]["issues"], ["five boxes not visible"])

    def test_failed_flash_verification_escalates_to_pro_adjudication(self):
        client = MagicMock()
        client.models.generate_content.side_effect = [
            SimpleNamespace(
                text=json.dumps({
                    "items": [{
                        "segment_id": "s1", "passed": False, "proved_claim_ids": [],
                        "observed_values": [], "issues": ["unclear"],
                    }]
                })
            ),
            SimpleNamespace(
                text='{"passed":true,"proved_claim_ids":["c1"],"observed_values":["5"],"issues":[]}'
            ),
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir); self.prepare(root)
            with patch("backend.final_visual_qa_vertex._client", return_value=client), patch(
                "backend.final_visual_qa_vertex._extract_frame",
                side_effect=lambda video, seconds, output: output.write_bytes(b"jpg"),
            ), patch("backend.final_visual_qa_vertex.model_for", side_effect=lambda stage: {
                "visual_verification": "gemini-3.7-flash",
                "visual_verification_fallback": "gemini-3.1-pro-preview",
            }[stage]):
                report = verify_final_rendered_meaning(str(root))

        self.assertTrue(report["passed"])
        self.assertEqual(
            [call.kwargs["model"] for call in client.models.generate_content.call_args_list],
            ["gemini-3.7-flash", "gemini-3.1-pro-preview"],
        )

    def test_flash_error_uses_one_pro_fallback_call(self):
        fallback_result = EvidenceVerification(
            passed=False,
            proved_claim_ids=[],
            observed_values=[],
            issues=["still unclear"],
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir); self.prepare(root)
            with patch("backend.final_visual_qa_vertex._client", return_value=MagicMock()), patch(
                "backend.final_visual_qa_vertex._extract_frame",
                side_effect=lambda video, seconds, output: output.write_bytes(b"jpg"),
            ), patch(
                "backend.final_visual_qa_vertex._request_verification",
                side_effect=[RuntimeError("Flash unavailable"), fallback_result],
            ) as request:
                report = verify_final_rendered_meaning(str(root))

        self.assertFalse(report["passed"])
        self.assertEqual(request.call_count, 2)

    def test_foreign_explanatory_text_fails_brand_language_gate(self):
        client = MagicMock()
        client.models.generate_content.side_effect = [
            SimpleNamespace(text=json.dumps({
                "items": [{
                    "segment_id": "s1", "passed": False, "proved_claim_ids": ["c1"],
                    "observed_values": ["5"],
                    "issues": ["Visible explanatory labels are in English"],
                }]
            })),
            SimpleNamespace(text='{"passed":false,"proved_claim_ids":["c1"],"observed_values":["5"],"issues":["Visible explanatory labels are in English"]}'),
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir); self.prepare(root)
            with patch("backend.final_visual_qa_vertex._client", return_value=client), patch(
                "backend.final_visual_qa_vertex._extract_frame",
                side_effect=lambda video, seconds, output: output.write_bytes(b"jpg"),
            ):
                report = verify_final_rendered_meaning(str(root))
        prompt = client.models.generate_content.call_args.kwargs["contents"][0]
        self.assertIn("Reject English or other foreign-language explanatory text", prompt)
        self.assertFalse(report["passed"])

    def test_partial_checkpoint_resumes_only_unfinished_segments(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            script = script_fixture()
            second = json.loads(json.dumps(script["segments"][0]))
            second["id"] = "s2"
            second["text"] = "လူသားက စစ်ဆေးတယ်။"
            second["visual"]["evidence_claims"][0]["claim_id"] = "c2"
            second["visual"]["evidence_shots"][0]["shot_id"] = "check"
            second["visual"]["evidence_shots"][0]["proves_claim_ids"] = ["c2"]
            script["segments"].append(second)
            (root / "script.json").write_text(json.dumps(script, ensure_ascii=False), encoding="utf-8")
            (root / "render_input.json").write_text(json.dumps({
                "fps": 30,
                "segments": [
                    {"id": "s1", "startFrame": 0, "endFrame": 300},
                    {"id": "s2", "startFrame": 300, "endFrame": 600},
                ],
            }), encoding="utf-8")
            (root / "video.mp4").write_bytes(b"video")
            first_client = MagicMock()
            first_client.models.generate_content.side_effect = [
                self.passing_batch_response_for("s1"),
                RuntimeError("temporary network failure"),
                RuntimeError("temporary network failure"),
                RuntimeError("temporary network failure"),
            ]
            second_client = MagicMock()
            second_client.models.generate_content.return_value = self.passing_batch_response_for("s2")
            with patch.dict("os.environ", {"FYF_FINAL_QA_BATCH_SIZE": "1"}), patch(
                "backend.final_visual_qa_vertex._extract_frame", side_effect=lambda video, seconds, output: output.write_bytes(b"jpg")), patch(
                "backend.final_visual_qa_vertex._client", return_value=first_client
            ):
                with self.assertRaisesRegex(RuntimeError, "temporary network"):
                    verify_final_rendered_meaning(str(root))
            checkpoint = json.loads((root / "final_visual_qa_checkpoint.json").read_text())
            self.assertEqual(checkpoint["completed_segment_ids"], ["s1"])

            with patch.dict("os.environ", {"FYF_FINAL_QA_BATCH_SIZE": "1"}), patch(
                "backend.final_visual_qa_vertex._extract_frame", side_effect=lambda video, seconds, output: output.write_bytes(b"jpg")), patch(
                "backend.final_visual_qa_vertex._client", return_value=second_client
            ):
                report = verify_final_rendered_meaning(str(root))
            self.assertTrue(report["passed"])
            self.assertEqual(first_client.models.generate_content.call_count, 4)
            self.assertEqual(second_client.models.generate_content.call_count, 1)
            self.assertEqual([item["segment_id"] for item in report["segments"]], ["s1", "s2"])

    def test_changed_scene_reuses_unchanged_scene_result(self):
        client = MagicMock()
        client.models.generate_content.side_effect = [
            self.passing_batch_response_for("s1", "s2"),
            self.passing_batch_response_for("s2"),
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self.prepare_two_scene_job(root)
            with patch("backend.final_visual_qa_vertex._client", return_value=client), patch(
                "backend.final_visual_qa_vertex._extract_frame",
                side_effect=lambda video, seconds, output: output.write_bytes(b"jpg"),
            ) as extract:
                first = verify_final_rendered_meaning(str(root))
                script = json.loads((root / "script.json").read_text(encoding="utf-8"))
                script["segments"][1]["visual"]["evidence_claims"][0]["statement"] = "လူသားက ပြန်စစ်ဆေးတယ်။"
                (root / "script.json").write_text(
                    json.dumps(script, ensure_ascii=False), encoding="utf-8"
                )
                second = verify_final_rendered_meaning(str(root))

        self.assertTrue(first["passed"])
        self.assertTrue(second["passed"])
        self.assertEqual(client.models.generate_content.call_count, 2)
        self.assertEqual(extract.call_count, 9)

    def test_model_route_change_invalidates_every_scene_result(self):
        client = MagicMock()
        client.models.generate_content.side_effect = [
            self.passing_batch_response_for("s1", "s2"),
            self.passing_batch_response_for("s1", "s2"),
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self.prepare_two_scene_job(root)
            with patch("backend.final_visual_qa_vertex._client", return_value=client), patch(
                "backend.final_visual_qa_vertex._extract_frame",
                side_effect=lambda video, seconds, output: output.write_bytes(b"jpg"),
            ):
                verify_final_rendered_meaning(str(root))
                with patch(
                    "backend.final_visual_qa_vertex.model_for",
                    side_effect=lambda stage: f"changed-{stage}",
                ):
                    report = verify_final_rendered_meaning(str(root))

        self.assertTrue(report["passed"])
        self.assertEqual(client.models.generate_content.call_count, 2)

    def test_prompt_contract_change_invalidates_every_scene_result(self):
        client = MagicMock()
        client.models.generate_content.side_effect = [
            self.passing_batch_response_for("s1", "s2"),
            self.passing_batch_response_for("s1", "s2"),
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self.prepare_two_scene_job(root)
            with patch("backend.final_visual_qa_vertex._client", return_value=client), patch(
                "backend.final_visual_qa_vertex._extract_frame",
                side_effect=lambda video, seconds, output: output.write_bytes(b"jpg"),
            ):
                verify_final_rendered_meaning(str(root))
                with patch("backend.final_visual_qa_vertex.QA_PROMPT_VERSION", 2):
                    report = verify_final_rendered_meaning(str(root))

        self.assertTrue(report["passed"])
        self.assertEqual(client.models.generate_content.call_count, 2)

    def test_checkpoint_without_fingerprints_is_rejected(self):
        client = MagicMock()
        client.models.generate_content.side_effect = [
            self.passing_batch_response_for("s1", "s2"),
            self.passing_batch_response_for("s1", "s2"),
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self.prepare_two_scene_job(root)
            with patch("backend.final_visual_qa_vertex._client", return_value=client), patch(
                "backend.final_visual_qa_vertex._extract_frame",
                side_effect=lambda video, seconds, output: output.write_bytes(b"jpg"),
            ):
                verify_final_rendered_meaning(str(root))
                checkpoint_path = root / "final_visual_qa_checkpoint.json"
                checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
                checkpoint["results"][0].pop("segment_fingerprint")
                checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")
                report = verify_final_rendered_meaning(str(root))

        self.assertTrue(report["passed"])
        self.assertEqual(client.models.generate_content.call_count, 2)

    def test_checkpoint_duplicate_or_unknown_ids_is_rejected(self):
        for corruption in ("duplicate", "unknown"):
            with self.subTest(corruption=corruption), tempfile.TemporaryDirectory() as temp_dir:
                client = MagicMock()
                client.models.generate_content.side_effect = [
                    self.passing_batch_response_for("s1", "s2"),
                    self.passing_batch_response_for("s1", "s2"),
                ]
                root = Path(temp_dir)
                self.prepare_two_scene_job(root)
                with patch("backend.final_visual_qa_vertex._client", return_value=client), patch(
                    "backend.final_visual_qa_vertex._extract_frame",
                    side_effect=lambda video, seconds, output: output.write_bytes(b"jpg"),
                ):
                    verify_final_rendered_meaning(str(root))
                    checkpoint_path = root / "final_visual_qa_checkpoint.json"
                    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
                    if corruption == "duplicate":
                        checkpoint["results"][1] = dict(checkpoint["results"][0])
                        checkpoint["completed_segment_ids"] = ["s1", "s1"]
                    else:
                        checkpoint["results"][1]["segment_id"] = "s3"
                        checkpoint["completed_segment_ids"] = ["s1", "s3"]
                    checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")
                    report = verify_final_rendered_meaning(str(root))

                self.assertTrue(report["passed"])
                self.assertEqual(client.models.generate_content.call_count, 2)

    def test_segmented_qa_extracts_relative_cached_frames_and_fallback_uses_global_frames(self):
        client = MagicMock()
        client.models.generate_content.side_effect = [
            self.passing_batch_response_for("s1", "s2"),
            self.passing_batch_response_for("s1", "s2"),
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self.prepare_two_scene_job(root)
            cache_root = root / "render-segments"
            cache_root.mkdir()
            entries = []
            for segment_id in ("s1", "s2"):
                path = cache_root / f"{segment_id}.mp4"
                payload = segment_id.encode("ascii")
                path.write_bytes(payload)
                entries.append({
                    "segment_id": segment_id,
                    "path": f"render-segments/{segment_id}.mp4",
                    "size_bytes": len(payload),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "complete": True,
                })
            (root / "segment_render_checkpoint.json").write_text(json.dumps({
                "version": 1,
                "complete": True,
                "segment_ids": ["s1", "s2"],
                "segments": entries,
            }))
            (root / "status.json").write_text(json.dumps({
                "job_id": "1234abcd", "status": "qa", "render_progress": {
                    "strategy": "segmented", "total": 2, "rendered": 2, "cache_hits": 0,
                },
            }))
            with patch("backend.final_visual_qa_vertex._client", return_value=client), patch(
                "backend.final_visual_qa_vertex._extract_frame",
                side_effect=lambda video, seconds, output: output.write_bytes(b"jpg"),
            ) as extract:
                segmented_report = verify_final_rendered_meaning(str(root))
                segmented_calls = list(extract.call_args_list)
                checkpoint_path = root / "final_visual_qa_checkpoint.json"
                checkpoint_path.unlink()
                (root / "status.json").write_text(json.dumps({
                    "job_id": "1234abcd", "status": "qa", "render_progress": {
                        "strategy": "monolithic-fallback", "total": 0, "rendered": 0, "cache_hits": 0,
                    },
                }))
                monolithic_report = verify_final_rendered_meaning(str(root))
                monolithic_calls = extract.call_args_list[len(segmented_calls):]

        self.assertTrue(segmented_report["passed"])
        self.assertTrue(monolithic_report["passed"])
        self.assertTrue(all(call.args[0].name in {"s1.mp4", "s2.mp4"} for call in segmented_calls))
        self.assertEqual([round(call.args[1], 1) for call in monolithic_calls], [2.0, 5.0, 8.0, 12.0, 15.0, 18.0])

    @patch("backend.final_visual_qa_vertex.genai.Client")
    def test_client_has_bounded_http_timeout(self, client_constructor):
        with patch.dict("os.environ", {"FYF_VERTEX_CALL_TIMEOUT_SECONDS": "45"}):
            from backend.final_visual_qa_vertex import _client

            _client()

        options = client_constructor.call_args.kwargs["http_options"]
        self.assertEqual(options.timeout, 45_000)


if __name__ == "__main__":
    unittest.main()
