"""B5 - Project spine storage seam.

``ProjectStore`` is the durable-versioning seam consumed by ``commands.py`` and
(by extension) Task 5 (durable queue) and Task 9 (chat->command / granular
locks). ``FileProjectStore`` is the only implementation today; a Postgres/GCS
backed store can be added additively by satisfying the same Protocol.

Durability & safety
-------------------
* Immutable versions are written with ``backend.job_store.write_json_atomically``
  (sibling temp file + flush + ``os.fsync`` + ``os.replace``), so a version file
  is always either fully present or absent - never torn.
* The append-only event log (``events.jsonl``) is rewritten through the SAME
  temp+fsync+os.replace technique, which guarantees no torn JSONL line even
  across a crash mid-append. Sequence numbers are monotonic per project.
* Project ids reuse ``job_store.is_valid_job_id`` (``[0-9a-f]{8}``) and every
  path is ``resolve()``-checked against the root, mirroring ``lock_store`` -
  malformed ids and traversal outside the root are structurally rejected.
* A per-project file lock (atomic ``os.link`` claim + pid/host liveness reclaim)
  serializes appends so concurrent writers get exactly-one-winner semantics and
  a crashed holder's lock is reclaimable on restart.

Layout::

    {root}/{project_id}/
        versions/000001.json   # immutable, one per version
        events.jsonl           # append-only, monotonic sequence
        .lock                  # transient per-project lock
"""

from __future__ import annotations

import json
import os
import socket
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional, Protocol, runtime_checkable

from backend.job_store import is_valid_job_id, write_json_atomically
from backend.projects.models import ProjectVersion, WorkflowEvent

__all__ = [
    "ProjectStore",
    "FileProjectStore",
    "resolve_projects_root",
    "InvalidProjectIdError",
    "ProjectNotFoundError",
    "ProjectVersionNotFoundError",
    "VersionAlreadyExistsError",
    "NonMonotonicSequenceError",
    "ProjectLockTimeoutError",
    "PROJECTS_ROOT_ENV",
    "DEFAULT_PROJECTS_ROOT",
]


PROJECTS_ROOT_ENV = "FYF_PROJECTS_ROOT"
# Default lives under the gitignored /output/ tree (mirrors main.py's JOBS_ROOT)
# so a stray real run never pollutes the tracked tree.
DEFAULT_PROJECTS_ROOT = "output/projects"


def resolve_projects_root(root: Path | str | None = None) -> Path:
    """Resolve the projects root following the ``FYF_JOBS_ROOT`` env pattern."""
    if root is not None:
        return Path(root)
    return Path(os.getenv(PROJECTS_ROOT_ENV, DEFAULT_PROJECTS_ROOT))


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class InvalidProjectIdError(ValueError):
    """Raised when a project id is malformed or attempts path escape."""


class ProjectNotFoundError(KeyError):
    """Raised when a project does not exist under the root."""


class ProjectVersionNotFoundError(KeyError):
    """Raised when a requested version number is absent."""


class VersionAlreadyExistsError(RuntimeError):
    """Raised when an append targets an already-committed version number."""


class NonMonotonicSequenceError(RuntimeError):
    """Raised when an event sequence is not strictly greater than the last."""


class ProjectLockTimeoutError(TimeoutError):
    """Raised when the per-project lock could not be acquired within timeout."""


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class ProjectStore(Protocol):
    """Durable project-version + event seam.

    The five core contract methods are ``load_version``, ``append_version``,
    ``list_versions``, ``append_event`` and ``read_events``. The remaining
    methods are the coordination primitives ``commands.apply_command`` needs
    (head lookup, idempotency replay, monotonic sequence, and a serializing
    ``transaction``); an alternative backend must provide them too.
    """

    # -- core contract ------------------------------------------------------
    def load_version(self, project_id: str, version_no: int) -> ProjectVersion: ...
    def append_version(self, version: ProjectVersion) -> ProjectVersion: ...
    def list_versions(self, project_id: str) -> list[int]: ...
    def append_event(self, event: WorkflowEvent) -> WorkflowEvent: ...
    def read_events(
        self, project_id: str, after_sequence: int = 0
    ) -> list[WorkflowEvent]: ...

    # -- coordination primitives -------------------------------------------
    def project_exists(self, project_id: str) -> bool: ...
    def create_project(self, project_id: str) -> None: ...
    def current_version_no(self, project_id: str) -> int: ...
    def next_sequence(self, project_id: str) -> int: ...
    def find_by_idempotency_key(
        self, project_id: str, idempotency_key: str
    ) -> Optional[ProjectVersion]: ...
    def transaction(self, project_id: str) -> "contextmanager[None]": ...


def _process_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


# ---------------------------------------------------------------------------
# FileProjectStore
# ---------------------------------------------------------------------------


class FileProjectStore:
    """Filesystem-backed :class:`ProjectStore` (the only implementation today)."""

    _VERSION_GLOB = "*.json"
    _LOCK_NAME = ".lock"
    _EVENTS_NAME = "events.jsonl"
    _VERSIONS_DIRNAME = "versions"

    def __init__(self, root: Path | str) -> None:
        self._root = Path(root)

    @property
    def root(self) -> Path:
        return self._root

    # -- path safety --------------------------------------------------------
    def _validate_project_id(self, project_id: str) -> None:
        if not isinstance(project_id, str) or not is_valid_job_id(project_id):
            raise InvalidProjectIdError(
                f"Invalid project id (expected 8 lowercase hex): {project_id!r}"
            )

    def _project_dir(self, project_id: str) -> Path:
        self._validate_project_id(project_id)
        root_resolved = self._root.resolve()
        project_dir = (self._root / project_id).resolve()
        try:
            project_dir.relative_to(root_resolved)
        except ValueError as exc:  # pragma: no cover - defense in depth
            raise InvalidProjectIdError("Forbidden project path") from exc
        return project_dir

    def _versions_dir(self, project_id: str) -> Path:
        return self._project_dir(project_id) / self._VERSIONS_DIRNAME

    def _version_path(self, project_id: str, version_no: int) -> Path:
        if not isinstance(version_no, int) or version_no < 1:
            raise ProjectVersionNotFoundError(
                f"version_no must be a positive int, got {version_no!r}"
            )
        return self._versions_dir(project_id) / f"{version_no:06d}.json"

    def _events_path(self, project_id: str) -> Path:
        return self._project_dir(project_id) / self._EVENTS_NAME

    # -- project lifecycle --------------------------------------------------
    def project_exists(self, project_id: str) -> bool:
        return self._versions_dir(project_id).is_dir()

    def create_project(self, project_id: str) -> None:
        self._versions_dir(project_id).mkdir(parents=True, exist_ok=True)

    # -- version storage ----------------------------------------------------
    def _committed_version_numbers(self, project_id: str) -> list[int]:
        versions_dir = self._versions_dir(project_id)
        if not versions_dir.is_dir():
            return []
        numbers: list[int] = []
        for path in versions_dir.glob(self._VERSION_GLOB):
            stem = path.stem  # e.g. "000001"; temp files never match \d{6}
            if len(stem) == 6 and stem.isdigit():
                numbers.append(int(stem))
        return sorted(numbers)

    def list_versions(self, project_id: str) -> list[int]:
        return self._committed_version_numbers(project_id)

    def current_version_no(self, project_id: str) -> int:
        numbers = self._committed_version_numbers(project_id)
        return numbers[-1] if numbers else 0

    def load_version(self, project_id: str, version_no: int) -> ProjectVersion:
        path = self._version_path(project_id, version_no)
        if not path.is_file() or path.stat().st_size == 0:
            raise ProjectVersionNotFoundError(
                f"project {project_id} version {version_no} not found"
            )
        return ProjectVersion.model_validate_json(path.read_text(encoding="utf-8"))

    def append_version(self, version: ProjectVersion) -> ProjectVersion:
        project_id = version.project_id
        versions_dir = self._versions_dir(project_id)
        versions_dir.mkdir(parents=True, exist_ok=True)
        path = self._version_path(project_id, version.version_no)
        if path.exists():
            raise VersionAlreadyExistsError(
                f"version {version.version_no} already committed for {project_id}"
            )
        # Atomic, durable, all-or-nothing (temp + fsync + os.replace).
        write_json_atomically(path, version.model_dump(mode="json"))
        return version

    def find_by_idempotency_key(
        self, project_id: str, idempotency_key: str
    ) -> Optional[ProjectVersion]:
        if not idempotency_key:
            return None
        for version_no in self._committed_version_numbers(project_id):
            try:
                version = self.load_version(project_id, version_no)
            except (ProjectVersionNotFoundError, ValueError):
                continue
            if version.idempotency_key == idempotency_key:
                return version
        return None

    # -- event log ----------------------------------------------------------
    def _read_event_lines(self, project_id: str) -> list[str]:
        path = self._events_path(project_id)
        if not path.is_file():
            return []
        raw = path.read_text(encoding="utf-8")
        return [line for line in raw.splitlines() if line.strip()]

    def _write_event_lines(self, project_id: str, lines: list[str]) -> None:
        path = self._events_path(project_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.tmp.{uuid.uuid4().hex[:8]}")
        try:
            with open(tmp, "w", encoding="utf-8") as handle:
                for line in lines:
                    handle.write(line.rstrip("\n") + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass

    def read_events(
        self, project_id: str, after_sequence: int = 0
    ) -> list[WorkflowEvent]:
        self._validate_project_id(project_id)
        events: list[WorkflowEvent] = []
        for line in self._read_event_lines(project_id):
            event = WorkflowEvent.model_validate_json(line)
            if event.sequence > after_sequence:
                events.append(event)
        events.sort(key=lambda e: e.sequence)
        return events

    def next_sequence(self, project_id: str) -> int:
        highest = 0
        for line in self._read_event_lines(project_id):
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:  # pragma: no cover - torn line impossible
                continue
            seq = payload.get("sequence")
            if isinstance(seq, int) and seq > highest:
                highest = seq
        return highest + 1

    def append_event(self, event: WorkflowEvent) -> WorkflowEvent:
        project_id = event.project_id
        self._validate_project_id(project_id)
        lines = self._read_event_lines(project_id)
        highest = 0
        for line in lines:
            try:
                seq = json.loads(line).get("sequence")
            except json.JSONDecodeError:  # pragma: no cover
                continue
            if isinstance(seq, int) and seq > highest:
                highest = seq
        if event.sequence <= highest:
            raise NonMonotonicSequenceError(
                f"event sequence {event.sequence} is not greater than last {highest}"
            )
        lines.append(event.model_dump_json())
        # Atomic full-file rewrite => no torn JSONL line even across a crash.
        self._write_event_lines(project_id, lines)
        return event

    # -- per-project lock / transaction -------------------------------------
    def _lock_path(self, project_id: str) -> Path:
        return self._project_dir(project_id) / self._LOCK_NAME

    def _lock_is_stale(self, lock_path: Path) -> bool:
        try:
            payload = json.loads(lock_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return True
        if not isinstance(payload, dict):
            return True
        same_host = payload.get("host") == socket.gethostname()
        try:
            pid = int(payload.get("pid", 0))
        except (TypeError, ValueError):
            return True
        # Mirror job_store.acquire_job_lease: only reclaim a same-host lock whose
        # owning process is provably gone.
        return same_host and not _process_is_alive(pid)

    def _acquire_lock(
        self, project_id: str, timeout: float = 10.0, poll: float = 0.005
    ) -> str:
        project_dir = self._project_dir(project_id)
        project_dir.mkdir(parents=True, exist_ok=True)
        lock_path = project_dir / self._LOCK_NAME
        token = uuid.uuid4().hex
        payload = {
            "token": token,
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "acquired_at": time.time(),
        }
        deadline = time.monotonic() + timeout
        while True:
            tmp = lock_path.with_name(f"{self._LOCK_NAME}.tmp.{token[:8]}")
            try:
                with open(tmp, "w", encoding="utf-8") as handle:
                    json.dump(payload, handle)
                    handle.flush()
                    os.fsync(handle.fileno())
                # os.link fails with FileExistsError if the lock already exists:
                # an atomic exclusive claim whose target already holds full content.
                os.link(tmp, lock_path)
                return token
            except FileExistsError:
                if self._lock_is_stale(lock_path):
                    try:
                        lock_path.unlink()
                    except FileNotFoundError:
                        pass
                    continue
                if time.monotonic() >= deadline:
                    raise ProjectLockTimeoutError(
                        f"could not acquire lock for project {project_id}"
                    )
                time.sleep(poll)
            finally:
                if tmp.exists():
                    try:
                        tmp.unlink()
                    except OSError:
                        pass

    def _release_lock(self, project_id: str, token: Optional[str]) -> None:
        if not token:
            return
        lock_path = self._lock_path(project_id)
        try:
            payload = json.loads(lock_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return
        if isinstance(payload, dict) and payload.get("token") == token:
            lock_path.unlink(missing_ok=True)

    @contextmanager
    def transaction(
        self, project_id: str, timeout: float = 10.0
    ) -> Iterator[None]:
        """Serialize all appends for one project (exactly-one-winner semantics)."""
        token = self._acquire_lock(project_id, timeout=timeout)
        try:
            yield
        finally:
            self._release_lock(project_id, token)
