"""Google ADK Agent Module for FYF Story and Video Planning."""

import sys
from pathlib import Path

_repo_root = Path(__file__).resolve().parent.parent.parent
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))
_backend_root = _repo_root / "backend"
if str(_backend_root) not in sys.path:
    sys.path.insert(0, str(_backend_root))

from backend.agent.fyf_producer import create_fyf_producer_agent
from backend.agent.runner import run_adk_pipeline
from backend.agent.tools import (
    audit_story_quality,
    draft_story_segments,
    plan_visual_shots,
    research_topic,
)

__all__ = [
    "create_fyf_producer_agent",
    "run_adk_pipeline",
    "research_topic",
    "draft_story_segments",
    "audit_story_quality",
    "plan_visual_shots",
]
