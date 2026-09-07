"""Skill module: Render.

WRAPS (import/read only, never edited or re-implemented):
    * ``backend.mouth_cues.build_render_input``
    * ``backend.render_video.{render_video_remotion, render_video_segment}``
    * ``backend.segment_render_cache.render_segments_and_assemble``
    * ``backend.output_qa.qa_job_directory``
    * ``video_contract.{VideoScript, RenderControls, ASPECT_RATIO_DIMENSIONS}``

This skill is the *deterministic render* boundary: it turns the locked script +
audio into the render contract and the final MP4 through Remotion. It never
authors new rendering code; the renderer is fixed and enumerated.
"""

from __future__ import annotations

from video_contract import RenderControls, VideoScript

from backend.agent.skills.base import (
    Example,
    QualityCheck,
    SkillDescriptor,
    ToolRef,
)

TRIGGER: str = (
    "A visual-planned VideoScript and a non-empty voice.wav exist and there is no "
    "usable render checkpoint; runs after Voice/Audio and before QA. Concretely: "
    "render_input.json is stale/absent or _render_checkpoint_is_usable is False "
    "for the current script+audio fingerprint."
)

INPUT_SCHEMA: tuple[type, ...] = (VideoScript,)
OUTPUT_SCHEMA: tuple[type, ...] = (RenderControls,)

ALLOWED_TOOLS: tuple[ToolRef, ...] = (
    ToolRef(
        module="backend.mouth_cues",
        name="build_render_input",
        role="Existing builder that derives the deterministic render_input.json "
        "(segments, mouth cues, controls) from the locked script + audio.",
    ),
    ToolRef(
        module="backend.render_video",
        name="render_video_remotion",
        role="Existing whole-job Remotion render entrypoint.",
    ),
    ToolRef(
        module="backend.render_video",
        name="render_video_segment",
        role="Existing per-segment Remotion render entrypoint.",
    ),
    ToolRef(
        module="backend.segment_render_cache",
        name="render_segments_and_assemble",
        role="Existing cached segment render + assembly strategy.",
    ),
)

INVARIANTS: tuple[str, ...] = (
    "PREVIEW/EXPORT PARITY: preview and export must use the SAME spec, assets, "
    "fonts and renderer configuration; render_input.json is derived once from the "
    "locked script and reused for both, never diverged.",
    "The immutable render contract (RenderControls: cta_text, "
    "retention_progress_bar, animated_lower_thirds, aspect_ratio) is server-owned "
    "and persisted in script.json; rendering must honour it exactly.",
    "aspect_ratio maps to the fixed ASPECT_RATIO_DIMENSIONS (9:16=1080x1920, "
    "16:9=1920x1080, 1:1=1080x1080); no arbitrary resolutions.",
    "Rendering is deterministic Remotion only — this skill never generates or "
    "executes arbitrary rendering code (reinforces the Visual/Motion invariant).",
    "render_input segment ids must match script segments exactly with no "
    "duplicates (enforced downstream by backend.output_qa.qa_job_directory).",
    "This skill adds no LlmAgent; rendering is an internal pipeline step.",
)

EXAMPLES: tuple[Example, ...] = (
    Example(
        name="render_controls_are_immutable_and_shared",
        input={
            "script_render_controls": {
                "cta_text": "Follow FYF Studio",
                "retention_progress_bar": True,
                "animated_lower_thirds": True,
                "aspect_ratio": "9:16",
            }
        },
        output={
            "preview_render_controls": {
                "cta_text": "Follow FYF Studio",
                "retention_progress_bar": True,
                "animated_lower_thirds": True,
                "aspect_ratio": "9:16",
            },
            "export_render_controls": {
                "cta_text": "Follow FYF Studio",
                "retention_progress_bar": True,
                "animated_lower_thirds": True,
                "aspect_ratio": "9:16",
            },
            "preview_equals_export": True,
            "dimensions": [1080, 1920],
        },
        note="Preview and export resolve to identical RenderControls and the "
        "9:16 dimensions from ASPECT_RATIO_DIMENSIONS. Static data, no render.",
    ),
)

QUALITY_CHECKS: tuple[QualityCheck, ...] = (
    QualityCheck(
        name="technical_output_qa",
        module="backend.output_qa",
        attribute="qa_job_directory",
        description="Existing deterministic technical QA over the rendered job "
        "directory (files, streams, durations, mouth cues, script/render "
        "consistency); delegated.",
        kind="technical",
    ),
)

VERSION: str = "1.0.0"

WRAPPED_MODULES: tuple[str, ...] = (
    "backend.mouth_cues",
    "backend.render_video",
    "backend.segment_render_cache",
    "backend.output_qa",
    "video_contract",
)

DESCRIPTOR = SkillDescriptor(
    name="render",
    responsibility="Render",
    trigger=TRIGGER,
    input_schema=INPUT_SCHEMA,
    output_schema=OUTPUT_SCHEMA,
    allowed_tools=ALLOWED_TOOLS,
    invariants=INVARIANTS,
    examples=EXAMPLES,
    quality_checks=QUALITY_CHECKS,
    version=VERSION,
    wrapped_modules=WRAPPED_MODULES,
    notes="Render boundary: VideoScript -> immutable RenderControls realised as "
    "video.mp4 via deterministic Remotion. Wraps existing render/mouth-cue code.",
)
