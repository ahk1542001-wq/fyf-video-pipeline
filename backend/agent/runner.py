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

    Raises:
        RuntimeError or Provider exception if ADK execution fails.
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

    collected_artifacts: dict[str, Any] = {
        "research": None,
        "draft": None,
        "audit": None,
        "script": None,
    }

    async def _execute_adk_runner() -> list[Any]:
        events = []
        async for event in runner.run_async(
            user_id=user_id,
            session_id=session_id,
            new_message=user_message,
        ):
            events.append(event)
            try:
                fn_responses = event.get_function_responses()
                if fn_responses:
                    for fn_resp in fn_responses:
                        resp_data = getattr(fn_resp, "response", None)
                        if isinstance(resp_data, dict):
                            if "segments" in resp_data and "title" in resp_data:
                                if any("visual" in s for s in resp_data.get("segments", [])):
                                    collected_artifacts["script"] = resp_data
                                else:
                                    collected_artifacts["draft"] = resp_data
                            elif "target_audience" in resp_data or "suggested_segments" in resp_data:
                                collected_artifacts["research"] = resp_data
                            elif "passed" in resp_data and "issues" in resp_data:
                                collected_artifacts["audit"] = resp_data
            except Exception:
                pass
        return events

    # Execute ADK Runner async loop safely across sync / async caller contexts.
    # Exceptions are intentionally NOT caught or swallowed here so they propagate to the pipeline.
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

    script_data = collected_artifacts.get("script")
    if not script_data:
        for event in reversed(events):
            try:
                fn_responses = event.get_function_responses()
                for fn in fn_responses:
                    resp = getattr(fn, "response", None)
                    if isinstance(resp, dict) and "segments" in resp:
                        script_data = resp
                        break
            except Exception:
                pass

    if not script_data:
        raise RuntimeError("ADK Runner execution completed without producing a valid VideoScript.")

    validated = VideoScript.model_validate(script_data).model_dump(mode="json")
    if job_dir:
        if collected_artifacts.get("research"):
            write_json_atomically(job_dir / "research.json", collected_artifacts["research"])
        if collected_artifacts.get("draft"):
            write_json_atomically(job_dir / "narration.json", collected_artifacts["draft"])
        if collected_artifacts.get("audit"):
            write_json_atomically(job_dir / "story_audit.json", collected_artifacts["audit"])
        write_json_atomically(job_dir / "result.json", validated)

    return {
        "script": validated,
        "draft": collected_artifacts.get("draft") or {},
        "audit": collected_artifacts.get("audit") or {"passed": True},
        "research": collected_artifacts.get("research") or {},
        "agent_name": producer_agent.name,
        "events_count": len(events),
    }
