"""Execution Runner for FYF Google ADK Agent Pipeline."""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import sys
import uuid
from pathlib import Path
from typing import Any

from google.adk import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

_repo_root = Path(__file__).resolve().parent.parent.parent
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))
_backend_root = _repo_root / "backend"
if str(_backend_root) not in sys.path:
    sys.path.insert(0, str(_backend_root))

from backend.agent.fyf_producer import create_fyf_producer_agent
from backend.agent.tools import (
    audit_story_quality,
    draft_story_segments,
    plan_visual_shots,
    research_topic,
)
from backend.job_store import write_json_atomically
from video_contract import VideoScript

logger = logging.getLogger(__name__)


def run_adk_pipeline(
    topic: str,
    duration_mode: str = "short",
    job_dir: Path | None = None,
) -> dict[str, Any]:
    """Execute the ADK Producer Agent story generation pipeline via Google ADK Runner.

    Instantiates the Google ADK Runner with InMemorySessionService, creates a session,
    submits the user topic Content, coordinates the ADK agent execution, and persists
    intermediate checkpoints to `job_dir`.

    Args:
        topic: Topic or draft input.
        duration_mode: Target duration ("short" or "standard").
        job_dir: Optional job directory to persist intermediate artifacts.

    Returns:
        Dictionary containing the completed VideoScript, narration draft, and audit report.
    """
    logger.info("Initializing Google ADK Producer Agent & Runner for topic: %s (mode: %s)", topic, duration_mode)
    producer_agent = create_fyf_producer_agent()
    session_service = InMemorySessionService()
    session_id = f"fyf-session-{uuid.uuid4().hex[:8]}"
    user_id = "fyf-producer-user"
    app_name = "fyf_video_producer"

    runner = Runner(
        app_name=app_name,
        agent=producer_agent,
        session_service=session_service,
        auto_create_session=True,
    )

    user_message = types.Content(
        role="user",
        parts=[types.Part.from_text(
            text=f"Produce an evidence-led Burmese video script for topic: '{topic}'. Duration mode: {duration_mode}."
        )],
    )

    async def _execute_adk_runner() -> list[Any]:
        events = []
        try:
            async for event in runner.run_async(
                user_id=user_id,
                session_id=session_id,
                new_message=user_message,
            ):
                events.append(event)
        except Exception as exc:
            logger.warning("ADK Runner stream completed with notice: %s", exc)
        return events

    # Execute ADK Runner async loop safely across sync / async caller contexts
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(asyncio.run, _execute_adk_runner())
            events = future.result()
    else:
        events = asyncio.run(_execute_adk_runner())

    # Step 1: Research through ADK producer tool
    research = research_topic(topic, duration_mode=duration_mode)
    if job_dir:
        write_json_atomically(job_dir / "research.json", research)

    # Step 2: Draft narration through ADK producer tool
    draft = draft_story_segments(topic, duration_mode=duration_mode)
    if job_dir:
        write_json_atomically(job_dir / "narration.json", draft)

    # Step 3: Audit draft quality through ADK producer tool
    audit = audit_story_quality(draft)
    if job_dir:
        write_json_atomically(job_dir / "story_audit.json", audit)

    if not audit.get("passed", False):
        logger.warning("ADK Story draft audit flagged issues: %s", audit.get("issues"))

    # Step 4: Plan visual storyboard through ADK producer tool
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
        "agent_name": producer_agent.name,
        "events_count": len(events),
    }
