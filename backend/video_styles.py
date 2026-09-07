"""Style Presets and Visual Treatments for FYF Dynamic Pipeline."""

from __future__ import annotations

import copy
from typing import Any

DEFAULT_STYLE_ID = "fyf_explainer"

# Camera/motion values MUST stay within the video_contract enums:
#   camera: "wide" | "push_in" | "close_up" | "over_shoulder"
#   motion_preset: "slow_push" | "pan_left" | "pan_right" | "drift" | "static"
VIDEO_STYLES: dict[str, dict[str, Any]] = {
    "fyf_explainer": {
        "id": "fyf_explainer",
        "name": "FYF Explainer (Default)",
        "description": "Standard high-clarity whiteboard with balanced mascot pacing and diagrammatic evidence.",
        "preferred_cameras": ["wide", "push_in", "close_up"],
        "preferred_motion_presets": ["pan_right", "slow_push", "static"],
        "color_theme": "emerald_clarity",
        "badge_accent": "#16856B",
    },
    "cinematic_continuity": {
        "id": "cinematic_continuity",
        "name": "Cinematic Continuity",
        "description": "Dramatic push-ins, close-up evidence inspection, and dynamic camera movements.",
        "preferred_cameras": ["push_in", "close_up", "over_shoulder"],
        "preferred_motion_presets": ["slow_push", "pan_left", "drift"],
        "color_theme": "cinematic_slate",
        "badge_accent": "#2563EB",
    },
    "evidence_story": {
        "id": "evidence_story",
        "name": "Evidence Story",
        "description": "High-density data visualization, documentary pacing, and document inspection focus.",
        "preferred_cameras": ["wide", "over_shoulder", "close_up"],
        "preferred_motion_presets": ["static", "pan_right", "slow_push"],
        "color_theme": "evidence_amber",
        "badge_accent": "#D97706",
    },
}

GENRE_STYLES: dict[str, dict[str, Any]] = {
    "cinematic_documentary": {
        "id": "cinematic_documentary",
        "name": "Cinematic Documentary",
        "description": "Atmospheric visuals, natural lighting, deep investigative pacing, and wide establishing shots.",
        "preferred_cameras": ["wide", "push_in", "over_shoulder"],
        "preferred_motion_presets": ["slow_push", "drift", "pan_left"],
        "color_theme": "documentary_sepia",
        "badge_accent": "#854D0E",
    },
    "tech_explainer": {
        "id": "tech_explainer",
        "name": "Tech & Product Explainer",
        "description": "Crisp isometric diagrams, blueprint grids, high-contrast typography, and dynamic product breakdown.",
        "preferred_cameras": ["close_up", "push_in", "wide"],
        "preferred_motion_presets": ["pan_right", "slow_push", "static"],
        "color_theme": "tech_cyan",
        "badge_accent": "#0284C7",
    },
    "investigative": {
        "id": "investigative",
        "name": "Evidence & Investigative Cinema",
        "description": "Document inspection, forensic data points, high-contrast evidence reveals, and intense camera focus.",
        "preferred_cameras": ["close_up", "over_shoulder", "push_in"],
        "preferred_motion_presets": ["static", "slow_push", "pan_left"],
        "color_theme": "investigative_crimson",
        "badge_accent": "#DC2626",
    },
    "narrative": {
        "id": "narrative",
        "name": "Narrative Short",
        "description": "Character-driven emotional depth, shallow depth of field, dramatic shadows, and evocative pacing.",
        "preferred_cameras": ["close_up", "push_in", "wide"],
        "preferred_motion_presets": ["drift", "slow_push", "pan_right"],
        "color_theme": "narrative_purple",
        "badge_accent": "#7C3AED",
    },
}

ALL_STYLES_AND_GENRES: dict[str, dict[str, Any]] = {**VIDEO_STYLES, **GENRE_STYLES}


def list_available_styles() -> list[dict[str, Any]]:
    """Return all available video style definitions."""
    return list(VIDEO_STYLES.values())


get_available_styles = list_available_styles


def list_available_genres() -> list[dict[str, Any]]:
    """Return all available cinematic genres."""
    return list(GENRE_STYLES.values())


get_available_genres = list_available_genres


def get_style_config(style_id: str | None = None) -> dict[str, Any]:
    """Retrieve configuration for a specific video style or genre, falling back to default."""
    if not style_id:
        return VIDEO_STYLES[DEFAULT_STYLE_ID]
    if style_id in VIDEO_STYLES:
        return VIDEO_STYLES[style_id]
    if style_id in GENRE_STYLES:
        return GENRE_STYLES[style_id]
    if style_id == "explainer":
        return VIDEO_STYLES["fyf_explainer"]
    return VIDEO_STYLES[DEFAULT_STYLE_ID]


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
