"""
Stage 2: Voice Generation Service
Two voice options:
1. VoxCPM2 (self-hosted reference-voice route) - Burmese
2. Google Chirp 3 HD (fallback) - Google-native Burmese voice

Both return 48kHz WAV audio that feeds into Remotion.
"""

import os
import json
import wave
import struct
import asyncio
import requests
from typing import Optional

# Gemini-TTS provider
from voice_service.gemini_tts import generate_gemini_tts as _gemini_tts

# === Configuration ===
VOXCPM_API_URL = os.getenv("VOXCPM_API_URL", "http://localhost:8000/v1/audio/speech")
GOOGLE_TTS_API_URL = "https://texttospeech.googleapis.com/v1/text:synthesize"
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY", "")
# Optional approved local reference voice for the self-hosted/partner route.
# Keep the value in the ignored local .env; the public example stays blank.
VOICE_REFERENCE = os.getenv(
    "VOICE_REFERENCE",
    ""
)

# Chunking parameters (from the production VoxCPM2 workflow)
MAX_CHUNK_SECONDS = 4.0  # Keep chunks under 4s to avoid voice drift
INTER_CHUNK_SILENCE = 0.25  # seconds
INTRO_PAD = 0.3  # seconds


def split_burmese_script(text: str, max_chars: int = 100) -> list[str]:
    """Split Burmese text into 2-4 second chunks.
    Splits on punctuation marks (။ ! ?) first, then falls back to length."""
    import re
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


def synthesize_voxcpm(chunks: list[str], reference_wav: str = VOICE_REFERENCE) -> bytes:
    """Call self-hosted VoxCPM2 via OpenAI-compatible vLLM-Omni API.
    Returns concatenated 48kHz WAV bytes with inter-chunk silence."""

    # vLLM-Omni exposes OpenAI-compatible /v1/audio/speech
    payload = {
        "model": "openbmb/VoxCPM2",
        "input": " ".join(chunks),  # vLLM-Omni handles chunking server-side
        "voice": reference_wav,     # approved reference path/URL when configured
        "response_format": "wav",
    }

    try:
        response = requests.post(VOXCPM_API_URL, json=payload, timeout=300)
        response.raise_for_status()
        return response.content
    except Exception as e:
        print(f"[Voice] VoxCPM2 error: {e}")
        raise


def synthesize_google(chunks: list[str], voice_name: str = "my-MM-Standard-A") -> bytes:
    """Call Google Cloud TTS (Chirp 3 HD) as fallback.
    Returns 48kHz WAV bytes."""

    # Build SSML with the full text
    text = " ".join(chunks)
    payload = {
        "audioConfig": {
            "audioEncoding": "LINEAR16",
            "sampleRateHertz": 48000,
        },
        "voice": {
            "languageCode": "my-MM",
            "name": voice_name,
        },
        "input": {"text": text},
    }

    url = f"{GOOGLE_TTS_API_URL}?key={GOOGLE_API_KEY}"

    try:
        response = requests.post(url, json=payload, timeout=60)
        response.raise_for_status()
        # Google returns base64-encoded audio content
        import base64
        audio_b64 = response.json().get("audioContent", "")
        return base64.b64decode(audio_b64)
    except Exception as e:
        print(f"[Voice] Google TTS error: {e}")
        raise


def wav_bytes_to_file(wav_data: bytes, output_path: str) -> str:
    """Save WAV bytes to file."""
    with open(output_path, "wb") as f:
        f.write(wav_data)
    return output_path


def generate_voice(
    script_json: dict,
    provider: str = "voxcpm",
    output_path: str = "output/voice.wav"
) -> str:
    """Main entry point: takes script JSON (from Writer Agent) and generates voice.

    provider:
      "voxcpm"  — VoxCPM2 via local/self-hosted vLLM API
      "google"  — Google Cloud TTS (no native Burmese; fallback only)
      "gemini"  — Gemini-TTS mascot voice (Puck + cute style) [DEMO]
      "kaggle"  — optional VoxCPM2 partner route on Kaggle GPU [HYBRID]
    """
    # Extract the narration text from the script JSON
    # The Writer Agent outputs segments with text; join them
    segments = script_json.get("segments", [])
    if not segments:
        # Fallback: maybe script_json has title only
        narration = script_json.get("title", "")
    else:
        narration = " ".join(seg.get("text", "") for seg in segments)

    # Split into manageable chunks
    chunks = split_burmese_script(narration)
    print(f"[Voice] Generated {len(chunks)} chunks from script")

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

    if provider == "voxcpm":
        audio = synthesize_voxcpm(chunks)
    elif provider == "google":
        audio = synthesize_google(chunks)
    elif provider == "gemini":
        # Use Gemini-TTS with FYF Mascot voice (Puck + cute style)
        wav_path = _gemini_tts(
            text=narration,
            voice="Puck",         # Mascot voice
            style="mascot",       # Cute playful style
            output_path=output_path,
        )
        return wav_path
    elif provider == "kaggle":
        # Optional approved reference-voice route via Kaggle GPU.
        from voice_service.kaggle_runner import generate_voice_on_kaggle
        return generate_voice_on_kaggle(
            script_text=narration,
            reference_path=VOICE_REFERENCE,
            output_path=output_path,
        )
    else:
        raise ValueError(f"Unknown provider: {provider}")

    return wav_bytes_to_file(audio, output_path)


if __name__ == "__main__":
    # Test with a sample script
    sample = {
        "title": "AI ဆိုတာ ဘာလဲ",
        "segments": [
            {"startFrame": 0, "endFrame": 30, "text": "AI ဆိုတာ ဘာလဲ။", "scene_type": "whiteboard"},
            {"startFrame": 30, "endFrame": 60, "text": "ရိုးရှင်းစွာပြောရရင်", "scene_type": "whiteboard"},
            {"startFrame": 60, "endFrame": 90, "text": "လူတွေရဲ့ အလုပ်ကို", "scene_type": "whiteboard"},
            {"startFrame": 90, "endFrame": 120, "text": "အလိုအလျောက် လုပ်ပေးတဲ့", "scene_type": "demo"},
            {"startFrame": 120, "endFrame": 150, "text": "နည်းပညာပါ။", "scene_type": "demo"},
        ],
    }

    # Test chunking only (no actual API call without GPU/API key)
    chunks = split_burmese_script(" ".join(s["text"] for s in sample["segments"]))
    print(f"[Test] Chunks: {chunks}")
    print("[Test] Voice service ready. Set VOXCPM_API_URL or GOOGLE_API_KEY to generate.")
