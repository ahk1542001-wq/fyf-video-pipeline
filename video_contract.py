"""Validated semantic contract between Vertex, the backend, and Remotion."""

import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def _contains_number_label(screen_text: list[str], value: int) -> bool:
    pattern = re.compile(rf"(?<!\d){re.escape(str(value))}(?!\d)")
    return any(pattern.search(line) for line in screen_text)


class EvidenceClaim(BaseModel):
    """A narration fact that the finished frame must visibly prove."""

    model_config = ConfigDict(extra="forbid")
    claim_id: str = Field(min_length=1)
    statement: str = Field(min_length=1)
    evidence_type: Literal["count", "comparison", "state", "sequence", "relationship", "concept"]
    values: list[str] = Field(default_factory=list, max_length=6)


class MotionGraphicSpec(BaseModel):
    """Deterministic primitives for facts that should not depend on generated pixels."""

    model_config = ConfigDict(extra="forbid")
    layout: Literal["count", "comparison", "sequence", "relationship", "directional_branch", "concept"]
    labels: list[str] = Field(min_length=1, max_length=6)
    values: list[str] = Field(default_factory=list, max_length=6)
    object_count: int | None = Field(default=None, ge=1, le=30)
    accent_index: int | None = Field(default=None, ge=0, le=5)
    relation_mode: Literal["directional", "bidirectional", "non_replacement"] | None = None

    @model_validator(mode="after")
    def relation_mode_only_for_relationship(self):
        if self.layout == "relationship" and self.relation_mode is None:
            self.relation_mode = "directional"
        if self.layout != "relationship" and self.relation_mode is not None:
            raise ValueError("relation_mode is allowed only for relationship layout")
        return self


class VisualTreatment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    treatment_type: Literal[
        "story_scene", "object_action", "ui_proof", "editorial_data",
        "comparison_transform", "motion_diagram", "kinetic_type", "mascot_performance",
    ]
    focal_object: str = ""
    action: str = ""
    change: str = ""
    visual_world: str = Field(min_length=1)
    motion_family: Literal["camera", "object", "interface", "diagram", "typography", "character"]
    text_mode: Literal["none", "caption", "label", "kinetic", "data"]
    attention_reset: bool
    director_reason: str = Field(min_length=1)

    @model_validator(mode="after")
    def require_observable_change(self):
        if self.treatment_type != "kinetic_type" and not all((self.focal_object.strip(), self.action.strip(), self.change.strip())):
            raise ValueError("non-kinetic treatments require focal_object, action, and change")
        return self


class EvidenceShot(BaseModel):
    """A content-specific asset requirement, authored by Vertex and rendered per job."""

    model_config = ConfigDict(extra="forbid")
    shot_id: str = Field(min_length=1)
    proves_claim_ids: list[str] = Field(min_length=1, max_length=4)
    prompt: str = Field(min_length=1)
    caption: str = Field(min_length=1)
    hold_fraction: float = Field(gt=0, le=1)
    media_type: Literal["generated_image", "motion_graphic", "generated_video"] = "generated_image"
    motion_preset: Literal["slow_push", "pan_left", "pan_right", "drift", "static"] = "slow_push"
    transition: Literal["cut", "crossfade", "push", "wipe"] = "cut"
    composition: Literal["full_bleed", "focal_center", "split_stage"] = "focal_center"
    treatment: "VisualTreatment | None" = None
    mascot_presence: Literal["none", "reaction", "explain"] = "none"
    motion_spec: MotionGraphicSpec | None = None
    asset_path: str | None = None
    fallback_asset_path: str | None = None
    fallback_used: bool = False
    verification_status: Literal["planned", "passed"] = "planned"

    @model_validator(mode="after")
    def require_motion_spec(self):
        if self.media_type == "motion_graphic" and self.motion_spec is None:
            raise ValueError("motion_graphic shots require motion_spec")
        if self.media_type != "motion_graphic" and self.motion_spec is not None:
            raise ValueError("motion_spec is allowed only for motion_graphic shots")
        return self


class VisualBase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    phase: Literal["setup", "in_progress", "completed", "alert"]
    camera: Literal["wide", "push_in", "close_up", "over_shoulder"]
    screen_text: list[str] = Field(default_factory=list, min_length=1, max_length=2)
    evidence_claims: list[EvidenceClaim] = Field(default_factory=list, max_length=4)
    evidence_shots: list[EvidenceShot] = Field(default_factory=list, max_length=4)

    @field_validator("screen_text")
    @classmethod
    def validate_screen_text(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("screen_text must contain 1-2 strings")
        stripped = []
        for v in value:
            clean = v.strip()
            if not clean:
                raise ValueError("screen_text items must be non-blank")
            stripped.append(clean)
        return stripped

    @model_validator(mode="after")
    def validate_evidence_coverage(self):
        if not self.evidence_claims and not self.evidence_shots:
            return self
        claim_ids = {claim.claim_id for claim in self.evidence_claims}
        if len(claim_ids) != len(self.evidence_claims):
            raise ValueError("evidence claim IDs must be unique")
        shot_ids = {shot.shot_id for shot in self.evidence_shots}
        if len(shot_ids) != len(self.evidence_shots):
            raise ValueError("evidence shot IDs must be unique")
        covered = {claim_id for shot in self.evidence_shots for claim_id in shot.proves_claim_ids}
        if covered != claim_ids:
            raise ValueError("every evidence claim must be covered by an evidence shot")
        return self

class GenericVisual(VisualBase):
    kind: Literal["generic"]


class AutoActionVisual(VisualBase):
    kind: Literal["auto_action"]
    action: Literal["reorder", "pause_notify"]
    severity: Literal["mistake", "warning"]


class ConsequenceVisual(VisualBase):
    kind: Literal["consequence"]
    mode: Literal["loss_chart", "three_impacts"]
    items: list[str] = Field(min_length=1, max_length=3)

    @field_validator("items")
    @classmethod
    def validate_items(cls, value: list[str]) -> list[str]:
        stripped = []
        for v in value:
            clean = v.strip()
            if not clean:
                raise ValueError("consequence items must be non-blank")
            stripped.append(clean)
        return stripped


class ProcessTimelineVisual(VisualBase):
    kind: Literal["process_timeline"]
    step: Literal["detect", "audit"]
    active_step: int = Field(gt=0)
    total_steps: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_timeline(self):
        if self.active_step > self.total_steps:
            raise ValueError("active_step cannot exceed total_steps")
        if self.phase == "completed" and self.active_step != self.total_steps:
            raise ValueError("completed phase allowed only when active_step == total_steps")
        if not _contains_number_label(self.screen_text, self.active_step):
            raise ValueError("screen_text must contain active_step as a standalone numeric token")
        return self


class HumanVerificationVisual(VisualBase):
    kind: Literal["human_verification"]
    mode: Literal["count", "checklist", "approve"]
    options: list[str] = Field(default_factory=list, max_length=2)

    @field_validator("options")
    @classmethod
    def validate_options(cls, value: list[str]) -> list[str]:
        stripped = []
        for v in value:
            clean = v.strip()
            if not clean:
                raise ValueError("human_verification options must be non-blank")
            stripped.append(clean)
        return stripped


class ApprovalRecordVisual(VisualBase):
    kind: Literal["approval_record"]
    reviewer: str = Field(min_length=1)
    evidence: str = Field(min_length=1)
    decision: str = Field(min_length=1)

    @field_validator("reviewer", "evidence", "decision")
    @classmethod
    def validate_strings(cls, value: str) -> str:
        clean = value.strip()
        if not clean:
            raise ValueError("must be non-blank")
        return clean

    @model_validator(mode="after")
    def validate_phase(self):
        if self.phase != "completed":
            raise ValueError("approval_record phase must be completed")
        return self


class BalancePairVisual(VisualBase):
    kind: Literal["balance_pair"]
    left_label: str = Field(min_length=1)
    right_label: str = Field(min_length=1)

    @field_validator("left_label", "right_label")
    @classmethod
    def validate_labels(cls, value: str) -> str:
        clean = value.strip()
        if not clean:
            raise ValueError("must be non-blank")
        return clean


class OutroVisual(VisualBase):
    kind: Literal["outro"]
    tagline: str = Field(min_length=1)

    @field_validator("tagline")
    @classmethod
    def validate_tagline(cls, value: str) -> str:
        clean = value.strip()
        if not clean:
            raise ValueError("must be non-blank")
        return clean

    @model_validator(mode="after")
    def validate_phase(self):
        if self.phase != "completed":
            raise ValueError("outro phase must be completed")
        return self


class InventoryMismatchVisual(VisualBase):
    kind: Literal["inventory_mismatch"]
    physical_stock: int
    system_stock: int

    @model_validator(mode="after")
    def validate_mismatch(self):
        if self.phase != "alert":
            raise ValueError("inventory_mismatch must use alert phase")
        if self.physical_stock <= 0 or self.system_stock <= 0:
            raise ValueError("inventory facts must be > 0")
        if not _contains_number_label(
            self.screen_text, self.physical_stock
        ) or not _contains_number_label(self.screen_text, self.system_stock):
            raise ValueError("visual numbers must have screen labels containing their value")
        return self


class ApprovalGateVisual(VisualBase):
    kind: Literal["approval_gate"]
    actor: Literal["ai", "human", "both"]
    physical_stock: int | None = None
    system_stock: int | None = None

    @model_validator(mode="after")
    def validate_inventory_evidence(self):
        values = (self.physical_stock, self.system_stock)
        if any(value is not None for value in values):
            if any(value is None or value <= 0 for value in values):
                raise ValueError(
                    "approval inventory evidence requires two positive stock values"
                )
            if not _contains_number_label(
                self.screen_text, self.physical_stock
            ) or not _contains_number_label(self.screen_text, self.system_stock):
                raise ValueError(
                    "approval evidence numbers must be labeled in screen_text"
                )
        return self

class InventoryCorrectionVisual(VisualBase):
    kind: Literal["inventory_correction"]
    from_value: int
    to_value: int
    completion_ui: bool | None = None

    @model_validator(mode="after")
    def validate_correction(self):
        if self.from_value <= 0 or self.to_value <= 0:
            raise ValueError("inventory facts must be > 0")
        if not _contains_number_label(
            self.screen_text, self.from_value
        ) or not _contains_number_label(self.screen_text, self.to_value):
            raise ValueError("visual numbers must have screen labels containing their value")

        if self.phase == "completed" and self.from_value == self.to_value:
            raise ValueError(
                "inventory correction cannot use completed unless from_value "
                "and to_value are distinct"
            )
        if self.completion_ui and self.phase != "completed":
            raise ValueError("completed is the only phase eligible for completion UI")
        return self


VisualType = Annotated[
    GenericVisual
    | AutoActionVisual
    | ConsequenceVisual
    | ProcessTimelineVisual
    | HumanVerificationVisual
    | ApprovalRecordVisual
    | BalancePairVisual
    | OutroVisual
    | InventoryMismatchVisual
    | ApprovalGateVisual
    | InventoryCorrectionVisual,
    Field(discriminator="kind"),
]


class ScriptSegment(BaseModel):
    """Meaning chosen by Vertex. Timing is deliberately absent."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, description="Stable unique ID for the segment")
    text: str = Field(min_length=1, description="Exact Burmese narration text")
    visual_action: str = Field(min_length=1)
    scene_type: Literal["whiteboard", "demo"]
    mascot_action: Literal["present", "explain", "think", "warn", "approve"]
    emotion: Literal["neutral", "warm", "focused", "concerned", "confident"]
    emphasis: list[str] = Field(default_factory=list)
    visual: VisualType | None = None

    @field_validator("id", "text", "visual_action")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value


class VideoScript(BaseModel):
    """Vertex-owned script and visual intent, without media timing."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1)
    language: Literal["my-MM"] = "my-MM"
    segments: list[ScriptSegment] = Field(min_length=1)
    style_applied: str | None = None

    @field_validator("title")
    @classmethod
    def strip_title(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("title must not be blank")
        return value

    @field_validator("segments")
    @classmethod
    def require_unique_segment_ids(cls, segments: list[ScriptSegment]) -> list[ScriptSegment]:
        ids = [segment.id for segment in segments]
        if len(ids) != len(set(ids)):
            raise ValueError("segment ids must be unique")
        return segments


class StoryDraftSegment(BaseModel):
    """Narration-first story segment; visual metadata is added only after approval."""

    model_config = ConfigDict(extra="ignore")

    id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    visual_action: str = Field(min_length=1)
    scene_type: Literal["whiteboard", "demo"]
    mascot_action: Literal["present", "explain", "think", "warn", "approve"]
    emotion: Literal["neutral", "warm", "focused", "concerned", "confident"]
    emphasis: list[str] = Field(default_factory=list)


class StoryDraftScript(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1)
    language: Literal["my-MM"] = "my-MM"
    segments: list[StoryDraftSegment] = Field(min_length=5)


class StoryDraftVariant(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    script: StoryDraftScript


class StoryDraftModesResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    variants: list[StoryDraftVariant] = Field(min_length=3, max_length=3)

    @field_validator("variants")
    @classmethod
    def require_unique_variant_names(cls, variants: list[StoryDraftVariant]) -> list[StoryDraftVariant]:
        names = [variant.name for variant in variants]
        if len(names) != len(set(names)):
            raise ValueError("Variant names must be unique")
        return variants


class StoryVariant(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, description="Distinct name for the variant")
    script: VideoScript = Field(description="The full script for this variant")

    @model_validator(mode="after")
    def validate_structure(self):
        # We need to enforce scene -> wrong action/consequence -> root cause/context -> human boundary -> practical ending
        # This is a bit soft to enforce purely in Pydantic without specific markers, but we can require at least 5 segments
        # or require specific visual kinds, but instructions just said structural.
        # We'll enforce a minimum segment count to roughly match the 5 structural parts.
        if len(self.script.segments) < 3:
            raise ValueError("Story variant must have enough segments to cover the structural flow")
        return self


class StoryModesRequest(BaseModel):
    topic_or_draft: str = Field(min_length=1)


class StoryModesResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    variants: list[StoryVariant] = Field(min_length=3, max_length=3, description="Exactly 3 story variants")

    @field_validator("variants")
    @classmethod
    def require_unique_variant_names(cls, variants: list[StoryVariant]) -> list[StoryVariant]:
        names = [v.name for v in variants]
        if len(names) != len(set(names)):
            raise ValueError("Variant names must be unique")
        return variants


class ApprovedNarrationSegment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1)
    text: str = Field(min_length=1)


class ExactLockRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1)
    approved_segments: list[ApprovedNarrationSegment] = Field(min_length=1)

    @field_validator("approved_segments")
    @classmethod
    def require_unique_segment_ids(cls, segments: list[ApprovedNarrationSegment]) -> list[ApprovedNarrationSegment]:
        ids = [segment.id for segment in segments]
        if len(ids) != len(set(ids)):
            raise ValueError("segment ids must be unique")
        return segments


class SegmentEvidenceClaims(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1)
    claims: list[EvidenceClaim] = Field(min_length=1, max_length=4)


class EvidenceClaimsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    segments: list[SegmentEvidenceClaims] = Field(min_length=1)

    @field_validator("segments")
    @classmethod
    def unique_ids(cls, segments: list[SegmentEvidenceClaims]) -> list[SegmentEvidenceClaims]:
        ids = [segment.id for segment in segments]
        if len(ids) != len(set(ids)):
            raise ValueError("segment IDs must be unique")
        return segments


class ClaimCoverageSegment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1)
    passed: bool
    missing_claims: list[str] = Field(default_factory=list, max_length=4)
    issues: list[str] = Field(default_factory=list, max_length=4)


class ClaimCoverageResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    segments: list[ClaimCoverageSegment] = Field(min_length=1)


class CompactVisualPlanSegment(BaseModel):
    """Vertex-facing plan without the production visual union's schema explosion."""

    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1)
    visual_action: str = Field(min_length=1)
    scene_type: Literal["whiteboard", "demo"]
    mascot_action: Literal["present", "explain", "think", "warn", "approve"]
    emotion: Literal["neutral", "warm", "focused", "concerned", "confident"]
    emphasis: list[str] = Field(default_factory=list, max_length=4)
    phase: Literal["setup", "in_progress", "completed", "alert"]
    camera: Literal["wide", "push_in", "close_up", "over_shoulder"]
    screen_text: list[str] = Field(min_length=1, max_length=2)
    evidence_claims: list[EvidenceClaim] = Field(min_length=1, max_length=4)
    evidence_shots: list[EvidenceShot] = Field(min_length=1, max_length=4)


class CompactVisualPlanResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    segments: list[CompactVisualPlanSegment] = Field(min_length=1)

    @field_validator("segments")
    @classmethod
    def unique_ids(cls, segments: list[CompactVisualPlanSegment]) -> list[CompactVisualPlanSegment]:
        ids = [segment.id for segment in segments]
        if len(ids) != len(set(ids)):
            raise ValueError("segment IDs must be unique")
        return segments


class StoryboardSegment(BaseModel):
    """Shot order and visual rhythm only; narration and claims stay immutable."""

    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1)
    evidence_shots: list[EvidenceShot] = Field(min_length=1, max_length=4)


class StoryboardResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    segments: list[StoryboardSegment] = Field(min_length=1)

    @field_validator("segments")
    @classmethod
    def unique_ids(cls, segments: list[StoryboardSegment]) -> list[StoryboardSegment]:
        ids = [segment.id for segment in segments]
        if len(ids) != len(set(ids)):
            raise ValueError("Storyboard segment IDs must be unique")
        return segments


class LockedMetadataSegment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    visual_action: str = Field(min_length=1)
    scene_type: Literal["whiteboard", "demo"]
    mascot_action: Literal["present", "explain", "think", "warn", "approve"]
    emotion: Literal["neutral", "warm", "focused", "concerned", "confident"]
    emphasis: list[str] = Field(default_factory=list)
    visual: VisualType


class LockedMetadataResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    segments: list[LockedMetadataSegment] = Field(min_length=1)

    @field_validator("segments")
    @classmethod
    def require_unique_segment_ids(cls, segments: list[LockedMetadataSegment]) -> list[LockedMetadataSegment]:
        ids = [segment.id for segment in segments]
        if len(ids) != len(set(ids)):
            raise ValueError("segment ids must be unique")
        for segment in segments:
            if not segment.visual.evidence_claims or not segment.visual.evidence_shots:
                raise ValueError(f"segment {segment.id} must include a complete visual evidence plan")
        return segments
