"""Stage B exit-gate tests - B6 atomic commands (backend.projects.commands).

Covers the atomicity/concurrency gates end-to-end through ``apply_command``:
happy edits, stale-version reject AND explicit rebase (never silent overwrite),
idempotency replay (no duplicate version/event), multi-scene invalid edit
rejects ENTIRELY with zero partial writes (E2E scenario 11), lock conflict via
the injectable hook (Task 9 seam), paid-operation fail-closed budget/approval
gating (unknown cost stays None, never 0), and restart/resume durability.
"""

from __future__ import annotations

import pytest

from video_contract import ScriptSegment, VideoScript

from backend.budget_store import record_approval
from backend.projects.commands import (
    ApprovalRequiredError,
    CommandValidationError,
    LockConflictError,
    ProjectNotFoundError,
    StaleVersionError,
    apply_command,
    create_project_with_script,
    default_lock_checker,
)
from backend.projects.models import (
    LockState,
    ProjectCommand,
    ProjectVersion,
    RenderRequestPayload,
    RenderManifest,
    OutputMetadata,
    SceneSelection,
    ScriptSegmentsPayload,
    SegmentTimingsPayload,
    SegmentTiming,
    SurfacePayload,
    SurfaceSpec,
    TextComponent,
)
from backend.projects.store import FileProjectStore, InvalidProjectIdError

PID = "a1b2c3d4"


def _segment(sid: str, text: str) -> ScriptSegment:
    return ScriptSegment(
        id=sid, text=text, visual_action=f"show {sid}", scene_type="whiteboard",
        mascot_action="present", emotion="neutral",
    )


def _script() -> VideoScript:
    return VideoScript(
        title="Demo", language="my-MM",
        segments=[_segment("s1", "one"), _segment("s2", "two"), _segment("s3", "three")],
    )


@pytest.fixture()
def env(tmp_path):
    store = FileProjectStore(tmp_path / "projects")
    budget_root = tmp_path / "budget"
    budget_root.mkdir()
    v1 = create_project_with_script(store, PID, _script(), actor="alice", idempotency_key="create")
    return store, budget_root, v1


def _edit(base_version: int, segments, selection=None, idem="edit") -> ProjectCommand:
    return ProjectCommand(
        project_id=PID, base_version=base_version, actor="bob", operation="edit_script",
        selection=selection, payload=ScriptSegmentsPayload(segments=segments),
        idempotency_key=idem,
    )


# --- happy paths -------------------------------------------------------------


def test_edit_script_applies_and_commits_one_version(env):
    store, _, _ = env
    cs = apply_command(store, _edit(1, [_segment("s1", "ONE"), _segment("s2", "TWO"), _segment("s3", "THREE")]))
    assert cs.status == "applied" and cs.applied is True
    assert cs.base_version == 1 and cs.target_version == 2
    assert cs.proposed_operations == cs.applied_operations == ["edit_script"]
    assert cs.affected_segment_ids == ["s1", "s2", "s3"]
    v2 = store.load_version(PID, 2)
    assert v2.script.segments[0].text == "ONE"
    assert v2.parent_version == 1
    assert store.current_version_no(PID) == 2


def test_scoped_edit_only_touches_selected_scenes(env):
    store, _, _ = env
    cs = apply_command(
        store,
        _edit(1, [_segment("s2", "TWO")], selection=SceneSelection(scene_ids=["s2"]), idem="scope"),
    )
    assert cs.affected_segment_ids == ["s2"]
    v2 = store.load_version(PID, 2)
    assert [s.text for s in v2.script.segments] == ["one", "TWO", "three"]


def test_edit_timing_merges_and_persists(env):
    store, _, _ = env
    command = ProjectCommand(
        project_id=PID, base_version=1, actor="bob", operation="edit_timing",
        selection=None,
        payload=SegmentTimingsPayload(timings=[SegmentTiming(segment_id="s1", start_seconds=0, end_seconds=2)]),
        idempotency_key="timing",
    )
    cs = apply_command(store, command)
    assert cs.status == "applied"
    v2 = store.load_version(PID, 2)
    assert [(t.segment_id, t.start_seconds, t.end_seconds) for t in v2.segment_timings] == [("s1", 0, 2)]


def test_update_surface_persists_closed_surface(env):
    store, _, _ = env
    command = ProjectCommand(
        project_id=PID, base_version=1, actor="bob", operation="update_surface",
        payload=SurfacePayload(surface=SurfaceSpec(components=[TextComponent(id="t", text="hi")])),
        idempotency_key="surface",
    )
    cs = apply_command(store, command)
    assert cs.status == "applied"
    assert store.load_version(PID, 2).surface.components[0].id == "t"


# --- stale version: reject AND rebase (never silent overwrite) ---------------


def test_stale_base_version_is_rejected(env):
    store, _, _ = env
    apply_command(store, _edit(1, [_segment("s1", "ONE"), _segment("s2", "TWO"), _segment("s3", "THREE")], idem="e1"))
    # A second command still claiming base_version=1 is stale (head is now 2).
    stale = _edit(1, [_segment("s1", "X"), _segment("s2", "Y"), _segment("s3", "Z")], idem="e2")
    with pytest.raises(StaleVersionError) as exc:
        apply_command(store, stale)
    assert exc.value.base_version == 1 and exc.value.current_version == 2
    # Nothing was overwritten.
    assert store.current_version_no(PID) == 2
    assert store.load_version(PID, 2).script.segments[0].text == "ONE"


def test_stale_base_version_can_be_explicitly_rebased(env):
    store, _, _ = env
    apply_command(store, _edit(1, [_segment("s1", "ONE"), _segment("s2", "TWO"), _segment("s3", "THREE")], idem="e1"))
    stale = _edit(1, [_segment("s2", "rebased-two")], selection=SceneSelection(scene_ids=["s2"]), idem="e2")
    cs = apply_command(store, stale, rebase=True)
    assert cs.status == "rebased" and cs.base_version == 2 and cs.target_version == 3
    v3 = store.load_version(PID, 3)
    # Rebase preserved the winner's s1 edit and applied the rebased s2 edit.
    assert [s.text for s in v3.script.segments] == ["ONE", "rebased-two", "THREE"]


# --- idempotency replay ------------------------------------------------------


def test_idempotent_replay_does_not_duplicate(env):
    store, _, _ = env
    command = _edit(1, [_segment("s1", "ONE"), _segment("s2", "TWO"), _segment("s3", "THREE")], idem="dup")
    first = apply_command(store, command)
    versions_after_first = store.list_versions(PID)
    events_after_first = len(store.read_events(PID))

    replay = apply_command(store, command)
    assert replay.status == "replayed" and replay.replayed is True
    assert replay.target_version == first.target_version == 2
    # No new version, no new event, no duplicate side effects.
    assert store.list_versions(PID) == versions_after_first
    assert len(store.read_events(PID)) == events_after_first


def test_create_project_is_idempotent(env):
    store, _, v1 = env
    again = create_project_with_script(store, PID, _script(), actor="alice", idempotency_key="create")
    assert again.version_no == v1.version_no == 1
    assert store.current_version_no(PID) == 1


# --- multi-scene invalid edit rejects ENTIRELY (zero partial edits) ----------


def test_multi_scene_invalid_edit_rejects_entirely_with_zero_partial_writes(env):
    """E2E scenario 11: one bad scene in a multi-scene edit rejects the whole command."""
    store, _, _ = env
    versions_before = store.list_versions(PID)
    events_before = len(store.read_events(PID))

    # Select ONLY s1, but the payload also tries to edit s2 (outside selection).
    bad = _edit(1, [_segment("s1", "ok"), _segment("s2", "sneaky")],
                selection=SceneSelection(scene_ids=["s1"]), idem="bad")
    with pytest.raises(CommandValidationError) as exc:
        apply_command(store, bad)

    assert exc.value.changeset.status == "rejected"
    assert exc.value.changeset.applied_operations == []
    assert exc.value.changeset.validation.passed is False
    # ZERO partial edits: no version appended, no event appended, s1 unchanged.
    assert store.list_versions(PID) == versions_before
    assert len(store.read_events(PID)) == events_before
    assert store.load_version(PID, 1).script.segments[0].text == "one"


def test_selection_referencing_unknown_scene_rejects_entirely(env):
    store, _, _ = env
    versions_before = store.list_versions(PID)
    bad = _edit(1, [_segment("s1", "ok")], selection=SceneSelection(scene_ids=["ghost"]), idem="ghost")
    with pytest.raises(CommandValidationError):
        apply_command(store, bad)
    assert store.list_versions(PID) == versions_before


def test_edit_cannot_inject_a_new_segment(env):
    store, _, _ = env
    versions_before = store.list_versions(PID)
    # selection None => all base ids; "s9" is not among them => outside selection.
    bad = _edit(1, [_segment("s9", "brand new")], idem="inject")
    with pytest.raises(CommandValidationError):
        apply_command(store, bad)
    assert store.list_versions(PID) == versions_before


# --- locks: conflict + injectable hook (Task 9 seam) ------------------------


def _lock_head(store, scope: str):
    head = store.load_version(PID, store.current_version_no(PID))
    data = head.model_dump(mode="json")
    data["version_no"] = head.version_no + 1
    data["parent_version"] = head.version_no
    data["locks"] = LockState(**{scope: True}).model_dump(mode="json")
    store.append_version(ProjectVersion.model_validate(data))
    return store.current_version_no(PID)


def test_locked_scope_blocks_edit(env):
    store, _, _ = env
    head = _lock_head(store, "content")
    command = _edit(head, [_segment("s1", "x"), _segment("s2", "y"), _segment("s3", "z")], idem="locked")
    with pytest.raises(LockConflictError) as exc:
        apply_command(store, command)
    assert exc.value.conflicts == ["content"]
    assert store.current_version_no(PID) == head  # nothing written


def test_lock_checker_is_an_injectable_hook(env):
    """Task 9 widens locks WITHOUT changing apply_command: the hook is the seam."""
    store, _, _ = env
    calls = []

    def granular_checker(version, command):
        calls.append(command.operation)
        return ["timing"]  # pretend a granular lock_store says timing is locked

    command = ProjectCommand(
        project_id=PID, base_version=1, actor="bob", operation="edit_timing",
        payload=SegmentTimingsPayload(timings=[SegmentTiming(segment_id="s1", start_seconds=0, end_seconds=1)]),
        idempotency_key="granular",
    )
    with pytest.raises(LockConflictError) as exc:
        apply_command(store, command, lock_checker=granular_checker)
    assert exc.value.conflicts == ["timing"]
    assert calls == ["edit_timing"]


def test_default_lock_checker_allows_unlocked_scope(env):
    store, _, v1 = env
    command = _edit(1, [_segment("s1", "x"), _segment("s2", "y"), _segment("s3", "z")], idem="dk")
    assert default_lock_checker(v1, command) == []


# --- paid operations: fail-closed budget / approval --------------------------


def _render_command(base_version: int, idem: str, estimate=None) -> ProjectCommand:
    return ProjectCommand(
        project_id=PID, base_version=base_version, actor="bob", operation="request_render",
        payload=RenderRequestPayload(estimated_cost_usd=estimate, manifest=None),
        idempotency_key=idem,
    )


def test_paid_operation_without_approval_is_refused(env):
    store, budget_root, _ = env
    with pytest.raises(ApprovalRequiredError) as exc:
        apply_command(store, _render_command(1, "render-no-approval"), budget_root=budget_root)
    assert "no matching approved" in exc.value.reason
    assert store.current_version_no(PID) == 1  # nothing written


def test_paid_operation_with_corrupted_ledger_fails_closed(env):
    store, budget_root, _ = env
    (budget_root / ".budget_ledger.json").write_text("{ not json", encoding="utf-8")
    with pytest.raises(ApprovalRequiredError):
        apply_command(store, _render_command(1, "render-corrupt"), budget_root=budget_root)


def test_paid_operation_with_unknown_cost_needs_explicit_approval_and_never_zero(env):
    store, budget_root, _ = env
    # Unknown estimate (None): an explicit approved spend is still required, and
    # the resulting changeset must report None (unknown), never 0.
    record_approval(
        "request_render", approved_spend_usd=1.0, decision="approved", actor="approver",
        target_ref="render-unknown", root_dir=budget_root,
    )
    cs = apply_command(store, _render_command(1, "render-unknown", estimate=None), budget_root=budget_root)
    assert cs.status == "applied"
    assert cs.estimated_spend_usd is None  # unknown != 0
    assert cs.approval_effects is not None and cs.approval_effects.required is True
    assert cs.approval_effects.approved_spend_usd == 1.0


def test_paid_operation_refused_when_budget_unavailable(env):
    store, budget_root, _ = env
    # An approval exists, but no account ceiling is configured => budget
    # unavailable => fail closed (is_budget_available returns False).
    record_approval(
        "request_render", approved_spend_usd=5.0, decision="approved", actor="approver",
        target_ref="render-known", root_dir=budget_root,
    )
    with pytest.raises(ApprovalRequiredError) as exc:
        apply_command(store, _render_command(1, "render-known", estimate=0.5), budget_root=budget_root)
    assert "budget is unavailable" in exc.value.reason
    assert store.current_version_no(PID) == 1


def test_denied_approval_does_not_authorize_spend(env):
    store, budget_root, _ = env
    record_approval(
        "request_render", approved_spend_usd=5.0, decision="denied", actor="approver",
        target_ref="render-denied", root_dir=budget_root,
    )
    with pytest.raises(ApprovalRequiredError):
        apply_command(store, _render_command(1, "render-denied"), budget_root=budget_root)


def test_reframe_is_paid_and_requires_approval(env):
    store, budget_root, _ = env
    command = ProjectCommand(
        project_id=PID,
        base_version=1,
        actor="bob",
        operation="reframe_aspect",
        payload=RenderRequestPayload(
            estimated_cost_usd=0.25,
            manifest=RenderManifest(
                video_spec_version="1",
                output=OutputMetadata(aspect_ratio="16:9", width=1920, height=1080),
            ),
        ),
        idempotency_key="reframe-16x9",
    )
    with pytest.raises(ApprovalRequiredError):
        apply_command(store, command, budget_root=budget_root)
    assert store.current_version_no(PID) == 1


def test_approved_reframe_updates_manifest_and_script_aspect(env, monkeypatch):
    store, budget_root, _ = env
    monkeypatch.setenv("FYF_DAILY_BUDGET_CAP_USD", "3")
    monkeypatch.setenv("FYF_TOTAL_BUDGET_CAP_USD", "3")
    record_approval(
        "reframe_aspect",
        approved_spend_usd=0.25,
        decision="approved",
        actor="approver",
        target_ref="reframe-16x9",
        root_dir=budget_root,
    )
    command = ProjectCommand(
        project_id=PID,
        base_version=1,
        actor="bob",
        operation="reframe_aspect",
        payload=RenderRequestPayload(
            estimated_cost_usd=0.25,
            manifest=RenderManifest(
                video_spec_version="1",
                output=OutputMetadata(aspect_ratio="16:9", width=1920, height=1080),
            ),
        ),
        idempotency_key="reframe-16x9",
    )
    changeset = apply_command(store, command, budget_root=budget_root)
    version = store.load_version(PID, 2)
    assert changeset.approval_effects is not None
    assert changeset.approval_effects.required is True
    assert version.render_manifest is not None
    assert version.render_manifest.output.aspect_ratio == "16:9"
    assert version.script.aspect_ratio == "16:9"


def test_duration_reedit_requires_non_spend_story_approval(env):
    store, budget_root, _ = env
    command = ProjectCommand(
        project_id=PID,
        base_version=1,
        actor="bob",
        operation="reedit_duration",
        payload=ScriptSegmentsPayload(segments=[_segment("s1", "shorter")]),
        selection=SceneSelection(scene_ids=["s1"]),
        idempotency_key="duration-15s",
    )
    with pytest.raises(ApprovalRequiredError):
        apply_command(store, command, budget_root=budget_root)

    record_approval(
        "reedit_duration",
        approved_spend_usd=0.0,
        decision="approved",
        actor="approver",
        target_ref="duration-15s",
        root_dir=budget_root,
    )
    changeset = apply_command(store, command, budget_root=budget_root)
    assert changeset.estimated_spend_usd is None
    assert changeset.approval_effects is not None
    assert changeset.approval_effects.approved_spend_usd == 0.0
    assert store.load_version(PID, 2).script.segments[0].text == "shorter"


# --- ownership / not-found / restart-resume ---------------------------------


def test_command_on_unknown_project_raises_not_found(env):
    store, _, _ = env
    command = ProjectCommand(
        project_id="deadbeef", base_version=1, actor="bob", operation="edit_script",
        payload=ScriptSegmentsPayload(segments=[_segment("s1", "x")]), idempotency_key="nf",
    )
    with pytest.raises(ProjectNotFoundError):
        apply_command(store, command)


def test_command_with_malformed_project_id_raises(env):
    store, _, _ = env
    # ProjectCommand enforces the id pattern at construction; the store rejects
    # malformed ids defensively too.
    with pytest.raises(InvalidProjectIdError):
        store.project_exists("../escape")


def test_restart_resume_continues_and_detects_replay(tmp_path):
    """A fresh store on the same root resumes; idempotency survives restart."""
    root = tmp_path / "projects"
    store = FileProjectStore(root)
    create_project_with_script(store, PID, _script(), actor="alice", idempotency_key="create")
    command = _edit(1, [_segment("s1", "ONE"), _segment("s2", "TWO"), _segment("s3", "THREE")], idem="e1")
    apply_command(store, command)

    # Simulate a process restart: brand-new store instance over the same files.
    restarted = FileProjectStore(root)
    assert restarted.current_version_no(PID) == 2

    # Replaying the pre-restart command is still detected (durable idempotency).
    replay = apply_command(restarted, command)
    assert replay.status == "replayed" and replay.target_version == 2

    # New work continues from the durable head.
    cs = apply_command(restarted, _edit(2, [_segment("s2", "TWO-2")],
                                        selection=SceneSelection(scene_ids=["s2"]), idem="e2"))
    assert cs.status == "applied" and cs.target_version == 3
    # Event log stayed monotonic across the restart.
    seqs = [e.sequence for e in restarted.read_events(PID)]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
