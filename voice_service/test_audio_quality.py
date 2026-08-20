import math
import tempfile
import unittest
import wave
from pathlib import Path

from voice_service.audio_quality import analyze_pcm16_wav, master_voice_audio


def _write_tone(path: Path, *, amplitude: int, seconds: float = 0.25) -> None:
    sample_rate = 24000
    frames = []
    for index in range(int(sample_rate * seconds)):
        value = int(amplitude * math.sin(2 * math.pi * 440 * index / sample_rate))
        frames.append(value.to_bytes(2, "little", signed=True))
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(b"".join(frames))


class TestAudioQuality(unittest.TestCase):
    def test_clean_pcm16_is_not_rewritten(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "clean.wav"
            _write_tone(path, amplitude=12000)
            before = path.read_bytes()

            result = master_voice_audio(path)

            self.assertFalse(result["changed"])
            self.assertEqual(path.read_bytes(), before)
            self.assertLessEqual(result["after"]["peak_dbfs"], -1.0)
            self.assertEqual(result["after"]["full_scale_samples"], 0)

    def test_clipped_pcm16_is_mastered_with_headroom_and_stable_duration(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "clipped.wav"
            _write_tone(path, amplitude=32767, seconds=0.5)
            before = analyze_pcm16_wav(path)
            self.assertGreater(before["full_scale_samples"], 0)

            result = master_voice_audio(path)
            after = analyze_pcm16_wav(path)

            self.assertTrue(result["changed"])
            self.assertEqual(after["full_scale_samples"], 0)
            self.assertLessEqual(after["peak_dbfs"], -1.0)
            self.assertAlmostEqual(after["duration_seconds"], before["duration_seconds"], places=2)


if __name__ == "__main__":
    unittest.main()
