"""Pure, serializable rolling context for visual direction."""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from video_contract import VisualTreatment


class DirectorPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: str = "fyf-director-v1"
    history_size: int = Field(default=5, ge=1)
    prohibited_run_length: int = Field(default=3, ge=1)


class DirectorContext(BaseModel):
    model_config = ConfigDict(extra="forbid")
    policy_version: str
    current_segment_id: str
    current_segment: dict[str, Any]
    previous_treatments: list[VisualTreatment] = Field(default_factory=list)
    prohibited_treatments: list[str] = Field(default_factory=list)
    recent_focal_objects: list[str] = Field(default_factory=list)
    recent_visual_worlds: list[str] = Field(default_factory=list)
    recent_compositions: list[str] = Field(default_factory=list)
    recent_motion_families: list[str] = Field(default_factory=list)
    recent_mascot_presence: list[str] = Field(default_factory=list)
    recent_text_modes: list[str] = Field(default_factory=list)
    recent_text_density: list[int] = Field(default_factory=list)
    brand_constraints: dict[str, Any] = Field(default_factory=lambda: {"colors": ["#F4F0E6", "#30382C", "#16856B", "#A8B7A2"], "typography": "Burmese-safe"})
    media_budget_remaining: int | None = None
    generated_media_allowed: bool = True


def build_director_context(segments: list[dict], current_index: int, policy: DirectorPolicy) -> DirectorContext:
    if not 0 <= current_index < len(segments):
        raise ValueError("current_index out of range")
    current = segments[current_index]
    accepted = []
    for segment in segments[:current_index]:
        visual = segment.get("visual") or {}
        shots = visual.get("evidence_shots") or []
        for shot in shots:
            treatment = shot.get("treatment")
            if treatment is not None:
                accepted.append((VisualTreatment.model_validate(treatment), shot, visual))
        if not shots and segment.get("treatment") is not None:
            accepted.append((VisualTreatment.model_validate(segment["treatment"]), {}, visual))
    recent = accepted[-policy.history_size:]
    recent_treatments = [item[0] for item in recent]
    prohibited = []
    if len(recent_treatments) >= policy.prohibited_run_length and len({item.treatment_type for item in recent_treatments[-policy.prohibited_run_length:]}) == 1:
        prohibited.append(recent_treatments[-1].treatment_type)
    return DirectorContext(
        policy_version=policy.version,
        current_segment_id=current["id"],
        current_segment=current,
        previous_treatments=recent_treatments,
        prohibited_treatments=prohibited,
        recent_focal_objects=[treatment.focal_object for treatment, _, _ in recent if treatment.focal_object],
        recent_visual_worlds=[treatment.visual_world for treatment, _, _ in recent],
        recent_compositions=[shot.get("composition", "focal_center") for _, shot, _ in recent],
        recent_motion_families=[treatment.motion_family for treatment, _, _ in recent],
        recent_mascot_presence=[shot.get("mascot_presence", "none") for _, shot, _ in recent],
        recent_text_modes=[treatment.text_mode for treatment, _, _ in recent],
        recent_text_density=[sum(1 for text in (visual.get("screen_text") or []) if text.strip()) for _, _, visual in recent],
    )
