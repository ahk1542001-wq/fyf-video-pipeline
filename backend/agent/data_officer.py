"""FYF Data Officer - an ADK agent that answers questions about the video
factory's own telemetry by querying ClickHouse Cloud through the official
mcp-clickhouse MCP server (Agentic Cinema partner-track integration).

The MCP server runs as a local stdio subprocess configured from the standard
CLICKHOUSE_* environment variables, so the same wiring works on a laptop and
inside the Cloud Run container.
"""

from __future__ import annotations

import logging
import os
import shutil
from typing import Any

from google.adk.agents import LlmAgent
from google.adk.models import Gemini
from google.adk.tools.mcp_tool.mcp_toolset import MCPToolset, StdioConnectionParams
from mcp import StdioServerParameters
from vertex_model_routing import model_for

logger = logging.getLogger(__name__)

DATA_OFFICER_INSTRUCTION = """You are the FYF Data Officer for a Burmese-language
AI video production factory.

You answer questions about production jobs, QA outcomes, scene telemetry and
model-call costs using ONLY the ClickHouse tools available to you.

The warehouse tables and their EXACT columns:
- video_pipeline_jobs(job_id, title, duration_sec, voice_mode, status,
  total_render_time_ms, total_tokens_used, cost_usd, qa_passed, created_at)
- video_qa_records(job_id, check_name, passed, detail, created_at)
- video_scene_telemetry(job_id, scene_id, treatment_type, render_time_ms,
  vertex_latency_ms, evidence_claim_count, segment_hash, created_at)
- video_vertex_calls(job_id, job_kind, call_id, stage, model, operation,
  attempt, status, billable, duration_ms, input_tokens, output_tokens,
  total_tokens, input_characters, audio_output_bytes, created_at)

Per-call cost is not stored anywhere: job cost lives in
video_pipeline_jobs.cost_usd, while call counts and tokens live in
video_vertex_calls (or video_pipeline_jobs.total_tokens_used).
- Prefer read-only SELECT queries. Never INSERT/ALTER/DROP.
- You have a very small time budget: use at most TWO tool calls, then answer
  with whatever you learned. A partial grounded answer beats no answer.
- Keep answers short (2-4 sentences), include concrete numbers when available,
  and mention the table you used.
- If no data exists yet, say so plainly instead of guessing.
"""


def _mcp_server_command() -> list[str] | None:
    """Resolve how to launch the official mcp-clickhouse server locally."""
    import importlib.util
    import sys

    # Prefer the venv/console script of the running environment; the package
    # itself has no __main__ module.
    venv_script = os.path.join(sys.prefix, "bin", "mcp-clickhouse")
    if os.path.exists(venv_script):
        return [venv_script]
    if shutil.which("uvx"):
        return ["uvx", "mcp-clickhouse"]
    script = shutil.which("mcp-clickhouse")
    if script:
        return [script]
    return None


def create_data_officer_agent(
    model_name: str | None = None,
) -> tuple[LlmAgent, "MCPToolset"] | None:
    """Build the Data Officer agent wired to mcp-clickhouse.

    Returns (agent, toolset) so callers can close the MCP session when done;
    each request spawns its own stdio subprocess and leaking it eventually
    starves the container. Returns None when ClickHouse env is not configured
    or no launcher found.
    """
    if not os.getenv("CLICKHOUSE_HOST"):
        logger.info("CLICKHOUSE_HOST not set; Data Officer disabled")
        return None

    command = _mcp_server_command()
    if command is None:
        logger.warning("mcp-clickhouse launcher not found; Data Officer disabled")
        return None

    env = {k: v for k, v in os.environ.items() if k.startswith("CLICKHOUSE_")}

    toolset = MCPToolset(
        connection_params=StdioConnectionParams(
            server_params=StdioServerParameters(
                command=command[0],
                args=command[1:],
                env=env,
            )
        )
    )

    resolved_model = model_name or model_for("script")
    agent = LlmAgent(
        name="fyf_data_officer",
        model=Gemini(model=resolved_model),
        instruction=DATA_OFFICER_INSTRUCTION,
        tools=[toolset],
    )
    return agent, toolset


async def ask_data_officer(question: str) -> dict[str, Any]:
    """Run one Data Officer turn; returns {answer, tool_used}."""
    from google.adk import Runner
    from google.adk.sessions import InMemorySessionService
    from google.genai import types as genai_types

    built = create_data_officer_agent()
    if built is None:
        raise RuntimeError("Data Officer unavailable: set CLICKHOUSE_HOST + install mcp-clickhouse")
    agent, toolset = built

    session_service = InMemorySessionService()
    session = await session_service.create_session(app_name="fyf_data_officer", user_id="fyf-operator")
    runner = Runner(app_name="fyf_data_officer", agent=agent, session_service=session_service)

    content = genai_types.Content(role="user", parts=[genai_types.Part(text=question)])
    final_text = ""
    tool_used = False
    errors: list[str] = []
    try:
        async for event in runner.run_async(user_id="fyf-operator", session_id=session.id, new_message=content):
            error_message = getattr(event, "error_message", None)
            if error_message:
                error_code = getattr(event, "error_code", "") or ""
                errors.append(f"{error_code}: {str(error_message)}"[:200])
            if event.content and event.content.parts:
                for part in event.content.parts:
                    if getattr(part, "function_call", None):
                        tool_used = True
                    text_part = getattr(part, "text", None)
                    # Skip model "thought" parts; only surfaced answers count.
                    if text_part and not getattr(part, "thought", False):
                        final_text += text_part
    finally:
        try:
            await toolset.close()
        except Exception:  # pragma: no cover - best-effort cleanup
            logger.debug("MCP toolset close failed", exc_info=True)

    answer = final_text.strip()
    if not answer:
        if errors:
            raise RuntimeError(
                "Data Officer model call failed: " + "; ".join(errors[:2])
            )
        raise RuntimeError("Data Officer returned an empty response")
    return {"answer": answer, "tool_used": tool_used}
