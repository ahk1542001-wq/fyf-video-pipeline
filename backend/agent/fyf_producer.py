"""Google ADK Agent Definition for FYF Story and Video Planning."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

from google.adk import Agent
from google.adk.models import Gemini

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
)
from backend.vertex_client import vertex_client_kwargs
from vertex_model_routing import model_for

def build_producer_instruction(
    language: str = "my-MM",
    genre: str = "explainer",
    presenter_mode: str = "on_screen",
    studio_name: str = "FYF Studio",
) -> str:
    """Build dynamic instruction prompt for the Screenwriter / Producer Agent."""
    if language == "en-US":
        presenter_clause = (
            "Alex hosts on-screen with clear, charismatic, natural presence."
            if presenter_mode == "on_screen"
            else "Voiceover-only mode: Pure atmospheric cinematic B-roll without on-screen presenter cards."
        )
        return f"""
You are the Executive Screenwriter and Producer Agent for {studio_name}, responsible for
creating high-impact, cinematic global English vertical videos in the '{genre}' genre.

Presenter Configuration: {presenter_clause}

Treat the user's supplied text as the source of truth. Do not research, fact-check,
or add claims that are not present in it.

Follow this production workflow strictly:
1. Draft punchy English narration segments using `draft_story_segments`, calibrated to natural spoken English pacing (130-150 words per minute) and visual clarity.
2. Audit the draft narration quality using `audit_story_quality` to ensure optimal segment length, pacing, and spoken flow.
3. Plan the visual storyboard, cinematic camera directions, and director treatments using `plan_visual_shots` matching the '{genre}' genre.
4. Return the final locked VideoScript structure with all segments, scenes, and visual directions.
""".strip()

    presenter_clause = (
        "Ko Kyaw hosts on-screen with friendly, authoritative presence."
        if presenter_mode == "on_screen"
        else "Voiceover-only mode: Pure atmospheric cinematic B-roll without on-screen presenter cards."
    )
    return f"""
You are the {studio_name} Video Executive Producer Agent, responsible for creating high-impact,
evidence-led Burmese vertical videos.

Presenter Configuration: {presenter_clause}

User ပေးထားသော script ကို source of truth အဖြစ် သတ်မှတ်ပါ။ Research, fact-check
သို့မဟုတ် script မပါသော claim အသစ်ထည့်ခြင်း မလုပ်ပါနှင့်။

Follow this production workflow strictly:
1. Draft the Burmese narration segments using `draft_story_segments`.
2. Audit the draft narration quality using `audit_story_quality` to ensure Burmese character limits and pacing.
3. Plan the visual storyboard and director treatments using `plan_visual_shots`.
4. Return the final locked VideoScript structure with all segments, scenes, and visual directions.
""".strip()


PRODUCER_INSTRUCTION = build_producer_instruction()


def create_fyf_producer_agent(
    model_name: str | None = None,
    language: str = "my-MM",
    genre: str = "explainer",
    presenter_mode: str = "on_screen",
    studio_name: str = "FYF Studio",
    **kwargs: Any,
) -> Agent:
    """Create and return the FYF Google ADK Producer Agent.

    Args:
        model_name: Gemini model name to use with ADK Agent (defaults to model_for('script')).
        language: Language code ("my-MM" or "en-US").
        genre: Cinematic genre (e.g. "explainer", "cinematic_documentary", "tech_explainer").
        presenter_mode: "on_screen" or "voiceover_only".
        studio_name: Studio or channel brand name.
        **kwargs: Additional parameters passed to ADK Agent constructor.

    Returns:
        Configured google.adk.Agent instance.
    """
    resolved_model = model_name or model_for("script")
    vertex_model = Gemini(
        model=resolved_model,
        client_kwargs=vertex_client_kwargs(
            location=os.getenv("GOOGLE_CLOUD_LOCATION", "global"),
        ),
    )
    instruction = build_producer_instruction(
        language=language,
        genre=genre,
        presenter_mode=presenter_mode,
        studio_name=studio_name,
    )

    # Bind the request context into the ADK tools. The model is allowed to omit
    # optional function arguments, so relying on it to repeat language/genre
    # would silently fall back to Burmese explainer defaults for an English
    # cinema run.
    def draft_story_segments_for_studio(
        topic: str, duration_mode: str = "short"
    ) -> dict[str, Any]:
        return draft_story_segments(
            topic,
            duration_mode=duration_mode,
            language=language,
            genre=genre,
            studio_name=studio_name,
        )

    def plan_visual_shots_for_studio(
        title: str, segments: list[dict[str, Any]]
    ) -> dict[str, Any]:
        return plan_visual_shots(
            title,
            segments,
            genre=genre,
            presenter_mode=presenter_mode,
            language=language,
            studio_name=studio_name,
        )

    # Preserve the stable tool names advertised in the producer instruction.
    draft_story_segments_for_studio.__name__ = "draft_story_segments"
    plan_visual_shots_for_studio.__name__ = "plan_visual_shots"
    return Agent(
        name="fyf_producer",
        description=f"Autonomous Video Producer for {studio_name} coordinating research, script writing, QA auditing, and visual storyboard planning.",
        model=vertex_model,
        instruction=instruction,
        tools=[
            draft_story_segments_for_studio,
            audit_story_quality,
            plan_visual_shots_for_studio,
        ],
        **kwargs,
    )
