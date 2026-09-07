"""Stage B exit-gate tests - B12 HTTP surface (backend.main project routes).

Verifies the three new routes are a thin, correct seam over backend.projects and
that they reuse the existing public-access guard. Roots are patched to temp dirs
so the FastAPI startup resume hook stays hermetic (no paid/provider work).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import backend.main as main

PID = "a1b2c3d4"


def _script_dict():
    return {
        "title": "Demo",
        "language": "my-MM",
        "segments": [
            {"id": "s1", "text": "one", "visual_action": "a", "scene_type": "whiteboard",
             "mascot_action": "present", "emotion": "neutral"},
            {"id": "s2", "text": "two", "visual_action": "b", "scene_type": "demo",
             "mascot_action": "explain", "emotion": "warm"},
            {"id": "s3", "text": "three", "visual_action": "c", "scene_type": "whiteboard",
             "mascot_action": "think", "emotion": "focused"},
        ],
    }


def _segments_dict(texts):
    return [
        {"id": sid, "text": txt, "visual_action": f"show {sid}", "scene_type": "whiteboard",
         "mascot_action": "present", "emotion": "neutral"}
        for sid, txt in texts
    ]


@pytest.fixture()
def client(tmp_path, monkeypatch):
    # Keep the startup resume hook hermetic and route the spine + budget at temp dirs.
    monkeypatch.setattr("backend.main.SCRIPT_JOBS_ROOT", tmp_path / "script-jobs")
    monkeypatch.setattr("backend.main.JOBS_ROOT", tmp_path / "jobs")
    monkeypatch.setenv("FYF_PROJECTS_ROOT", str(tmp_path / "projects"))
    monkeypatch.setenv("FYF_BUDGET_LEDGER_PATH", str(tmp_path / "budget" / ".budget_ledger.json"))
    monkeypatch.delenv("FYF_PUBLIC_DEPLOYMENT", raising=False)
    with TestClient(main.app) as c:
        yield c


def _create(client, project_id=PID, idem="create"):
    return client.post(
        "/api/projects",
        json={"script": _script_dict(), "actor": "alice", "project_id": project_id,
              "idempotency_key": idem},
    )


# --- POST /api/projects ------------------------------------------------------


def test_create_project_returns_201_and_version_one(client):
    resp = _create(client)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["success"] is True
    assert body["project_id"] == PID
    assert body["version_no"] == 1
    assert body["version"]["script"]["title"] == "Demo"
    assert [s["id"] for s in body["version"]["script"]["segments"]] == ["s1", "s2", "s3"]


def test_create_project_generates_id_when_omitted(client):
    resp = client.post("/api/projects", json={"script": _script_dict(), "actor": "alice"})
    assert resp.status_code == 201, resp.text
    generated = resp.json()["project_id"]
    assert len(generated) == 8 and all(c in "0123456789abcdef" for c in generated)


def test_create_project_is_idempotent(client):
    assert _create(client, idem="same").status_code == 201
    replay = _create(client, idem="same")
    assert replay.status_code == 201
    assert replay.json()["version_no"] == 1  # no duplicate version committed


def test_create_project_rejects_unknown_field(client):
    resp = client.post(
        "/api/projects",
        json={"script": _script_dict(), "actor": "alice", "bogus": 1},
    )
    assert resp.status_code == 422  # extra="forbid"


def test_create_project_rejects_malformed_project_id(client):
    resp = client.post(
        "/api/projects",
        json={"script": _script_dict(), "actor": "alice", "project_id": "../escape"},
    )
    assert resp.status_code == 422  # PROJECT_ID_PATTERN


# --- GET /api/projects/{id}/versions/{n} ------------------------------------


def test_get_version_returns_committed_version(client):
    _create(client)
    resp = client.get(f"/api/projects/{PID}/versions/1")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["version_no"] == 1
    assert body["version"]["project_id"] == PID


def test_get_missing_version_is_404(client):
    _create(client)
    assert client.get(f"/api/projects/{PID}/versions/99").status_code == 404


def test_get_version_zero_is_400(client):
    _create(client)
    assert client.get(f"/api/projects/{PID}/versions/0").status_code == 400


def test_get_version_invalid_project_id_is_400(client):
    assert client.get("/api/projects/ZZZZZZZZ/versions/1").status_code == 400


def test_get_version_unknown_project_is_404(client):
    assert client.get("/api/projects/deadbeef/versions/1").status_code == 404


# --- POST /api/projects/{id}/commands ---------------------------------------


def _edit_command(base_version, texts, selection=None, idem="edit"):
    return {
        "project_id": PID, "base_version": base_version, "actor": "bob",
        "operation": "edit_script", "selection": selection,
        "payload": {"kind": "script_segments", "segments": _segments_dict(texts)},
        "idempotency_key": idem,
    }


def test_command_applies_edit(client):
    _create(client)
    resp = client.post(
        f"/api/projects/{PID}/commands",
        json=_edit_command(1, [("s1", "ONE"), ("s2", "TWO"), ("s3", "THREE")]),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "applied"
    assert body["changeset"]["target_version"] == 2
    assert client.get(f"/api/projects/{PID}/versions/2").json()["version"]["script"]["segments"][0]["text"] == "ONE"


def test_command_stale_base_version_is_409(client):
    _create(client)
    client.post(f"/api/projects/{PID}/commands",
                json=_edit_command(1, [("s1", "ONE"), ("s2", "TWO"), ("s3", "THREE")], idem="e1"))
    stale = client.post(f"/api/projects/{PID}/commands",
                        json=_edit_command(1, [("s1", "X"), ("s2", "Y"), ("s3", "Z")], idem="e2"))
    assert stale.status_code == 409, stale.text
    detail = stale.json()["detail"]
    assert detail["error"] == "stale_version"
    assert detail["base_version"] == 1 and detail["current_version"] == 2


def test_command_stale_with_rebase_succeeds(client):
    _create(client)
    client.post(f"/api/projects/{PID}/commands",
                json=_edit_command(1, [("s1", "ONE"), ("s2", "TWO"), ("s3", "THREE")], idem="e1"))
    rebased = client.post(
        f"/api/projects/{PID}/commands?rebase=true",
        json=_edit_command(1, [("s2", "rebased")], selection={"kind": "scene", "scene_ids": ["s2"]}, idem="e2"),
    )
    assert rebased.status_code == 200, rebased.text
    assert rebased.json()["status"] == "rebased"


def test_command_idempotent_replay(client):
    _create(client)
    cmd = _edit_command(1, [("s1", "ONE"), ("s2", "TWO"), ("s3", "THREE")], idem="dup")
    first = client.post(f"/api/projects/{PID}/commands", json=cmd)
    replay = client.post(f"/api/projects/{PID}/commands", json=cmd)
    assert first.json()["status"] == "applied"
    assert replay.status_code == 200
    assert replay.json()["status"] == "replayed"
    assert replay.json()["changeset"]["target_version"] == 2
    # Head did not advance on replay.
    assert client.get(f"/api/projects/{PID}/versions/3").status_code == 404


def test_command_multi_scene_invalid_edit_is_422_with_zero_partial_writes(client):
    _create(client)
    bad = _edit_command(
        1, [("s1", "ok"), ("s2", "sneaky")],
        selection={"kind": "scene", "scene_ids": ["s1"]}, idem="bad",
    )
    resp = client.post(f"/api/projects/{PID}/commands", json=bad)
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "validation_failed"
    assert detail["changeset"]["status"] == "rejected"
    assert detail["changeset"]["applied_operations"] == []
    # Zero partial edits: no version 2 exists and version 1 is unchanged.
    assert client.get(f"/api/projects/{PID}/versions/2").status_code == 404
    assert client.get(f"/api/projects/{PID}/versions/1").json()["version"]["script"]["segments"][0]["text"] == "one"


def test_command_body_project_id_must_match_path(client):
    _create(client)
    mismatch = _edit_command(1, [("s1", "ONE"), ("s2", "TWO"), ("s3", "THREE")])
    mismatch["project_id"] = "deadbeef"
    resp = client.post(f"/api/projects/{PID}/commands", json=mismatch)
    assert resp.status_code == 400


def test_command_on_unknown_project_is_404(client):
    resp = client.post(
        "/api/projects/deadbeef/commands",
        json={"project_id": "deadbeef", "base_version": 1, "actor": "bob",
              "operation": "edit_script",
              "payload": {"kind": "script_segments", "segments": _segments_dict([("s1", "x")])},
              "idempotency_key": "nf"},
    )
    assert resp.status_code == 404


def test_paid_render_without_approval_is_402(client):
    _create(client)
    resp = client.post(
        f"/api/projects/{PID}/commands",
        json={"project_id": PID, "base_version": 1, "actor": "bob",
              "operation": "request_render",
              "payload": {"kind": "render_request", "estimated_cost_usd": None, "manifest": None},
              "idempotency_key": "render"},
    )
    assert resp.status_code == 402, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "approval_required"
    # Budget surfaced honestly: unknown remaining stays null, never 0.
    assert detail["budget_status"]["remaining_usd"] is None
    # Nothing was committed.
    assert client.get(f"/api/projects/{PID}/versions/2").status_code == 404


def test_command_rejects_unknown_field(client):
    _create(client)
    body = _edit_command(1, [("s1", "ONE"), ("s2", "TWO"), ("s3", "THREE")])
    body["unexpected"] = True
    resp = client.post(f"/api/projects/{PID}/commands", json=body)
    assert resp.status_code == 422  # ProjectCommand extra="forbid"


# --- reuse of the existing public-access guard ------------------------------


def test_public_deployment_requires_access_token(client, monkeypatch):
    monkeypatch.setenv("FYF_PUBLIC_DEPLOYMENT", "true")
    monkeypatch.setenv("FYF_GENERATION_ACCESS_TOKEN", "operator-only")

    # Without the operator token => 401 (guard reused, not reimplemented).
    denied = client.post("/api/projects", json={"script": _script_dict(), "actor": "alice",
                                                "project_id": PID})
    assert denied.status_code == 401

    # With the token => allowed.
    allowed = client.post("/api/projects", json={"script": _script_dict(), "actor": "alice",
                                                 "project_id": PID},
                          headers={"x-fyf-access-token": "operator-only"})
    assert allowed.status_code == 201, allowed.text


def test_existing_routes_remain_behaviour_compatible(client):
    """Smoke: a couple of pre-existing routes still respond as before."""
    assert client.get("/health").json() == {"status": "ok", "service": "fyf-video-pipeline"}
    assert client.get("/api/health").status_code == 200
    runtime = client.get("/api/runtime")
    assert runtime.status_code == 200
    assert "generation_available" in runtime.json()
