"""Controlled Dynamic Video Styles and Direction Presets for FYF Pipeline."""

from __future__ import annotations

import copy
import logging
from typing import Any, Literal

logger = logging.getLogger(__name__)

VideoStyleId = Literal["fyf_explainer", "cinematic_continuity", "evidence_story"]

VIDEO_STYLES: dict[str, dict[str, Any]] = {
    "fyf_explainer": {
        "id": "fyf_explainer",
        "name": "FYF Explainer",
        "description": "Balanced explanatory pacing with interactive whiteboard diagrams, concept cards, and engaging mascot presence.",
        "pacing_multiplier": 1.0,
        "default_scene_types": ["whiteboard", "demo"],
        "mascot_presence_policy": "balanced",
        "preferred_cameras": ["wide", "push_in"],
        "preferred_motion_presets": ["static", "pan_left", "pan_right"],
    },
    "cinematic_continuity": {
        "id": "cinematic_continuity",
        "name": "Cinematic Continuity",
        "description": "Smooth, continuous visual flow with dynamic camera push-ins, close-ups, and subtle mascot framing.",
        "pacing_multiplier": 0.95,
        "default_scene_types": ["demo", "whiteboard"],
        "mascot_presence_policy": "subtle",
        "preferred_cameras": ["push_in", "close_up", "over_shoulder"],
        "preferred_motion_presets": ["zoom_in", "pan_left"],
    },
    "evidence_story": {
        "id": "evidence_story",
        "name": "Evidence Story",
        "description": "High-density data visualization, comparison metrics, structured proofs, and strict claim verification.",
        "pacing_multiplier": 1.05,
        "default_scene_types": ["whiteboard", "demo"],
        "mascot_presence_policy": "evidence_focused",
        "preferred_cameras": ["wide", "close_up"],
        "preferred_motion_presets": ["static", "zoom_in"],
    },
}

DEFAULT_STYLE_ID: VideoStyleId = "fyf_explainer"


def get_available_styles() -> list[dict[str, Any]]:
    """Return all available video style definitions."""
    return list(VIDEO_STYLES.values())


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
