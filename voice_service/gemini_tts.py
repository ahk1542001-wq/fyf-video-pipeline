"""
Gemini-TTS Voice Service — Google's natural AI male voices for Burmese.
Supports my-MM (Preview) with 16 male voices + natural-language emotion prompts.

Voice options (Male):
- Zubenelgenubi (Casual) — recommended for general narration
- Umbriel (Easy-going) — relaxed
- Charon (Informative) — news-style
- Achird (Friendly) — warm
- Orus (Firm) — confident
- Sadaltager (Knowledgeable) — expert feel
"""

import os
import base64
from typing import Optional
from google import genai
from backend.vertex_telemetry import track_client

# Cute Mascot Voice prompt (Puck voice — playful, adorable, friendly)
MASCOT_CUTE_PROMPT = (
    "You are FYF, a cute and friendly mascot character speaking in a warm, "
    "playful, adorable way. Your voice is cheerful and bright, like a lovable "
    "cartoon character. Speak clearly and happily, with natural enthusiasm, "
    "like you genuinely enjoy helping your friend understand something new."
)

# FYF Brand Voice prompt (matches BRAND_FOUNDATION.md)
# "A thoughtful Burmese builder explaining complex AI systems to a capable friend."
FYF_BRAND_PROMPT = (
    "You are FYF, a thoughtful Burmese AI builder explaining complex AI systems "
    "to a capable friend. Speak with clear, calm precision. Use practical, "
    "down-to-earth language. Be honest about limitations and emphasize where "
    "human approval matters. Never hype — explain. Keep a steady, confident, "
    "friendly rhythm with natural pauses between ideas, like a trusted friend "
    "who really knows the subject."
)

# Human-style prompt presets (natural-language controls for Gemini-TTS)
HUMAN_STYLE_PROMPTS = {
    "natural": (
        "You are a Burmese man speaking naturally and conversationally, "
        "like chatting with a friend over coffee. Use relaxed pacing, "
        "natural pauses, and a warm friendly tone. Vary your rhythm — "
        "don't sound like you're reading a script."
    ),
    "storyteller": (
        "You are a Burmese storyteller sharing something interesting. "
        "Speak with genuine curiosity and warmth. Slow down at key moments, "
        "add small pauses for effect, and sound engaged with your topic."
    ),
    "teacher": (
        "You are a Burmese teacher explaining something clearly to a student. "
        "Speak calmly and patiently. Emphasize important words naturally, "
        "pause briefly after key points, and keep a steady reassuring tone."
    ),
    "news": (
        "You are a Burmese news anchor delivering information. "
        "Speak clearly and professionally with calm authority. "
        "Use measured pacing with short pauses between key facts."
    ),
    "mascot": (
        "You are FYF, a cute and friendly mascot character speaking in a warm, "
        "playful, adorable way. Your voice is cheerful and bright, like a lovable "
        "cartoon character. Speak clearly and happily, with natural enthusiasm, "
        "like you genuinely enjoy helping your friend understand something new."
    ),
    "excited": (
        "You are a Burmese person sharing exciting news with a friend. "
        "Speak with genuine enthusiasm and energy. Your voice rises and falls "
        "naturally with excitement, with quick lively pacing."
    ),
}

# Available male voices
MALE_VOICES = {
    "casual": "Zubenelgenubi",       # Casual - best for general
    "easygoing": "Umbriel",          # Easy-going
    "informative": "Charon",         # Informative
    "friendly": "Achird",            # Friendly
    "firm": "Orus",                  # Firm
    "knowledgeable": "Sadaltager",   # Knowledgeable
    "lively": "Sadachbia",           # Lively
    "clear": "Iapetus",              # Clear
}

def generate_gemini_tts(
    text: str,
    voice: str = "casual",
    style: str = "natural",
    prompt: Optional[str] = None,
    output_path: str = "output/voice.mp3",
    model: str = "gemini-2.5-flash-preview-tts",
) -> str:
    """Generate Burmese speech using Gemini-TTS.

    Args:
        text: Burmese text to speak
        voice: Male voice name from MALE_VOICES or a full voice name
        style: Human-style preset from HUMAN_STYLE_PROMPTS (natural, storyteller, teacher, news, excited)
        prompt: Custom natural language style instruction (overrides style)
        output_path: Where to save the audio
        model: Gemini TTS model ID
    """
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

    voice_name = MALE_VOICES.get(voice, voice)

    # Default prompt if none given
    if not prompt:
        if style == "fyf":
            prompt = FYF_BRAND_PROMPT
        elif style == "mascot":
            prompt = MASCOT_CUTE_PROMPT
        else:
            prompt = HUMAN_STYLE_PROMPTS.get(style, HUMAN_STYLE_PROMPTS["natural"])

    from backend.vertex_client import vertex_client_kwargs

    kwargs = vertex_client_kwargs(location=os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1"))
    client = track_client(genai.Client(**kwargs), stage="tts")

    response = client.models.generate_content(
        model=model,
        contents=text,
        config={
            "response_modalities": ["AUDIO"],
            "speech_config": {
                "voice_config": {
                    "prebuilt_voice_config": {
                        "voice_name": voice_name
                    }
                }
            },
        },
    )

    # Extract audio from candidates[0].content.parts[0].inline_data
    audio_data = None
    if response.candidates and response.candidates[0].content.parts:
        part = response.candidates[0].content.parts[0]
        if part.inline_data:
            audio_data = part.inline_data.data
            mime_type = part.inline_data.mime_type

    if not audio_data:
        raise RuntimeError(f"No audio in Gemini-TTS response: {response}")

    # Convert raw PCM to WAV if needed
    if "audio/L16" in mime_type:
        import wave
        import struct
        rate = 24000
        wav_path = output_path.replace('.mp3', '.wav') if output_path.endswith('.mp3') else output_path
        with wave.open(wav_path, 'wb') as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(audio_data)
        print(f"✅ Gemini-TTS voice ({voice_name}) saved to {wav_path}")
        return wav_path
    else:
        with open(output_path, "wb") as f:
            f.write(audio_data)
        print(f"✅ Gemini-TTS voice ({voice_name}) saved to {output_path}")
        return output_path


if __name__ == "__main__":
    # Test
    test_text = "မင်္ဂလာပါ။ ဒီနေ့တော့ AI ကို ဘယ်လိုသုံးမလဲဆိုတာ ပြောပြပေးပါမယ်။"
    try:
        generate_gemini_tts(test_text, voice="casual", output_path="/tmp/gemini_male_test.mp3")
        print("Test complete!")
    except Exception as e:
        print(f"Test failed: {e}")

# Alias for easier imports
__all__ = ['generate_gemini_tts', 'MALE_VOICES', 'HUMAN_STYLE_PROMPTS']
