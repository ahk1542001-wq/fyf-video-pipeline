"""Task 2 RED/GREEN coverage for the additive chat proposal lifecycle.

These tests deliberately use the real FastAPI app and FileProjectStore with
hermetic roots.  No provider or paid render is involved: a chat proposal is
only a persisted review record until its explicit approval.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import backend.main as main
from backend.job_store import initialize_job_status, read_job_status, update_job_status


PID = "a1b2c3d4"


def _segment(sid: str, text: str) -> dict:
    return {
        "id": sid,
        "text": text,
        "visual_action": f"show {sid}",
        "scene_type": "whiteboard",
        "mascot_action": "present",
        "emotion": "neutral",
        "emphasis": [],
    }


def _script() -> dict:
    return {
        "title": "Proposal test",
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
            "idempotency_key": "create-proposal-test",
        },
    )
    assert response.status_code == 201, response.text


def _propose(
    client: TestClient,
    *,
    idem: str = "proposal-1",
    message: str = 'rewrite the narration as "reviewed line"',
):
    return client.post(
        f"/api/projects/{PID}/chat/proposals",
        json={
            "message": message,
            "selection": {"kind": "scene", "scene_ids": ["s1"]},
            "actor": "director",
            "idempotency_key": idem,
        },
    )


def _head(client: TestClient) -> int:
    return client.get(f"/api/projects/{PID}/versions").json()["head"]


def test_chat_proposal_persists_exact_diff_without_mutating_head(client):
    _create(client)

    response = _propose(client)

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "proposed"
    proposal = body["proposal"]
    assert proposal["project_id"] == PID
    assert proposal["base_version"] == 1
    assert proposal["actor"] == "director"
    assert proposal["status"] == "proposed"
    assert proposal["idempotency_key"] == "proposal-1"
    assert proposal["command"]["project_id"] == PID
    assert proposal["command"]["base_version"] == 1
    assert proposal["command"]["idempotency_key"] == "proposal-1"
    assert proposal["affected_segment_ids"] == ["s1"]
    assert "content" in proposal["affected_scopes"]
    assert proposal["diff"]["before"]["s1"]["text"] == "one"
    assert proposal["diff"]["after"]["s1"]["text"] == "reviewed line"
    assert _head(client) == 1
    assert client.get(f"/api/projects/{PID}/versions/2").status_code == 404


def test_proposal_idempotency_replays_the_same_record(client):
    _create(client)

    first = _propose(client, idem="same-proposal")
    replay = _propose(client, idem="same-proposal")

    assert first.status_code == 201
    assert replay.status_code == 200, replay.text
    assert replay.json()["status"] == "replayed"
    assert replay.json()["proposal"]["proposal_id"] == first.json()["proposal"]["proposal_id"]
    assert _head(client) == 1


def test_proposal_idempotency_key_cannot_bind_a_different_command(client):
    _create(client)

    first = _propose(client, idem="bound-key")
    conflict = _propose(
        client,
        idem="bound-key",
        message='rewrite the narration as "different line"',
    )

    assert first.status_code == 201
    assert conflict.status_code == 409, conflict.text
    assert "already bound" in conflict.json()["detail"]
    assert _head(client) == 1


def test_approval_rechecks_head_and_commits_exactly_once(client):
    _create(client)
    proposal = _propose(client, idem="approve-once").json()["proposal"]

    first = client.post(f"/api/projects/{PID}/proposals/{proposal['proposal_id']}/approve")
    replay = client.post(f"/api/projects/{PID}/proposals/{proposal['proposal_id']}/approve")

    assert first.status_code == 200, first.text
    assert first.json()["status"] == "approved"
    assert first.json()["target_version"] == 2
    assert replay.status_code == 200, replay.text
    assert replay.json()["status"] == "replayed"
    assert replay.json()["target_version"] == 2
    assert _head(client) == 2
    assert client.get(f"/api/projects/{PID}/versions/2").json()["version"]["script"]["segments"][0]["text"] == "reviewed line"


def test_stale_or_locked_approval_fails_without_mutating_project(client):
    _create(client)
    proposal = _propose(client, idem="stale-approval").json()["proposal"]

    edit = client.post(
        f"/api/projects/{PID}/commands",
        json={
            "project_id": PID,
            "base_version": 1,
            "actor": "canvas",
            "operation": "edit_script",
            "selection": {"kind": "scene", "scene_ids": ["s2"]},
            "payload": {"kind": "script_segments", "segments": [_segment("s2", "newer")]},
            "idempotency_key": "newer-edit",
        },
    )
    assert edit.status_code == 200, edit.text
    stale = client.post(f"/api/projects/{PID}/proposals/{proposal['proposal_id']}/approve")
    assert stale.status_code == 409, stale.text
    assert stale.json()["detail"]["error"] == "stale_version"
    assert _head(client) == 2
    assert client.get(f"/api/projects/{PID}/versions/2").json()["version"]["script"]["segments"][1]["text"] == "newer"

    # A fresh proposal is still reviewable, but a lock added after proposal
    # creation blocks its approval without changing the head.
    fresh = _propose(client, idem="locked-approval").json()["proposal"]
    lock = client.post(
        f"/api/projects/{PID}/locks",
        json={"scope": "content", "locked": True, "locked_by": "director", "reason": "approved"},
    )
    assert lock.status_code == 200, lock.text
    locked = client.post(f"/api/projects/{PID}/proposals/{fresh['proposal_id']}/approve")
    assert locked.status_code == 409, locked.text
    assert locked.json()["detail"]["error"] == "lock_conflict"
    assert _head(client) == 2


def test_reject_and_revoke_are_noop_and_replay_safely(client):
    _create(client)
    rejected = _propose(client, idem="reject-me").json()["proposal"]

    reject = client.post(
        f"/api/projects/{PID}/proposals/{rejected['proposal_id']}/reject",
        json={"actor": "director"},
    )
    replay = client.post(
        f"/api/projects/{PID}/proposals/{rejected['proposal_id']}/reject",
        json={"actor": "director"},
    )
    assert reject.status_code == 200, reject.text
    assert reject.json()["status"] == "rejected"
    assert replay.status_code == 200, replay.text
    assert replay.json()["status"] == "replayed"
    assert _head(client) == 1

    revoked = _propose(client, idem="revoke-me").json()["proposal"]
    revoke = client.post(
        f"/api/projects/{PID}/proposals/{revoked['proposal_id']}/revoke",
        json={"actor": "admin"},
    )
    assert revoke.status_code == 200, revoke.text
    assert revoke.json()["status"] == "revoked"
    assert _head(client) == 1


@pytest.mark.asyncio
async def test_stale_completed_job_is_attention_state_not_current_success(tmp_path, monkeypatch):
    """A late completion is isolated and cannot remain a false completed preview."""
    job_id = "f1e2d3c4"
    job_dir = tmp_path / "jobs" / job_id
    job_dir.mkdir(parents=True)
    initialize_job_status(job_dir, job_id, "gemini")
    monkeypatch.setenv("FYF_BUDGET_LEDGER_PATH", str(tmp_path / "budget" / "ledger.json"))

    async def complete_without_attachment(*_args, **_kwargs):
        update_job_status(
            job_dir,
            {"status": "completed", "video_url": f"/api/jobs/{job_id}/video"},
        )

    monkeypatch.setattr(main, "run_pipeline", complete_without_attachment)
    monkeypatch.setattr(
        main,
        "_attach_completed_project_result",
        lambda *_args: {"status": "stale_isolated", "applied": False},
    )

    await main._run_video_pipeline_tracked(job_id, {"title": "stale"}, "gemini", tmp_path / "jobs")

    status = read_job_status(job_dir)
    assert status["status"] == "needs_attention"
    assert status["video_url"] is None
    assert "stale" in status["error"].lower()
