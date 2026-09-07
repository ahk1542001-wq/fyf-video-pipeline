"""Skill module: Voice / Audio.

WRAPS (import/read only, never edited or re-implemented):
    * ``voice_service.voice_generator.generate_voice``
    * ``voice_service.gemini_tts.generate_gemini_tts``
    * ``voice_service.audio_quality.{master_voice_audio, analyze_pcm16_wav}``
    * ``voice_service.production_voice.{build_production_filter,
      tighten_ai_silences, apply_production_postprocessing}``
    * ``voice_service.burmese_asr_qa.compare_asr``
    * ``video_contract.VideoScript``

This skill is the *audio* boundary: it realises the locked narration as safe,
balanced voice audio. It does not change the script contract and does not render.
"""

from __future__ import annotations

from video_contract import VideoScript

from backend.agent.skills.base import (
    Example,
    QualityCheck,
    SkillDescriptor,
    ToolRef,
)

TRIGGER: str = (
    "A locked VideoScript with approved narration exists and there is no usable "
    "checkpointed voice for its fingerprint; runs after Visual/Motion and before "
    "Render. Concretely: voice.wav is missing/empty or _voice_checkpoint_is_usable "
    "is False for the current script+provider fingerprint."
)

INPUT_SCHEMA: tuple[type, ...] = (VideoScript,)
OUTPUT_SCHEMA: tuple[type, ...] = (VideoScript,)

ALLOWED_TOOLS: tuple[ToolRef, ...] = (
    ToolRef(
        module="voice_service.voice_generator",
        name="generate_voice",
        role="Existing provider-agnostic voice synthesis entrypoint.",
    ),
    ToolRef(
        module="voice_service.gemini_tts",
        name="generate_gemini_tts",
        role="Existing Gemini TTS backend used by generate_voice.",
    ),
    ToolRef(
        module="voice_service.audio_quality",
        name="master_voice_audio",
        role="Existing conditional loudness/peak mastering (safety only).",
    ),
    ToolRef(
        module="voice_service.production_voice",
        name="build_production_filter",
        role="Existing approved-speed production filter chain (balance/ducking).",
    ),
    ToolRef(
        module="voice_service.production_voice",
        name="tighten_ai_silences",
        role="Existing purposeful-silence editor that trims unnatural AI gaps.",
    ),
    ToolRef(
        module="voice_service.production_voice",
        name="apply_production_postprocessing",
        role="Existing voice/music/SFX post-processing mix pass.",
    ),
)

INVARIANTS: tuple[str, ...] = (
    "Voice/music balance and ducking are produced by the existing approved "
    "production filter chain (build_production_filter / apply_production_"
    "postprocessing); this facade never re-derives mix levels.",
    "Peak safety is non-negotiable: mastered voice must have zero full-scale "
    "clipping samples and peak <= FYF_AUDIO_MAX_PEAK_DBFS (default -1.0 dBFS); "
    "already-safe source audio is preserved untouched (master_voice_audio).",
    "SFX and silence are purposeful: unnatural AI gaps are tightened "
    "(tighten_ai_silences) and silence is used intentionally, never as dead air.",
    "Locked narration text is immutable — voice realises it verbatim; language "
    "(my-MM/en-US) and protected terms are preserved, no transliteration.",
    "ASR mismatch is a human-review warning signal only, never an automatic "
    "production rejection (voice_service.burmese_asr_qa.compare_asr semantics).",
    "This skill adds no LlmAgent; voice is an internal pipeline responsibility.",
)

EXAMPLES: tuple[Example, ...] = (
    Example(
        name="asr_coverage_review_flag",
        input={
            "expected": "ငွေက အချိန်နဲ့အမျှ ပွားပါတယ်",
            "transcription": "ငွေက အချိန်နဲ့အမျှ",
            "threshold": 0.97,
        },
        output={
            "coverage_ratio_below_threshold": True,
            "review_required": True,
            "missing_spans_nonempty": True,
        },
        note="Reflects voice_service.burmese_asr_qa.compare_asr: truncated "
        "transcription lowers coverage_ratio and sets review_required=True as a "
        "warning (not auto-reject). Static shape, no ASR/provider call.",
    ),
)

QUALITY_CHECKS: tuple[QualityCheck, ...] = (
    QualityCheck(
        name="asr_coverage_qa",
        module="voice_service.burmese_asr_qa",
        attribute="compare_asr",
        description="Existing Burmese ASR-vs-script coverage QA; delegated.",
        kind="technical",
    ),
    QualityCheck(
        name="voice_peak_clipping_qa",
        module="voice_service.audio_quality",
        attribute="analyze_pcm16_wav",
        description="Existing deterministic PCM16 peak/clipping analysis; "
        "delegated.",
        kind="technical",
    ),
    QualityCheck(
        name="voice_mastering_qa",
        module="voice_service.audio_quality",
        attribute="master_voice_audio",
        description="Existing conditional mastering whose report proves peak "
        "safety; delegated.",
        kind="technical",
    ),
)

VERSION: str = "1.0.0"

WRAPPED_MODULES: tuple[str, ...] = (
    "voice_service.voice_generator",
    "voice_service.gemini_tts",
    "voice_service.audio_quality",
    "voice_service.production_voice",
    "voice_service.burmese_asr_qa",
    "video_contract",
)

DESCRIPTOR = SkillDescriptor(
    name="voice_audio",
    responsibility="Voice/Audio",
    trigger=TRIGGER,
    input_schema=INPUT_SCHEMA,
    output_schema=OUTPUT_SCHEMA,
    allowed_tools=ALLOWED_TOOLS,
    invariants=INVARIANTS,
    examples=EXAMPLES,
    quality_checks=QUALITY_CHECKS,
    version=VERSION,
    wrapped_modules=WRAPPED_MODULES,
    notes="Audio boundary. The voice artefacts (voice.wav + voice_checkpoint.json) "
    "are side files; the governing contract stays VideoScript, so INPUT and OUTPUT "
    "both reference it (no new schema invented). Wraps existing voice modules.",
)
