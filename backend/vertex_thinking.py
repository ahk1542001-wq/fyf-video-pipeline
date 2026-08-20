"""Stage-specific Gemini 3 thinking controls for the FYF Vertex runtime."""

from __future__ import annotations

import logging
import os
from typing import Any

from google.genai import types

logger = logging.getLogger(__name__)

_STAGE_SETTINGS = {
    "script": ("FYF_VERTEX_THINKING_SCRIPT", "MEDIUM"),
    "story": ("FYF_VERTEX_THINKING_STORY", "MEDIUM"),
    "fact": ("FYF_VERTEX_THINKING_FACT", "HIGH"),
    "fact_coverage": ("FYF_VERTEX_THINKING_FACT_COVERAGE", "HIGH"),
    # Keep lock metadata latency-bounded; the immutable claim gate is enforced
    # by the Fact Agent and the downstream storyboard/QA validators.
    "lock": ("FYF_VERTEX_THINKING_LOCK", "MEDIUM"),
    "storyboard": ("FYF_VERTEX_THINKING_STORYBOARD", "MEDIUM"),
    "treatment": ("FYF_VERTEX_THINKING_TREATMENT", "MEDIUM"),
    "relationship": ("FYF_VERTEX_THINKING_RELATIONSHIP", "LOW"),
    "motion_repair": ("FYF_VERTEX_THINKING_MOTION_REPAIR", "MEDIUM"),
    "visual_verification": ("FYF_VERTEX_THINKING_VISUAL_VERIFY", "HIGH"),
    "final_visual_qa": ("FYF_VERTEX_THINKING_FINAL_QA", "HIGH"),
    "final_visual_repair": ("FYF_VERTEX_THINKING_FINAL_REPAIR", "MEDIUM"),
}

DEFAULT_THINKING_LEVELS = {
    stage: default for stage, (_, default) in _STAGE_SETTINGS.items()
}
_SUPPORTED_LEVELS = {"LOW", "MEDIUM", "HIGH"}


def thinking_level_for(stage: str) -> types.ThinkingLevel:
    """Resolve a supported Gemini 3.7 Flash thinking level for one stage."""
    try:
        env_name, default = _STAGE_SETTINGS[stage]
    except KeyError as exc:
        raise ValueError(f"Unknown Vertex thinking stage: {stage}") from exc

    configured = os.getenv(env_name, default).strip().upper()
    if configured not in _SUPPORTED_LEVELS:
        logger.warning(
            "Ignoring unsupported %s=%r; using %s",
            env_name,
            configured,
            default,
        )
        configured = default
    return getattr(types.ThinkingLevel, configured)


def generation_config_for(stage: str, **kwargs: Any) -> types.GenerateContentConfig:
    """Build a GenerateContentConfig with the stage's thinking policy."""
    return types.GenerateContentConfig(
        thinking_config=types.ThinkingConfig(thinking_level=thinking_level_for(stage)),
        **kwargs,
    )
