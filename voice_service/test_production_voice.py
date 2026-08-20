import os
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from voice_service.production_voice import (
    _parse_silence_intervals,
    _select_ai_gap_cuts,
    apply_production_postprocessing,
    build_production_filter,
)


class ProductionVoiceTests(unittest.TestCase):
    def test_silence_parser_drops_unmatched_tail_without_misalignment(self):
        stderr = "\n".join(
            (
                "silence_start: 0.10",
                "silence_end: 0.18 | silence_duration: 0.08",
                "silence_start: 1.90",
            )
        )
        self.assertEqual(_parse_silence_intervals(stderr), [(0.10, 0.18)])

    def test_selects_only_ai_silences_one_to_one(self):
        text = (
            "အေ အိုင် ကို သုံးပြီး အလုပ်ကို ပိုမြန်အောင် လုပ်ကြမယ်။ "
            "အေ အိုင် က ကျွန်တော်တို့ကို ကူညီပေးနိုင်ပါတယ်။"
        )
        cuts = _select_ai_gap_cuts(
            text,
            6.056479,
            [
                (0.158229, 0.221271),
                (0.983271, 1.269625),
                (2.821708, 3.633604),
                (3.767521, 3.847187),
                (4.786000, 4.842729),
            ],
        )
        self.assertEqual(len(cuts), 2)
        self.assertAlmostEqual(cuts[0][0], 0.173229, places=6)
        self.assertAlmostEqual(cuts[0][1], 0.206271, places=6)
        self.assertAlmostEqual(cuts[1][0], 3.782521, places=6)
        self.assertAlmostEqual(cuts[1][1], 3.832187, places=6)

    def test_ai_gap_selection_leaves_unrelated_silence_unchanged(self):
        self.assertEqual(
            _select_ai_gap_cuts("အေ အိုင် စမ်းမယ်", 2.0, [(1.5, 1.57)]),
            [],
        )

    def test_filter_matches_approved_order(self):
        self.assertEqual(
            build_production_filter(),
            "highpass=f=65,"
            "equalizer=f=280:width_type=q:width=1.0:g=-0.8,"
            "equalizer=f=850:width_type=q:width=1.15:g=-1.8,"
            "equalizer=f=1250:width_type=q:width=1.2:g=-0.9,"
            "equalizer=f=3400:width_type=q:width=1.0:g=0.4,"
            "volume=0.7dB,atempo=0.96",
        )

    def test_filter_rejects_unsafe_speed(self):
        for speed in (0.89, 1.11, float("inf"), float("nan")):
            with self.subTest(speed=speed):
                with self.assertRaises(ValueError):
                    build_production_filter(speed)

    def test_missing_input_is_rejected(self):
        with self.assertRaises(FileNotFoundError):
            apply_production_postprocessing("/missing/input.wav")

    @patch("voice_service.production_voice.shutil.which", return_value=None)
    def test_missing_ffmpeg_preserves_input(self, _which):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = os.path.join(temp_dir, "source.wav")
            with open(source, "wb") as handle:
                handle.write(b"original")

            with self.assertRaisesRegex(RuntimeError, "ffmpeg is unavailable"):
                apply_production_postprocessing(source)

            with open(source, "rb") as handle:
                self.assertEqual(handle.read(), b"original")

    @patch("voice_service.production_voice.shutil.which", return_value="/usr/bin/ffmpeg")
    @patch("voice_service.production_voice.subprocess.run")
    def test_in_place_success_is_atomic(self, run, _which):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = os.path.join(temp_dir, "source.wav")
            with open(source, "wb") as handle:
                handle.write(b"original")

            def create_output(command, **_kwargs):
                with open(command[-1], "wb") as handle:
                    handle.write(b"processed")
                return subprocess.CompletedProcess(command, 0)

            run.side_effect = create_output
            self.assertEqual(apply_production_postprocessing(source), source)

            with open(source, "rb") as handle:
                self.assertEqual(handle.read(), b"processed")
            command = run.call_args.args[0]
            self.assertEqual(command[0], "/usr/bin/ffmpeg")
            self.assertEqual(command[command.index("-af") + 1], build_production_filter())
            self.assertEqual(command[command.index("-c:a") + 1], "pcm_s16le")
            self.assertEqual(os.path.dirname(command[-1]), temp_dir)

    @patch("voice_service.production_voice.shutil.which", return_value="/usr/bin/ffmpeg")
    @patch("voice_service.production_voice.subprocess.run")
    def test_ffmpeg_failure_preserves_source(self, run, _which):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = os.path.join(temp_dir, "source.wav")
            with open(source, "wb") as handle:
                handle.write(b"original")
            run.side_effect = subprocess.CalledProcessError(
                1,
                ["ffmpeg"],
                stderr="bad filter",
            )

            with self.assertRaisesRegex(RuntimeError, "bad filter"):
                apply_production_postprocessing(source)

            with open(source, "rb") as handle:
                self.assertEqual(handle.read(), b"original")
            self.assertEqual(os.listdir(temp_dir), ["source.wav"])


if __name__ == "__main__":
    unittest.main()
