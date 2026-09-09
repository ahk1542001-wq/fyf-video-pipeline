"""Stage C-II route seam (C2/C3/C5/C7/C9) over the REAL FileProjectStore.

Thin-HTTP correctness only; all durability/atomicity lives in backend.projects.
Roots (projects, locks, budget, jobs) are patched to temp dirs so the suite is
hermetic and never touches the repo's gitignored output/ tree, and no paid
provider work is dispatched.

Proves:
* chat and canvas edit the SAME canonical version (one spine, no private copy);
* an explicit stale base_version is rejected (409), never silently overwritten;
* undo restores a target's content EXACTLY by APPENDING a new version (no
  destructive rollback: prior versions still exist unchanged);
* a named variant is server-persisted and survives a "reload" (a fresh GET);
* a granular lock blocks an edit with an HONEST reason (canvas and chat alike);
* a stale generation result is isolated and does not mutate a newer draft (C7);
* events expose progress_source verbatim (honest progress, C9);
* budget reports unknown cost as null, never 0 (fail-closed, C9).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import backend.main as main

PID = "a1b2c3d4"


def _seg(sid: str, text: str, visual: str = "v") -> dict:
    return {"id": sid, "text": text, "visual_action": visual, "scene_type": "whiteboard",
            "mascot_action": "present", "emotion": "neutral"}


def _script_dict() -> dict:
    return {"title": "Demo", "language": "my-MM",
            "segments": [_seg("s1", "one"), _seg("s2", "two"), _seg("s3", "three")]}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr("backend.main.SCRIPT_JOBS_ROOT", tmp_path / "script-jobs")
    monkeypatch.setattr("backend.main.JOBS_ROOT", tmp_path / "jobs")
    monkeypatch.setattr("backend.main.LOCKS_ROOT", tmp_path / "locks")
    monkeypatch.setenv("FYF_PROJECTS_ROOT", str(tmp_path / "projects"))
    monkeypatch.setenv("FYF_BUDGET_LEDGER_PATH", str(tmp_path / "budget" / ".budget_ledger.json"))
    monkeypatch.delenv("FYF_PUBLIC_DEPLOYMENT", raising=False)
    with TestClient(main.app) as c:
        yield c


def _create(client) -> dict:
    resp = client.post("/api/projects", json={"script": _script_dict(), "actor": "alice",
                                              "project_id": PID, "idempotency_key": "create"})
    assert resp.status_code == 201, resp.text
    return resp.json()


def _canvas_edit(client, sid, text, base, idem) -> "TestClient":
    return client.post(
        f"/api/projects/{PID}/commands",
        json={"project_id": PID, "base_version": base, "actor": "canvas",
              "operation": "edit_script", "selection": {"kind": "scene", "scene_ids": [sid]},
              "payload": {"kind": "script_segments", "segments": [_seg(sid, text)]},
              "idempotency_key": idem},
    )


def _chat_edit(client, message, sid=None, base=None) -> "TestClient":
    body: dict = {"message": message, "actor": "director"}
    if sid is not None:
        body["selection"] = {"kind": "scene", "scene_ids": [sid]}
    if base is not None:
        body["base_version"] = base
    return client.post(f"/api/projects/{PID}/chat", json=body)


def _head_no(client) -> int:
    return client.get(f"/api/projects/{PID}/versions").json()["head"]


def _texts(client, version_no) -> dict:
    version = client.get(f"/api/projects/{PID}/versions/{version_no}").json()["version"]
    return {s["id"]: s["text"] for s in version["script"]["segments"]}


# --- C2: chat and canvas share ONE canonical version ------------------------


def test_versions_list_reports_head_and_segments(client):
    _create(client)
    body = client.get(f"/api/projects/{PID}/versions").json()
    assert body["head"] == 1
    assert body["versions"][0]["segment_ids"] == ["s1", "s2", "s3"]


def test_chat_and_canvas_edit_the_same_canonical_version(client):
    _create(client)
    canvas = _canvas_edit(client, "s1", "canvas text", base=1, idem="canvas-1")
    assert canvas.status_code == 200, canvas.text
    assert canvas.json()["changeset"]["target_version"] == 2

    chat_resp = _chat_edit(client, 'rewrite scene s2 as "chat text"', sid="s2")
    assert chat_resp.status_code == 200, chat_resp.text
    cb = chat_resp.json()
    assert cb["operation"] == "edit_script"
    assert cb["changeset"]["target_version"] == 3  # same spine, head advanced once

    # Both edits are visible in the ONE canonical head (v3): no private copies.
    texts = _texts(client, 3)
    assert texts == {"s1": "canvas text", "s2": "chat text", "s3": "three"}


def test_chat_without_base_version_uses_head(client):
    _create(client)
    assert _chat_edit(client, 'rewrite scene s1 as "v2"', sid="s1").status_code == 200
    assert _head_no(client) == 2
    assert _texts(client, 2)["s1"] == "v2"


def test_chat_with_explicit_stale_base_version_is_409(client):
    _create(client)
    _chat_edit(client, 'rewrite scene s1 as "v2"', sid="s1")
    stale = _chat_edit(client, 'rewrite scene s1 as "stale"', sid="s1", base=1)
    assert stale.status_code == 409
    assert stale.json()["detail"]["error"] == "stale_version"
    # head did not move; the newer draft is intact
    assert _head_no(client) == 2
    assert _texts(client, 2)["s1"] == "v2"


def test_chat_mapping_failure_is_422(client):
    _create(client)
    resp = _chat_edit(client, "rewrite scene s1", sid="s1")  # no new text
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "chat_mapping_failed"


def test_chat_invalid_project_id_is_400(client):
    assert client.post("/api/projects/ZZZZZZZZ/chat", json={"message": "hi"}).status_code == 400


def test_versions_unknown_project_is_404(client):
    assert client.get("/api/projects/deadbeef/versions").status_code == 404


# --- C5: undo appends a restoring version (no destructive rollback) ---------


def test_undo_restores_target_content_exactly_and_keeps_history(client):
    _create(client)
    _canvas_edit(client, "s1", "CHANGED", base=1, idem="c1")
    assert _texts(client, 2)["s1"] == "CHANGED"

    undo = client.post(f"/api/projects/{PID}/undo", json={"target_version": 1, "actor": "director"})
    assert undo.status_code == 200, undo.text
    body = undo.json()
    assert body["status"] == "applied" and body["restored_from"] == 1
    assert body["version"]["version_no"] == 3
    # restored content == v1 exactly
    assert _texts(client, 3) == {"s1": "one", "s2": "two", "s3": "three"}
    # NO destructive rollback: v1 and v2 still exist unchanged, head advanced
    assert _texts(client, 1)["s1"] == "one"
    assert _texts(client, 2)["s1"] == "CHANGED"
    assert _head_no(client) == 3


def test_undo_is_idempotent(client):
    _create(client)
    _canvas_edit(client, "s1", "CHANGED", base=1, idem="c1")
    first = client.post(f"/api/projects/{PID}/undo",
                        json={"target_version": 1, "idempotency_key": "undo-1"})
    replay = client.post(f"/api/projects/{PID}/undo",
                         json={"target_version": 1, "idempotency_key": "undo-1"})
    assert first.json()["version"]["version_no"] == 3
    assert replay.json()["status"] == "replayed"
    assert _head_no(client) == 3  # replay wrote nothing new


def test_undo_missing_target_is_404(client):
    _create(client)
    assert client.post(f"/api/projects/{PID}/undo", json={"target_version": 9}).status_code == 404


# --- C5: named variants are server-persisted (survive reload) ---------------


def test_named_variant_persists_and_survives_reload(client):
    _create(client)
    resp = client.post(f"/api/projects/{PID}/variants",
                       json={"variant_name": "Punchier Hook", "actor": "director"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["variant_name"] == "Punchier Hook"
    variant_no = body["version"]["version_no"]
    assert variant_no == 2

    # "Reload" == a fresh GET against the persisted store (no client-side memory).
    hist = client.get(f"/api/projects/{PID}/versions").json()
    names = {row["version_no"]: row["variant_name"] for row in hist["versions"]}
    assert names[variant_no] == "Punchier Hook"
    fetched = client.get(f"/api/projects/{PID}/versions/{variant_no}").json()["version"]
    assert fetched["variant_name"] == "Punchier Hook"


def test_named_variant_is_idempotent_when_base_version_is_omitted(client):
    _create(client)
    first = client.post(
        f"/api/projects/{PID}/variants",
        json={"variant_name": "Punchier Hook", "idempotency_key": "variant-replay"},
    )
    replay = client.post(
        f"/api/projects/{PID}/variants",
        json={"variant_name": "Punchier Hook", "idempotency_key": "variant-replay"},
    )
    assert first.status_code == 200, first.text
    assert replay.status_code == 200, replay.text
    assert replay.json()["status"] == "replayed"
    assert _head_no(client) == 2


def test_variant_name_length_is_bounded(client):
    _create(client)
    resp = client.post(f"/api/projects/{PID}/variants", json={"variant_name": "x" * 200})
    assert resp.status_code == 422  # max_length=80


# --- C4: granular lock blocks an edit with an honest reason -----------------


def test_lock_blocks_canvas_and_chat_with_honest_reason(client):
    _create(client)
    lock = client.post(f"/api/projects/{PID}/locks",
                       json={"scope": "content", "locked": True, "locked_by": "director",
                             "reason": "narration is approved & locked"})
    assert lock.status_code == 200, lock.text
    assert lock.json()["locked"] == ["content"]

    blocked = _canvas_edit(client, "s1", "nope", base=1, idem="blocked")
    assert blocked.status_code == 409
    detail = blocked.json()["detail"]
    assert detail["error"] == "lock_conflict" and "content" in detail["conflicts"]
    assert _head_no(client) == 1  # nothing written

    chat_blocked = _chat_edit(client, 'rewrite scene s1 as "nope"', sid="s1")
    assert chat_blocked.status_code == 409
    cd = chat_blocked.json()["detail"]
    assert cd["error"] == "lock_conflict"
    assert cd["reasons"]["content"] == "narration is approved & locked"


def test_lock_release_allows_edit_again(client):
    _create(client)
    client.post(f"/api/projects/{PID}/locks", json={"scope": "content", "locked": True})
    client.post(f"/api/projects/{PID}/locks", json={"scope": "content", "locked": False})
    assert client.get(f"/api/projects/{PID}/locks").json()["locked"] == []
    assert _canvas_edit(client, "s1", "fine now", base=1, idem="ok").status_code == 200


def test_unknown_lock_scope_is_422(client):
    _create(client)
    resp = client.post(f"/api/projects/{PID}/locks", json={"scope": "story", "locked": True})
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "unknown_lock_scope"


# --- C7: stale-result isolation at the version level ------------------------


def test_result_attaches_to_its_dispatched_version(client):
    _create(client)
    resp = client.post(f"/api/projects/{PID}/results",
                       json={"dispatched_version": 1, "actor": "gen",
                             "asset_references": [{"asset_id": "a1", "kind": "image",
                                                   "uri": "local://a1.png"}]})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "attached" and body["target_version"] == 2
    v2 = client.get(f"/api/projects/{PID}/versions/2").json()["version"]
    assert any(a["asset_id"] == "a1" for a in v2["asset_references"])


def test_stale_result_is_isolated_and_never_mutates_newer_draft(client):
    _create(client)
    # An edit lands DURING the render, advancing head to 2 (a newer draft).
    _canvas_edit(client, "s1", "NEWER DRAFT", base=1, idem="edit-during-render")
    assert _head_no(client) == 2

    # A result dispatched against v1 arrives late -> isolated, nothing written.
    stale = client.post(f"/api/projects/{PID}/results",
                        json={"dispatched_version": 1,
                              "asset_references": [{"asset_id": "old", "kind": "image"}]})
    assert stale.status_code == 200
    body = stale.json()
    assert body["status"] == "stale_isolated" and body["applied"] is False
    assert body["current_version"] == 2

    # The newer draft is UNCHANGED and no v3 was created.
    assert _head_no(client) == 2
    assert _texts(client, 2)["s1"] == "NEWER DRAFT"
    v2 = client.get(f"/api/projects/{PID}/versions/2").json()["version"]
    assert all(a["asset_id"] != "old" for a in v2["asset_references"])


# --- C9: honest progress + fail-closed budget -------------------------------


def test_events_expose_monotonic_sequence_and_progress_source(client):
    _create(client)
    _chat_edit(client, 'rewrite scene s1 as "v2"', sid="s1")
    events = client.get(f"/api/projects/{PID}/events").json()["events"]
    assert len(events) >= 2
    assert events[0]["event_type"] == "project_created"
    assert all(e["progress_source"] in ("actual", "estimated") for e in events)
    seqs = [e["sequence"] for e in events]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)  # monotonic, unique


def test_budget_reports_unknown_cost_as_null_never_zero(client):
    body = client.get("/api/budget").json()["budget"]
    # Empty temp ledger + unconfigured ceiling => fail-closed, honest unknowns.
    assert body["paid_production_enabled"] is False
    assert body["budget_exceeded"] is True
    assert body["remaining_usd"] is None  # unknown, never 0
    assert body["provider_reported_usd"] is None
    assert body["invoice_confirmed_usd"] is None


# --- guard reuse ------------------------------------------------------------


def test_chat_route_reuses_public_access_guard(client, monkeypatch):
    _create(client)  # created while public deployment is off
    monkeypatch.setenv("FYF_PUBLIC_DEPLOYMENT", "true")
    monkeypatch.setenv("FYF_GENERATION_ACCESS_TOKEN", "operator-only")
    denied = _chat_edit(client, 'rewrite scene s1 as "x"', sid="s1")
    assert denied.status_code == 401
    allowed = client.post(
        f"/api/projects/{PID}/chat",
        json={"message": 'rewrite scene s1 as "x"', "actor": "director",
              "selection": {"kind": "scene", "scene_ids": ["s1"]}},
        headers={"x-fyf-access-token": "operator-only"},
    )
    assert allowed.status_code == 200, allowed.text
