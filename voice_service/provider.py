"""Public Gemini-only Voice Synthesis Provider."""

from __future__ import annotations

import os
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import List, Literal

from voice_service.gemini_tts import generate_gemini_tts


@dataclass(frozen=True)
class VoiceResult:
    provider: Literal["gemini"]
    output_path: Path
    duration_seconds: float
    character_count: int
    request_count: int


def _get_wav_duration(wav_path: Path) -> float:
    """Read duration in seconds from a PCM WAV file."""
    try:
        with wave.open(str(wav_path), "rb") as handle:
            frames = handle.getnframes()
            rate = handle.getframerate()
            return frames / rate if rate > 0 else 0.0
    except (wave.Error, OSError, ZeroDivisionError):
        return 0.0


def synthesize_voice(
    chunks: List[str],
    *,
    language: str = "my-MM",
    output_path: Path,
    voice: str = "Puck",
    style: str = "mascot",
) -> VoiceResult:
    """Synthesize voice chunks using Gemini TTS only for public hackathon release."""
    if not chunks:
        raise ValueError("At least one text chunk is required for voice synthesis")

    full_text = " ".join(chunk.strip() for chunk in chunks if chunk.strip())
    character_count = sum(len(c) for c in chunks)

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Generate TTS audio via Gemini
    generated_path_str = generate_gemini_tts(
        text=full_text,
        voice=voice,
        style=style,
        output_path=str(output_path),
    )
    generated_path = Path(generated_path_str)

    # If the output path was changed (e.g. mp3 -> wav), ensure it matches
    if generated_path != output_path and generated_path.exists():
        if output_path.suffix == ".wav" and generated_path.suffix == ".wav":
            output_path = generated_path

    duration_seconds = _get_wav_duration(output_path)

    return VoiceResult(
        provider="gemini",
        output_path=output_path,
        duration_seconds=duration_seconds,
        character_count=character_count,
        request_count=1,
    )
