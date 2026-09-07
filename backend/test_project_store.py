"""Stage B exit-gate tests - B5 storage seam (backend.projects.store).

Covers the durability/safety gates: atomic immutable versions, append-only
monotonic event log, NO torn JSONL line across an interrupted write (restart /
resume), per-project exactly-one-winner concurrency, stale-lock reclaim on
restart, idempotency lookup, path-escape / malformed-id rejection, and the
FYF_PROJECTS_ROOT override.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest import mock

import pytest

from video_contract import ScriptSegment, VideoScript

from backend.projects.store import (
    DEFAULT_PROJECTS_ROOT,
    PROJECTS_ROOT_ENV,
    FileProjectStore,
    InvalidProjectIdError,
    NonMonotonicSequenceError,
    ProjectNotFoundError,
    ProjectVersionNotFoundError,
    VersionAlreadyExistsError,
    resolve_projects_root,
)
from backend.projects.models import ProjectVersion, WorkflowEvent

PID = "a1b2c3d4"


def _script() -> VideoScript:
    return VideoScript(
        title="Demo",
        language="my-MM",
        segments=[
            ScriptSegment(id="s1", text="one", visual_action="a", scene_type="whiteboard",
                          mascot_action="present", emotion="neutral"),
            ScriptSegment(id="s2", text="two", visual_action="b", scene_type="demo",
                          mascot_action="explain", emotion="warm"),
        ],
    )


def _version(version_no: int, idem: str | None = None) -> ProjectVersion:
    return ProjectVersion.model_validate(
        {
            "project_id": PID,
            "version_no": version_no,
            "parent_version": None if version_no == 1 else version_no - 1,
            "script": _script().model_dump(mode="json"),
            "actor": "alice",
            "created_at": "2026-01-01T00:00:00Z",
            "idempotency_key": idem,
        }
    )


def _event(sequence: int) -> WorkflowEvent:
    return WorkflowEvent(
        event_id=f"e{sequence}",
        project_id=PID,
        version_no=1,
        event_type="version_appended",
        sequence=sequence,
        timestamp="2026-01-01T00:00:00Z",
    )


@pytest.fixture()
def store(tmp_path) -> FileProjectStore:
    return FileProjectStore(tmp_path / "projects")


# --- root resolution ---------------------------------------------------------


def test_resolve_projects_root_default_and_env(monkeypatch):
    monkeypatch.delenv(PROJECTS_ROOT_ENV, raising=False)
    assert resolve_projects_root() == Path(DEFAULT_PROJECTS_ROOT)
    monkeypatch.setenv(PROJECTS_ROOT_ENV, "/tmp/custom-projects")
    assert resolve_projects_root() == Path("/tmp/custom-projects")
    # Explicit arg always wins.
    assert resolve_projects_root("/x/y") == Path("/x/y")


# --- versions: atomic + immutable -------------------------------------------


def test_append_then_load_version_roundtrips(store):
    store.create_project(PID)
    v1 = _version(1, idem="create")
    store.append_version(v1)
    loaded = store.load_version(PID, 1)
    assert loaded == v1
    assert store.list_versions(PID) == [1]
    assert store.current_version_no(PID) == 1


def test_version_file_is_written_atomically_no_temp_leftover(store):
    store.create_project(PID)
    store.append_version(_version(1))
    versions_dir = store.root / PID / "versions"
    files = sorted(p.name for p in versions_dir.iterdir())
    assert files == ["000001.json"], files  # no stray temp files


def test_append_duplicate_version_is_rejected(store):
    store.create_project(PID)
    store.append_version(_version(1))
    with pytest.raises(VersionAlreadyExistsError):
        store.append_version(_version(1))


def test_load_missing_version_raises(store):
    store.create_project(PID)
    with pytest.raises(ProjectVersionNotFoundError):
        store.load_version(PID, 5)


def test_list_versions_is_sorted_and_ignores_temp_files(store):
    store.create_project(PID)
    for n in (2, 1, 3):
        store.append_version(_version(n))
    versions_dir = store.root / PID / "versions"
    # A leftover temp file must never be mistaken for a committed version.
    (versions_dir / "000004.json.tmp.deadbeef").write_text("{ partial", encoding="utf-8")
    assert store.list_versions(PID) == [1, 2, 3]
    assert store.current_version_no(PID) == 3


# --- events: append-only, monotonic -----------------------------------------


def test_events_are_monotonic_and_readable(store):
    store.create_project(PID)
    store.append_event(_event(1))
    store.append_event(_event(2))
    events = store.read_events(PID)
    assert [e.sequence for e in events] == [1, 2]
    assert store.next_sequence(PID) == 3
    assert [e.sequence for e in store.read_events(PID, after_sequence=1)] == [2]


def test_non_monotonic_sequence_is_rejected(store):
    store.create_project(PID)
    store.append_event(_event(5))
    with pytest.raises(NonMonotonicSequenceError):
        store.append_event(_event(5))
    with pytest.raises(NonMonotonicSequenceError):
        store.append_event(_event(4))


# --- restart / resume: NO torn JSONL line -----------------------------------


def test_interrupted_event_write_leaves_log_intact(store):
    """An interrupted append (os.replace fails) must not tear the JSONL log."""
    store.create_project(PID)
    store.append_event(_event(1))
    store.append_event(_event(2))

    with mock.patch("os.replace", side_effect=OSError("simulated crash mid-append")):
        with pytest.raises(OSError):
            store.append_event(_event(3))

    # The log still holds exactly the first two events; every line is complete
    # and parses (no torn/partial third line).
    events = store.read_events(PID)
    assert [e.sequence for e in events] == [1, 2]
    raw = (store.root / PID / "events.jsonl").read_text(encoding="utf-8")
    for line in raw.splitlines():
        if line.strip():
            json.loads(line)  # must not raise


def test_restart_with_fresh_store_instance_resumes_cleanly(store):
    """A brand-new store on the same root (process restart) reads prior state."""
    store.create_project(PID)
    store.append_version(_version(1))
    store.append_event(_event(1))

    restarted = FileProjectStore(store.root)  # simulate a fresh process
    assert restarted.current_version_no(PID) == 1
    assert [e.sequence for e in restarted.read_events(PID)] == [1]
    assert restarted.next_sequence(PID) == 2
    # A leftover temp artifact from a crash is ignored, not read as a version.
    (restarted.root / PID / "versions" / "000002.json.tmp.cafe").write_text("{", encoding="utf-8")
    assert restarted.list_versions(PID) == [1]


def test_stale_lock_from_dead_process_is_reclaimed_on_restart(store):
    store.create_project(PID)
    # Obtain a pid that is provably dead (subprocess has exited and been reaped).
    proc = subprocess.run(
        [sys.executable, "-c", "import os; print(os.getpid())"],
        capture_output=True, text=True,
    )
    dead_pid = int(proc.stdout.strip())
    lock_path = store.root / PID / ".lock"
    lock_path.write_text(
        json.dumps({"token": "deadbeef", "pid": dead_pid, "host": socket.gethostname(),
                    "acquired_at": time.time()}),
        encoding="utf-8",
    )
    assert store._lock_is_stale(lock_path) is True
    # The transaction must reclaim the stale lock rather than deadlock/timeout.
    with store.transaction(PID, timeout=2.0):
        store.append_version(_version(1))
    assert store.current_version_no(PID) == 1


def test_live_lock_is_not_stale(store):
    store.create_project(PID)
    lock_path = store.root / PID / ".lock"
    lock_path.write_text(
        json.dumps({"token": "live", "pid": os.getpid(), "host": socket.gethostname(),
                    "acquired_at": time.time()}),
        encoding="utf-8",
    )
    assert store._lock_is_stale(lock_path) is False


# --- concurrency: exactly-one-winner ----------------------------------------


def test_concurrent_creators_yield_exactly_one_winner(store):
    """Many threads race to commit version 1; exactly one may win."""
    store.create_project(PID)
    results: list[str] = []
    results_lock = threading.Lock()
    start = threading.Barrier(8)

    def worker():
        start.wait()
        try:
            with store.transaction(PID, timeout=10.0):
                if store.current_version_no(PID) == 0:
                    store.append_version(_version(1))
                    outcome = "won"
                else:
                    outcome = "lost"
        except Exception as exc:  # pragma: no cover - surfaced for debugging
            outcome = f"error:{type(exc).__name__}"
        with results_lock:
            results.append(outcome)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert results.count("won") == 1, results
    assert all(r in {"won", "lost"} for r in results), results
    assert store.current_version_no(PID) == 1
    assert store.list_versions(PID) == [1]


# --- idempotency lookup ------------------------------------------------------


def test_find_by_idempotency_key(store):
    store.create_project(PID)
    store.append_version(_version(1, idem="create-key"))
    store.append_version(_version(2, idem="edit-key"))
    assert store.find_by_idempotency_key(PID, "edit-key").version_no == 2
    assert store.find_by_idempotency_key(PID, "create-key").version_no == 1
    assert store.find_by_idempotency_key(PID, "missing") is None
    assert store.find_by_idempotency_key(PID, "") is None


# --- ownership / path-escape -------------------------------------------------


@pytest.mark.parametrize("bad_id", ["../../etc", "..", "ZZZZZZZZ", "abcd", "a1b2c3d", "", "a1b2c3d4e"])
def test_malformed_or_escaping_project_ids_are_rejected(store, bad_id):
    with pytest.raises(InvalidProjectIdError):
        store._project_dir(bad_id)
    with pytest.raises(InvalidProjectIdError):
        store.project_exists(bad_id)


def test_project_dir_stays_under_root(store):
    project_dir = store._project_dir(PID)
    assert project_dir.resolve().is_relative_to(store.root.resolve())


def test_load_version_on_unknown_project_raises(store):
    with pytest.raises((ProjectNotFoundError, ProjectVersionNotFoundError)):
        store.load_version("deadbeef", 1)


# --- store satisfies the Protocol -------------------------------------------


def test_file_store_satisfies_protocol_shape(store):
    from backend.projects.store import ProjectStore

    assert isinstance(store, ProjectStore)
