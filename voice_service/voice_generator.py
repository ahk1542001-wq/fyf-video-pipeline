"""Stage 2: Voice Generation Service.

Public Hackathon Release: Gemini-TTS only for natural Burmese speech synthesis.
Returns 48kHz / 24kHz WAV audio that feeds into Remotion rendering.
"""

import os
import re
from typing import List, Optional

from voice_service.gemini_tts import generate_gemini_tts as _gemini_tts
from voice_service.provider import synthesize_voice


def split_burmese_script(text: str, max_chars: int = 100) -> List[str]:
    """Split Burmese text into 2-4 second chunks.
    Splits on punctuation marks (။ ! ?) first, then falls back to length."""
    text = text.strip()
    # Insert space after Burmese full stop (။) if followed directly by non-space
    text_fixed = re.sub(r'(?<=။)(?=\S)', ' ', text)
    # Split on sentence-ending punctuation (။ ! ?) keeping the marker
    sentences = re.split(r'(?<=[။!?])\s*', text_fixed)
    sentences = [s.strip() for s in sentences if s.strip()]

    chunks = []
    current = ""
    for sentence in sentences:
        if len(current + sentence) > max_chars and current:
            chunks.append(current)
            current = sentence
        else:
            current += sentence
    if current:
        chunks.append(current)
    return chunks or [text]


def generate_voice(
    script_json: dict,
    provider: str = "gemini",
    output_path: str = "output/voice.wav",
    voice: str = "Puck",
    style: str = "mascot",
) -> str:
    """Main entry point: takes script JSON (from Writer/Producer Agent) and generates voice.

    Public release enforces provider='gemini' (Gemini-TTS).
    """
    if provider != "gemini":
        # Public hackathon release strictly defaults to Gemini-TTS
        provider = "gemini"

    segments = script_json.get("segments", [])
    if not segments:
        narration = script_json.get("title", "")
    else:
        narration = " ".join(seg.get("text", "") for seg in segments)

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

    wav_path = _gemini_tts(
        text=narration,
        voice=voice,
        style=style,
        output_path=output_path,
    )
    return wav_path
