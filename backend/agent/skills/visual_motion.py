"""Skill module: Visual / Motion.

WRAPS (import/read only, never edited or re-implemented):
    * ``backend.agent.tools.plan_visual_shots`` — the ADK storyboard tool
    * ``visual_evidence_vertex.{plan_visual_treatments,
      generate_and_verify_visual_evidence, ensure_relationship_modes}``
    * ``backend.video_director.apply_director_pass`` /
      ``backend.video_styles.apply_video_style``
    * ``backend.creative_quality.audit_creative_quality``
    * ``video_contract.{VideoScript, StoryboardResponse, MotionGraphicSpec,
      VisualTreatment, EvidenceShot}``

This skill is the *storyboard* boundary: it selects validated motion primitives
and composable storytelling patterns that prove each claim. It NEVER emits
arbitrary generated executable rendering code.
"""

from __future__ import annotations

from video_contract import StoryboardResponse, VideoScript

from backend.agent.skills.base import (
    Example,
    QualityCheck,
    SkillDescriptor,
    ToolRef,
)

TRIGGER: str = (
    "A locked, claim-covered VideoScript needs a shot plan: segments have "
    "evidence claims but their visual.evidence_shots / treatments / motion_spec "
    "are unpopulated, or creative QA flagged a rhythm failure "
    "(TREATMENT_RUN_REPEATED, CENTER_CARD_SATURATION, MOTION_DIAGRAM_SATURATION, "
    "VISUAL_WORLD_NOT_RESET). Runs after Research/Claims and before Voice/Audio."
)

INPUT_SCHEMA: tuple[type, ...] = (VideoScript,)
OUTPUT_SCHEMA: tuple[type, ...] = (StoryboardResponse,)

ALLOWED_TOOLS: tuple[ToolRef, ...] = (
    ToolRef(
        module="backend.agent.tools",
        name="plan_visual_shots",
        role="Plan treatments, scene types, mascot actions and director passes "
        "into a locked VideoScript.",
    ),
    ToolRef(
        module="visual_evidence_vertex",
        name="plan_visual_treatments",
        role="Existing Vertex treatment planner (validated primitives only).",
    ),
    ToolRef(
        module="visual_evidence_vertex",
        name="generate_and_verify_visual_evidence",
        role="Existing generate+verify step producing evidence shots/assets.",
    ),
    ToolRef(
        module="visual_evidence_vertex",
        name="ensure_relationship_modes",
        role="Existing relation-mode resolver for relationship diagrams.",
    ),
    ToolRef(
        module="backend.video_director",
        name="apply_director_pass",
        role="Existing deterministic director rhythm pass.",
    ),
    ToolRef(
        module="backend.video_styles",
        name="apply_video_style",
        role="Existing cinematic genre styling pass.",
    ),
)

INVARIANTS: tuple[str, ...] = (
    "MOTION PRIMITIVE INVARIANT (document line 43): visuals are composed ONLY "
    "from validated MotionGraphicSpec layouts (count, comparison, sequence, "
    "relationship, directional_branch, concept), the fixed VisualTreatment "
    "treatment_type set, and the enumerated motion_preset/transition/composition "
    "literals. NO arbitrary generated executable rendering code (no model-authored "
    "JSX/TSX/CSS/ffmpeg/Remotion source, no eval/exec of generated code) may ever "
    "be emitted or run by this skill.",
    "Every motion_graphic EvidenceShot MUST carry a motion_spec, and a non "
    "motion_graphic shot MUST NOT (video_contract EvidenceShot.require_motion_spec).",
    "Composable storytelling patterns only: a shot must prove its claim via an "
    "observable change (VisualTreatment.require_observable_change) — focal_object, "
    "action and change are all required for non-kinetic treatments.",
    "Storyboard visual variety: at least two generated story-scene shots across a "
    ">=4 segment storyboard; do not render every segment as cards or diagrams "
    "(backend.agent.tools._restore_storyboard_visual_variety guard).",
    "Attention-reset cadence and rhythm limits are enforced by the existing "
    "DirectorPolicy via audit_creative_quality; this skill delegates and never "
    "re-implements those thresholds.",
    "This skill adds no LlmAgent; visual planning is an internal responsibility "
    "dispatched by the single Producer agent.",
)

EXAMPLES: tuple[Example, ...] = (
    Example(
        name="count_claim_to_validated_motion_primitive",
        input={
            "claim": {
                "claim_id": "s1-c1",
                "statement": "Three checkpoints catch the mistake",
                "evidence_type": "count",
                "values": ["3"],
            }
        },
        output={
            "media_type": "motion_graphic",
            "motion_spec": {
                "layout": "count",
                "labels": ["checkpoint 1", "checkpoint 2", "checkpoint 3"],
                "values": ["3"],
                "object_count": 3,
                "accent_index": 2,
            },
            "proves_claim_ids": ["s1-c1"],
        },
        note="A validated MotionGraphicSpec primitive — enumerated layout + "
        "bounded labels/values/object_count. No executable rendering code is "
        "generated. Static example, no provider call.",
    ),
)

QUALITY_CHECKS: tuple[QualityCheck, ...] = (
    QualityCheck(
        name="creative_rhythm_audit",
        module="backend.creative_quality",
        attribute="audit_creative_quality",
        description="Existing creative/rhythm audit (treatment runs, center-card "
        "saturation, diagram ratio, attention-reset cadence); delegated.",
        kind="creative",
    ),
    QualityCheck(
        name="rendered_meaning_verification",
        module="backend.final_visual_qa_vertex",
        attribute="verify_final_rendered_meaning",
        description="Existing semantic verification that the rendered frame "
        "proves each claim; delegated.",
        kind="technical",
    ),
)

VERSION: str = "1.0.0"

WRAPPED_MODULES: tuple[str, ...] = (
    "backend.agent.tools",
    "visual_evidence_vertex",
    "backend.video_director",
    "backend.video_styles",
    "backend.creative_quality",
    "backend.final_visual_qa_vertex",
    "video_contract",
)

DESCRIPTOR = SkillDescriptor(
    name="visual_motion",
    responsibility="Visual/Motion",
    trigger=TRIGGER,
    input_schema=INPUT_SCHEMA,
    output_schema=OUTPUT_SCHEMA,
    allowed_tools=ALLOWED_TOOLS,
    invariants=INVARIANTS,
    examples=EXAMPLES,
    quality_checks=QUALITY_CHECKS,
    version=VERSION,
    wrapped_modules=WRAPPED_MODULES,
    notes="Storyboard boundary. Supporting contract types: MotionGraphicSpec, "
    "VisualTreatment, EvidenceShot, CompactVisualPlanResponse. Enforces the "
    "validated-motion-primitive invariant (no generated executable render code).",
)
