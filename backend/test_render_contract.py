import copy
import os
import tempfile
import unittest
import wave

from backend.render_contract import validate_render_input


def valid_input() -> dict:
    return {
        "fps": 30,
        "durationInFrames": 90,
        "audioSrc": "voice.wav",
        "segments": [
            {
                "startFrame": 0,
                "endFrame": 45,
                "text": "Warehouse and system inventory differ.",
                "visual": {
                    "kind": "inventory_mismatch",
                    "physical_stock": 12,
                    "system_stock": 2,
                    "phase": "alert",
                    "screen_text": ["Warehouse 12 items", "System 2 items"],
                    "evidence_claims": [{"claim_id": "c1"}],
                    "evidence_shots": [{"proves_claim_ids": ["c1"], "verification_status": "passed", "asset_path": "job-visuals/s1.png"}],
                },
            },
            {
                "startFrame": 45,
                "endFrame": 90,
                "text": "A person reviews the evidence.",
                "visual": {
                    "kind": "approval_gate",
                    "phase": "in_progress",
                    "screen_text": ["Human review"],
                    "evidence_claims": [{"claim_id": "c2"}],
                    "evidence_shots": [{"proves_claim_ids": ["c2"], "verification_status": "passed", "asset_path": "job-visuals/s2.png"}],
                },
            },
        ],
        "mouthCues": [
            {"start": 0.0, "end": 1.0, "value": "A"},
            {"start": 1.0, "end": 2.0, "value": "X"},
        ],
    }


def write_wav(path: str, seconds: float, rate: int = 16_000) -> None:
    with wave.open(path, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(rate)
        wav_file.writeframes(b"\x00\x00" * round(seconds * rate))


def write_visuals(job_dir: str) -> None:
    visuals = os.path.join(job_dir, "visuals")
    os.makedirs(visuals, exist_ok=True)
    for name in ("s1.png", "s2.png"):
        with open(os.path.join(visuals, name), "wb") as handle:
            handle.write(b"png")


class RenderContractTests(unittest.TestCase):
    def assert_invalid(self, mutate, match: str) -> None:
        data = copy.deepcopy(valid_input())
        mutate(data)
        with self.assertRaisesRegex(ValueError, match):
            validate_render_input(data)

    def test_valid_input(self):
        validate_render_input(valid_input())

    def test_positive_integer_top_level_fields(self):
        self.assert_invalid(lambda d: d.update(fps=True), "fps")
        self.assert_invalid(lambda d: d.update(durationInFrames=0), "durationInFrames")

    def test_audio_src_required(self):
        self.assert_invalid(lambda d: d.update(audioSrc="  "), "audioSrc")

    def test_segments_must_be_contiguous_and_cover_duration(self):
        self.assert_invalid(lambda d: d["segments"][1].update(startFrame=46), "contiguous")
        self.assert_invalid(lambda d: d["segments"][1].update(endFrame=89), "cover durationInFrames")

    def test_typed_visual_and_screen_text_required(self):
        self.assert_invalid(lambda d: d["segments"][0].pop("visual"), "typed visual")
        self.assert_invalid(lambda d: d["segments"][0]["visual"].update(kind="unknown"), "unknown visual kind")
        self.assert_invalid(lambda d: d["segments"][0]["visual"].update(screen_text=["", "ok"]), "non-blank")
        self.assert_invalid(lambda d: d["segments"][0]["visual"].update(screen_text=["a", "b", "c"]), "1 or 2")

    def test_screen_text_cannot_duplicate_narration(self):
        self.assert_invalid(
            lambda d: d["segments"][1]["visual"].update(screen_text=[d["segments"][1]["text"]]),
            "duplicates narration",
        )

    def test_in_progress_cannot_show_completion_ui(self):
        self.assert_invalid(lambda d: d["segments"][1]["visual"].update(completion_ui=True), "completion_ui")

    def test_inventory_values_must_be_positive_and_visible_as_standalone_numbers(self):
        self.assert_invalid(lambda d: d["segments"][0]["visual"].update(physical_stock=0), "positive integer")
        self.assert_invalid(
            lambda d: d["segments"][0]["visual"].update(screen_text=["Warehouse 120 items", "System 2 items"]),
            "standalone labeled number",
        )

    def test_correction_values_must_be_visible(self):
        data = valid_input()
        data["segments"][0]["visual"] = {
            "kind": "inventory_correction",
            "from_value": 2,
            "to_value": 12,
            "phase": "in_progress",
            "screen_text": ["Before 2", "After 12"],
        }
        validate_render_input(data)
        data["segments"][0]["visual"]["screen_text"] = ["Before two", "After twelve"]
        with self.assertRaisesRegex(ValueError, "standalone labeled number"):
            validate_render_input(data)

    def test_mouth_cues_are_strictly_valid(self):
        self.assert_invalid(lambda d: d["mouthCues"][1].update(start=0.5), "overlap")
        self.assert_invalid(lambda d: d["mouthCues"][0].update(value="Y"), "invalid value")
        self.assert_invalid(lambda d: d["mouthCues"][1].update(end=3.1), "after video duration")

    def test_wav_duration_matches_within_one_frame(self):
        with tempfile.TemporaryDirectory() as job_dir:
            audio_path = os.path.join(job_dir, "voice.wav")
            write_visuals(job_dir)
            write_wav(audio_path, 3.0)
            validate_render_input(valid_input(), job_dir=job_dir)
            write_wav(audio_path, 3.2)
            with self.assertRaisesRegex(ValueError, "within one frame"):
                validate_render_input(valid_input(), job_dir=job_dir)

    def test_unreadable_wav_fails_closed(self):
        with tempfile.TemporaryDirectory() as job_dir:
            write_visuals(job_dir)
            with open(os.path.join(job_dir, "voice.wav"), "wb") as handle:
                handle.write(b"not wav")
            with self.assertRaisesRegex(ValueError, "readable WAV"):
                validate_render_input(valid_input(), job_dir=job_dir)


if __name__ == "__main__":
    unittest.main()
