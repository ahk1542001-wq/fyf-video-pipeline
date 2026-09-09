"""B6 - Atomic command application (document lines 83, 93, 97-99).

``apply_command`` is the single mutating entry point of the project spine. Its
contract:

1. Resolve the head version and **reject or rebase** a stale ``base_version`` -
   never silently overwrite (document line 93).
2. Build the proposed operations, then validate **ALL** of them by fully
   re-validating the resulting :class:`ProjectVersion` (which re-validates the
   wrapped ``VideoScript``). Any validation / lock / ownership / version failure
   yields **zero partial edits** (document lines 97-99): nothing is written
   unless the whole new version validates.
3. Write exactly one new immutable version, then append one ``WorkflowEvent``.
4. The approved diff equals the applied operations - enforced by the
   ``ChangeSet`` model validator (``proposed_operations == applied_operations``).

Paid operations are refused server-side unless a persisted approval record exists
(``budget_store.read_approvals``); budget availability is checked with
``budget_store.is_budget_available``. ``budget_store`` is never modified here.

Locks are consumed through an injectable ``LockCheckHook``. The default hook
reads the version's whole-script ``LockState``; Task 9 can inject a granular
lock_store-backed hook WITHOUT changing ``apply_command``.

Concurrency: all mutation happens inside ``store.transaction(project_id)``, a
per-project lock, so concurrent commands on the same base version resolve to
exactly one winner (the loser sees the advanced head and is rejected/rebased).
"""

from __future__ import annotations

import uuid
import hashlib
import json
from contextlib import contextmanager
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Callable, Optional

from pydantic import ValidationError

from video_contract import VideoScript

from backend.budget_store import (
    get_budget_status,
    is_budget_available,
    read_approvals,
)
from backend.projects.models import (
    ApprovalEffects,
    ChangeSet,
    LockEffects,
    ProjectCommand,
    ProjectVersion,
    PinnedProductionConfig,
    ValidationResult,
    WorkflowEvent,
    operation_is_paid,
    operation_requires_approval,
)
from backend.projects.selection import UnknownSelectionTargetError, resolve_selection
from backend.projects.store import (
    InvalidProjectIdError,
    ProjectNotFoundError,
    ProjectStore,
    VersionAlreadyExistsError,
)

__all__ = [
    "apply_command",
    "create_project_with_script",
    "default_lock_checker",
    "LockCheckHook",
    "SpineError",
    "StaleVersionError",
    "LockConflictError",
    "ApprovalRequiredError",
    "CommandValidationError",
    "IdempotencyConflictError",
    "CommandPreview",
    "preview_command",
    "command_touched_scopes",
    "command_fingerprint",
    "InvalidProjectIdError",
    "utc_now_iso",
]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class SpineError(Exception):
    """Base class for project-spine command failures."""


class StaleVersionError(SpineError):
    """``base_version`` no longer matches the committed head (document line 93)."""

    def __init__(self, project_id: str, base_version: int, current_version: int) -> None:
        self.project_id = project_id
        self.base_version = base_version
        self.current_version = current_version
        super().__init__(
            f"stale base_version {base_version} for project {project_id}; "
            f"committed head is {current_version} (reject or rebase, never overwrite)"
        )


class LockConflictError(SpineError):
    """The command's scope is locked against edits."""

    def __init__(self, project_id: str, conflicts: list[str]) -> None:
        self.project_id = project_id
        self.conflicts = list(conflicts)
        super().__init__(
            f"locked scope(s) {sorted(self.conflicts)} block this command on {project_id}"
        )


class ApprovalRequiredError(SpineError):
    """A paid operation lacks a persisted approval / available budget."""

    def __init__(self, project_id: str, operation: str, reason: str, budget_status: dict) -> None:
        self.project_id = project_id
        self.operation = operation
        self.reason = reason
        self.budget_status = budget_status
        super().__init__(f"paid operation {operation!r} refused: {reason}")


class CommandValidationError(SpineError):
    """Validation of the proposed operations failed; nothing was written."""

    def __init__(self, changeset: ChangeSet) -> None:
        self.changeset = changeset
        super().__init__(
            "command rejected: " + "; ".join(changeset.validation.errors)
        )


class IdempotencyConflictError(SpineError):
    """An idempotency key is already bound to a different command."""

    def __init__(self, project_id: str, idempotency_key: str) -> None:
        self.project_id = project_id
        self.idempotency_key = idempotency_key
        super().__init__(
            f"idempotency key {idempotency_key!r} is already bound to a different command"
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

LockCheckHook = Callable[[Optional[ProjectVersion], ProjectCommand], list[str]]


_SCENE_SCOPE_FIELDS: dict[str, str] = {
    "text": "content",
    "caption": "content",
    "visual_action": "visual",
    "visual": "visual",
    "duration_seconds": "timing",
    "voice": "voice",
}


def command_touched_scopes(
    version: Optional[ProjectVersion], command: ProjectCommand
) -> list[str]:
    """Return every scene lock scope changed by ``command`` in stable order.

    Older commands declare one operation scope, while ``edit_scene`` can carry
    a validated atomic replacement containing fields from more than one scope.
    Comparing the replacement to the current version keeps lock checks honest
    when a caller submits a complete segment snapshot rather than a patch.
    """

    declared = command.effective_scope()
    scopes: set[str] = {declared} if declared is not None else set()
    payload = command.payload
    if payload is None or payload.kind != "script_segments":
        if command.operation == "edit_timing" and payload is not None:
            scopes.add("timing")
        return [scope for scope in ("content", "visual", "timing", "voice") if scope in scopes]

    by_id = {segment.id: segment for segment in version.script.segments} if version else {}
    for replacement in payload.segments:
        current = by_id.get(replacement.id)
        if current is None:
            # Selection validation reports unknown ids later. Keep the declared
            # operation scope here so a whole-scope lock still fails closed.
            continue
        replacement_values = replacement.model_dump(mode="python", exclude_none=False)
        # ``exclude_if`` keeps legacy JSON compact when an optional field is
        # absent, but it must not erase the distinction between an omitted
        # field and an explicit ``null`` clear for lock checks.  Pydantic keeps
        # that distinction in ``model_fields_set``.
        replacement_fields = getattr(replacement, "model_fields_set", set(replacement_values))
        for field, scope in _SCENE_SCOPE_FIELDS.items():
            if field in replacement_fields and getattr(replacement, field, None) != getattr(current, field, None):
                scopes.add(scope)
    return [scope for scope in ("content", "visual", "timing", "voice") if scope in scopes]


@dataclass(frozen=True)
class CommandPreview:
    """Validated command effect with no persisted version or event."""

    head_version: int
    selected_segment_ids: list[str]
    resulting_version: ProjectVersion


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def command_fingerprint(command: ProjectCommand) -> str:
    """Stable digest of the complete validated command identity."""

    payload = json.dumps(
        command.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def default_lock_checker(
    version: Optional[ProjectVersion], command: ProjectCommand
) -> list[str]:
    """Whole-scope lock check backed by the version's own ``LockState``.

    Returns the list of conflicting scopes (empty == allowed). Task 9 replaces
    this with a granular ``lock_store``-backed hook via the ``lock_checker``
    parameter of :func:`apply_command`; the signature is the seam.
    """
    if version is None:
        return []
    return [
        scope
        for scope in command_touched_scopes(version, command)
        if bool(getattr(version.locks, scope, False))
    ]


def _new_event_id() -> str:
    return uuid.uuid4().hex


def _match_approval(
    approvals: dict,
    command: ProjectCommand,
    estimated: Optional[float],
    *,
    require_positive_spend: bool,
) -> Optional[dict]:
    """Find an approved record matching this command's operation + target.

    Convention: an approval's ``target_ref`` equals the command idempotency_key,
    the bare project id, or the exact ``{project_id}@v{base_version}`` target.
    When a project id is persisted on an approval it must match the command;
    this prevents a key collision in one project from authorizing another.
    The approved spend must cover the estimate; an unknown estimate (None)
    still requires an explicit approved spend so cost is never treated as 0.
    """
    targets = {
        command.idempotency_key,
        command.project_id,
        f"{command.project_id}@v{command.base_version}",
    }
    for record in approvals.values():
        if not isinstance(record, dict):
            continue
        if record.get("operation") != command.operation:
            continue
        if str(record.get("decision", "")).lower() != "approved":
            continue
        if record.get("target_ref") not in targets:
            continue
        record_project = record.get("project_id")
        if record_project is not None and record_project != command.project_id:
            continue
        approved_spend = record.get("approved_spend_usd")
        if not isinstance(approved_spend, (int, float)):
            return None
        if estimated is None:
            if require_positive_spend and approved_spend <= 0:
                return None
        elif approved_spend < estimated:
            return None
        return record
    return None


# ---------------------------------------------------------------------------
# Version construction (validate ALL, zero partial edits)
# ---------------------------------------------------------------------------


def _merged_segment_dicts(
    base: ProjectVersion, command: ProjectCommand, selected_ids: list[str]
) -> list[dict]:
    payload = command.payload
    assert payload is not None and payload.kind == "script_segments"
    replacements = {segment.id: segment for segment in payload.segments}
    outside = [sid for sid in replacements if sid not in set(selected_ids)]
    if outside:
        raise ValueError(
            f"payload edits segments outside the selection: {sorted(outside)}"
        )
    merged: list[dict] = []
    for segment in base.script.segments:
        chosen = replacements.get(segment.id, segment)
        merged.append(chosen.model_dump(mode="json"))
    return merged


def _merged_timing_dicts(
    base: ProjectVersion, command: ProjectCommand, selected_ids: list[str]
) -> list[dict]:
    payload = command.payload
    assert payload is not None and payload.kind == "segment_timings"
    selected = set(selected_ids)
    outside = [t.segment_id for t in payload.timings if t.segment_id not in selected]
    if outside:
        raise ValueError(
            f"timing payload targets segments outside the selection: {sorted(outside)}"
        )
    by_id = {t.segment_id: t for t in base.segment_timings}
    for timing in payload.timings:
        by_id[timing.segment_id] = timing
    # Preserve script order for known segments; keep deterministic ordering.
    order = {sid: i for i, sid in enumerate(base.segment_ids())}
    items = sorted(by_id.values(), key=lambda t: order.get(t.segment_id, 1 << 30))
    return [t.model_dump(mode="json") for t in items]


def _build_new_version(
    base: ProjectVersion,
    command: ProjectCommand,
    selected_ids: list[str],
    new_version_no: int,
    now: str,
    *,
    identity_command: Optional[ProjectCommand] = None,
) -> ProjectVersion:
    """Construct + FULLY re-validate the next version. Raises on any invalid edit."""
    data = base.model_dump(mode="json")
    data["version_no"] = new_version_no
    data["parent_version"] = base.version_no
    data["actor"] = command.actor
    data["created_at"] = now
    data["idempotency_key"] = command.idempotency_key
    # Preserve the caller's submitted identity when an explicit rebase creates
    # an effective command with a rewritten base_version. Retries must replay
    # the same request rather than collide merely because the head moved.
    data["command_fingerprint"] = command_fingerprint(identity_command or command)
    data["applied_operations"] = [command.operation]
    data["source_command_operation"] = command.operation

    operation = command.operation
    payload = command.payload
    if operation in {"edit_script", "edit_scene", "edit_visual", "edit_voice", "reedit_duration"}:
        data["script"]["segments"] = _merged_segment_dicts(base, command, selected_ids)
    elif operation == "edit_timing" and payload is not None and payload.kind == "script_segments":
        data["script"]["segments"] = _merged_segment_dicts(base, command, selected_ids)
    elif operation == "edit_timing":
        data["segment_timings"] = _merged_timing_dicts(base, command, selected_ids)
    elif operation == "update_surface":
        assert payload is not None and payload.kind == "surface"
        data["surface"] = payload.surface.model_dump(mode="json")
    elif operation in {"request_render", "reframe_aspect"}:
        assert payload is not None and payload.kind == "render_request"
        if operation == "reframe_aspect":
            if payload.manifest is None or payload.manifest.output.aspect_ratio is None:
                raise ValueError(
                    "reframe_aspect requires a manifest with output.aspect_ratio"
                )
            data["script"]["aspect_ratio"] = payload.manifest.output.aspect_ratio
        data["render_manifest"] = (
            payload.manifest.model_dump(mode="json") if payload.manifest is not None else None
        )
    else:  # pragma: no cover - operation is a closed Literal
        raise ValueError(f"unsupported operation {operation!r}")

    # A draft edit creates a new semantic head.  Any video/manifest inherited
    # from the previous head was rendered for different content (or a
    # different timing/voice contract), so retaining it would let the client
    # present stale output as the current Ready preview.  Keep the immutable
    # source version intact and invalidate only the newly-created head; a
    # subsequent result attachment can add an exact-head asset again.
    if operation in {
        "edit_script",
        "edit_scene",
        "edit_visual",
        "edit_voice",
        "edit_timing",
        "reedit_duration",
        "update_surface",
        "request_render",
        "reframe_aspect",
    }:
        data["asset_references"] = [
            asset
            for asset in data.get("asset_references", [])
            if asset.get("kind") != "video"
        ]
    if operation in {
        "edit_script",
        "edit_scene",
        "edit_visual",
        "edit_voice",
        "edit_timing",
        "reedit_duration",
        "update_surface",
    }:
        data["render_manifest"] = None

    # Full revalidation: a multi-scene invalid edit raises here, before any write.
    return ProjectVersion.model_validate(data)


def _rejected_changeset(
    command: ProjectCommand, base_version: int, errors: list[str]
) -> ChangeSet:
    return ChangeSet(
        project_id=command.project_id,
        base_version=base_version,
        target_version=None,
        status="rejected",
        applied=False,
        proposed_operations=[command.operation],
        applied_operations=[],
        validation=ValidationResult(passed=False, errors=errors),
        idempotency_key=command.idempotency_key,
        reason="validation_failed",
    )


@contextmanager
def _transaction_if_needed(store: ProjectStore, project_id: str, lock_held: bool):
    """Use the caller's project lock when proposal approval already holds it."""
    if lock_held:
        yield
    else:
        with store.transaction(project_id):
            yield


def _preview_command_locked(
    store: ProjectStore,
    command: ProjectCommand,
    *,
    now_fn: Callable[[], str],
) -> CommandPreview:
    """Validate one command against the current head without writing anything."""
    if not store.project_exists(command.project_id):
        raise ProjectNotFoundError(f"project {command.project_id} not found")
    head = store.current_version_no(command.project_id)
    if head < 1:
        raise ProjectNotFoundError(f"project {command.project_id} has no versions")
    if command.base_version != head:
        raise StaleVersionError(command.project_id, command.base_version, head)
    base = store.load_version(command.project_id, head)
    try:
        selected_ids = resolve_selection(base, command.selection)
    except UnknownSelectionTargetError as exc:
        raise CommandValidationError(_rejected_changeset(command, head, [str(exc)])) from exc
    try:
        resulting = _build_new_version(base, command, selected_ids, head + 1, now_fn())
    except (ValidationError, ValueError) as exc:
        raise CommandValidationError(
            _rejected_changeset(command, head, [_sanitize_error(exc)])
        ) from exc
    return CommandPreview(
        head_version=head,
        selected_segment_ids=selected_ids,
        resulting_version=resulting,
    )


def preview_command(
    store: ProjectStore,
    command: ProjectCommand,
    *,
    now_fn: Callable[[], str] = utc_now_iso,
    _lock_held: bool = False,
) -> CommandPreview:
    """Return the fully validated effect of ``command`` without committing it.

    This is the validation seam used by chat proposals.  It deliberately skips
    persisted approvals and lock checks: those are re-checked at approval time
    while the project transaction is held, so a review card cannot become stale
    authority merely because it was created earlier.
    """
    with _transaction_if_needed(store, command.project_id, _lock_held):
        return _preview_command_locked(store, command, now_fn=now_fn)


# ---------------------------------------------------------------------------
# Project creation (version 1) - used by POST /api/projects
# ---------------------------------------------------------------------------


def create_project_with_script(
    store: ProjectStore,
    project_id: str,
    script: VideoScript,
    actor: str,
    *,
    variant_name: Optional[str] = None,
    idempotency_key: Optional[str] = None,
    pinned_production_config: Optional[PinnedProductionConfig] = None,
    now_fn: Callable[[], str] = utc_now_iso,
) -> ProjectVersion:
    """Append version 1 wrapping ``script`` and record a ``project_created`` event.

    Idempotent: replaying the same ``idempotency_key`` returns the existing
    version without writing a duplicate.
    """
    with store.transaction(project_id):
        if idempotency_key:
            existing = store.find_by_idempotency_key(project_id, idempotency_key)
            if existing is not None:
                return existing
        if store.current_version_no(project_id) != 0:
            raise VersionAlreadyExistsError(
                f"project {project_id} already has committed versions"
            )
        now = now_fn()
        store.create_project(project_id)
        version = ProjectVersion.model_validate(
            {
                "project_id": project_id,
                "version_no": 1,
                "parent_version": None,
                "script": script.model_dump(mode="json"),
                "variant_name": variant_name,
                "pinned_production_config": (
                    pinned_production_config.model_dump(mode="json")
                    if pinned_production_config is not None
                    else {}
                ),
                "actor": actor,
                "created_at": now,
                "idempotency_key": idempotency_key,
                "applied_operations": ["create_project"],
                "source_command_operation": None,
            }
        )
        store.append_version(version)
        store.append_event(
            WorkflowEvent(
                event_id=_new_event_id(),
                project_id=project_id,
                version_no=1,
                event_type="project_created",
                stage="created",
                status="completed",
                sequence=store.next_sequence(project_id),
                progress_source="actual",
                actor=actor,
                idempotency_key=idempotency_key,
                timestamp=now,
            )
        )
        return version


# ---------------------------------------------------------------------------
# apply_command
# ---------------------------------------------------------------------------


def apply_command(
    store: ProjectStore,
    command: ProjectCommand,
    *,
    lock_checker: LockCheckHook = default_lock_checker,
    rebase: bool = False,
    budget_root=None,
    now_fn: Callable[[], str] = utc_now_iso,
    _lock_held: bool = False,
) -> ChangeSet:
    """Atomically validate + apply one command; return the resulting ChangeSet.

    Raises:
        InvalidProjectIdError: malformed project id / path escape.
        ProjectNotFoundError: the project has no committed versions.
        StaleVersionError: ``base_version`` != head and ``rebase`` is False.
        LockConflictError: the command's scope is locked.
        ApprovalRequiredError: a paid op lacks an approval / available budget.
        CommandValidationError: proposed operations failed validation (no write).
    """
    # Ownership / id validation happens first (raises InvalidProjectIdError).
    if not store.project_exists(command.project_id):
        # project_exists validates the id before checking existence; a malformed
        # id raises InvalidProjectIdError, a well-formed-but-absent one is 404.
        raise ProjectNotFoundError(f"project {command.project_id} not found")

    with _transaction_if_needed(store, command.project_id, _lock_held):
        # 1) Idempotency replay: never a duplicate version/reservation/approval.
        existing = store.find_by_idempotency_key(
            command.project_id, command.idempotency_key
        )
        if existing is not None:
            fingerprint = command_fingerprint(command)
            # A replay is valid only for the exact command that originally
            # committed this idempotency key. Older versions without a stored
            # fingerprint cannot prove that identity and therefore fail closed.
            if existing.command_fingerprint != fingerprint:
                raise IdempotencyConflictError(
                    command.project_id,
                    command.idempotency_key,
                )
            return ChangeSet(
                project_id=command.project_id,
                base_version=command.base_version,
                target_version=existing.version_no,
                status="replayed",
                applied=False,
                replayed=True,
                proposed_operations=[command.operation],
                applied_operations=[],
                affected_segment_ids=[],
                validation=ValidationResult(passed=True),
                idempotency_key=command.idempotency_key,
                reason="idempotent_replay",
            )

        # 2) Resolve head; reject or rebase a stale base_version.
        head = store.current_version_no(command.project_id)
        if head < 1:
            raise ProjectNotFoundError(f"project {command.project_id} has no versions")
        effective = command
        if command.base_version != head:
            if not rebase:
                raise StaleVersionError(command.project_id, command.base_version, head)
            effective = command.model_copy(update={"base_version": head})

        base_version = store.load_version(command.project_id, head)

        # 3) Locks (injectable hook; Task 9 widens to granular scopes).
        conflicts = lock_checker(base_version, effective)
        if conflicts:
            raise LockConflictError(command.project_id, conflicts)

        # 4) Approval-gated operations require a persisted human decision.
        # Paid operations additionally require available budget. A duration
        # re-edit is a zero-spend story decision, not a provider dispatch.
        approval_effects: Optional[ApprovalEffects] = None
        estimated_spend: Optional[float] = None
        is_paid = operation_is_paid(effective.operation)
        if operation_requires_approval(effective.operation):
            estimated_spend = (
                getattr(effective.payload, "estimated_cost_usd", None) if is_paid else None
            )
            approvals = read_approvals(root_dir=budget_root)
            if approvals.get("corrupted"):
                raise ApprovalRequiredError(
                    command.project_id,
                    effective.operation,
                    approvals.get("reason") or "budget ledger unavailable (fail-closed)",
                    get_budget_status(root_dir=budget_root, project_id=command.project_id),
                )
            matched = _match_approval(
                approvals.get("approvals", {}),
                effective,
                estimated_spend,
                require_positive_spend=is_paid,
            )
            if matched is None:
                raise ApprovalRequiredError(
                    project_id=command.project_id,
                    operation=effective.operation,
                    reason="no matching approved spend record for this operation",
                    budget_status=get_budget_status(root_dir=budget_root, project_id=command.project_id),
                )
            if is_paid and estimated_spend is not None and not is_budget_available(
                estimated_spend, root_dir=budget_root, project_id=command.project_id
            ):
                raise ApprovalRequiredError(
                    project_id=command.project_id,
                    operation=effective.operation,
                    reason="approved but budget is unavailable/exceeded (fail-closed)",
                    budget_status=get_budget_status(root_dir=budget_root, project_id=command.project_id),
                )
            approval_effects = ApprovalEffects(
                required=True,
                operation=effective.operation,
                approved_spend_usd=matched.get("approved_spend_usd"),
                approval_id=matched.get("approval_id"),
                decision=matched.get("decision"),
            )

        # 5) Resolve selection (pure) -> affected dependencies.
        try:
            selected_ids = resolve_selection(base_version, effective.selection)
        except UnknownSelectionTargetError as exc:
            raise CommandValidationError(
                _rejected_changeset(effective, head, [str(exc)])
            ) from exc

        # 6) Build + validate ALL operations. Zero partial edits on failure.
        new_version_no = head + 1
        now = now_fn()
        try:
            new_version = _build_new_version(
                base_version,
                effective,
                selected_ids,
                new_version_no,
                now,
                identity_command=command,
            )
        except (ValidationError, ValueError) as exc:
            raise CommandValidationError(
                _rejected_changeset(effective, head, [_sanitize_error(exc)])
            ) from exc

        changeset = ChangeSet(
            project_id=command.project_id,
            base_version=head,
            target_version=new_version_no,
            status="rebased" if effective is not command else "applied",
            applied=True,
            proposed_operations=[effective.operation],
            applied_operations=list(new_version.applied_operations),
            affected_segment_ids=selected_ids,
            lock_effects=LockEffects(),
            approval_effects=approval_effects,
            estimated_spend_usd=estimated_spend,
            validation=ValidationResult(passed=True),
            idempotency_key=command.idempotency_key,
            reason=None,
        )

        # 7) Write exactly one new immutable version, then one event.
        store.append_version(new_version)
        event_type = (
            "render_requested"
            if effective.operation in {"request_render", "reframe_aspect"}
            else "version_appended"
        )
        store.append_event(
            WorkflowEvent(
                event_id=_new_event_id(),
                project_id=command.project_id,
                version_no=new_version_no,
                event_type=event_type,
                stage=effective.operation,
                status="completed",
                sequence=store.next_sequence(command.project_id),
                progress_source="actual",
                artifact_refs=(
                    [new_version.render_manifest.video_spec_version]
                    if effective.operation in {"request_render", "reframe_aspect"}
                    and new_version.render_manifest is not None
                    else []
                ),
                actor=command.actor,
                idempotency_key=command.idempotency_key,
                timestamp=now,
            )
        )
        return changeset


def _sanitize_error(exc: Exception) -> str:
    """A short, secret-free validation message for the ChangeSet/HTTP surface."""
    text = str(exc).strip().replace("\n", " ")
    return text[:500] if text else exc.__class__.__name__
