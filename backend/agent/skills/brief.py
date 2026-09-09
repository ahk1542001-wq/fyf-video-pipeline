"""Skill module: Brief / Strategy.

WRAPS (import/read only, never edited or re-implemented):
    * ``backend.agent.tools.draft_story_segments`` — the ADK script tool
    * ``video_contract.ScriptGenerationRequest`` — the canonical request contract

This skill is the *strategy* boundary: it turns a raw client request into a
contract-validated generation brief (language, genre,
presenter mode, studio, duration, narrative hook) that every downstream skill
consumes. It does not draft narration, plan visuals, or synthesise voice.
"""

from __future__ import annotations

from video_contract import ScriptGenerationRequest

from backend.agent.skills.base import (
    Example,
    QualityCheck,
    SkillDescriptor,
    ToolRef,
)

# --- When this skill is used ------------------------------------------------
TRIGGER: str = (
    "A new video request arrives (a ScriptGenerationRequest with a topic) and no "
    "resolved strategy brief exists yet for the job; runs before any narration "
    "drafting. The user's supplied script or brief remains the source of truth."
)

# --- Contract boundary (existing video_contract types, referenced not copied)
INPUT_SCHEMA: tuple[type, ...] = (ScriptGenerationRequest,)
OUTPUT_SCHEMA: tuple[type, ...] = (ScriptGenerationRequest,)

# --- Existing tools this skill is allowed to dispatch to (by name) ----------
ALLOWED_TOOLS: tuple[ToolRef, ...] = (
    ToolRef(
        module="backend.agent.tools",
        name="draft_story_segments",
        role="Structure the supplied script or brief for the requested "
        "topic/genre/language/studio without external research.",
    ),
)

# --- Rules this skill must never violate ------------------------------------
INVARIANTS: tuple[str, ...] = (
    "Strategy resolution never fabricates facts: the brief records angles and "
    "audience only; every factual claim is proven later by the Research/Claims "
    "and Visual/Motion skills against evidence.",
    "language, genre, presenter_mode, studio_name and duration_mode are resolved "
    "ONCE here and passed downstream verbatim; a downstream skill must not "
    "silently fall back to Burmese/explainer defaults for an English run.",
    "This skill adds no LlmAgent: strategy is an internal responsibility of the "
    "single Producer agent (docs/HANDOFF_AGENTIC_BUSINESS_STUDIO.md §68).",
    "The resolved brief conforms to ScriptGenerationRequest (extra='ignore' on "
    "that contract); no new schema is invented by this facade.",
)

# --- Deterministic illustration (static data, zero provider calls) ----------
EXAMPLES: tuple[Example, ...] = (
    Example(
        name="resolve_english_cinematic_brief",
        input={
            "topic": "How compound interest works",
            "duration_mode": "standard",
            "language": "en-US",
            "genre": "cinematic_documentary",
            "studio_name": "FYF Studio",
        },
        output={
            "topic": "How compound interest works",
            "duration_mode": "standard",
            "language": "en-US",
            "genre": "cinematic_documentary",
            "studio_name": "FYF Studio",
            "suggested_segments": 6,
        },
        note="Static resolved request example. No research or provider call.",
    ),
)

# --- Existing QA this skill delegates to (does not re-implement) ------------
QUALITY_CHECKS: tuple[QualityCheck, ...] = (
    QualityCheck(
        name="brief_downstream_audit",
        module="backend.agent.tools",
        attribute="audit_story_quality",
        description="Strategy quality is gated downstream: the seeded draft is "
        "audited for title/segment/pacing compliance by the existing ADK tool.",
        kind="technical",
    ),
)

VERSION: str = "1.0.0"

WRAPPED_MODULES: tuple[str, ...] = (
    "backend.agent.tools",
    "video_contract",
)

DESCRIPTOR = SkillDescriptor(
    name="brief",
    responsibility="Brief/Strategy",
    trigger=TRIGGER,
    input_schema=INPUT_SCHEMA,
    output_schema=OUTPUT_SCHEMA,
    allowed_tools=ALLOWED_TOOLS,
    invariants=INVARIANTS,
    examples=EXAMPLES,
    quality_checks=QUALITY_CHECKS,
    version=VERSION,
    wrapped_modules=WRAPPED_MODULES,
    notes="Strategy boundary only; produces the resolved generation brief that "
    "downstream skills consume. No research stage is present.",
)
