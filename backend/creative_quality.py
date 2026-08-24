"""Deterministic creative-quality audit for rendered director inputs."""
from __future__ import annotations

import copy
import math
from typing import Any

from pydantic import Field

from .director_context import DirectorPolicy as _DirectorPolicy


class DirectorPolicy(_DirectorPolicy):
    """Director policy with configurable creative-quality guardrails."""

    max_treatment_run: int = Field(default=2, ge=1)
    max_center_run: int = Field(default=2, ge=1)
    max_diagram_ratio: float = Field(default=0.25, ge=0, le=1)
    diagram_ratio_min_scenes: int = Field(default=12, ge=1)
    max_seconds_without_reset: float = Field(default=30, gt=0)
    max_mascot_run: int = Field(default=3, ge=1)
    max_transition_run: int = Field(default=3, ge=1)


def enforce_attention_reset_cadence(script: dict[str, Any], policy: DirectorPolicy | None = None) -> dict[str, Any]:
    """Force an attention_reset beat wherever the deterministic QA cadence would breach.

    Creative QA fails a scene with VISUAL_WORLD_NOT_RESET when too many frames pass
    without any treatment carrying attention_reset. The director model cannot fix
    this reliably because fallback treatments assign attention_reset by index, so
    we mirror the QA arithmetic here and set the flag on the last shot of any
    breaching scene. Locked narration and evidence claims are untouched.
    """
    policy = policy or DirectorPolicy()
    fps = float(script.get("fps") or 30)
    # Mirror the creative-QA threshold; the director policy variant may not carry it.
    max_seconds = float(getattr(policy, "max_seconds_without_reset", 30))
    max_gap = max_seconds * fps
    reset_start = None
    for scene in script.get("segments", []):
        start = scene.get("startFrame")
        end = scene.get("endFrame")
        if start is None or end is None:
            continue
        if reset_start is None:
            reset_start = start
        visual = scene.get("visual") or {}
        shots = visual.get("evidence_shots") or []
        treatments = [shot.get("treatment") or {} for shot in shots]
        if any(treatment.get("attention_reset") for treatment in treatments):
            reset_start = end
        elif end - reset_start > max_gap and shots:
            treatments[-1]["attention_reset"] = True
            shots[-1]["treatment"] = treatments[-1]
            reset_start = end
    return script


def _scenes(value: dict[str, Any]) -> list[dict[str, Any]]:
    return value.get("segments") or value.get("scenes") or []


def _shot(scene: dict[str, Any]) -> dict[str, Any]:
    shots = (scene.get("visual") or {}).get("evidence_shots") or []
    return shots[0] if shots else scene


def _signature(scene: dict[str, Any]) -> tuple[str, bool, dict[str, Any], bool]:
    shot = _shot(scene)
    treatment = shot.get("treatment") or scene.get("treatment") or {}
    treatment_present = "treatment" in shot or "treatment" in scene
    kind = treatment.get("treatment_type")
    if not kind:
        media = shot.get("media_type")
        motion_spec = shot.get("motion_spec") or {}
        layout = str(motion_spec.get("layout", "")).lower()
        diagram_layouts = {"relationship", "sequence", "concept", "count", "comparison", "directional_branch"}
        if media == "motion_graphic":
            kind = "motion_diagram" if layout in diagram_layouts else "motion_graphic"
        elif media:
            kind = "story_scene"
        else:
            kind = "unknown"
    center = (
        kind == "motion_diagram"
        and shot.get("composition") == "focal_center"
        and shot.get("media_type") == "motion_graphic"
    )
    return kind, center, treatment, treatment_present


def _compatible_treatments(shot: dict[str, Any]) -> list[str]:
    """Return renderer grammars that can faithfully use this shot's verified evidence."""
    if shot.get("media_type") != "motion_graphic" or not shot.get("motion_spec"):
        return ["story_scene", "object_action", "comparison_transform"]
    layout = str((shot.get("motion_spec") or {}).get("layout", ""))
    return {
        "comparison": ["comparison_transform", "editorial_data", "motion_diagram"],
        "count": ["editorial_data", "object_action", "motion_diagram"],
        "sequence": ["object_action", "ui_proof", "motion_diagram"],
        "relationship": ["comparison_transform", "object_action", "ui_proof", "motion_diagram"],
        "directional_branch": ["ui_proof", "comparison_transform", "motion_diagram"],
        "concept": ["kinetic_type", "object_action", "motion_diagram"],
    }.get(layout, ["object_action", "ui_proof", "motion_diagram"])


def rebalance_creative_rhythm(
    script_or_render_input: dict[str, Any],
    policy: DirectorPolicy | None = None,
    scene_ids: set[str] | None = None,
) -> dict[str, Any]:
    """Enforce brand rhythm constraints without modifying narration or verified evidence."""
    policy = policy or DirectorPolicy()
    result = copy.deepcopy(script_or_render_input)
    scenes = _scenes(result)
    editable = scene_ids or {str(scene.get("id", f"S{i + 1}")) for i, scene in enumerate(scenes)}
    max_diagrams = math.floor(len(scenes) * policy.max_diagram_ratio)
    if len(scenes) < policy.diagram_ratio_min_scenes:
        max_diagrams = len(scenes)

    treatment_counts: dict[str, int] = {}
    previous_treatment: str | None = None
    treatment_run = 0
    diagram_count = 0
    mascot_run = 0
    previous_transition: str | None = None
    transition_run = 0
    transitions = ("cut", "crossfade", "push", "wipe")

    for index, scene in enumerate(scenes):
        shot = _shot(scene)
        treatment = shot.get("treatment") or {}
        kind = str(treatment.get("treatment_type") or "unknown")
        scene_id = str(scene.get("id", f"S{index + 1}"))
        editable_scene = scene_id in editable
        would_repeat = kind == previous_treatment and treatment_run >= policy.max_treatment_run
        diagram_exhausted = kind == "motion_diagram" and diagram_count >= max_diagrams
        if editable_scene and treatment and (would_repeat or diagram_exhausted):
            candidates = [candidate for candidate in _compatible_treatments(shot)
                          if candidate != previous_treatment
                          and (candidate != "motion_diagram" or diagram_count < max_diagrams)]
            if candidates:
                kind = min(candidates, key=lambda candidate: (treatment_counts.get(candidate, 0), candidates.index(candidate)))
                treatment["treatment_type"] = kind

        if kind == previous_treatment:
            treatment_run += 1
        else:
            previous_treatment, treatment_run = kind, 1
        treatment_counts[kind] = treatment_counts.get(kind, 0) + 1
        if kind == "motion_diagram":
            diagram_count += 1

        has_mascot = shot.get("mascot_presence", "none") != "none"
        if has_mascot and mascot_run >= policy.max_mascot_run and editable_scene:
            shot["mascot_presence"] = "none"
            mascot_run = 0
        elif has_mascot:
            mascot_run += 1
        else:
            mascot_run = 0

        transition = shot.get("transition")
        if transition is not None:
            if transition == previous_transition:
                transition_run += 1
            else:
                previous_transition, transition_run = transition, 1
            if transition_run > policy.max_transition_run and editable_scene and shot is not scene:
                replacement = next(value for value in transitions if value != previous_transition)
                shot["transition"] = replacement
                previous_transition, transition_run = replacement, 1
        else:
            previous_transition = None
            transition_run = 0

    return result


def _run_failures(items: list[Any], limit: int, code: str, true_only: bool = False) -> list[tuple[int, str]]:
    failures = []
    start = 0
    while start < len(items):
        end = start + 1
        while end < len(items) and items[end] == items[start]:
            end += 1
        if (not true_only or items[start]) and end - start > limit:
            failures.extend((i, code) for i in range(start, end))
        start = end
    return failures


def audit_creative_quality(script_or_render_input: dict[str, Any], policy: DirectorPolicy | None = None, strict: bool = True) -> dict[str, Any]:
    policy = policy or DirectorPolicy()
    scenes = _scenes(script_or_render_input)
    signatures, treatments = [], []
    failures: dict[int, set[str]] = {}
    def fail(index: int, code: str) -> None:
        failures.setdefault(index, set()).add(code)
    for i, scene in enumerate(scenes):
        kind, center, treatment, treatment_present = _signature(scene)
        signatures.append({"scene_id": scene.get("id", f"S{i + 1}"), "treatment": kind,
                           "center_card": center})
        treatments.append(treatment)
        kinetic = treatment.get("treatment_type") == "kinetic_type"
        if treatment_present and not kinetic and (not treatment.get("action") or not treatment.get("change")):
            fail(i, "STATIC_ACTION_MISSING")
    for values, code, limit, true_only in (([x["treatment"] for x in signatures], "TREATMENT_RUN_REPEATED", policy.max_treatment_run, False),
                                           ([x["center_card"] for x in signatures], "CENTER_CARD_SATURATION", policy.max_center_run, True),
                                           ([_shot(s).get("mascot_presence", "none") != "none" for s in scenes], "MASCOT_CADENCE_REPEATED", policy.max_mascot_run, True),
                                           ([_shot(s).get("transition") for s in scenes], "TRANSITION_RUN_REPEATED", policy.max_transition_run, False)):
        for i, code_at in _run_failures(values, limit, code, true_only): fail(i, code_at)
    diagrams = [i for i, x in enumerate(signatures) if x["treatment"] == "motion_diagram"]
    if len(scenes) >= policy.diagram_ratio_min_scenes and len(diagrams) / len(scenes) > policy.max_diagram_ratio:
        for i in diagrams: fail(i, "MOTION_DIAGRAM_SATURATION")
    fps = float(script_or_render_input.get("fps") or 30)
    previous_end = None
    reset_start = None
    for i, scene in enumerate(scenes):
        start, end = scene.get("startFrame"), scene.get("endFrame")
        if start is None or end is None: continue
        if reset_start is None: reset_start = start
        if treatments[i].get("attention_reset"): reset_start = end
        if end - reset_start > policy.max_seconds_without_reset * fps: fail(i, "VISUAL_WORLD_NOT_RESET")
        previous_end = end
    failed_clusters = []
    for i in sorted(failures):
        if not failed_clusters or i != failed_clusters[-1]["_end"] + 1:
            failed_clusters.append({"cluster_id": len(failed_clusters), "scene_ids": [], "failure_codes": [], "_end": i})
        cluster = failed_clusters[-1]; cluster["scene_ids"].append(signatures[i]["scene_id"]); cluster["failure_codes"] = sorted(set(cluster["failure_codes"]) | failures[i]); cluster["_end"] = i
    for cluster in failed_clusters: cluster.pop("_end", None)
    codes = sorted({code for values in failures.values() for code in values})
    return {"passed": not failures, "policy_version": policy.version, "scene_signatures": signatures,
            "failed_clusters": failed_clusters, "failure_codes": codes, "issues": [{"scene_id": signatures[i]["scene_id"], "codes": sorted(c)} for i, c in sorted(failures.items())],
            "terminal_state": "pass" if not failures else "repair_required"}


def failed_scene_ids(report: dict[str, Any]) -> list[str]:
    ids = {scene_id for cluster in report.get("failed_clusters", []) for scene_id in cluster.get("scene_ids", [])}
    def key(value: str) -> tuple[Any, ...]:
        import re
        return tuple(int(x) if x.isdigit() else x for x in re.split(r"(\d+)", value))
    return sorted(ids, key=key)
