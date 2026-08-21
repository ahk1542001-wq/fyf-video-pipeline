"""Execution Runner for FYF Google ADK Agent Pipeline."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

try:
    from agent.tools import (
        audit_story_quality,
        draft_story_segments,
        plan_visual_shots,
        research_topic,
    )
    from job_store import write_json_atomically
    from video_contract import VideoScript
except ImportError:
    from backend.agent.tools import (
        audit_story_quality,
        draft_story_segments,
        plan_visual_shots,
        research_topic,
    )
    from backend.job_store import write_json_atomically
    from backend.video_contract import VideoScript

logger = logging.getLogger(__name__)


def run_adk_pipeline(
    topic: str,
    duration_mode: str = "short",
    job_dir: Path | None = None,
) -> dict[str, Any]:
    """Execute the ADK-orchestrated story generation pipeline.
    
    Coordinates research, narration drafting, quality audit, and visual shot planning.
    Persists checkpoints to `job_dir` when provided.
    
    Args:
        topic: Topic or draft input.
        duration_mode: Target duration ("short" or "standard").
        job_dir: Optional job directory to persist intermediate artifacts.
        
    Returns:
        Dictionary containing the completed VideoScript, narration draft, and audit report.
    """
    logger.info("Starting ADK pipeline for topic: %s (mode: %s)", topic, duration_mode)
    
    # Step 1: Research
    research = research_topic(topic, duration_mode=duration_mode)
    if job_dir:
        write_json_atomically(job_dir / "research.json", research)
        
    # Step 2: Draft narration
    draft = draft_story_segments(topic, duration_mode=duration_mode)
    if job_dir:
        write_json_atomically(job_dir / "narration.json", draft)
        
    # Step 3: Audit draft quality
    audit = audit_story_quality(draft)
    if job_dir:
        write_json_atomically(job_dir / "story_audit.json", audit)
        
    if not audit.get("passed", False):
        logger.warning("Story draft audit flagged issues: %s", audit.get("issues"))
        
    # Step 4: Plan visual storyboard
    title = draft.get("title", topic)
    segments = draft.get("segments", [])
    directed_script = plan_visual_shots(title=title, segments=segments)
    
    validated = VideoScript.model_validate(directed_script).model_dump(mode="json")
    if job_dir:
        write_json_atomically(job_dir / "result.json", validated)
        
    return {
        "script": validated,
        "draft": draft,
        "audit": audit,
        "research": research,
    }
