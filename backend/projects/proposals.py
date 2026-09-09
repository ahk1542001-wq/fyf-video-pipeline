"""Durable command-proposal persistence for the Creative Director chat.

Proposal records live beside (but never inside) immutable project versions:

    {projects_root}/{project_id}/proposals/{proposal_id}.json

The route layer owns the project transaction and uses this small store while the
project lock is held.  Every write is atomic, and no method here mutates a
project version or event log.
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path
from typing import Optional

from backend.job_store import is_valid_job_id, write_json_atomically
from backend.projects.models import ProjectProposal

__all__ = [
    "ProposalStore",
    "FileProposalStore",
    "ProposalNotFoundError",
    "ProposalCorruptError",
    "ProposalConflictError",
    "InvalidProposalIdError",
    "new_proposal_id",
]


_PROPOSAL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


class InvalidProposalIdError(ValueError):
    """A proposal id is malformed or would escape the project directory."""


class ProposalNotFoundError(KeyError):
    """The requested proposal does not exist."""


class ProposalCorruptError(ValueError):
    """A persisted proposal could not be validated safely."""


class ProposalConflictError(RuntimeError):
    """An idempotency key or proposal id is already bound to another command."""


def new_proposal_id() -> str:
    """Create an opaque, path-safe proposal identifier."""
    return uuid.uuid4().hex


class FileProposalStore:
    """Filesystem-backed proposal records.

    A caller that needs proposal + version atomicity must wrap calls in the
    corresponding ``FileProjectStore.transaction(project_id)``.  This class
    deliberately does not acquire a second lock, which keeps approval from
    deadlocking when it commits the command under the project lock.
    """

    def __init__(self, root: Path | str) -> None:
        self._root = Path(root)

    @property
    def root(self) -> Path:
        return self._root

    def _project_dir(self, project_id: str) -> Path:
        if not isinstance(project_id, str) or not is_valid_job_id(project_id):
            raise InvalidProposalIdError(
                f"Invalid project id (expected 8 lowercase hex): {project_id!r}"
            )
        root = self._root.resolve()
        project_dir = (self._root / project_id).resolve()
        try:
            project_dir.relative_to(root)
        except ValueError as exc:
            raise InvalidProposalIdError("Forbidden proposal project path") from exc
        if project_dir.is_symlink():
            raise InvalidProposalIdError("Symlinked proposal project path is forbidden")
        return project_dir

    def _proposal_path(self, project_id: str, proposal_id: str) -> Path:
        if not isinstance(proposal_id, str) or not _PROPOSAL_ID_RE.fullmatch(proposal_id):
            raise InvalidProposalIdError(f"Invalid proposal id: {proposal_id!r}")
        project_dir = self._project_dir(project_id)
        proposals_dir = project_dir / "proposals"
        if proposals_dir.exists() and proposals_dir.is_symlink():
            raise InvalidProposalIdError("Symlinked proposals directory is forbidden")
        path = (proposals_dir / f"{proposal_id}.json").resolve()
        try:
            path.relative_to(project_dir.resolve())
        except ValueError as exc:
            raise InvalidProposalIdError("Forbidden proposal path") from exc
        return path

    def _read(self, path: Path) -> ProjectProposal:
        try:
            payload = path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise ProposalNotFoundError(path.stem) from exc
        except OSError as exc:
            raise ProposalCorruptError(f"Could not read proposal {path.stem}") from exc
        try:
            return ProjectProposal.model_validate_json(payload)
        except ValueError as exc:
            raise ProposalCorruptError(f"Proposal {path.stem} is invalid") from exc

    def load(self, project_id: str, proposal_id: str) -> ProjectProposal:
        path = self._proposal_path(project_id, proposal_id)
        if not path.is_file() or path.stat().st_size == 0:
            raise ProposalNotFoundError(proposal_id)
        proposal = self._read(path)
        if proposal.project_id != project_id or proposal.proposal_id != proposal_id:
            raise ProposalCorruptError(f"Proposal {proposal_id} has mismatched identity")
        return proposal

    def list(self, project_id: str) -> list[ProjectProposal]:
        project_dir = self._project_dir(project_id)
        proposals_dir = project_dir / "proposals"
        if not proposals_dir.is_dir():
            return []
        records: list[ProjectProposal] = []
        for path in sorted(proposals_dir.glob("*.json")):
            if not _PROPOSAL_ID_RE.fullmatch(path.stem):
                continue
            records.append(self.load(project_id, path.stem))
        return records

    def find_by_idempotency_key(
        self, project_id: str, idempotency_key: str
    ) -> Optional[ProjectProposal]:
        if not idempotency_key:
            return None
        for proposal in self.list(project_id):
            if proposal.idempotency_key == idempotency_key:
                return proposal
        return None

    def create(self, proposal: ProjectProposal) -> ProjectProposal:
        path = self._proposal_path(proposal.project_id, proposal.proposal_id)
        existing_by_key = self.find_by_idempotency_key(
            proposal.project_id, proposal.idempotency_key
        )
        if existing_by_key is not None:
            if existing_by_key.command != proposal.command:
                raise ProposalConflictError(
                    f"idempotency key {proposal.idempotency_key!r} is already bound"
                )
            return existing_by_key
        if path.exists():
            existing = self._read(path)
            if existing == proposal:
                return existing
            raise ProposalConflictError(f"proposal {proposal.proposal_id!r} already exists")
        path.parent.mkdir(parents=True, exist_ok=True)
        write_json_atomically(path, proposal.model_dump(mode="json"))
        return proposal

    def save(self, proposal: ProjectProposal) -> ProjectProposal:
        path = self._proposal_path(proposal.project_id, proposal.proposal_id)
        if not path.is_file():
            raise ProposalNotFoundError(proposal.proposal_id)
        current = self._read(path)
        if current.project_id != proposal.project_id or current.proposal_id != proposal.proposal_id:
            raise ProposalCorruptError("proposal identity changed")
        if current.idempotency_key != proposal.idempotency_key or current.command != proposal.command:
            raise ProposalConflictError("proposal command or idempotency identity is immutable")
        write_json_atomically(path, proposal.model_dump(mode="json"))
        return proposal


ProposalStore = FileProposalStore
