"""Skill module: Research / Claims.

WRAPS (import/read only, never edited or re-implemented):
    * ``writer_agent_vertex._extract_complete_evidence_claims`` — claim extraction
    * ``visual_evidence_vertex.ensure_relationship_modes`` — relation-mode resolver
    * ``backend.agent.tools.plan_visual_shots`` — ADK visual-planning tool
    * ``video_contract.{VideoScript, EvidenceClaim, EvidenceClaimsResponse}``

This skill is the *evidence* boundary: for every narration segment it derives
the factual claims the finished frame must visibly prove, and guarantees each
claim is covered by an evidence shot. It does not write narration and does not
generate pixels.
"""

from __future__ import annotations

from video_contract import EvidenceClaimsResponse, VideoScript

from backend.agent.skills.base import (
    Example,
    QualityCheck,
    SkillDescriptor,
    ToolRef,
)

TRIGGER: str = (
    "A locked VideoScript exists whose segments carry narration but whose "
    "evidence_claims are missing or not yet fully covered by evidence_shots; "
    "runs after Script and before Visual/Motion. Concretely: "
    "VisualBase.validate_evidence_coverage would fail because a claim id is not "
    "present in some shot's proves_claim_ids."
)

INPUT_SCHEMA: tuple[type, ...] = (VideoScript,)
OUTPUT_SCHEMA: tuple[type, ...] = (EvidenceClaimsResponse,)

ALLOWED_TOOLS: tuple[ToolRef, ...] = (
    ToolRef(
        module="backend.agent.tools",
        name="plan_visual_shots",
        role="Locks narration and emits evidence shots that prove the claims.",
    ),
    ToolRef(
        module="writer_agent_vertex",
        name="_extract_complete_evidence_claims",
        role="Existing authoritative claim extractor (returns "
        "EvidenceClaimsResponse); wrapped by reference, never copied.",
    ),
    ToolRef(
        module="visual_evidence_vertex",
        name="ensure_relationship_modes",
        role="Resolves directional/bidirectional/non_replacement relation modes "
        "for relationship claims.",
    ),
)

INVARIANTS: tuple[str, ...] = (
    "Every EvidenceClaim must have a unique claim_id and be covered by at least "
    "one EvidenceShot.proves_claim_ids (video_contract VisualBase invariant); "
    "uncovered claims are a hard failure, never a silent drop.",
    "A claim's evidence_type (count/comparison/state/sequence/relationship/"
    "concept) constrains the motion primitive that may prove it; relationship "
    "claims require an explicit relation_mode.",
    "Claims are extracted from the locked narration only; this skill must not "
    "rewrite narration text or invent values absent from the script.",
    "No LlmAgent is created here; claim extraction is an internal "
    "responsibility dispatched by the single Producer agent.",
)

EXAMPLES: tuple[Example, ...] = (
    Example(
        name="count_claim_covered_by_shot",
        input={
            "segment_id": "s1",
            "narration": "Three separate checkpoints catch the mistake.",
        },
        output={
            "segments": [
                {
                    "id": "s1",
                    "claims": [
                        {
                            "claim_id": "s1-c1",
                            "statement": "Three checkpoints catch the mistake",
                            "evidence_type": "count",
                            "values": ["3"],
                        }
                    ],
                }
            ]
        },
        note="Static EvidenceClaimsResponse shape; the matching EvidenceShot "
        "must list 's1-c1' in proves_claim_ids. No provider call.",
    ),
)

QUALITY_CHECKS: tuple[QualityCheck, ...] = (
    QualityCheck(
        name="claims_proven_on_screen",
        module="backend.final_visual_qa_vertex",
        attribute="verify_final_rendered_meaning",
        description="Existing semantic QA that verifies the rendered frame "
        "actually proves each locked claim; delegated, not re-implemented.",
        kind="technical",
    ),
)

VERSION: str = "1.0.0"

WRAPPED_MODULES: tuple[str, ...] = (
    "writer_agent_vertex",
    "visual_evidence_vertex",
    "backend.agent.tools",
    "backend.final_visual_qa_vertex",
    "video_contract",
)

DESCRIPTOR = SkillDescriptor(
    name="research_claims",
    responsibility="Research/Claims",
    trigger=TRIGGER,
    input_schema=INPUT_SCHEMA,
    output_schema=OUTPUT_SCHEMA,
    allowed_tools=ALLOWED_TOOLS,
    invariants=INVARIANTS,
    examples=EXAMPLES,
    quality_checks=QUALITY_CHECKS,
    version=VERSION,
    wrapped_modules=WRAPPED_MODULES,
    notes="Evidence-claim boundary. Supporting contract types: EvidenceClaim, "
    "SegmentEvidenceClaims, EvidenceClaimsResponse. Wraps existing extractors.",
)
