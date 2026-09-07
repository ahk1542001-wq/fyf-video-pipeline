"""C4 - granular locks (content/visual/timing) + the ``lock_checker`` seam.

Proves, without touching Jason's ``backend.projects.{models,store,commands}``:

* the whole-script ``story`` lock (``create_script_lock``/``read_script_lock``,
  backing ``/api/story-lock``) is UNCHANGED;
* the granular registry sets/reads/releases scope locks atomically, validates
  scope + project id, and fails OPEN on a corrupt registry (never a silent
  freeze);
* ``granular_lock_checker`` WIDENS - never narrows - ``default_lock_checker`` and
  injects into ``apply_command`` through the existing ``lock_checker`` parameter,
  leaving ``apply_command``'s signature and default behaviour untouched;
* locked content survives an automated repair attempt UNCHANGED (doc line 249);
* lock granularity is real: a ``timing`` lock does not block a ``content`` edit.
"""

from __future__ import annotations

import pytest

from backend import lock_store
from backend.lock_store import (
    GRANULAR_LOCK_SCOPES,
    WHOLE_SCRIPT_LOCK_SCOPE,
    granular_lock_checker,
    is_scope_locked,
    locked_scopes,
    read_scope_locks,
    release_scope_lock,
    set_scope_lock,
)
from backend.projects.commands import (
    LockConflictError,
    apply_command,
    create_project_with_script,
    default_lock_checker,
)
from backend.projects.models import ProjectCommand
from backend.projects.store import FileProjectStore
from video_contract import VideoScript

PID = "a1b2c3d4"


def _script() -> VideoScript:
    return VideoScript.model_validate(
        {
            "title": "Demo",
            "language": "my-MM",
            "segments": [
                {"id": "s1", "text": "locked narration", "visual_action": "a",
                 "scene_type": "whiteboard", "mascot_action": "present", "emotion": "neutral"},
                {"id": "s2", "text": "two", "visual_action": "b",
                 "scene_type": "demo", "mascot_action": "explain", "emotion": "warm"},
            ],
        }
    )


def _segments(texts):
    return [
        {"id": sid, "text": txt, "visual_action": f"show {sid}", "scene_type": "whiteboard",
         "mascot_action": "present", "emotion": "neutral"}
        for sid, txt in texts
    ]


def _store_with_project(tmp_path) -> FileProjectStore:
    store = FileProjectStore(tmp_path / "projects")
    create_project_with_script(store, PID, _script(), "alice", idempotency_key="create")
    return store


def _edit(operation="edit_script", texts=(("s1", "x"), ("s2", "y")), idem="edit", base=1):
    return ProjectCommand.model_validate(
        {
            "project_id": PID, "base_version": base, "actor": "bob", "operation": operation,
            "payload": {"kind": "script_segments", "segments": _segments(texts)},
            "idempotency_key": idem,
        }
    )


# --- whole-script lock behaviour is unchanged -------------------------------


def test_whole_script_scope_is_story_and_granular_scopes_are_three():
    assert WHOLE_SCRIPT_LOCK_SCOPE == "story"
    assert GRANULAR_LOCK_SCOPES == ("content", "visual", "timing")
    assert lock_store.LOCK_SCOPES == ("story", "content", "visual", "timing")


def test_create_and_read_script_lock_unchanged(tmp_path):
    root = tmp_path / "locks"
    lock_id = lock_store.create_script_lock(root, _script().model_dump(mode="json"))
    loaded = lock_store.read_script_lock(root, lock_id)
    assert loaded["title"] == "Demo"
    assert [s["id"] for s in loaded["segments"]] == ["s1", "s2"]


# --- granular registry ------------------------------------------------------


def test_set_read_release_scope_lock(tmp_path):
    root = tmp_path / "locks"
    assert read_scope_locks(root, PID) == {}
    record = set_scope_lock(root, PID, "content", locked=True, locked_by="alice",
                            reason="approved narration")
    assert record["locked"] is True
    assert record["reason"] == "approved narration"
    assert record["locked_by"] == "alice"
    assert is_scope_locked(root, PID, "content") is True
    assert is_scope_locked(root, PID, "visual") is False
    assert locked_scopes(root, PID) == ["content"]
    release_scope_lock(root, PID, "content")
    assert is_scope_locked(root, PID, "content") is False
    assert locked_scopes(root, PID) == []


def test_unknown_scope_and_bad_project_id_rejected(tmp_path):
    root = tmp_path / "locks"
    with pytest.raises(ValueError):
        set_scope_lock(root, PID, "story", locked=True)  # story is not granular
    with pytest.raises(ValueError):
        is_scope_locked(root, PID, "bogus")
    with pytest.raises(ValueError):
        read_scope_locks(root, "../escape")


def test_corrupt_registry_fails_open(tmp_path):
    root = tmp_path / "locks"
    set_scope_lock(root, PID, "timing", locked=True)
    (root / "granular" / f"{PID}.json").write_text("{ not json", encoding="utf-8")
    # A corrupt registry is never treated as a lock (no silent operator freeze).
    assert read_scope_locks(root, PID) == {}
    assert locked_scopes(root, PID) == []
    assert is_scope_locked(root, PID, "timing") is False


# --- the lock_checker seam widens the default; apply_command unchanged -------


def test_granular_checker_matches_default_when_no_locks(tmp_path):
    store = _store_with_project(tmp_path)
    head = store.load_version(PID, 1)
    cmd = _edit()
    assert default_lock_checker(head, cmd) == []
    assert granular_lock_checker(tmp_path / "locks")(head, cmd) == []


def test_granular_checker_preserves_version_lockstate_default(tmp_path):
    store = _store_with_project(tmp_path)
    head = store.load_version(PID, 1)
    locked_head = head.model_copy(update={"locks": head.locks.model_copy(update={"content": True})})
    cmd = _edit()
    # The version's own LockState still conflicts (default behaviour preserved).
    assert granular_lock_checker(tmp_path / "locks")(locked_head, cmd) == ["content"]


def test_apply_command_blocks_locked_scope_via_registry(tmp_path):
    store = _store_with_project(tmp_path)
    locks_root = tmp_path / "locks"
    set_scope_lock(locks_root, PID, "content", locked=True, reason="narration approved")
    with pytest.raises(LockConflictError) as exc:
        apply_command(store, _edit(), lock_checker=granular_lock_checker(locks_root))
    assert "content" in exc.value.conflicts
    # Zero partial edits: head unchanged, original text intact.
    assert store.current_version_no(PID) == 1
    assert store.load_version(PID, 1).script.segments[0].text == "locked narration"


def test_apply_command_default_checker_ignores_registry(tmp_path):
    # Injection-only seam: with the DEFAULT checker the registry is not consulted,
    # so apply_command's default behaviour is provably unchanged.
    store = _store_with_project(tmp_path)
    set_scope_lock(tmp_path / "locks", PID, "content", locked=True)
    changeset = apply_command(store, _edit(texts=(("s1", "allowed-by-default"), ("s2", "y"))))
    assert changeset.status == "applied"
    assert store.load_version(PID, 2).script.segments[0].text == "allowed-by-default"


def test_locked_content_survives_automated_repair(tmp_path):
    # Doc line 249: locked content must survive automated repair UNCHANGED.
    store = _store_with_project(tmp_path)
    locks_root = tmp_path / "locks"
    set_scope_lock(locks_root, PID, "content", locked=True, locked_by="director",
                   reason="locked narration")
    before = store.load_version(PID, 1).script.segments[0].text
    repair = _edit(texts=(("s1", "AUTO-REPAIRED"), ("s2", "two")), idem="repair")
    with pytest.raises(LockConflictError):
        apply_command(store, repair, lock_checker=granular_lock_checker(locks_root))
    head_after = store.load_version(PID, store.current_version_no(PID))
    assert head_after.script.segments[0].text == before == "locked narration"
    assert store.current_version_no(PID) == 1  # the repair wrote nothing


def test_timing_lock_does_not_block_content_edit(tmp_path):
    # Granularity is real: a timing lock must not block a content (edit_script) edit.
    store = _store_with_project(tmp_path)
    locks_root = tmp_path / "locks"
    set_scope_lock(locks_root, PID, "timing", locked=True)
    changeset = apply_command(
        store, _edit(texts=(("s1", "still editable"), ("s2", "y"))),
        lock_checker=granular_lock_checker(locks_root),
    )
    assert changeset.status == "applied"
    assert store.load_version(PID, 2).script.segments[0].text == "still editable"
