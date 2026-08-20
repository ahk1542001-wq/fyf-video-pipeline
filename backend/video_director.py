"""Deterministic FYF storyboard direction gates applied before immutable locks."""

from __future__ import annotations

import logging
from typing import Any

from backend.creative_quality import audit_creative_quality, rebalance_creative_rhythm
from video_contract import VideoScript


logger = logging.getLogger(__name__)


def _normalized(value: str) -> str:
    return " ".join(value.casefold().split())


def validate_director_contract(script_data: dict[str, Any]) -> dict[str, Any]:
    """Return a validated, narration-preserving script or raise on visual repetition."""
    script = VideoScript.model_validate(script_data)
    original_narration = [(segment.id, segment.text) for segment in script.segments]
    media_by_segment: list[set[str]] = []
    treatment_by_segment: list[set[tuple[str, str, str]]] = []
    mascot_by_segment: list[bool] = []
    previous_caption: str | None = None

    for segment in script.segments:
        if segment.visual is None:
            raise ValueError(f"Director contract requires visual evidence for {segment.id}")
        shots = segment.visual.evidence_shots
        if not shots:
            raise ValueError(f"Director contract requires at least one shot for {segment.id}")
        total = sum(shot.hold_fraction for shot in shots)
        if abs(total - 1.0) > 0.001:
            raise ValueError(f"Director shot holds must sum to 1.0 for {segment.id}")
        media_by_segment.append({shot.media_type for shot in shots})
        treatment_by_segment.append({
            (shot.media_type, shot.composition, shot.motion_preset) for shot in shots
        })
        mascot_by_segment.append(any(shot.mascot_presence != "none" for shot in shots))
        for shot in shots:
            caption = _normalized(shot.caption)
            if caption == previous_caption:
                raise ValueError(f"Director contract rejects adjacent duplicate caption at {shot.shot_id}")
            previous_caption = caption

    if len(script.segments) >= 8:
        all_media = set().union(*media_by_segment)
        if "motion_graphic" not in all_media or not all_media.intersection({"generated_image", "generated_video"}):
            raise ValueError("Long-form direction requires generated media and deterministic motion evidence")

    for index in range(max(0, len(treatment_by_segment) - 3)):
        window = treatment_by_segment[index:index + 4]
        if all(len(item) == 1 and item == window[0] for item in window):
            raise ValueError("Director contract rejects four consecutive single-treatment segments")
    for index in range(max(0, len(mascot_by_segment) - 3)):
        if all(mascot_by_segment[index:index + 4]):
            raise ValueError("Director contract rejects four consecutive mascot-present segments")

    all_treatments = [shot.treatment for segment in script.segments for shot in segment.visual.evidence_shots]
    if any(t is not None for t in all_treatments) and not all(t is not None for t in all_treatments):
        raise ValueError("incomplete treatment metadata")

    directed = script.model_dump(mode="json")

    if all(t is not None for t in all_treatments):
        report_dict = audit_creative_quality(directed, strict=True)
        if not report_dict.get("passed", False):
            codes = ", ".join(report_dict.get("failure_codes", []))
            raise ValueError(f"Creative audit failed: {codes}")

    if original_narration != [(item["id"], item["text"]) for item in directed["segments"]]:
        raise ValueError("Director pass must not change narration or segment IDs")
    return directed


def _drop_partial_treatment_metadata(script_data: dict[str, Any]) -> None:
    """Treat optional director treatments as all-or-nothing metadata.

    A model may attach a valid treatment to only some storyboard shots. The
    treatment is enrichment, not evidence, so partial enrichment must not block
    a production lock or silently change the visual contract. Dropping the
    incomplete optional set preserves the required shots and lets the renderer
    use its existing media/composition fields.
    """
    shots = [
        shot
        for segment in script_data.get("segments", [])
        for shot in segment.get("visual", {}).get("evidence_shots", [])
    ]
    treated = [shot for shot in shots if shot.get("treatment") is not None]
    if treated and len(treated) != len(shots):
        logger.warning(
            "Dropping partial optional treatment metadata before director validation"
        )
        for shot in shots:
            shot["treatment"] = None


def apply_director_pass(script_data: dict[str, Any]) -> dict[str, Any]:
    """Normalize visual rhythm without changing narration, claims, or shot assets."""
    script = VideoScript.model_validate(script_data).model_dump(mode="json")
    script = rebalance_creative_rhythm(script)
    _drop_partial_treatment_metadata(script)
    consecutive_mascot_segments = 0
    for segment in script["segments"]:
        shots = segment["visual"]["evidence_shots"]
        has_mascot = any(shot["mascot_presence"] != "none" for shot in shots)
        consecutive_mascot_segments = consecutive_mascot_segments + 1 if has_mascot else 0
        if consecutive_mascot_segments >= 4:
            for shot in shots:
                shot["mascot_presence"] = "none"
            consecutive_mascot_segments = 0
    return validate_director_contract(script)
