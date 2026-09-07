"""Stage B exit-gate tests - B4 interface contracts (backend.projects.models).

Covers: ProjectVersion WRAPS VideoScript (no duplicated fields), extra="forbid"
everywhere, lossless JSON round-trip, the CLOSED surface union (no arbitrary
HTML/JS), cost-is-None-never-0, the ChangeSet applied-diff invariant, the
payload<->operation match rule, and version coherence.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from video_contract import ScriptSegment, VideoScript

from backend.projects.models import (
    AllSelection,
    ButtonComponent,
    ChangeSet,
    CostState,
    ObjectSelection,
    PinnedProductionConfig,
    ProjectCommand,
    ProjectVersion,
    RenderRequestPayload,
    SceneSelection,
    ScriptSegmentsPayload,
    SurfaceAction,
    SurfaceBinding,
    SurfaceSpec,
    TextComponent,
    WorkflowEvent,
    operation_is_paid,
    scope_for_operation,
)


def _segment(sid: str, text: str = " narration ") -> ScriptSegment:
    return ScriptSegment(
        id=sid,
        text=text,
        visual_action=f"show {sid}",
        scene_type="whiteboard",
        mascot_action="present",
        emotion="neutral",
    )


def _script() -> VideoScript:
    return VideoScript(
        title="Demo", language="my-MM", segments=[_segment("s1"), _segment("s2")]
    )


def _version(**overrides) -> ProjectVersion:
    data = {
        "project_id": "a1b2c3d4",
        "version_no": 1,
        "parent_version": None,
        "script": _script(),
        "actor": "alice",
        "created_at": "2026-01-01T00:00:00Z",
    }
    data.update(overrides)
    return ProjectVersion.model_validate(data)


# --- ProjectVersion wraps VideoScript, never duplicates its fields -----------


def test_project_version_wraps_video_script_instance():
    version = _version()
    assert isinstance(version.script, VideoScript)
    assert version.segment_ids() == ["s1", "s2"]


def test_project_version_does_not_duplicate_video_script_fields():
    """No VideoScript field may be re-declared at the version layer."""
    version_fields = set(ProjectVersion.model_fields)
    script_fields = set(VideoScript.model_fields)
    duplicated = version_fields & script_fields
    assert duplicated == set(), f"ProjectVersion duplicates VideoScript fields: {duplicated}"
    # The wrap is by reference under the single 'script' key.
    assert "script" in version_fields
    for leaked in ("title", "language", "segments", "render_controls"):
        assert leaked not in version_fields


# --- extra="forbid" ----------------------------------------------------------


@pytest.mark.parametrize(
    "model_cls,kwargs",
    [
        (ProjectCommand, dict(project_id="a1b2c3d4", base_version=1, actor="a",
                              operation="edit_script",
                              payload=ScriptSegmentsPayload(segments=[_segment("s1")]),
                              idempotency_key="k1")),
        (SurfaceSpec, dict()),
        (CostState, dict()),
    ],
)
def test_extra_keys_are_forbidden(model_cls, kwargs):
    with pytest.raises(ValidationError):
        model_cls.model_validate({**kwargs, "totally_unknown_key": 1})


def test_project_version_forbids_extra_keys():
    with pytest.raises(ValidationError):
        _version(unknown_field="nope")


# --- lossless JSON round-trip ------------------------------------------------


def test_project_version_roundtrips_through_json():
    version = _version(
        variant_name="v-a",
        pinned_production_config=PinnedProductionConfig(voice_actor="Sadaltager"),
    )
    dumped = version.model_dump(mode="json")
    restored = ProjectVersion.model_validate(dumped)
    assert restored == version
    assert restored.model_dump(mode="json") == dumped


def test_project_command_roundtrips_through_json():
    command = ProjectCommand(
        project_id="a1b2c3d4",
        base_version=3,
        actor="bob",
        operation="edit_script",
        selection=SceneSelection(scene_ids=["s1"]),
        payload=ScriptSegmentsPayload(segments=[_segment("s1", "edited")]),
        idempotency_key="idem-1",
    )
    restored = ProjectCommand.model_validate_json(command.model_dump_json())
    assert restored == command


# --- CLOSED surface union: no arbitrary HTML/JS ------------------------------


def test_surface_spec_rejects_unknown_component_type():
    with pytest.raises(ValidationError):
        SurfaceSpec.model_validate(
            {"components": [{"component": "raw_html", "id": "x", "html": "<script>"}]}
        )


def test_surface_component_cannot_carry_arbitrary_markup():
    # A text component has no field able to hold script, and extra keys are banned.
    with pytest.raises(ValidationError):
        TextComponent.model_validate({"id": "t", "text": "hi", "on_click_js": "alert(1)"})


def test_surface_spec_binding_vocabulary_is_closed():
    with pytest.raises(ValidationError):
        SurfaceBinding.model_validate(
            {"component_id": "t", "field": "text", "source": "arbitrary.eval.path"}
        )


def test_surface_spec_referential_integrity_enforced():
    # A button referencing an unknown action must be rejected.
    with pytest.raises(ValidationError):
        SurfaceSpec.model_validate(
            {
                "components": [
                    {"component": "button", "id": "b", "label": "Go", "action_id": "missing"}
                ],
                "actions": [],
            }
        )


def test_surface_spec_valid_closed_form_roundtrips():
    spec = SurfaceSpec(
        components=[
            TextComponent(id="title", text="Hello", role="title"),
            ButtonComponent(id="go", label="Render", action_id="act_render"),
        ],
        bindings=[SurfaceBinding(component_id="title", field="text", source="version.title")],
        actions=[SurfaceAction(action_id="act_render", kind="request_render")],
    )
    assert SurfaceSpec.model_validate(spec.model_dump(mode="json")) == spec


# --- cost is None (unknown), never 0 ----------------------------------------


def test_cost_state_defaults_to_unknown_none_not_zero():
    cost = CostState()
    assert cost.amount_usd is None
    assert cost.basis == "unknown"


def test_unknown_cost_basis_must_not_carry_amount():
    with pytest.raises(ValidationError):
        CostState(basis="unknown", amount_usd=0.0)


def test_render_request_estimate_defaults_to_none_not_zero():
    payload = RenderRequestPayload()
    assert payload.estimated_cost_usd is None


# --- ChangeSet applied-diff invariant ---------------------------------------


def test_changeset_applied_requires_proposed_equals_applied():
    with pytest.raises(ValidationError):
        ChangeSet.model_validate(
            {
                "project_id": "a1b2c3d4",
                "base_version": 1,
                "target_version": 2,
                "status": "applied",
                "applied": True,
                "proposed_operations": ["edit_script"],
                "applied_operations": ["edit_script", "request_render"],  # mismatch
            }
        )


def test_changeset_applied_requires_target_version():
    with pytest.raises(ValidationError):
        ChangeSet.model_validate(
            {
                "project_id": "a1b2c3d4",
                "base_version": 1,
                "status": "applied",
                "applied": True,
                "proposed_operations": ["edit_script"],
                "applied_operations": ["edit_script"],
            }
        )


def test_changeset_valid_applied_roundtrips():
    cs = ChangeSet.model_validate(
        {
            "project_id": "a1b2c3d4",
            "base_version": 1,
            "target_version": 2,
            "status": "applied",
            "applied": True,
            "proposed_operations": ["edit_script"],
            "applied_operations": ["edit_script"],
        }
    )
    assert ChangeSet.model_validate(cs.model_dump(mode="json")) == cs


# --- command payload <-> operation match ------------------------------------


def test_command_requires_matching_payload_kind():
    # edit_script requires a script_segments payload, not segment_timings.
    with pytest.raises(ValidationError):
        ProjectCommand.model_validate(
            {
                "project_id": "a1b2c3d4",
                "base_version": 1,
                "actor": "a",
                "operation": "edit_script",
                "payload": {"kind": "surface", "surface": {}},
                "idempotency_key": "k",
            }
        )


def test_command_requires_a_payload():
    with pytest.raises(ValidationError):
        ProjectCommand.model_validate(
            {
                "project_id": "a1b2c3d4",
                "base_version": 1,
                "actor": "a",
                "operation": "edit_script",
                "idempotency_key": "k",
            }
        )


def test_command_rejects_malformed_project_id():
    with pytest.raises(ValidationError):
        ProjectCommand.model_validate(
            {
                "project_id": "../escape",
                "base_version": 1,
                "actor": "a",
                "operation": "edit_script",
                "payload": {"kind": "script_segments", "segments": [_segment("s1").model_dump()]},
                "idempotency_key": "k",
            }
        )


def test_effective_scope_derives_from_operation():
    command = ProjectCommand(
        project_id="a1b2c3d4", base_version=1, actor="a", operation="edit_timing",
        payload={"kind": "segment_timings",
                 "timings": [{"segment_id": "s1", "start_seconds": 0, "end_seconds": 2}]},
        idempotency_key="k",
    )
    assert command.effective_scope() == "timing"
    assert scope_for_operation("edit_visual") == "visual"
    assert operation_is_paid("request_render") is True
    assert operation_is_paid("edit_script") is False


# --- version coherence + selection union ------------------------------------


def test_version_parent_must_equal_version_no_minus_one():
    with pytest.raises(ValidationError):
        _version(version_no=3, parent_version=1)


def test_only_version_one_may_have_null_parent():
    with pytest.raises(ValidationError):
        _version(version_no=2, parent_version=None)


def test_segment_timings_must_reference_known_segments():
    with pytest.raises(ValidationError):
        _version(
            segment_timings=[{"segment_id": "ghost", "start_seconds": 0, "end_seconds": 1}]
        )


def test_selection_union_is_discriminated_and_closed():
    assert SceneSelection(scene_ids=["s1"]).kind == "scene"
    assert ObjectSelection(object_refs=["mascot"]).kind == "object"
    assert AllSelection().kind == "all"
    with pytest.raises(ValidationError):
        SceneSelection.model_validate({"kind": "scene", "scene_ids": []})


def test_workflow_event_requires_monotonic_sequence():
    event = WorkflowEvent(
        event_id="e1", project_id="a1b2c3d4", event_type="version_appended",
        sequence=1, timestamp="2026-01-01T00:00:00Z",
    )
    assert WorkflowEvent.model_validate(event.model_dump(mode="json")) == event
    with pytest.raises(ValidationError):
        WorkflowEvent.model_validate(
            {"event_id": "e", "project_id": "a1b2c3d4", "event_type": "version_appended",
             "sequence": 0, "timestamp": "2026-01-01T00:00:00Z"}
        )
