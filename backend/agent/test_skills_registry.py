"""Architectural guard: the skill facade must NOT add ADK agents.

docs/HANDOFF_AGENTIC_BUSINESS_STUDIO.md §68 forbids presenting the internal
pipeline responsibilities as separate agents. The runtime must therefore keep
exactly TWO ``LlmAgent`` instances (``google.adk.Agent`` is an alias of
``LlmAgent``): the Producer and the Data Officer.

These tests statically (AST) scan ``backend/agent/`` — including the new
``skills/`` facade — for ``Agent(...)``/``LlmAgent(...)`` construction sites and
assert the count stays at 2. The scan is pure source analysis: it imports no ADK
module, constructs no agent, and makes no provider call.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

AGENT_DIR = Path(__file__).resolve().parent
SKILLS_DIR = AGENT_DIR / "skills"
_AGENT_CONSTRUCTORS = {"Agent", "LlmAgent"}
EXPECTED_TOTAL_AGENTS = 2
EXPECTED_PRODUCER_FILE = "fyf_producer.py"
EXPECTED_DATA_OFFICER_FILE = "data_officer.py"


def _is_agent_construction(node: ast.AST) -> bool:
    """True for a call like ``Agent(...)`` or ``LlmAgent(...)``."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Name):
        return func.id in _AGENT_CONSTRUCTORS
    if isinstance(func, ast.Attribute):
        return func.attr in _AGENT_CONSTRUCTORS
    return False


def _source_files(root: Path) -> list[Path]:
    """Non-test Python sources under ``root`` (recursive)."""
    return sorted(
        path
        for path in root.rglob("*.py")
        if not path.name.startswith("test_")
    )


def _construction_sites(root: Path) -> dict[str, int]:
    """Map relative filename -> number of ADK agent construction sites."""
    counts: dict[str, int] = {}
    for path in _source_files(root):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        hits = sum(1 for node in ast.walk(tree) if _is_agent_construction(node))
        if hits:
            counts[str(path.relative_to(root))] = hits
    return counts


def test_backend_agent_has_exactly_two_llm_agents():
    counts = _construction_sites(AGENT_DIR)
    total = sum(counts.values())
    assert total == EXPECTED_TOTAL_AGENTS, (
        f"Expected exactly {EXPECTED_TOTAL_AGENTS} ADK LlmAgent construction "
        f"sites in backend/agent/, found {total}: {counts}"
    )


def test_agent_construction_confined_to_producer_and_data_officer():
    counts = _construction_sites(AGENT_DIR)
    assert set(counts) == {EXPECTED_PRODUCER_FILE, EXPECTED_DATA_OFFICER_FILE}, (
        f"ADK agents must only be built in {EXPECTED_PRODUCER_FILE} and "
        f"{EXPECTED_DATA_OFFICER_FILE}; found: {counts}"
    )
    assert counts[EXPECTED_PRODUCER_FILE] == 1
    assert counts[EXPECTED_DATA_OFFICER_FILE] == 1


def test_skills_facade_adds_no_agent():
    """The new skills/ package must contain zero agent constructions."""
    assert SKILLS_DIR.is_dir(), "skills/ facade directory is missing"
    counts = _construction_sites(SKILLS_DIR)
    assert counts == {}, f"skills facade must not construct an agent: {counts}"


def test_google_adk_agent_is_llmagent_alias():
    """Guard the assumption behind counting ``Agent(...)`` as an LlmAgent."""
    from google.adk import Agent
    from google.adk.agents import LlmAgent

    assert Agent is LlmAgent


@pytest.mark.parametrize(
    "factory_module,factory_name",
    [
        ("backend.agent.fyf_producer", "create_fyf_producer_agent"),
        ("backend.agent.data_officer", "create_data_officer_agent"),
    ],
)
def test_agent_factory_functions_exist(factory_module, factory_name):
    import importlib

    module = importlib.import_module(factory_module)
    assert callable(getattr(module, factory_name, None)), (
        f"{factory_module}.{factory_name} must remain the single construction "
        "path for its agent"
    )
