import unittest
import os
import json
import tempfile
import wave
import subprocess
import math
from pathlib import Path
from unittest.mock import patch

from backend.mouth_cues import generate_rhubarb_mouth_cues, build_render_input


def create_mock_wav(path, duration_seconds=1.0):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(44100)
        w.writeframes(b"\x00" * int(44100 * duration_seconds * 2))

class TestLipSync(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.wav_path = Path(self.temp_dir.name) / "test.wav"
        create_mock_wav(self.wav_path, 2.0)

        self.bin_path = Path(self.temp_dir.name) / "rhubarb"
        self.bin_path.write_text("dummy")
        self.bin_path.chmod(0o755)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_safe_exact_argv_no_shell(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.stdout = json.dumps({"mouthCues": [{"start": 0.0, "end": 2.0, "value": "A"}]})
            generate_rhubarb_mouth_cues(self.wav_path, "hello", self.bin_path)

            mock_run.assert_called_once()
            args, kwargs = mock_run.call_args
            cmd = args[0]

            self.assertEqual(cmd[0], str(self.bin_path.resolve()))
            self.assertEqual(cmd[1:5], ["-r", "phonetic", "-f", "json"])
            self.assertEqual(cmd[5], "-d")
            # cmd[6] is the dialog path
            self.assertEqual(cmd[7], str(self.wav_path.resolve()))

            self.assertFalse(kwargs.get("shell"))
            self.assertTrue(kwargs.get("check"))
            self.assertTrue(kwargs.get("capture_output"))
            self.assertTrue(kwargs.get("text"))
            self.assertEqual(kwargs.get("timeout"), 120)

    def test_dialog_content_not_argv_utf8(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.stdout = json.dumps({"mouthCues": [{"start": 0.0, "end": 2.0, "value": "A"}]})
            dialog = "မင်္ဂလာပါ" # Burmese

            # Use patch to capture temp_dir path instead of just subprocess.run args
            # tempfile cleanup deletes the file before assert exists, so we intercept the call
            def fake_run(*args, **kwargs):
                cmd = args[0]
                dialog_file = cmd[6]
                self.assertTrue(Path(dialog_file).exists())
                with open(dialog_file, "r", encoding="utf-8") as f:
                    self.assertEqual(f.read(), dialog)
                mock_res = unittest.mock.Mock()
                mock_res.stdout = json.dumps({"mouthCues": [{"start": 0.0, "end": 2.0, "value": "A"}]})
                return mock_res

            mock_run.side_effect = fake_run
            generate_rhubarb_mouth_cues(self.wav_path, dialog, self.bin_path)

    def test_initial_middle_final_x_gap_and_coalesce(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.stdout = json.dumps({
                "mouthCues": [
                    {"start": 0.5, "end": 1.0, "value": "A"},
                    {"start": 1.5, "end": 1.8, "value": "A"}
                ]
            })
            cues = generate_rhubarb_mouth_cues(self.wav_path, None, self.bin_path)

            self.assertEqual(len(cues), 5)
            self.assertEqual(cues[0], {"start": 0.0, "end": 0.5, "value": "X"})
            self.assertEqual(cues[1], {"start": 0.5, "end": 1.0, "value": "A"})
            self.assertEqual(cues[2], {"start": 1.0, "end": 1.5, "value": "X"})
            self.assertEqual(cues[3], {"start": 1.5, "end": 1.8, "value": "A"})
            self.assertEqual(cues[4], {"start": 1.8, "end": 2.0, "value": "X"})

    def test_coalesce_adjacent_same_values(self):
         with patch("subprocess.run") as mock_run:
            mock_run.return_value.stdout = json.dumps({
                "mouthCues": [
                    {"start": 0.0, "end": 1.0, "value": "A"},
                    {"start": 1.0, "end": 2.0, "value": "A"}
                ]
            })
            cues = generate_rhubarb_mouth_cues(self.wav_path, None, self.bin_path)
            self.assertEqual(len(cues), 1)
            self.assertEqual(cues[0], {"start": 0.0, "end": 2.0, "value": "A"})

    def test_unknown_value(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.stdout = json.dumps({"mouthCues": [{"start": 0.0, "end": 2.0, "value": "Y"}]})
            with self.assertRaisesRegex(ValueError, "Unknown mouth cue value"):
                generate_rhubarb_mouth_cues(self.wav_path, None, self.bin_path)

    def test_nan_inf_bool(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.stdout = json.dumps({"mouthCues": [{"start": True, "end": 2.0, "value": "A"}]})
            with self.assertRaisesRegex(ValueError, "bool is not numeric"):
                generate_rhubarb_mouth_cues(self.wav_path, None, self.bin_path)

            mock_run.return_value.stdout = json.dumps({"mouthCues": [{"start": math.inf, "end": 2.0, "value": "A"}]})
            with self.assertRaisesRegex(ValueError, "non-finite"):
                generate_rhubarb_mouth_cues(self.wav_path, None, self.bin_path)

    def test_overlap(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.stdout = json.dumps({
                "mouthCues": [
                    {"start": 0.0, "end": 1.0, "value": "A"},
                    {"start": 0.5, "end": 2.0, "value": "B"}
                ]
            })
            with self.assertRaisesRegex(ValueError, "Overlapping"):
                generate_rhubarb_mouth_cues(self.wav_path, None, self.bin_path)

    def test_beyond_duration(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.stdout = json.dumps({"mouthCues": [{"start": 0.0, "end": 3.0, "value": "A"}]})
            with self.assertRaisesRegex(ValueError, "beyond duration"):
                generate_rhubarb_mouth_cues(self.wav_path, None, self.bin_path)

    def test_clamp_small_tolerance(self):
         with patch("subprocess.run") as mock_run:
            mock_run.return_value.stdout = json.dumps({"mouthCues": [{"start": 0.0, "end": 2.0005, "value": "A"}]})
            cues = generate_rhubarb_mouth_cues(self.wav_path, None, self.bin_path)
            self.assertEqual(cues[-1]["end"], 2.0)

    def test_non_dict_json_cue(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.stdout = json.dumps(["not", "a", "dict"])
            with self.assertRaisesRegex(ValueError, "not a dictionary"):
                generate_rhubarb_mouth_cues(self.wav_path, None, self.bin_path)

            mock_run.return_value.stdout = json.dumps({"mouthCues": [["start", 0.0]]})
            with self.assertRaisesRegex(ValueError, "cue is not a dict"):
                generate_rhubarb_mouth_cues(self.wav_path, None, self.bin_path)

            mock_run.return_value.stdout = json.dumps({"mouthCues": {}})
            with self.assertRaisesRegex(ValueError, "missing mouthCues list"):
                generate_rhubarb_mouth_cues(self.wav_path, None, self.bin_path)

    def test_missing_relative_non_executable_binary(self):
        with self.assertRaisesRegex(ValueError, "not found"):
            generate_rhubarb_mouth_cues(self.wav_path, None, str(self.temp_dir.name) + "/nonexistent")

        non_exec = Path(self.temp_dir.name) / "nonexec"
        non_exec.write_text("test")
        with self.assertRaisesRegex(ValueError, "not executable"):
            generate_rhubarb_mouth_cues(self.wav_path, None, non_exec)

        with self.assertRaisesRegex(ValueError, "must be absolute"):
            generate_rhubarb_mouth_cues("relative.wav", None, self.bin_path)

        with self.assertRaisesRegex(ValueError, "must be absolute"):
            generate_rhubarb_mouth_cues(self.wav_path, None, "relative/rhubarb")

    def test_timeout(self):
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(["cmd"], 120)):
            with self.assertRaisesRegex(ValueError, "execution failed"):
                generate_rhubarb_mouth_cues(self.wav_path, None, self.bin_path)

        with self.assertRaisesRegex(ValueError, "timeout_seconds must be positive"):
            generate_rhubarb_mouth_cues(self.wav_path, None, self.bin_path, timeout_seconds=0)

    def test_empty_mouth_cues_rejection(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.stdout = json.dumps({"mouthCues": []})
            with self.assertRaisesRegex(ValueError, "No mouth cues generated"):
                generate_rhubarb_mouth_cues(self.wav_path, None, self.bin_path)

    def test_dialog_validation(self):
        with self.assertRaisesRegex(ValueError, "dialog_text must be a string"):
            generate_rhubarb_mouth_cues(self.wav_path, 123, self.bin_path)

        with self.assertRaisesRegex(ValueError, "dialog_text cannot be empty"):
            generate_rhubarb_mouth_cues(self.wav_path, "   ", self.bin_path)

    def test_build_render_input_successful_source(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.stdout = json.dumps({"mouthCues": [{"start": 0.0, "end": 2.0, "value": "A"}]})
            script_data = {"title": "Test", "segments": [{"text": "hello", "scene_type": "whiteboard", "mascot_action": "present", "emotion": "neutral", "emphasis": []}]}

            res = build_render_input(script_data, self.wav_path, rhubarb_bin=self.bin_path)
            self.assertEqual(res["mouthCueSource"], "rhubarb-phonetic")
            self.assertEqual(res["mouthCues"][0]["value"], "A")

            # Verify default timeout of 300 is forwarded
            mock_run.assert_called_once()
            _, kwargs = mock_run.call_args
            self.assertEqual(kwargs.get("timeout"), 300)

    def test_build_render_input_timeout_env_override(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.stdout = json.dumps({"mouthCues": [{"start": 0.0, "end": 2.0, "value": "A"}]})
            script_data = {"title": "Test", "segments": [{"text": "hello"}]}

            with patch.dict(os.environ, {"RHUBARB_TIMEOUT_SECONDS": "400"}):
                build_render_input(script_data, self.wav_path, rhubarb_bin=self.bin_path)

            mock_run.assert_called_once()
            _, kwargs = mock_run.call_args
            self.assertEqual(kwargs.get("timeout"), 400)

    def test_build_render_input_timeout_invalid_env_default(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.stdout = json.dumps({"mouthCues": [{"start": 0.0, "end": 2.0, "value": "A"}]})
            script_data = {"title": "Test", "segments": [{"text": "hello"}]}

            with patch.dict(os.environ, {"RHUBARB_TIMEOUT_SECONDS": "not_an_int"}):
                build_render_input(script_data, self.wav_path, rhubarb_bin=self.bin_path)

            mock_run.assert_called_once()
            _, kwargs = mock_run.call_args
            self.assertEqual(kwargs.get("timeout"), 300)

    def test_build_render_input_timeout_explicit_override_and_bounds(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.stdout = json.dumps({"mouthCues": [{"start": 0.0, "end": 2.0, "value": "A"}]})
            script_data = {"title": "Test", "segments": [{"text": "hello"}]}

            # Explicit arg overrides env var
            with patch.dict(os.environ, {"RHUBARB_TIMEOUT_SECONDS": "400"}):
                build_render_input(script_data, self.wav_path, rhubarb_bin=self.bin_path, rhubarb_timeout_seconds=500)
                _, kwargs = mock_run.call_args
                self.assertEqual(kwargs.get("timeout"), 500)

            # Clamping lower bound
            mock_run.reset_mock()
            build_render_input(script_data, self.wav_path, rhubarb_bin=self.bin_path, rhubarb_timeout_seconds=10)
            _, kwargs = mock_run.call_args
            self.assertEqual(kwargs.get("timeout"), 30)

            # Clamping upper bound
            mock_run.reset_mock()
            build_render_input(script_data, self.wav_path, rhubarb_bin=self.bin_path, rhubarb_timeout_seconds=2000)
            _, kwargs = mock_run.call_args
            self.assertEqual(kwargs.get("timeout"), 1800)

    def test_build_render_input_timeout_invalid_explicit_arg(self):
        script_data = {"title": "Test", "segments": [{"text": "hello"}]}
        with self.assertRaisesRegex(ValueError, "rhubarb_timeout_seconds must be an integer"):
            build_render_input(script_data, self.wav_path, rhubarb_bin=self.bin_path, rhubarb_timeout_seconds="400")

    def test_build_render_input_no_config_failure_fallback(self):
        script_data = {"title": "Test", "segments": [{"text": "hello", "scene_type": "whiteboard", "mascot_action": "present", "emotion": "neutral", "emphasis": []}]}

        # no config
        res = build_render_input(script_data, self.wav_path, rhubarb_bin=None)
        self.assertEqual(res["mouthCueSource"], "burmese-text-audio")

        # failure fallback
        with patch("subprocess.run", side_effect=OSError("failed")):
            res = build_render_input(script_data, self.wav_path, rhubarb_bin=self.bin_path)
            self.assertEqual(res["mouthCueSource"], "burmese-text-audio")

if __name__ == '__main__':
    unittest.main()
