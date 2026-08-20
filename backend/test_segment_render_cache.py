import copy
import hashlib
import json
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from pathlib import Path

from backend.segment_render_cache import (
    SegmentRenderResult,
    _manifest_fingerprint,
    _segment_render_concurrency,
    load_reusable_segment,
    render_segments_and_assemble,
    segment_render_fingerprint,
    validate_segment_media,
    write_segment_checkpoint,
)


def render_input_fixture() -> dict:
    return {
        "fps": 30,
        "width": 1080,
        "height": 1920,
        "segments": [
            {
                "id": "s1",
                "startFrame": 0,
                "endFrame": 30,
                "text": "First scene",
                "visual": {"kind": "object_action", "asset_path": "job-visuals/one.png"},
            },
            {
                "id": "s2",
                "startFrame": 30,
                "endFrame": 60,
                "text": "Second scene",
                "visual": {"kind": "comparison", "asset_path": "job-visuals/two.png"},
            },
        ],
        "mouthCues": [
            {"start": 0.10, "end": 0.30, "value": "A"},
            {"start": 1.10, "end": 1.30, "value": "B"},
            {"start": 2.10, "end": 2.30, "value": "C"},
        ],
    }


def fingerprint_kwargs(asset_path: Path) -> dict:
    return {
        "renderer_source_hash": "renderer-v1",
        "remotion_version": "4.0.506",
        "composition_id": "VisualSystemV4Full",
        "output_settings": {
            "codec": "h264",
            "pixel_format": "yuv420p",
            "fps": 30,
            "width": 1080,
            "height": 1920,
        },
        "asset_paths": [asset_path],
    }


def assembly_render_input() -> dict:
    return {
        "fps": 30,
        "width": 1080,
        "height": 1920,
        "durationInFrames": 90,
        "audioSrc": "voice.wav",
        "segments": [
            {
                "id": "s1",
                "startFrame": 0,
                "endFrame": 30,
                "text": "First",
                "visual": {"kind": "object_action"},
            },
            {
                "id": "s2",
                "startFrame": 30,
                "endFrame": 60,
                "text": "Second",
                "visual": {"kind": "comparison"},
            },
            {
                "id": "s3",
                "startFrame": 60,
                "endFrame": 90,
                "text": "Third",
                "visual": {"kind": "story_scene"},
            },
        ],
        "mouthCues": [],
    }


def seed_assembly_job(job_dir: Path) -> None:
    (job_dir / "voice.wav").write_bytes(b"voice")
    (job_dir / "render_input.json").write_text(json.dumps(assembly_render_input()))
    (job_dir / "render-segments").mkdir()


class SegmentRenderCacheTests(unittest.TestCase):
    def test_segment_render_concurrency_defaults_to_two_and_rejects_out_of_bounds_values(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(_segment_render_concurrency(), 2)
        for value in ("1", "2", "3", "4"):
            with self.subTest(value=value), patch.dict(
                os.environ, {"FYF_SEGMENT_RENDER_CONCURRENCY": value}
            ):
                self.assertEqual(_segment_render_concurrency(), int(value))
        for value in ("0", "5", "two", ""):
            with self.subTest(value=value), patch.dict(
                os.environ, {"FYF_SEGMENT_RENDER_CONCURRENCY": value}
            ):
                with self.assertRaises(ValueError):
                    _segment_render_concurrency()

    def test_warm_cache_reassembles_when_final_video_is_substituted(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            job_dir = Path(temp_dir)
            seed_assembly_job(job_dir)
            fingerprints = {f"s{i}": f"{i}" * 64 for i in (1, 2, 3)}
            results = []
            for segment_id in ("s1", "s2", "s3"):
                path = job_dir / "render-segments" / f"{segment_id}.mp4"
                path.write_bytes(segment_id.encode())
                results.append(
                    SegmentRenderResult(
                        segment_id,
                        fingerprints[segment_id],
                        path,
                        True,
                        30,
                    )
                )
            write_segment_checkpoint(
                job_dir,
                results,
                complete=True,
                manifest_fingerprint=_manifest_fingerprint(results),
            )
            video_path = job_dir / "video.mp4"
            video_path.write_bytes(b"trusted-original-video")
            video_path.write_bytes(b"foreign-compatible-video")

            def reassemble(*_args, **_kwargs):
                video_path.write_bytes(b"trusted-reassembled-video")
                return video_path

            with patch("backend.segment_render_cache.validate_render_input"), patch(
                "backend.segment_render_cache.segment_render_fingerprint",
                side_effect=lambda _data, *, segment_id, **_kwargs: fingerprints[segment_id],
            ), patch(
                "backend.segment_render_cache.validate_segment_media",
                return_value={"frame_count": 30},
            ), patch("backend.segment_render_cache._validate_final_output"), patch(
                "backend.segment_render_cache._assemble_segments",
                side_effect=reassemble,
            ) as mock_assemble:
                report = render_segments_and_assemble(str(job_dir))

            self.assertEqual(report.rendered_segments, 0)
            self.assertEqual(report.cache_hits, 3)
            self.assertNotEqual(video_path.read_bytes(), b"foreign-compatible-video")
            self.assertGreaterEqual(mock_assemble.call_count, 1)
            checkpoint = json.loads(
                (job_dir / "segment_render_checkpoint.json").read_text()
            )
            self.assertEqual(checkpoint["video_bytes"], video_path.stat().st_size)
            self.assertEqual(
                checkpoint["video_sha256"],
                hashlib.sha256(video_path.read_bytes()).hexdigest(),
            )

    @patch("backend.segment_render_cache.subprocess.run")
    def test_validate_segment_media_rejects_every_non_contract_stream_shape(self, mock_run):
        with tempfile.TemporaryDirectory() as temp_dir:
            media = Path(temp_dir) / "segment.mp4"
            media.write_bytes(b"mp4")
            valid_stream = {
                "codec_type": "video",
                "codec_name": "h264",
                "width": 1080,
                "height": 1920,
                "pix_fmt": "yuv420p",
                "r_frame_rate": "30/1",
                "avg_frame_rate": "30/1",
                "nb_frames": "30",
                "nb_read_frames": "30",
            }

            def probe(streams):
                mock_run.return_value = MagicMock(
                    returncode=0,
                    stdout=json.dumps({"streams": streams, "format": {}}),
                    stderr="",
                )
                return validate_segment_media(media, 30, 30, 1080, 1920)

            self.assertEqual(probe([valid_stream])["frame_count"], 30)
            invalid_streams = [
                ([{**valid_stream, "codec_type": "audio"}], "video stream"),
                ([{**valid_stream, "codec_name": "vp9"}], "codec"),
                ([{**valid_stream, "width": 720}], "width"),
                ([{**valid_stream, "height": 1280}], "height"),
                ([{**valid_stream, "pix_fmt": "yuv444p"}], "pixel"),
                ([{**valid_stream, "r_frame_rate": "24/1", "avg_frame_rate": "24/1"}], "FPS"),
                ([{**valid_stream, "nb_frames": "29", "nb_read_frames": "29"}], "frame count"),
                ([{**valid_stream}, {**valid_stream}], "exactly one"),
                ([{**valid_stream}, {"codec_type": "audio"}], "audio"),
            ]
            for streams, message in invalid_streams:
                with self.subTest(message=message):
                    with self.assertRaisesRegex(ValueError, message):
                        probe(streams)

    @patch("backend.segment_render_cache.validate_render_input")
    @patch("backend.segment_render_cache._validate_final_output")
    @patch("backend.segment_render_cache._run_ffmpeg")
    @patch("backend.segment_render_cache.validate_segment_media", return_value={"frame_count": 30})
    @patch("backend.segment_render_cache.segment_render_fingerprint")
    @patch("backend.segment_render_cache.render_video_segment")
    def test_three_segments_reuse_two_cache_hits_and_render_only_changed_segment(
        self,
        mock_render,
        mock_fingerprint,
        mock_validate_media,
        mock_ffmpeg,
        mock_validate_output,
        mock_validate_input,
    ):
        with tempfile.TemporaryDirectory() as temp_dir:
            job_dir = Path(temp_dir)
            seed_assembly_job(job_dir)
            fingerprints = {"s1": "1" * 64, "s2": "2" * 64, "s3": "3" * 64}
            for segment_id in ("s1", "s2"):
                path = job_dir / "render-segments" / f"{segment_id}.mp4"
                path.write_bytes(segment_id.encode())
                write_segment_checkpoint(
                    job_dir,
                    [SegmentRenderResult(segment_id, fingerprints[segment_id], path, True, 30)],
                    complete=False,
                )
                if segment_id == "s2":
                    checkpoint = json.loads((job_dir / "segment_render_checkpoint.json").read_text())
                    checkpoint["segments"] = [
                        {
                            "segment_id": "s1",
                            "fingerprint": fingerprints["s1"],
                            "path": "render-segments/s1.mp4",
                            "frame_count": 30,
                            "size_bytes": (job_dir / "render-segments/s1.mp4").stat().st_size,
                            "sha256": hashlib.sha256(b"s1").hexdigest(),
                            "complete": True,
                        },
                        {
                            "segment_id": "s2",
                            "fingerprint": fingerprints["s2"],
                            "path": "render-segments/s2.mp4",
                            "frame_count": 30,
                            "size_bytes": (job_dir / "render-segments/s2.mp4").stat().st_size,
                            "sha256": hashlib.sha256(b"s2").hexdigest(),
                            "complete": True,
                        },
                    ]
                    checkpoint["segment_ids"] = ["s1", "s2"]
                    (job_dir / "segment_render_checkpoint.json").write_text(json.dumps(checkpoint))

            mock_fingerprint.side_effect = lambda _data, *, segment_id, **_kwargs: fingerprints[segment_id]

            def render_side_effect(_job_dir, *, segment_id, output_path, **_kwargs):
                Path(output_path).write_bytes(segment_id.encode())
                return str(Path(output_path).resolve())

            mock_render.side_effect = render_side_effect
            mock_ffmpeg.side_effect = lambda command: Path(command[-1]).write_bytes(b"assembled")

            report = render_segments_and_assemble(str(job_dir))

            self.assertEqual(report.total_segments, 3)
            self.assertEqual(report.rendered_segments, 1)
            self.assertEqual(report.cache_hits, 2)
            self.assertEqual(mock_render.call_count, 1)
            self.assertEqual(mock_render.call_args.kwargs["segment_id"], "s3")
            self.assertTrue(report.output_path.is_file())

    @patch("backend.segment_render_cache.validate_render_input")
    @patch("backend.segment_render_cache._validate_final_output")
    @patch("backend.segment_render_cache.validate_segment_media", return_value={"frame_count": 30})
    @patch("backend.segment_render_cache.segment_render_fingerprint")
    @patch("backend.segment_render_cache.render_video_segment")
    @patch("backend.segment_render_cache.subprocess.run")
    def test_assembly_preserves_segment_order_uses_stream_copy_and_atomically_muxes_voice(
        self,
        mock_run,
        mock_render,
        mock_fingerprint,
        mock_validate_media,
        mock_validate_input,
        mock_validate_output,
    ):
        with tempfile.TemporaryDirectory() as temp_dir:
            job_dir = Path(temp_dir)
            seed_assembly_job(job_dir)
            fingerprints = {f"s{i}": f"{i}" * 64 for i in (1, 2, 3)}
            mock_fingerprint.side_effect = lambda _data, *, segment_id, **_kwargs: fingerprints[segment_id]

            def render_side_effect(_job_dir, *, segment_id, output_path, **_kwargs):
                Path(output_path).write_bytes(segment_id.encode())
                return str(Path(output_path).resolve())

            mock_render.side_effect = render_side_effect
            concat_lists = []

            def run_side_effect(command, **_kwargs):
                if "-f" in command and "concat" in command:
                    concat_lists.append(Path(command[command.index("-i") + 1]).read_text())
                Path(command[-1]).write_bytes(b"ffmpeg output")
                return MagicMock(returncode=0, stdout="", stderr="")

            mock_run.side_effect = run_side_effect
            report = render_segments_and_assemble(str(job_dir))

            self.assertEqual(len(concat_lists), 1)
            ordered = concat_lists[0].splitlines()
            ordered_text = "\n".join(ordered)
            self.assertLess(ordered_text.index("s1-"), ordered_text.index("s2-"))
            self.assertLess(ordered_text.index("s2-"), ordered_text.index("s3-"))
            commands = [call.args[0] for call in mock_run.call_args_list]
            concat_command = next(command for command in commands if "concat" in command)
            mux_command = next(command for command in commands if "-map" in command)
            self.assertEqual(concat_command[concat_command.index("-c") + 1], "copy")
            self.assertEqual(mux_command[mux_command.index("-map") + 1], "0:v:0")
            self.assertIn("1:a:0", mux_command)
            self.assertIn("-c:v", mux_command)
            self.assertIn("-c:a", mux_command)
            self.assertNotIn("-shortest", mux_command)
            for call in mock_run.call_args_list:
                self.assertFalse(call.kwargs["shell"])
            self.assertEqual(report.output_path, (job_dir / "video.mp4").resolve())
            self.assertTrue(report.output_path.is_file())

    @patch.dict(os.environ, {"FYF_SEGMENT_RENDER_CONCURRENCY": "1"})
    @patch("backend.segment_render_cache.validate_render_input")
    @patch("backend.segment_render_cache._validate_final_output")
    @patch("backend.segment_render_cache.validate_segment_media", return_value={"frame_count": 30})
    @patch("backend.segment_render_cache.segment_render_fingerprint")
    @patch("backend.segment_render_cache.render_video_segment")
    def test_interrupted_render_persists_completed_segment_for_resume(
        self,
        mock_render,
        mock_fingerprint,
        mock_validate_media,
        mock_validate_input,
        mock_validate_output,
    ):
        with tempfile.TemporaryDirectory() as temp_dir:
            job_dir = Path(temp_dir)
            seed_assembly_job(job_dir)
            fingerprints = {f"s{i}": f"{i}" * 64 for i in (1, 2, 3)}
            mock_fingerprint.side_effect = lambda _data, *, segment_id, **_kwargs: fingerprints[segment_id]
            first_attempt = True

            def render_side_effect(_job_dir, *, segment_id, output_path, **_kwargs):
                nonlocal first_attempt
                if first_attempt and segment_id == "s2":
                    raise RuntimeError("simulated interruption")
                Path(output_path).write_bytes(segment_id.encode())
                return str(Path(output_path).resolve())

            mock_render.side_effect = render_side_effect
            with self.assertRaisesRegex(RuntimeError, "interruption"):
                render_segments_and_assemble(str(job_dir))
            checkpoint = json.loads((job_dir / "segment_render_checkpoint.json").read_text())
            self.assertEqual(checkpoint["segment_ids"], ["s1"])

            first_attempt = False
            mock_render.reset_mock()
            mock_render.side_effect = render_side_effect
            with patch("backend.segment_render_cache._run_ffmpeg") as mock_ffmpeg:
                mock_ffmpeg.side_effect = lambda command: Path(command[-1]).write_bytes(b"assembled")
                report = render_segments_and_assemble(str(job_dir))
            self.assertEqual(report.cache_hits, 1)
            self.assertEqual(
                [call.kwargs["segment_id"] for call in mock_render.call_args_list],
                ["s2", "s3"],
            )
    def test_identical_segment_inputs_produce_identical_fingerprint(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            asset = Path(temp_dir) / "one.png"
            asset.write_bytes(b"asset-one")
            data = render_input_fixture()

            first = segment_render_fingerprint(
                data, segment_id="s1", **fingerprint_kwargs(asset)
            )
            second = segment_render_fingerprint(
                copy.deepcopy(data), segment_id="s1", **fingerprint_kwargs(asset)
            )

            self.assertEqual(first, second)
            self.assertRegex(first, r"^[0-9a-f]{64}$")

    def test_unrelated_segment_changes_do_not_change_target_fingerprint(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            asset = Path(temp_dir) / "one.png"
            asset.write_bytes(b"asset-one")
            original = render_input_fixture()
            changed = copy.deepcopy(original)
            changed["segments"][1]["visual"]["kind"] = "story_scene"
            changed["segments"][1]["text"] = "Unrelated repair"

            self.assertEqual(
                segment_render_fingerprint(
                    original, segment_id="s1", **fingerprint_kwargs(asset)
                ),
                segment_render_fingerprint(
                    changed, segment_id="s1", **fingerprint_kwargs(asset)
                ),
            )

    def test_target_visual_frame_range_mouth_cue_renderer_and_asset_changes_invalidate(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            asset = Path(temp_dir) / "one.png"
            asset.write_bytes(b"asset-one")
            original = render_input_fixture()
            baseline = segment_render_fingerprint(
                original, segment_id="s1", **fingerprint_kwargs(asset)
            )

            changed_visual = copy.deepcopy(original)
            changed_visual["segments"][0]["visual"]["kind"] = "story_scene"
            self.assertNotEqual(
                baseline,
                segment_render_fingerprint(
                    changed_visual, segment_id="s1", **fingerprint_kwargs(asset)
                ),
            )

            changed_range = copy.deepcopy(original)
            changed_range["segments"][0]["endFrame"] = 29
            self.assertNotEqual(
                baseline,
                segment_render_fingerprint(
                    changed_range, segment_id="s1", **fingerprint_kwargs(asset)
                ),
            )

            changed_cue = copy.deepcopy(original)
            changed_cue["mouthCues"][0]["value"] = "X"
            self.assertNotEqual(
                baseline,
                segment_render_fingerprint(
                    changed_cue, segment_id="s1", **fingerprint_kwargs(asset)
                ),
            )

            changed_renderer = segment_render_fingerprint(
                original,
                segment_id="s1",
                **{**fingerprint_kwargs(asset), "renderer_source_hash": "renderer-v2"},
            )
            self.assertNotEqual(baseline, changed_renderer)

            asset.write_bytes(b"asset-one-repaired")
            self.assertNotEqual(
                baseline,
                segment_render_fingerprint(
                    original, segment_id="s1", **fingerprint_kwargs(asset)
                ),
            )

    def test_non_intersecting_mouth_cue_does_not_invalidate_target(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            asset = Path(temp_dir) / "one.png"
            asset.write_bytes(b"asset-one")
            original = render_input_fixture()
            changed = copy.deepcopy(original)
            changed["mouthCues"][2]["value"] = "X"

            self.assertEqual(
                segment_render_fingerprint(
                    original, segment_id="s1", **fingerprint_kwargs(asset)
                ),
                segment_render_fingerprint(
                    changed, segment_id="s1", **fingerprint_kwargs(asset)
                ),
            )

    def test_reusable_segment_requires_matching_complete_integrity_metadata(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            job_dir = Path(temp_dir)
            cache_dir = job_dir / "render-segments"
            cache_dir.mkdir()
            path = cache_dir / "s1-abcdef.mp4"
            path.write_bytes(b"segment")
            fingerprint = "a" * 64
            result = SegmentRenderResult(
                segment_id="s1",
                fingerprint=fingerprint,
                path=path,
                cache_hit=False,
                frame_count=30,
            )
            write_segment_checkpoint(job_dir, [result], complete=True)

            loaded = load_reusable_segment(
                job_dir,
                segment_id="s1",
                fingerprint=fingerprint,
                expected_frame_count=30,
            )
            self.assertIsNotNone(loaded)
            self.assertTrue(loaded.cache_hit)
            self.assertEqual(loaded.path, path.resolve())

            self.assertIsNone(
                load_reusable_segment(
                    job_dir, segment_id="s1", fingerprint="b" * 64, expected_frame_count=30
                )
            )

            path.write_bytes(b"")
            self.assertIsNone(
                load_reusable_segment(
                    job_dir,
                    segment_id="s1",
                    fingerprint=fingerprint,
                    expected_frame_count=30,
                )
            )

    def test_wrong_sha256_duplicate_id_path_traversal_and_incomplete_entries_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            job_dir = Path(temp_dir)
            cache_dir = job_dir / "render-segments"
            cache_dir.mkdir()
            path = cache_dir / "s1-abcdef.mp4"
            path.write_bytes(b"segment")
            fingerprint = "a" * 64
            write_segment_checkpoint(
                job_dir,
                [
                    SegmentRenderResult(
                        segment_id="s1",
                        fingerprint=fingerprint,
                        path=path,
                        cache_hit=False,
                        frame_count=30,
                    )
                ],
                complete=True,
            )

            checkpoint_path = job_dir / "segment_render_checkpoint.json"
            checkpoint = json.loads(checkpoint_path.read_text())
            checkpoint["segments"][0]["sha256"] = hashlib.sha256(b"wrong").hexdigest()
            checkpoint_path.write_text(json.dumps(checkpoint))
            self.assertIsNone(
                load_reusable_segment(job_dir, segment_id="s1", fingerprint=fingerprint)
            )

            checkpoint["segments"][0]["sha256"] = hashlib.sha256(b"segment").hexdigest()
            checkpoint["segments"].append(dict(checkpoint["segments"][0]))
            checkpoint_path.write_text(json.dumps(checkpoint))
            with self.assertRaisesRegex(ValueError, "duplicate"):
                load_reusable_segment(job_dir, segment_id="s1", fingerprint=fingerprint)

            checkpoint["segments"] = [dict(checkpoint["segments"][0])]
            checkpoint["segments"][0]["path"] = "render-segments/../outside.mp4"
            checkpoint_path.write_text(json.dumps(checkpoint))
            with self.assertRaisesRegex(ValueError, "path"):
                load_reusable_segment(job_dir, segment_id="s1", fingerprint=fingerprint)

            checkpoint["segments"][0]["path"] = "render-segments/s1-abcdef.mp4"
            checkpoint["segments"][0]["complete"] = False
            checkpoint_path.write_text(json.dumps(checkpoint))
            self.assertIsNone(
                load_reusable_segment(job_dir, segment_id="s1", fingerprint=fingerprint)
            )

    def test_checkpoint_writer_rejects_duplicate_and_unsafe_segment_ids(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            job_dir = Path(temp_dir)
            cache_dir = job_dir / "render-segments"
            cache_dir.mkdir()
            first = cache_dir / "s1.mp4"
            first.write_bytes(b"one")
            entry = SegmentRenderResult("s1", "a" * 64, first, False, 1)

            with self.assertRaisesRegex(ValueError, "duplicate"):
                write_segment_checkpoint(job_dir, [entry, entry], complete=False)

            unsafe = SegmentRenderResult("../escape", "b" * 64, first, False, 1)
            with self.assertRaisesRegex(ValueError, "segment ID"):
                write_segment_checkpoint(job_dir, [unsafe], complete=False)


if __name__ == "__main__":
    unittest.main()
