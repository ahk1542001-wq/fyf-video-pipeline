"""Skill module: Script.

WRAPS (import/read only, never edited or re-implemented):
    * ``writer_agent_vertex.generate_narration_script`` / ``lock_narration_in_batches``
    * ``backend.agent.tools.draft_story_segments`` / ``audit_story_quality``
    * ``voice_service.script_humanizer.humanize_burmese``
    * ``video_contract.{StoryDraftScript, VideoScript}``

This skill is the *narration* boundary: hook-body-conclusion Burmese/English
narration segments, locked and audited. It does not plan visuals, synthesise
voice, or render.
"""

from __future__ import annotations

from video_contract import StoryDraftScript, VideoScript

from backend.agent.skills.base import (
    Example,
    QualityCheck,
    SkillDescriptor,
    ToolRef,
)

TRIGGER: str = (
    "A resolved strategy brief exists but there is no approved narration yet; "
    "runs after Brief and before Research/Claims. Concretely: draft_story_segments "
    "has not produced a StoryDraftScript with >= 5 segments for this topic, or a "
    "draft exists but audit_story_quality returned passed=False."
)

INPUT_SCHEMA: tuple[type, ...] = (StoryDraftScript,)
OUTPUT_SCHEMA: tuple[type, ...] = (VideoScript,)

ALLOWED_TOOLS: tuple[ToolRef, ...] = (
    ToolRef(
        module="backend.agent.tools",
        name="draft_story_segments",
        role="Draft hook-body-conclusion narration into a validated "
        "StoryDraftScript.",
    ),
    ToolRef(
        module="backend.agent.tools",
        name="audit_story_quality",
        role="Audit character lengths, script compliance and segment pacing.",
    ),
    ToolRef(
        module="writer_agent_vertex",
        name="generate_narration_script",
        role="Existing narration generator wrapped by draft_story_segments.",
    ),
    ToolRef(
        module="writer_agent_vertex",
        name="lock_narration_in_batches",
        role="Existing batch locker that freezes approved narration text.",
    ),
    ToolRef(
        module="voice_service.script_humanizer",
        name="humanize_burmese",
        role="Existing deterministic Burmese naturalisation helper.",
    ),
)

INVARIANTS: tuple[str, ...] = (
    "Burmese line breaking must keep combining marks and virama-linked (\\u1039) "
    "sequences attached to their base cluster; never split inside a grapheme "
    "cluster (see voice_service.burmese_asr_qa.segment_burmese semantics).",
    "Mixed-language segments are handled without transliteration: protected "
    "terms/numerals stay verbatim, and language is preserved as declared on the "
    "script (my-MM or en-US) — no silent language flip.",
    "Once narration is locked it is immutable: downstream skills must not edit "
    "segment text; only visual/voice metadata is added after approval.",
    "Segment ids must be unique and non-blank (video_contract invariant); an "
    "empty-text segment is a hard failure.",
    "This skill adds no LlmAgent; narration writing is an internal "
    "responsibility of the single Producer agent.",
)

EXAMPLES: tuple[Example, ...] = (
    Example(
        name="audit_flags_empty_segment",
        input={
            "title": "Compound interest",
            "segments": [
                {"id": "s1", "text": "ငွေက အချိန်နဲ့အမျှ ပွားပါတယ်။"},
                {"id": "s2", "text": "   "},
            ],
        },
        output={
            "passed": False,
            "segment_count": 2,
            "total_characters": 21,
            "issues": ["Segment 2 has empty text"],
        },
        note="Mirrors backend.agent.tools.audit_story_quality's deterministic "
        "report for a blank segment. No provider call.",
    ),
)

QUALITY_CHECKS: tuple[QualityCheck, ...] = (
    QualityCheck(
        name="narration_audit",
        module="backend.agent.tools",
        attribute="audit_story_quality",
        description="Existing ADK audit for title/segment/pacing compliance; "
        "delegated verbatim.",
        kind="technical",
    ),
    QualityCheck(
        name="burmese_normalisation_reference",
        module="voice_service.burmese_asr_qa",
        attribute="clean_text",
        description="Existing NFC normalisation used to reason about Burmese "
        "line breaking; delegated, not re-implemented.",
        kind="technical",
    ),
)

VERSION: str = "1.0.0"

WRAPPED_MODULES: tuple[str, ...] = (
    "writer_agent_vertex",
    "backend.agent.tools",
    "voice_service.script_humanizer",
    "voice_service.burmese_asr_qa",
    "video_contract",
)

DESCRIPTOR = SkillDescriptor(
    name="script",
    responsibility="Script",
    trigger=TRIGGER,
    input_schema=INPUT_SCHEMA,
    output_schema=OUTPUT_SCHEMA,
    allowed_tools=ALLOWED_TOOLS,
    invariants=INVARIANTS,
    examples=EXAMPLES,
    quality_checks=QUALITY_CHECKS,
    version=VERSION,
    wrapped_modules=WRAPPED_MODULES,
    notes="Narration boundary: StoryDraftScript -> locked VideoScript. Wraps "
    "existing writer/humanizer/audit functions; no logic copied.",
)
