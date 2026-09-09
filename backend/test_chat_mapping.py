"""C3 - chat -> command mapping is pure, deterministic, and never writes state.

Also guards the ADK wiring: ``propose_project_command`` is appended to the
producer's tools WITHOUT adding a third ``LlmAgent`` and WITHOUT reordering
``tools[0]`` (the research tool the cinema-architecture test relies on).
"""

from __future__ import annotations

import ast
import inspect

import pytest

from backend.projects import chat
from backend.projects.chat import (
    ChatMappingError,
    classify_operation,
    map_message_to_command,
)
from backend.projects.models import ProjectVersion

PID = "a1b2c3d4"

_SEGMENTS = [
    {"id": "s1", "text": "one", "visual_action": "a", "scene_type": "whiteboard",
     "mascot_action": "present", "emotion": "neutral"},
    {"id": "s2", "text": "two", "visual_action": "b", "scene_type": "demo",
     "mascot_action": "explain", "emotion": "warm"},
]


def _version() -> ProjectVersion:
    return ProjectVersion.model_validate(
        {
            "project_id": PID, "version_no": 1,
            "script": {"title": "Demo", "language": "my-MM", "segments": _SEGMENTS},
            "actor": "alice", "created_at": "2026-01-01T00:00:00Z",
        }
    )


def _version_with_timings() -> ProjectVersion:
    return ProjectVersion.model_validate(
        {
            "project_id": PID, "version_no": 1,
            "script": {"title": "Demo", "language": "my-MM", "segments": _SEGMENTS},
            "segment_timings": [
                {"segment_id": "s1", "start_seconds": 0, "end_seconds": 5},
                {"segment_id": "s2", "start_seconds": 5, "end_seconds": 10},
            ],
            "actor": "alice", "created_at": "2026-01-01T00:00:00Z",
        }
    )


# --- operation classification ------------------------------------------------


def test_classify_operation_vocabulary():
    assert classify_operation("retime scene s1 to 4 seconds") == "edit_timing"
    assert classify_operation("trim the beat") == "edit_timing"
    assert classify_operation("reframe the shot") == "edit_visual"
    assert classify_operation("add some b-roll") == "edit_visual"
    assert classify_operation("rewrite the narration") == "edit_script"
    assert classify_operation("do something else entirely") == "edit_script"


# --- one message -> exactly one command -------------------------------------


def test_content_edit_maps_to_one_command():
    m = map_message_to_command(
        project_id=PID, base_version=1, message='rewrite scene s1 as "Hello world"',
        version=_version(), selection={"kind": "scene", "scene_ids": ["s1"]},
    )
    assert m.operation == "edit_script"
    assert m.affected_segment_ids == ["s1"]
    assert m.command.payload.kind == "script_segments"
    assert m.command.payload.segments[0].text == "Hello world"
    assert m.command.base_version == 1
    assert m.command.project_id == PID
    assert m.command.selection.kind == "scene"
    assert m.command.idempotency_key  # a key is always minted


def test_visual_edit_updates_visual_action_only():
    m = map_message_to_command(
        project_id=PID, base_version=1, message='reframe the shot for s2 as "slow zoom"',
        version=_version(), selection={"kind": "scene", "scene_ids": ["s2"]},
    )
    assert m.operation == "edit_visual"
    assert m.command.payload.segments[0].visual_action == "slow zoom"
    # narration text is carried through unchanged, not fabricated
    assert m.command.payload.segments[0].text == "two"


def test_timing_edit_parses_seconds_and_ignores_scene_id_digits():
    m = map_message_to_command(
        project_id=PID, base_version=1, message="retime scene s1 from 0 to 4 seconds",
        version=_version(), selection={"kind": "scene", "scene_ids": ["s1"]},
    )
    assert m.operation == "edit_timing"
    t = m.command.payload.timings[0]
    assert (t.segment_id, t.start_seconds, t.end_seconds) == ("s1", 0.0, 4.0)


def test_timing_single_number_is_treated_as_duration():
    m = map_message_to_command(
        project_id=PID, base_version=1, message="set s2 duration to 6 seconds",
        version=_version(), selection={"kind": "scene", "scene_ids": ["s2"]},
    )
    t = m.command.payload.timings[0]
    assert t.start_seconds == 0.0 and t.end_seconds == 6.0


def test_time_range_selection_is_carried_into_the_command():
    m = map_message_to_command(
        project_id=PID, base_version=1, message='rewrite as "VO"',
        version=_version_with_timings(),
        selection={"kind": "time_range", "start_seconds": 4, "end_seconds": 6},
    )
    # [4,6) overlaps s1 [0,5) and s2 [5,10)
    assert m.affected_segment_ids == ["s1", "s2"]
    assert m.command.selection.kind == "time_range"


def test_all_selection_targets_every_segment():
    m = map_message_to_command(
        project_id=PID, base_version=1, message='rewrite as "X"',
        version=_version(), selection={"kind": "all"},
    )
    assert m.affected_segment_ids == ["s1", "s2"]


def test_none_selection_defaults_to_all_segments():
    m = map_message_to_command(
        project_id=PID, base_version=1, message='rewrite as "X"', version=_version()
    )
    assert m.affected_segment_ids == ["s1", "s2"]


# --- honest failures (chat never guesses / fabricates) ----------------------


def test_blank_message_raises():
    with pytest.raises(ChatMappingError):
        map_message_to_command(project_id=PID, base_version=1, message="   ", version=_version())


def test_content_without_new_text_raises():
    with pytest.raises(ChatMappingError):
        map_message_to_command(
            project_id=PID, base_version=1, message="rewrite scene s1",
            version=_version(), selection={"kind": "scene", "scene_ids": ["s1"]},
        )


def test_timing_without_number_raises():
    with pytest.raises(ChatMappingError):
        map_message_to_command(
            project_id=PID, base_version=1, message="retime scene s1",
            version=_version(), selection={"kind": "scene", "scene_ids": ["s1"]},
        )


def test_empty_selection_raises():
    with pytest.raises(ChatMappingError):
        map_message_to_command(
            project_id=PID, base_version=1, message='rewrite as "X"',
            version=_version_with_timings(),
            selection={"kind": "time_range", "start_seconds": 100, "end_seconds": 200},
        )


# --- purity: chat is a command emitter, never a state writer ----------------


def test_chat_module_imports_neither_store_nor_commands():
    tree = ast.parse(inspect.getsource(chat))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
    assert "backend.projects.store" not in imported
    assert "backend.projects.commands" not in imported


# --- ADK tool wiring (no third agent) ---------------------------------------


def test_propose_project_command_tool_is_a_pure_mapping():
    from backend.agent.tools import propose_project_command

    out = propose_project_command(
        PID, 1, 'rewrite scene s1 as "Tool text"', _version().model_dump(mode="json"),
        selection={"kind": "scene", "scene_ids": ["s1"]},
    )
    assert "error" not in out
    assert out["operation"] == "edit_script"
    assert out["command"]["payload"]["segments"][0]["text"] == "Tool text"
    assert out["affected_segment_ids"] == ["s1"]


def test_propose_project_command_reports_mapping_error_honestly():
    from backend.agent.tools import propose_project_command

    out = propose_project_command(PID, 1, "   ", _version().model_dump(mode="json"))
    assert out.get("error") == "chat_mapping_failed"


def test_chat_tool_lives_in_tools_module_and_producer_has_no_research_step():
    # The chat->command tool is wired into backend.agent.tools (the task's
    # "and/or tools.py" option). The producer agent's toolset is intentionally
    # remains isolated from the producer toolset, and no research step or third
    # LlmAgent is created.
    from backend.agent import tools as tools_mod
    from backend.agent.fyf_producer import create_fyf_producer_agent

    assert callable(getattr(tools_mod, "propose_project_command", None))
    agent = create_fyf_producer_agent()
    names = [getattr(tool, "__name__", str(tool)) for tool in agent.tools]
    assert "research_topic" not in names
    assert len(names) == 3


def test_backend_agent_still_has_exactly_two_llm_agents():
    # Independent re-assertion of the architectural guard after wiring the tool.
    from pathlib import Path

    import backend.agent as agent_pkg

    agent_dir = Path(agent_pkg.__file__).resolve().parent
    constructors = {"Agent", "LlmAgent"}

    def _is_agent_construction(node):
        if not isinstance(node, ast.Call):
            return False
        func = node.func
        if isinstance(func, ast.Name):
            return func.id in constructors
        if isinstance(func, ast.Attribute):
            return func.attr in constructors
        return False

    total = 0
    for path in sorted(agent_dir.rglob("*.py")):
        if path.name.startswith("test_"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        total += sum(1 for node in ast.walk(tree) if _is_agent_construction(node))
    assert total == 2, f"expected exactly 2 ADK agents, found {total}"
