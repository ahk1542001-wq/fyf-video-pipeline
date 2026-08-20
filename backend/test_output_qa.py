import os
import json
import tempfile
import unittest
from unittest.mock import patch
from backend.output_qa import qa_job_directory, TOLERANCE_SECONDS

class TestOutputQA(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.job_dir = self.temp_dir.name
        self.audio_patch = patch(
            "backend.output_qa.analyze_pcm16_wav",
            return_value={"peak_dbfs": -3.0, "full_scale_samples": 0},
        )
        self.mock_audio_analysis = self.audio_patch.start()

    def tearDown(self):
        self.audio_patch.stop()
        self.temp_dir.cleanup()

    def create_mock_file(self, filename, content=None, is_json=False):
        path = os.path.join(self.job_dir, filename)
        if content is None:
            content = "dummy data"
        if is_json:
            with open(path, "w") as f:
                json.dump(content, f)
        else:
            with open(path, "w") as f:
                f.write(content)
        return path

    def setup_valid_files(self):
        self.create_mock_file("voice.wav", "audio data")
        self.create_mock_file("video.mp4", "video data")
        self.create_mock_file("script.json", {"id": "1", "text": "hello"}, is_json=True)
        self.create_mock_file("render_input.json", {"id": "1", "text": "hello"}, is_json=True)
        self.create_mock_file("mouth_cues.json", [{"start": 0.0, "end": 1.0, "value": "A"}], is_json=True)

    @patch("backend.output_qa._get_ffprobe_info")
    def test_all_checks_pass(self, mock_ffprobe):
        self.setup_valid_files()

        def mock_probe(filepath):
            if "voice.wav" in filepath:
                return {"format": {"duration": "2.0"}, "streams": [{"codec_type": "audio"}]}
            elif "video.mp4" in filepath:
                return {"format": {"duration": "2.0"}, "streams": [{"codec_type": "video"}, {"codec_type": "audio"}]}
            return {}

        mock_ffprobe.side_effect = mock_probe

        report = qa_job_directory(self.job_dir)
        self.assertTrue(report["passed"])
        self.assertEqual(len(report["failure_codes"]), 0)
        self.assertEqual(report["metrics"]["voice_duration"], 2.0)
        self.assertEqual(report["metrics"]["video_duration"], 2.0)

    def test_missing_files(self):
        self.create_mock_file("voice.wav", "audio")
        # missing video.mp4, etc.
        report = qa_job_directory(self.job_dir)
        self.assertFalse(report["passed"])
        self.assertIn("MISSING_VIDEO", report["failure_codes"])

    @patch("backend.output_qa._get_ffprobe_info")
    def test_video_too_short(self, mock_ffprobe):
        self.setup_valid_files()

        def mock_probe(filepath):
            if "voice.wav" in filepath:
                return {"format": {"duration": "5.0"}, "streams": [{"codec_type": "audio"}]}
            elif "video.mp4" in filepath:
                return {"format": {"duration": "1.0"}, "streams": [{"codec_type": "video"}, {"codec_type": "audio"}]}
            return {}

        mock_ffprobe.side_effect = mock_probe

        report = qa_job_directory(self.job_dir)
        self.assertFalse(report["passed"])
        self.assertIn("VIDEO_TOO_SHORT", report["failure_codes"])

    @patch("backend.output_qa._get_ffprobe_info")
    def test_mouth_cues_out_of_bounds(self, mock_ffprobe):
        self.create_mock_file("voice.wav", "audio data")
        self.create_mock_file("video.mp4", "video data")
        self.create_mock_file("script.json", {"id": "1", "text": "hello"}, is_json=True)
        self.create_mock_file("render_input.json", {"id": "1", "text": "hello"}, is_json=True)
        # Mouth cue extends beyond voice duration + tolerance
        self.create_mock_file("mouth_cues.json", [{"start": 0.0, "end": 10.0, "value": "A"}], is_json=True)

        def mock_probe(filepath):
            if "voice.wav" in filepath:
                return {"format": {"duration": "2.0"}, "streams": [{"codec_type": "audio"}]}
            elif "video.mp4" in filepath:
                return {"format": {"duration": "2.0"}, "streams": [{"codec_type": "video"}, {"codec_type": "audio"}]}
            return {}

        mock_ffprobe.side_effect = mock_probe

        report = qa_job_directory(self.job_dir)
        self.assertFalse(report["passed"])
        self.assertIn("MOUTH_CUES_OUT_OF_BOUNDS", report["failure_codes"])

    @patch("backend.output_qa._get_ffprobe_info")
    def test_phonetic_source_requires_viseme_variety(self, mock_ffprobe):
        self.setup_valid_files()
        self.create_mock_file(
            "mouth_cues.json",
            [{"start": 0.0, "end": 2.0, "value": "A"}],
            is_json=True,
        )
        self.create_mock_file(
            "render_input.json",
            {"id": "1", "text": "hello", "mouthCueSource": "burmese-text-audio"},
            is_json=True,
        )
        mock_ffprobe.side_effect = lambda filepath: {
            "format": {"duration": "2.0"},
            "streams": [{"codec_type": "audio"}] if "voice.wav" in filepath else [{"codec_type": "video"}, {"codec_type": "audio"}],
        }
        report = qa_job_directory(self.job_dir)
        self.assertFalse(report["passed"])
        self.assertIn("MOUTH_CUES_LOW_VARIETY", report["failure_codes"])

    @patch("backend.output_qa._get_ffprobe_info")
    def test_duplicate_render_ids(self, mock_ffprobe):
        self.setup_valid_files()
        # Overwrite with duplicates
        self.create_mock_file("render_input.json", [{"id": "1", "text": "hello"}, {"id": "1", "text": "hello"}], is_json=True)

        def mock_probe(filepath):
            if "voice.wav" in filepath:
                return {"format": {"duration": "2.0"}, "streams": [{"codec_type": "audio"}]}
            elif "video.mp4" in filepath:
                return {"format": {"duration": "2.0"}, "streams": [{"codec_type": "video"}, {"codec_type": "audio"}]}
            return {}

        mock_ffprobe.side_effect = mock_probe

        report = qa_job_directory(self.job_dir)
        self.assertFalse(report["passed"])
        self.assertIn("RENDER_HAS_DUPLICATES", report["failure_codes"])

    @patch("backend.output_qa._get_ffprobe_info")
    def test_segment_mismatch(self, mock_ffprobe):
        self.setup_valid_files()
        # Overwrite with mismatch
        self.create_mock_file("script.json", {"id": "1", "text": "hello"}, is_json=True)
        self.create_mock_file("render_input.json", {"id": "1", "text": "world"}, is_json=True)

        def mock_probe(filepath):
            if "voice.wav" in filepath:
                return {"format": {"duration": "2.0"}, "streams": [{"codec_type": "audio"}]}
            elif "video.mp4" in filepath:
                return {"format": {"duration": "2.0"}, "streams": [{"codec_type": "video"}, {"codec_type": "audio"}]}
            return {}

        mock_ffprobe.side_effect = mock_probe

        report = qa_job_directory(self.job_dir)
        self.assertFalse(report["passed"])
        self.assertIn("SEGMENTS_MISMATCH", report["failure_codes"])

    @patch("backend.output_qa._get_ffprobe_info")
    def test_visual_fallback_metrics_warn_without_rejecting_verified_diversity(self, mock_ffprobe):
        self.setup_valid_files()
        shots = [{
            "fallback_used": index < 5,
            "media_type": "generated_image" if index < 2 else "motion_graphic",
        } for index in range(10)]
        segments = [{"id": str(i), "text": f"text {i}", "visual": {"evidence_shots": [shot]}} for i, shot in enumerate(shots)]
        self.create_mock_file("script.json", {"segments": segments}, is_json=True)
        self.create_mock_file("render_input.json", {"segments": segments}, is_json=True)
        mock_ffprobe.side_effect = lambda filepath: {
            "format": {"duration": "2.0"},
            "streams": ([{"codec_type": "audio"}] if "voice.wav" in filepath else [{"codec_type": "video"}, {"codec_type": "audio"}]),
        }
        report = qa_job_directory(self.job_dir)
        self.assertTrue(report["passed"])
        self.assertEqual(report["metrics"]["visual_fallback_ratio"], 0.5)
        self.assertIn("VISUAL_FALLBACK_RATIO_HIGH", report["warnings"])

    @patch("backend.output_qa._get_ffprobe_info")
    def test_long_form_requires_two_generated_media_shots(self, mock_ffprobe):
        self.setup_valid_files()
        segments = [{
            "id": str(i), "text": f"text {i}",
            "visual": {"evidence_shots": [{"fallback_used": True, "media_type": "motion_graphic"}]},
        } for i in range(8)]
        self.create_mock_file("script.json", {"segments": segments}, is_json=True)
        self.create_mock_file("render_input.json", {"segments": segments}, is_json=True)
        mock_ffprobe.side_effect = lambda filepath: {
            "format": {"duration": "2.0"},
            "streams": ([{"codec_type": "audio"}] if "voice.wav" in filepath else [{"codec_type": "video"}, {"codec_type": "audio"}]),
        }
        report = qa_job_directory(self.job_dir)
        self.assertFalse(report["passed"])
        self.assertIn("VISUAL_MEDIA_DIVERSITY_LOW", report["failure_codes"])

    @patch("backend.output_qa._get_ffprobe_info")
    def test_clipped_voice_fails_audio_headroom_gate(self, mock_ffprobe):
        self.setup_valid_files()
        self.mock_audio_analysis.return_value = {
            "peak_dbfs": 0.0,
            "full_scale_samples": 59,
        }
        mock_ffprobe.side_effect = lambda filepath: {
            "format": {"duration": "2.0"},
            "streams": ([{"codec_type": "audio"}] if "voice.wav" in filepath else [{"codec_type": "video"}, {"codec_type": "audio"}]),
        }

        report = qa_job_directory(self.job_dir)

        self.assertFalse(report["passed"])
        self.assertIn("VOICE_FULL_SCALE_CLIPPING", report["failure_codes"])
        self.assertIn("VOICE_PEAK_HEADROOM_LOW", report["failure_codes"])
        self.assertEqual(report["metrics"]["voice_full_scale_samples"], 59)

if __name__ == "__main__":
    unittest.main()
