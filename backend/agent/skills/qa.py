"""Skill module: QA.

WRAPS (import/read only, never edited or re-implemented):
    * ``backend.output_qa.qa_job_directory`` — deterministic technical QA
    * ``backend.creative_quality.audit_creative_quality`` — creative rhythm QA
    * ``backend.final_visual_qa_vertex.verify_final_rendered_meaning`` — semantic QA
    * ``voice_service.burmese_asr_qa.compare_asr`` — voice review-warning signal
    * ``video_contract.{VideoScript, ClaimCoverageResponse}``

This skill is the *acceptance* boundary. Its defining invariant is SEPARATION:
automated technical QA and creative/human acceptance are distinct lanes and must
never be collapsed into a single pass/fail gate.
"""

from __future__ import annotations

from video_contract import ClaimCoverageResponse, VideoScript

from backend.agent.skills.base import (
    Example,
    QualityCheck,
    SkillDescriptor,
    ToolRef,
)

TRIGGER: str = (
    "A rendered job directory (video.mp4, voice.wav, script.json, "
    "render_input.json, mouth_cues.json) exists and needs acceptance; runs after "
    "Render. Concretely: job status transitions to 'qa' / 'creative_qa' and no "
    "passing qa_report.json + creative_qa.json + final_visual_qa.json exist yet."
)

INPUT_SCHEMA: tuple[type, ...] = (VideoScript,)
OUTPUT_SCHEMA: tuple[type, ...] = (ClaimCoverageResponse,)

ALLOWED_TOOLS: tuple[ToolRef, ...] = (
    ToolRef(
        module="backend.output_qa",
        name="qa_job_directory",
        role="Existing deterministic technical QA over the rendered job dir.",
    ),
    ToolRef(
        module="backend.creative_quality",
        name="audit_creative_quality",
        role="Existing creative rhythm/pacing audit (DirectorPolicy driven).",
    ),
    ToolRef(
        module="backend.final_visual_qa_vertex",
        name="verify_final_rendered_meaning",
        role="Existing semantic QA that the rendered frames prove the claims.",
    ),
    ToolRef(
        module="voice_service.burmese_asr_qa",
        name="compare_asr",
        role="Existing ASR coverage check; a human-review warning, not a gate.",
    ),
)

INVARIANTS: tuple[str, ...] = (
    "SEPARATION OF CONCERNS: technical QA (files, streams, durations, mouth cues, "
    "script/render consistency, peak safety) and creative/human acceptance (rhythm, "
    "storytelling quality, brand fit) are DISTINCT lanes with distinct outcomes; "
    "passing technical QA never implies creative acceptance and vice versa.",
    "Creative QA failure routes to repair or 'needs_human_review' — it is never "
    "auto-approved; technical QA failure blocks publish outright.",
    "ASR mismatch (compare_asr) is surfaced as a review_required WARNING only and "
    "must never be treated as an automatic rejection.",
    "QA delegates to the existing implementations verbatim; this skill re-implements "
    "no thresholds, no ffprobe parsing, no policy constants.",
    "Claim coverage is judged against the locked claims: every claim id must be "
    "covered/proven (ClaimCoverageResponse), no silent claim dropping.",
    "This skill adds no LlmAgent; QA is an internal pipeline responsibility.",
)

EXAMPLES: tuple[Example, ...] = (
    Example(
        name="technical_pass_does_not_imply_creative_pass",
        input={
            "technical_qa": {"passed": True, "failure_codes": []},
            "creative_qa": {
                "passed": False,
                "failure_codes": ["CENTER_CARD_SATURATION"],
            },
        },
        output={
            "technical_gate": "pass",
            "creative_gate": "repair_required",
            "overall": "needs_human_review_or_repair",
            "lanes_independent": True,
        },
        note="A technically clean render can still fail creative rhythm QA; the "
        "two lanes are reported separately and never merged. Static, no call.",
    ),
)

QUALITY_CHECKS: tuple[QualityCheck, ...] = (
    QualityCheck(
        name="technical_output_qa",
        module="backend.output_qa",
        attribute="qa_job_directory",
        description="Deterministic technical QA lane.",
        kind="technical",
    ),
    QualityCheck(
        name="semantic_rendered_meaning_qa",
        module="backend.final_visual_qa_vertex",
        attribute="verify_final_rendered_meaning",
        description="Semantic claim-verification lane (rendered frames prove claims).",
        kind="technical",
    ),
    QualityCheck(
        name="creative_rhythm_qa",
        module="backend.creative_quality",
        attribute="audit_creative_quality",
        description="Creative rhythm lane; failure routes to repair/human review.",
        kind="creative",
    ),
    QualityCheck(
        name="voice_asr_review_signal",
        module="voice_service.burmese_asr_qa",
        attribute="compare_asr",
        description="Human-acceptance warning signal for voice coverage.",
        kind="human_acceptance",
    ),
)

VERSION: str = "1.0.0"

WRAPPED_MODULES: tuple[str, ...] = (
    "backend.output_qa",
    "backend.creative_quality",
    "backend.final_visual_qa_vertex",
    "voice_service.burmese_asr_qa",
    "video_contract",
)

DESCRIPTOR = SkillDescriptor(
    name="qa",
    responsibility="QA",
    trigger=TRIGGER,
    input_schema=INPUT_SCHEMA,
    output_schema=OUTPUT_SCHEMA,
    allowed_tools=ALLOWED_TOOLS,
    invariants=INVARIANTS,
    examples=EXAMPLES,
    quality_checks=QUALITY_CHECKS,
    version=VERSION,
    wrapped_modules=WRAPPED_MODULES,
    notes="Acceptance boundary. Enforces separation of technical QA from "
    "creative/human acceptance. Wraps the existing QA trio + ASR review signal.",
)
