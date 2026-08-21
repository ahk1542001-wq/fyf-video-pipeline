"""Tests for public Gemini voice synthesis provider."""

from pathlib import Path
from unittest.mock import patch
import wave

from voice_service.provider import VoiceResult, synthesize_voice


def _create_dummy_wav(path: Path, duration_seconds: float = 1.0, sample_rate: int = 24000):
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        frame_count = int(duration_seconds * sample_rate)
        w.writeframes(b"\x00\x00" * frame_count)


def test_voice_result_structure():
    result = VoiceResult(
        provider="gemini",
        output_path=Path("/tmp/test.wav"),
        duration_seconds=3.5,
        character_count=42,
        request_count=1,
    )
    assert result.provider == "gemini"
    assert result.duration_seconds == 3.5
    assert result.character_count == 42
    assert result.request_count == 1


@patch("voice_service.provider.generate_gemini_tts")
def test_synthesize_voice_delegates_to_gemini(mock_gemini, tmp_path):
    out_wav = tmp_path / "voice.wav"

    def mock_tts_impl(text, voice, style, output_path):
        _create_dummy_wav(Path(output_path), duration_seconds=2.0)
        return str(output_path)

    mock_gemini.side_effect = mock_tts_impl

    chunks = ["မင်္ဂလာပါ ခင်ဗျာ။", "ဒါကတော့ FYF Video Pipeline စမ်းသပ်မှု ဖြစ်ပါတယ်။"]
    result = synthesize_voice(chunks, language="my-MM", output_path=out_wav)

    assert result.provider == "gemini"
    assert result.output_path == out_wav
    assert result.duration_seconds == 2.0
    assert result.character_count == sum(len(c) for c in chunks)
    assert result.request_count == 1
    mock_gemini.assert_called_once()
