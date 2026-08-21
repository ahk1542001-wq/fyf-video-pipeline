"""Google ADK Agent Definition for FYF Story and Video Planning."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from google.adk import Agent

_repo_root = Path(__file__).resolve().parent.parent.parent
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))
_backend_root = _repo_root / "backend"
if str(_backend_root) not in sys.path:
    sys.path.insert(0, str(_backend_root))

from backend.agent.tools import (
    audit_story_quality,
    draft_story_segments,
    plan_visual_shots,
    research_topic,
)

PRODUCER_INSTRUCTION = """
You are the FYF Video Executive Producer Agent, responsible for creating high-impact,
evidence-led Burmese vertical videos.

Follow this production workflow strictly:
1. Research the user's topic using `research_topic` to understand the factual angle and target audience.
2. Draft the Burmese narration segments using `draft_story_segments`.
3. Audit the draft narration quality using `audit_story_quality` to ensure Burmese character limits and pacing.
4. Plan the visual storyboard and director treatments using `plan_visual_shots`.
5. Return the final locked VideoScript structure with all segments, scenes, and visual directions.
""".strip()


def create_fyf_producer_agent(
    model_name: str = "gemini-2.5-flash",
    **kwargs: Any,
) -> Agent:
    """Create and return the FYF Google ADK Producer Agent.

    Args:
        model_name: Gemini model name to use with ADK Agent.
        **kwargs: Additional parameters passed to ADK Agent constructor.

    Returns:
        Configured google.adk.Agent instance.
    """
    return Agent(
        name="fyf_producer",
        description="Autonomous FYF Video Producer coordinating research, Burmese script writing, QA auditing, and visual storyboard planning.",
        model=model_name,
        instruction=PRODUCER_INSTRUCTION,
        tools=[
            research_topic,
            draft_story_segments,
            audit_story_quality,
            plan_visual_shots,
        ],
        **kwargs,
    )
