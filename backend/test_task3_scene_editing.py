"""Task 3 RED/GREEN coverage for scene contracts and the edit seam.

The tests use the real FastAPI app and FileProjectStore with temporary roots.
They never call a provider or dispatch a paid render.  The expected failures
before the Task 3 implementation are intentional: scene fields/routes and the
dependency invalidation rules do not exist in the baseline yet.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

import backend.main as main
from backend.pipeline_graph import build_pipeline_graph, plan_recompute
from backend.projects.proposals import FileProposalStore
from backend.projects.store import FileProjectStore
from video_contract import ScriptSegment, VideoScript


PID = "a1b2c3d4"


def _segment(
    sid: str,
    text: str,
    *,
    caption: str = "caption",
    voice: str = "voice",
    duration_seconds: float = 3.0,
) -> dict:
    return {
        "id": sid,
        "text": text,
        "visual_action": f"show {sid}",
        "scene_type": "whiteboard",
        "mascot_action": "present",
        "emotion": "neutral",
        "emphasis": [],
        "caption": caption,
        "voice": voice,
        "duration_seconds": duration_seconds,
    }


def _script() -> dict:
    return {
        "title": "Scene editing test",
        "language": "my-MM",
        "segments": [_segment("s1", "one"), _segment("s2", "two")],
    }


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "SCRIPT_JOBS_ROOT", tmp_path / "script-jobs")
    monkeypatch.setattr(main, "JOBS_ROOT", tmp_path / "jobs")
    monkeypatch.setattr(main, "LOCKS_ROOT", tmp_path / "locks")
    monkeypatch.setenv("FYF_PROJECTS_ROOT", str(tmp_path / "projects"))
    monkeypatch.setenv("FYF_BUDGET_LEDGER_PATH", str(tmp_path / "budget" / "ledger.json"))
    monkeypatch.delenv("FYF_PUBLIC_DEPLOYMENT", raising=False)
    with TestClient(main.app) as test_client:
        yield test_client


def _create(client: TestClient) -> None:
    response = client.post(
        "/api/projects",
        json={
            "script": _script(),
            "actor": "operator",
            "project_id": PID,
            "idempotency_key": "create-task3",
        },
    )
    assert response.status_code == 201, response.text


def _head(client: TestClient) -> int:
    return client.get(f"/api/projects/{PID}/versions").json()["head"]


def _version(client: TestClient, version_no: int) -> dict:
    return client.get(f"/api/projects/{PID}/versions/{version_no}").json()["version"]


def test_scene_contract_rejects_blank_caption():
    with pytest.raises(ValidationError):
        ScriptSegment.model_validate({**_segment("s1", "one"), "caption": "  "})


def test_scene_contract_rejects_non_positive_or_non_finite_duration():
    with pytest.raises(ValidationError):
        ScriptSegment.model_validate({**_segment("s1", "one"), "duration_seconds": 0})
    with pytest.raises(ValidationError):
        ScriptSegment.model_validate({**_segment("s1", "one"), "duration_seconds": float("nan")})


def test_scene_contract_rejects_playback_speed_shortcut():
    with pytest.raises(ValidationError):
        ScriptSegment.model_validate({**_segment("s1", "one"), "speed_factor": 1.2})


def test_scene_edit_updates_selected_scene_editable_fields_only(client):
    _create(client)
    response = client.post(
        f"/api/projects/{PID}/scenes/edit",
        json={
            "base_version": 1,
            "scene_ids": ["s1"],
            "caption": "updated caption",
            "voice": "updated voice",
            "duration_seconds": 4.5,
            "actor": "canvas",
            "idempotency_key": "scene-edit-1",
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["target_version"] == 2
    after = _version(client, 2)["script"]["segments"]
    assert after[0]["caption"] == "updated caption"
    assert after[0]["voice"] == "updated voice"
    assert after[0]["duration_seconds"] == 4.5
    assert after[1] == _version(client, 1)["script"]["segments"][1]


def test_scene_edit_api_rejects_invalid_duration_and_caption_before_mutation(client):
    _create(client)
    for field, value in (("caption", "  "), ("duration_seconds", 0), ("duration_seconds", "NaN")):
        response = client.post(
            f"/api/projects/{PID}/scenes/edit",
            json={
                "base_version": 1,
                "scene_ids": ["s1"],
                field: value,
                "actor": "canvas",
                "idempotency_key": f"invalid-{field}-{value}",
            },
        )
        assert response.status_code == 422, response.text
    assert _head(client) == 1


def test_invalid_multi_scene_edit_is_atomic(client):
    _create(client)
    response = client.post(
        f"/api/projects/{PID}/scenes/edit",
        json={
            "base_version": 1,
            "scene_ids": ["s1", "s2"],
            "caption": "  ",
            "actor": "canvas",
            "idempotency_key": "invalid-scenes-1",
        },
    )
    assert response.status_code == 422, response.text
    assert _head(client) == 1
    assert client.get(f"/api/projects/{PID}/versions/2").status_code == 404


def test_scene_lock_blocks_only_selected_scene_and_scope(client):
    _create(client)
    locked = client.post(
        f"/api/projects/{PID}/locks",
        json={
            "scope": "content",
            "scene_ids": ["s1"],
            "locked": True,
            "locked_by": "director",
        },
    )
    assert locked.status_code == 200, locked.text
    assert locked.json()["scene_locks"]["s1"]["content"]["locked"] is True

    blocked = client.post(
        f"/api/projects/{PID}/scenes/edit",
        json={
            "base_version": 1,
            "scene_ids": ["s1"],
            "caption": "blocked",
            "actor": "canvas",
            "idempotency_key": "locked-s1",
        },
    )
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["detail"]["error"] == "lock_conflict"

    sibling = client.post(
        f"/api/projects/{PID}/scenes/edit",
        json={
            "base_version": 1,
            "scene_ids": ["s2"],
            "caption": "sibling remains editable",
            "actor": "canvas",
            "idempotency_key": "unlocked-s2",
        },
    )
    assert sibling.status_code == 200, sibling.text
    assert _version(client, 2)["script"]["segments"][1]["caption"] == "sibling remains editable"


def test_mixed_scene_edit_checks_each_global_granular_scope(client):
    """Neutral edit_scene operations must not bypass global granular locks."""
    _create(client)
    locked = client.post(
        f"/api/projects/{PID}/locks",
        json={"scope": "visual", "locked": True, "locked_by": "director"},
    )
    assert locked.status_code == 200, locked.text

    response = client.post(
        f"/api/projects/{PID}/scenes/edit",
        json={
            "base_version": 1,
            "scene_ids": ["s1"],
            "text": "updated narration",
            "visual_action": "updated visual",
            "actor": "canvas",
            "idempotency_key": "mixed-global-lock",
        },
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["error"] == "lock_conflict"
    assert "visual" in response.json()["detail"]["conflicts"]
    assert _head(client) == 1


def test_lock_mutation_and_proposal_approval_share_project_transaction(client, monkeypatch):
    _create(client)
    proposal = client.post(
        f"/api/projects/{PID}/chat/proposals",
        json={
            "message": 'rewrite the narration as "approved after lock boundary"',
            "selection": {"kind": "scene", "scene_ids": ["s1"]},
            "actor": "director",
            "idempotency_key": "shared-transaction-proposal",
        },
    )
    assert proposal.status_code == 201, proposal.text
    proposal_id = proposal.json()["proposal"]["proposal_id"]

    original_transaction = FileProjectStore.transaction
    original_apply = main.apply_command
    original_set_scope_lock = main.set_scope_lock
    transaction_depth = {"value": 0}
    observed = {"lock": False, "approval": False}

    @contextmanager
    def tracked_transaction(self, project_id, timeout=10.0):
        transaction_depth["value"] += 1
        try:
            with original_transaction(self, project_id, timeout=timeout):
                yield
        finally:
            transaction_depth["value"] -= 1

    def tracked_apply(*args, **kwargs):
        if kwargs.get("_lock_held") is True:
            observed["approval"] = transaction_depth["value"] > 0
        return original_apply(*args, **kwargs)

    def tracked_set_scope_lock(*args, **kwargs):
        observed["lock"] = transaction_depth["value"] > 0
        return original_set_scope_lock(*args, **kwargs)

    monkeypatch.setattr(FileProjectStore, "transaction", tracked_transaction)
    monkeypatch.setattr(main, "apply_command", tracked_apply)
    monkeypatch.setattr(main, "set_scope_lock", tracked_set_scope_lock)

    lock = client.post(
        f"/api/projects/{PID}/locks",
        json={"scope": "visual", "locked": True, "locked_by": "director"},
    )
    assert lock.status_code == 200, lock.text
    approved = client.post(f"/api/projects/{PID}/proposals/{proposal_id}/approve")
    assert approved.status_code == 200, approved.text
    assert observed == {"lock": True, "approval": True}


def test_scene_lock_blocks_explicit_optional_field_clear(client):
    _create(client)
    locked = client.post(
        f"/api/projects/{PID}/locks",
        json={
            "scope": "content",
            "scene_ids": ["s1"],
            "locked": True,
            "locked_by": "director",
        },
    )
    assert locked.status_code == 200, locked.text

    clear_caption = client.post(
        f"/api/projects/{PID}/scenes/edit",
        json={
            "base_version": 1,
            "scene_ids": ["s1"],
            "caption": None,
            "actor": "canvas",
            "idempotency_key": "clear-locked-caption",
        },
    )
    assert clear_caption.status_code == 409, clear_caption.text
    assert clear_caption.json()["detail"]["error"] == "lock_conflict"
    assert _head(client) == 1

    # A mixed edit is represented by the neutral ``edit_scene`` operation, so
    # its per-field presence must still catch an explicit voice clear.
    client.post(
        f"/api/projects/{PID}/locks",
        json={"scope": "content", "scene_ids": ["s1"], "locked": False},
    )
    voice_lock = client.post(
        f"/api/projects/{PID}/locks",
        json={"scope": "voice", "scene_ids": ["s1"], "locked": True},
    )
    assert voice_lock.status_code == 200, voice_lock.text
    clear_voice = client.post(
        f"/api/projects/{PID}/scenes/edit",
        json={
            "base_version": 1,
            "scene_ids": ["s1"],
            "text": "new text",
            "voice": None,
            "actor": "canvas",
            "idempotency_key": "clear-locked-voice",
        },
    )
    assert clear_voice.status_code == 409, clear_voice.text
    assert clear_voice.json()["detail"]["error"] == "lock_conflict"
    assert _head(client) == 1


def test_regeneration_is_bound_to_exact_head_and_recomputes_selected_descendants(client):
    _create(client)
    response = client.post(
        f"/api/projects/{PID}/scenes/regenerate",
        json={
            "base_version": 1,
            "scene_ids": ["s1"],
            "actor": "director",
            "idempotency_key": "regen-s1",
        },
    )
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["status"] == "planned"
    assert _head(client) == 1
    plan = body["plan"]
    assert {"script:s1", "visual:s1", "voice:s1", "render:s1", "qa:s1"}.issubset(plan["dirty"])
    assert {"script:s2", "visual:s2", "voice:s2", "render:s2", "qa:s2"}.issubset(plan["cache_hits"])

    newer = client.post(
        f"/api/projects/{PID}/scenes/edit",
        json={
            "base_version": 1,
            "scene_ids": ["s2"],
            "caption": "newer",
            "actor": "canvas",
            "idempotency_key": "newer-for-regen",
        },
    )
    assert newer.status_code == 200, newer.text
    stale = client.post(
        f"/api/projects/{PID}/scenes/regenerate",
        json={
            "base_version": 1,
            "scene_ids": ["s1"],
            "actor": "director",
            "idempotency_key": "regen-stale",
        },
    )
    assert stale.status_code == 409, stale.text
    assert stale.json()["detail"]["error"] == "stale_version"


def test_scene_regeneration_honors_global_granular_locks(client):
    _create(client)
    locked = client.post(
        f"/api/projects/{PID}/locks",
        json={"scope": "visual", "locked": True, "locked_by": "director"},
    )
    assert locked.status_code == 200, locked.text

    response = client.post(
        f"/api/projects/{PID}/scenes/regenerate",
        json={
            "base_version": 1,
            "scene_ids": ["s1"],
            "actor": "director",
            "idempotency_key": "regen-global-lock",
        },
    )
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["status"] == "blocked"
    assert "visual:s1" in body["plan"]["blocked"]


def test_explicit_undo_and_variant_base_must_match_current_head(client):
    _create(client)
    edited = client.post(
        f"/api/projects/{PID}/scenes/edit",
        json={
            "base_version": 1,
            "scene_ids": ["s1"],
            "caption": "new",
            "actor": "canvas",
            "idempotency_key": "edit-before-base-check",
        },
    )
    assert edited.status_code == 200, edited.text
    stale_undo = client.post(
        f"/api/projects/{PID}/undo",
        json={"target_version": 1, "base_version": 1, "actor": "director"},
    )
    assert stale_undo.status_code == 409, stale_undo.text
    stale_variant = client.post(
        f"/api/projects/{PID}/variants",
        json={"variant_name": "stale", "base_version": 1, "actor": "director"},
    )
    assert stale_variant.status_code == 409, stale_variant.text
    assert _head(client) == 2


def test_undo_idempotency_replay_validates_target_identity(client):
    _create(client)
    edited = client.post(
        f"/api/projects/{PID}/scenes/edit",
        json={
            "base_version": 1,
            "scene_ids": ["s1"],
            "caption": "edited before undo",
            "actor": "canvas",
            "idempotency_key": "undo-collision-edit",
        },
    )
    assert edited.status_code == 200, edited.text

    first = client.post(
        f"/api/projects/{PID}/undo",
        json={
            "target_version": 1,
            "base_version": 2,
            "actor": "director",
            "idempotency_key": "undo-collision",
        },
    )
    assert first.status_code == 200, first.text
    assert _head(client) == 3

    collision = client.post(
        f"/api/projects/{PID}/undo",
        json={
            "target_version": 2,
            "base_version": 3,
            "actor": "director",
            "idempotency_key": "undo-collision",
        },
    )
    assert collision.status_code == 409, collision.text
    assert collision.json()["detail"]["error"] == "idempotency_conflict"
    assert _head(client) == 3


def test_variant_idempotency_replay_validates_name_identity(client):
    _create(client)
    first = client.post(
        f"/api/projects/{PID}/variants",
        json={
            "variant_name": "first",
            "base_version": 1,
            "actor": "director",
            "idempotency_key": "variant-collision",
        },
    )
    assert first.status_code == 200, first.text
    assert _head(client) == 2

    collision = client.post(
        f"/api/projects/{PID}/variants",
        json={
            "variant_name": "different-name",
            "base_version": 2,
            "actor": "director",
            "idempotency_key": "variant-collision",
        },
    )
    assert collision.status_code == 409, collision.text
    assert collision.json()["detail"]["error"] == "idempotency_conflict"
    assert _head(client) == 2


def test_graph_invalidation_includes_scene_caption_voice_and_duration_only_for_scene():
    before = {
        "language": "my-MM",
        "segments": [_segment("s1", "one"), _segment("s2", "two")],
    }
    after = {
        "language": "my-MM",
        "segments": [
            {**_segment("s1", "one"), "caption": "changed", "voice": "new", "duration_seconds": 4.0},
            _segment("s2", "two"),
        ],
    }
    plan = plan_recompute(build_pipeline_graph(before), build_pipeline_graph(after))
    assert {"script:s1", "visual:s1", "voice:s1", "render:s1", "qa:s1"}.issubset(plan.dirty)
    assert {"script:s2", "visual:s2", "voice:s2", "render:s2", "qa:s2"}.issubset(plan.cache_hits)


def test_approval_retry_finalizes_after_version_commit_when_proposal_save_failed(client, monkeypatch):
    _create(client)
    proposal = client.post(
        f"/api/projects/{PID}/chat/proposals",
        json={
            "message": 'rewrite the narration as "approved after retry"',
            "selection": {"kind": "scene", "scene_ids": ["s1"]},
            "actor": "director",
            "idempotency_key": "proposal-save-retry",
        },
    )
    assert proposal.status_code == 201, proposal.text
    proposal_id = proposal.json()["proposal"]["proposal_id"]

    original_save = FileProposalStore.save
    failed_once = {"value": False}

    def fail_first_save(self, record):
        if not failed_once["value"]:
            failed_once["value"] = True
            raise OSError("simulated proposal-store crash")
        return original_save(self, record)

    monkeypatch.setattr(FileProposalStore, "save", fail_first_save)
    first = client.post(f"/api/projects/{PID}/proposals/{proposal_id}/approve")
    assert first.status_code == 500, first.text
    assert _head(client) == 2

    retry = client.post(f"/api/projects/{PID}/proposals/{proposal_id}/approve")
    assert retry.status_code == 200, retry.text
    assert retry.json()["status"] == "approved"
    assert retry.json()["target_version"] == 2
    assert _head(client) == 2


def test_scene_edit_invalidates_inherited_video_preview_truth(client):
    _create(client)
    attached = client.post(
        f"/api/projects/{PID}/results",
        json={
            "dispatched_version": 1,
            "actor": "generation",
            "asset_references": [
                {
                    "asset_id": "job:deadbeef:video",
                    "kind": "video",
                    "uri": "/api/jobs/deadbeef/video",
                    "sha256": "a" * 64,
                }
            ],
            "idempotency_key": "attach-preview-v1",
        },
    )
    assert attached.status_code == 200, attached.text
    assert _version(client, 2)["asset_references"]

    edited = client.post(
        f"/api/projects/{PID}/scenes/edit",
        json={
            "base_version": 2,
            "scene_ids": ["s1"],
            "caption": "new draft is not rendered yet",
            "actor": "canvas",
            "idempotency_key": "edit-clears-preview",
        },
    )
    assert edited.status_code == 200, edited.text
    draft = _version(client, 3)
    assert not any(asset["kind"] == "video" for asset in draft["asset_references"])
    assert draft["render_manifest"] is None
    # The immutable attached version remains available for history/audit.
    assert any(asset["kind"] == "video" for asset in _version(client, 2)["asset_references"])


def test_scene_edit_idempotency_key_cannot_replay_a_different_command(client):
    _create(client)
    first = client.post(
        f"/api/projects/{PID}/scenes/edit",
        json={
            "base_version": 1,
            "scene_ids": ["s1"],
            "caption": "first command",
            "actor": "canvas",
            "idempotency_key": "same-key-different-command",
        },
    )
    assert first.status_code == 200, first.text

    collision = client.post(
        f"/api/projects/{PID}/scenes/edit",
        json={
            "base_version": 2,
            "scene_ids": ["s1"],
            "caption": "different command must not replay",
            "actor": "canvas",
            "idempotency_key": "same-key-different-command",
        },
    )
    assert collision.status_code == 409, collision.text
    assert collision.json()["detail"]["error"] == "idempotency_conflict"
    assert _head(client) == 2


def test_surface_edit_also_invalidates_inherited_video_preview_truth(client):
    _create(client)
    attached = client.post(
        f"/api/projects/{PID}/results",
        json={
            "dispatched_version": 1,
            "actor": "generation",
            "asset_references": [
                {
                    "asset_id": "job:deadbeef:video",
                    "kind": "video",
                    "uri": "/api/jobs/deadbeef/video",
                    "sha256": "b" * 64,
                }
            ],
            "idempotency_key": "attach-surface-v1",
        },
    )
    assert attached.status_code == 200, attached.text
    surface = client.post(
        f"/api/projects/{PID}/commands",
        json={
            "project_id": PID,
            "base_version": 2,
            "actor": "canvas",
            "operation": "update_surface",
            "payload": {
                "kind": "surface",
                "surface": {"components": [{"component": "text", "id": "t", "text": "new"}]},
            },
            "idempotency_key": "surface-invalidates-video",
        },
    )
    assert surface.status_code == 200, surface.text
    version = _version(client, 3)
    assert not any(asset["kind"] == "video" for asset in version["asset_references"])
    assert version["render_manifest"] is None


def test_explicit_rebase_replays_with_the_original_command_identity(client):
    _create(client)
    winner = client.post(
        f"/api/projects/{PID}/scenes/edit",
        json={
            "base_version": 1,
            "scene_ids": ["s1"],
            "caption": "winner advances the head",
            "actor": "canvas",
            "idempotency_key": "rebase-winner",
        },
    )
    assert winner.status_code == 200, winner.text

    command = {
        "project_id": PID,
        "base_version": 1,
        "actor": "canvas",
        "operation": "edit_script",
        "selection": {"kind": "scene", "scene_ids": ["s2"]},
        "payload": {
            "kind": "script_segments",
            "segments": [_segment("s2", "rebased text")],
        },
        "idempotency_key": "rebase-replay",
    }
    rebased = client.post(f"/api/projects/{PID}/commands?rebase=true", json=command)
    assert rebased.status_code == 200, rebased.text
    assert rebased.json()["status"] == "rebased"
    replayed = client.post(f"/api/projects/{PID}/commands?rebase=true", json=command)
    assert replayed.status_code == 200, replayed.text
    assert replayed.json()["status"] == "replayed"
    assert _head(client) == 3
