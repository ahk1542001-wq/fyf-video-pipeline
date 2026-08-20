"""Central FYF Vertex model routes; env-overridable to survive model lifecycle changes."""

import os


ROUTES = {
    "script": ("FYF_VERTEX_SCRIPT_MODEL", "gemini-3.7-flash"),
    "story_polish": ("FYF_VERTEX_STORY_MODEL", "gemini-3.7-flash"),
    # Keep retries on the proven primary Flash route by default.  A Pro route
    # remains an explicit env override, not an automatic production surprise.
    "story_fallback": ("FYF_VERTEX_STORY_FALLBACK_MODEL", "gemini-3.7-flash"),
    "fact_extraction": ("FYF_VERTEX_FACT_MODEL", "gemini-3.7-flash"),
    "visual_direction": ("FYF_VERTEX_VISUAL_DIRECTOR_MODEL", "gemini-3.7-flash"),
    "storyboard_direction": ("FYF_VERTEX_STORYBOARD_MODEL", "gemini-3.7-flash"),
    "visual_generation": ("FYF_VERTEX_IMAGE_MODEL", "gemini-3.1-flash-image"),
    "visual_generation_quality": ("FYF_VERTEX_QUALITY_IMAGE_MODEL", "gemini-3-pro-image"),
    "video_generation": ("FYF_VERTEX_VIDEO_MODEL", "veo-3.1-generate-001"),
    "visual_verification": ("FYF_VERTEX_VISUAL_VERIFY_MODEL", "gemini-3.7-flash"),
    "visual_verification_fallback": ("FYF_VERTEX_VISUAL_VERIFY_FALLBACK_MODEL", "gemini-3.7-flash"),
    "repair": ("FYF_VERTEX_REPAIR_MODEL", "gemini-3.7-flash"),
    "lock": ("FYF_VERTEX_LOCK_MODEL", "gemini-3.7-flash"),
    "high_volume": ("FYF_VERTEX_HIGH_VOLUME_MODEL", "gemini-3.7-flash"),
}


def model_for(stage: str) -> str:
    try:
        env_name, default = ROUTES[stage]
    except KeyError as exc:
        raise ValueError(f"Unknown Vertex model route: {stage}") from exc
    return os.getenv(env_name, default)
