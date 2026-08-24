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

Guidelines:
- The relevant tables are: video_pipeline_jobs, video_qa_records,
  video_scene_telemetry, video_vertex_calls.
- Prefer read-only SELECT queries. Never INSERT/ALTER/DROP.
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


def create_data_officer_agent(model_name: str | None = None) -> LlmAgent | None:
    """Build the Data Officer agent wired to mcp-clickhouse.

    Returns None when ClickHouse env is not configured or no launcher found.
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
    return LlmAgent(
        name="fyf_data_officer",
        model=Gemini(model=resolved_model),
        instruction=DATA_OFFICER_INSTRUCTION,
        tools=[toolset],
    )


async def ask_data_officer(question: str) -> dict[str, Any]:
    """Run one Data Officer turn; returns {answer, tool_used}."""
    from google.adk import Runner
    from google.adk.sessions import InMemorySessionService
    from google.genai import types as genai_types

    agent = create_data_officer_agent()
    if agent is None:
        raise RuntimeError("Data Officer unavailable: set CLICKHOUSE_HOST + install mcp-clickhouse")

    session_service = InMemorySessionService()
    session = await session_service.create_session(app_name="fyf_data_officer", user_id="fyf-operator")
    runner = Runner(app_name="fyf_data_officer", agent=agent, session_service=session_service)

    content = genai_types.Content(role="user", parts=[genai_types.Part(text=question)])
    final_text = ""
    tool_used = False
    async for event in runner.run_async(user_id="fyf-operator", session_id=session.id, new_message=content):
        if event.content and event.content.parts:
            for part in event.content.parts:
                if getattr(part, "function_call", None):
                    tool_used = True
                if getattr(part, "text", None):
                    final_text += part.text

    return {"answer": final_text.strip(), "tool_used": tool_used}
