"""Style Presets and Visual Treatments for FYF Dynamic Pipeline."""

from __future__ import annotations

import copy
from typing import Any

DEFAULT_STYLE_ID = "fyf_explainer"

VIDEO_STYLES: dict[str, dict[str, Any]] = {
    "fyf_explainer": {
        "id": "fyf_explainer",
        "name": "FYF Explainer (Default)",
        "description": "Standard high-clarity whiteboard with balanced mascot pacing and diagrammatic evidence.",
        "preferred_cameras": ["wide", "medium", "mascot_focus"],
        "preferred_motion_presets": ["pan_right", "zoom_in", "steady"],
        "color_theme": "emerald_clarity",
        "badge_accent": "#16856B",
    },
    "cinematic_continuity": {
        "id": "cinematic_continuity",
        "name": "Cinematic Continuity",
        "description": "Dramatic push-ins, close-up evidence inspection, and dynamic camera movements.",
        "preferred_cameras": ["push_in", "close_up", "medium"],
        "preferred_motion_presets": ["zoom_in", "pan_left", "push_in"],
        "color_theme": "cinematic_slate",
        "badge_accent": "#2563EB",
    },
    "evidence_story": {
        "id": "evidence_story",
        "name": "Evidence Story",
        "description": "High-density data visualization, documentary pacing, and document inspection focus.",
        "preferred_cameras": ["diorama_overview", "document_split", "macro_inspection"],
        "preferred_motion_presets": ["steady", "pan_right", "zoom_in"],
        "color_theme": "evidence_amber",
        "badge_accent": "#D97706",
    },
}


def list_available_styles() -> list[dict[str, Any]]:
    """Return all available video style definitions."""
    return list(VIDEO_STYLES.values())


get_available_styles = list_available_styles


def get_style_config(style_id: str | None = None) -> dict[str, Any]:
    """Retrieve configuration for a specific video style, falling back to default."""
    if not style_id or style_id not in VIDEO_STYLES:
        return VIDEO_STYLES[DEFAULT_STYLE_ID]
    return VIDEO_STYLES[style_id]


def apply_video_style(
    script: dict[str, Any],
    style_id: str | None = None,
) -> dict[str, Any]:
    """Apply style-specific camera, pacing, and visual preferences to a script.

    Preserves exact narration, segment IDs, and verified evidence claims while
    adjusting camera angles and motion presets to match the selected style.

    Args:
        script: Validated VideoScript dictionary.
        style_id: One of 'fyf_explainer', 'cinematic_continuity', 'evidence_story'.

    Returns:
        New script dictionary with updated stylistic director hints.
    """
    config = get_style_config(style_id)
    styled = copy.deepcopy(script)

    preferred_cameras = config["preferred_cameras"]
    preferred_motions = config["preferred_motion_presets"]

    segments = styled.get("segments", [])
    for index, segment in enumerate(segments):
        visual = segment.get("visual")
        if not visual or not isinstance(visual, dict):
            continue

        camera = preferred_cameras[index % len(preferred_cameras)]
        visual["camera"] = camera

        shots = visual.get("evidence_shots", [])
        for shot_idx, shot in enumerate(shots):
            if isinstance(shot, dict):
                motion = preferred_motions[shot_idx % len(preferred_motions)]
                shot["motion_preset"] = motion

    styled["style_applied"] = config["id"]
    return styled
