"""B4 - Versioned project spine interface contracts (document lines 83-91).

Every model in this module follows the repository convention established in
``video_contract.py``: ``ConfigDict(extra="forbid")`` so unknown keys are
rejected structurally, and lossless round-tripping through
``model_dump(mode="json")``.

Design rules honored here
-------------------------
* ``ProjectVersion`` **wraps** ``video_contract.VideoScript`` (imported by
  reference); it never re-declares or duplicates a single ``VideoScript`` field
  (document line 84).
* ``SurfaceSpec`` is a **closed Pydantic discriminated union** of approved
  components + bindings + actions + a schema version. There is deliberately no
  general component-registry / binding DSL and no field capable of carrying
  arbitrary HTML/JS: ``extra="forbid"`` plus a closed union of literal-typed
  components makes arbitrary payloads structurally impossible (explicit Leader
  decision; satisfies document line 86).
* Cost/usage values are ``Optional`` and default to ``None`` (meaning
  *unknown*). They are **never** defaulted to ``0`` (document line 89 / 154).

Nothing in this module performs I/O, a provider call, or constructs an ADK
agent on import.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from video_contract import AspectRatio, ScriptSegment, VideoScript

__all__ = [
    "SCHEMA_VERSION",
    "PROJECT_ID_PATTERN",
    "CommandOperation",
    "CommandScope",
    "OPERATION_SCOPE",
    "OPERATION_PAYLOAD_KIND",
    "PAID_OPERATIONS",
    "operation_is_paid",
    "scope_for_operation",
    "CostState",
    "Usage",
    "SanitizedError",
    "ToolResult",
    "SceneSelection",
    "ObjectSelection",
    "TimeRangeSelection",
    "AllSelection",
    "Selection",
    "SegmentTiming",
    "AssetReference",
    "CreativeDirection",
    "LockState",
    "PinnedProductionConfig",
    "OutputMetadata",
    "RenderManifest",
    "TextComponent",
    "ButtonComponent",
    "SceneListComponent",
    "PreviewComponent",
    "StatusComponent",
    "SurfaceComponent",
    "SurfaceBinding",
    "SurfaceAction",
    "SurfaceSpec",
    "ScriptSegmentsPayload",
    "SegmentTimingsPayload",
    "SurfacePayload",
    "RenderRequestPayload",
    "CommandPayload",
    "ProjectCommand",
    "ProjectVersion",
    "ValidationResult",
    "LockEffects",
    "ApprovalEffects",
    "ChangeSet",
    "Approval",
    "ProposalStatus",
    "ProposalDiff",
    "ProjectProposal",
    "ChangeProposal",
    "Proposal",
    "WorkflowEventType",
    "WorkflowEvent",
]


# ---------------------------------------------------------------------------
# Shared constants and identifiers
# ---------------------------------------------------------------------------

SCHEMA_VERSION = "1.0"

# Reuse the exact job-id shape so project ids inherit job_store's path-escape
# safety (``is_valid_job_id`` validates ``[0-9a-f]{8}``).
PROJECT_ID_PATTERN = r"^[0-9a-f]{8}$"

_SHA256_PATTERN = r"^[0-9a-f]{64}$"

CommandOperation = Literal[
    "edit_script",
    "edit_scene",
    "edit_timing",
    "edit_visual",
    "edit_voice",
    "update_surface",
    "request_render",
    # D6 (additive extension, no existing literal touched): aspect reframing and
    # duration re-editing are TWO SEPARATE commands.  They are deliberately not
    # one "resize" operation, because shortening a video is a story re-edit that
    # needs re-approval while reframing is a safe-zone composition problem.
    "reframe_aspect",
    "reedit_duration",
]

#: D6 operation names, exported so routes and planners cannot typo them.
REFRAME_ASPECT_OPERATION = "reframe_aspect"
REEDIT_DURATION_OPERATION = "reedit_duration"

#: D6 operations that require a persisted approval before they may be committed.
#: A duration re-edit changes the approved story, so it is re-approved; a
#: reframe dispatches a fresh render, which is paid work.
APPROVAL_REQUIRED_OPERATIONS: frozenset[str] = frozenset(
    {REFRAME_ASPECT_OPERATION, REEDIT_DURATION_OPERATION}
)

#: A duration re-edit can NEVER be satisfied by changing the playback rate.
#: Nothing in the payload vocabulary for these operations can express a speed
#: factor, so the prohibition is structural rather than a convention.
SPEED_FACTOR_FORBIDDEN_OPERATIONS: frozenset[str] = frozenset({REEDIT_DURATION_OPERATION})

# Lock scopes understood by the spine.  ``voice`` is a first-class scene scope
# rather than being folded into timing: a director may freeze pronunciation
# while still adjusting a scene's media duration.
CommandScope = Literal["content", "visual", "timing", "voice"]

# operation -> the lock scope it touches (None = not lock-gated).
OPERATION_SCOPE: dict[str, Optional[str]] = {
    "edit_script": "content",
    "edit_scene": None,
    "edit_timing": "timing",
    "edit_visual": "visual",
    "edit_voice": "voice",
    "update_surface": None,
    "request_render": None,
    # D6 (additive): a reframe re-dispatches a render and is not lock-gated,
    # exactly like request_render; a duration re-edit rewrites narration, so it
    # takes the content lock.
    REFRAME_ASPECT_OPERATION: None,
    REEDIT_DURATION_OPERATION: "content",
}

# operation -> the required ``CommandPayload`` discriminator (kind).
OPERATION_PAYLOAD_KIND: dict[str, str] = {
    "edit_script": "script_segments",
    "edit_scene": "script_segments",
    "edit_timing": "segment_timings",
    "edit_visual": "script_segments",
    "edit_voice": "script_segments",
    "update_surface": "surface",
    "request_render": "render_request",
    # D6 (additive).  reframe_aspect reuses render_request because the target
    # ratio travels in RenderManifest.output.aspect_ratio and the operation
    # dispatches a paid re-render.  reedit_duration reuses script_segments
    # because shortening is expressed ONLY as a replacement set of story
    # segments -- there is no payload in this vocabulary that can carry a speed
    # factor, which is the point.
    REFRAME_ASPECT_OPERATION: "render_request",
    REEDIT_DURATION_OPERATION: "script_segments",
}

# Operations that dispatch a paid provider and therefore require a persisted
# approval record (budget_store.record_approval) before they may be applied.
PAID_OPERATIONS: frozenset[str] = frozenset({"request_render", REFRAME_ASPECT_OPERATION})


def operation_is_paid(operation: str) -> bool:
    """True when ``operation`` dispatches paid provider work."""
    return operation in PAID_OPERATIONS


def operation_requires_approval(operation: str) -> bool:
    """True when an operation needs an explicit persisted human decision.

    Paid operations always require approval. ``reedit_duration`` is not itself
    a provider call, but it changes the approved story and therefore requires a
    zero-spend story approval before the new version can be committed.
    """

    return operation in PAID_OPERATIONS or operation in APPROVAL_REQUIRED_OPERATIONS


def scope_for_operation(operation: str) -> Optional[str]:
    """Return the lock scope an operation touches, or ``None`` when ungated."""
    return OPERATION_SCOPE.get(operation)


def _strip_non_blank(value: str, field: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{field} must not be blank")
    return cleaned


# ---------------------------------------------------------------------------
# Cost / usage / tool result (document line 89)
# ---------------------------------------------------------------------------


class CostState(BaseModel):
    """A cost figure with an explicit basis. Unknown => ``amount_usd is None``.

    The basis labels mirror ``budget_store.get_budget_status`` so the spine never
    conflates an estimate with an invoice-confirmed figure. ``amount_usd`` is
    ``None`` when unknown and is **never** silently ``0``.
    """

    model_config = ConfigDict(extra="forbid")

    basis: Literal[
        "unknown", "estimated", "actual", "provider_reported", "invoice_confirmed"
    ] = "unknown"
    amount_usd: Optional[float] = Field(default=None, ge=0)
    currency: Literal["USD"] = "USD"

    @model_validator(mode="after")
    def _unknown_carries_no_amount(self) -> "CostState":
        if self.basis == "unknown" and self.amount_usd is not None:
            raise ValueError("an 'unknown' cost basis must not carry an amount_usd")
        return self


class Usage(BaseModel):
    """Provider usage counters. Any unknown counter stays ``None``."""

    model_config = ConfigDict(extra="forbid")

    prompt_tokens: Optional[int] = Field(default=None, ge=0)
    completion_tokens: Optional[int] = Field(default=None, ge=0)
    total_tokens: Optional[int] = Field(default=None, ge=0)
    unit: Optional[str] = Field(default=None, min_length=1)


class SanitizedError(BaseModel):
    """A redacted, human-safe error. Never carries credentials or raw payloads."""

    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    retryable: bool = False

    @field_validator("code", "message")
    @classmethod
    def _strip(cls, value: str) -> str:
        return value.strip() or value


class ToolResult(BaseModel):
    """Outcome of one tool/provider invocation (document line 89)."""

    model_config = ConfigDict(extra="forbid")

    tool_name: str = Field(min_length=1)
    skill_name: Optional[str] = None
    skill_version: Optional[str] = None
    outcome: Literal[
        "success", "failed", "cancelled", "retryable_error", "blocked"
    ]
    # The provider-side operation id used for idempotent reconciliation.
    provider_operation_id: Optional[str] = None
    usage: Optional[Usage] = None
    cost: CostState = Field(default_factory=CostState)
    artifacts: list[str] = Field(default_factory=list)
    retryable: bool = False
    error: Optional[SanitizedError] = None

    @model_validator(mode="after")
    def _coherent_outcome(self) -> "ToolResult":
        if self.retryable and self.outcome not in {"retryable_error", "failed", "blocked"}:
            raise ValueError("retryable=True requires a non-success outcome")
        if self.outcome == "success" and self.error is not None:
            raise ValueError("a successful ToolResult must not carry an error")
        return self


# ---------------------------------------------------------------------------
# Selection (document line 19) - resolved purely by projects/selection.py
# ---------------------------------------------------------------------------


class SceneSelection(BaseModel):
    """Select whole scenes by their stable segment ids."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["scene"] = "scene"
    scene_ids: list[str] = Field(min_length=1)

    @field_validator("scene_ids")
    @classmethod
    def _clean(cls, value: list[str]) -> list[str]:
        cleaned = [_strip_non_blank(v, "scene_ids item") for v in value]
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("scene_ids must be unique")
        return cleaned


class ObjectSelection(BaseModel):
    """Select the scenes that contain one or more named objects."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["object"] = "object"
    object_refs: list[str] = Field(min_length=1)

    @field_validator("object_refs")
    @classmethod
    def _clean(cls, value: list[str]) -> list[str]:
        cleaned = [_strip_non_blank(v, "object_refs item") for v in value]
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("object_refs must be unique")
        return cleaned


class TimeRangeSelection(BaseModel):
    """Select the scenes whose timing overlaps ``[start_seconds, end_seconds)``."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["time_range"] = "time_range"
    start_seconds: float = Field(ge=0)
    end_seconds: float = Field(ge=0)

    @model_validator(mode="after")
    def _ordered(self) -> "TimeRangeSelection":
        if self.end_seconds <= self.start_seconds:
            raise ValueError("end_seconds must be strictly greater than start_seconds")
        return self


class AllSelection(BaseModel):
    """Select every scene in the version."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["all"] = "all"


Selection = Annotated[
    Union[SceneSelection, ObjectSelection, TimeRangeSelection, AllSelection],
    Field(discriminator="kind"),
]


# ---------------------------------------------------------------------------
# Version composition blocks (document line 84)
# ---------------------------------------------------------------------------


class SegmentTiming(BaseModel):
    """Media timing for one segment. Timing is absent from ``VideoScript`` and is
    added here at the project-version layer."""

    model_config = ConfigDict(extra="forbid")

    segment_id: str = Field(min_length=1)
    start_seconds: float = Field(ge=0)
    end_seconds: float = Field(ge=0)

    @model_validator(mode="after")
    def _ordered(self) -> "SegmentTiming":
        if self.end_seconds <= self.start_seconds:
            raise ValueError("end_seconds must be strictly greater than start_seconds")
        return self


class AssetReference(BaseModel):
    """A reference to a produced asset. Bytes live in the artifact store; the
    version only carries the reference (key/uri + optional content hash)."""

    model_config = ConfigDict(extra="forbid")

    asset_id: str = Field(min_length=1)
    kind: Literal["image", "video", "audio", "font", "motion_graphic", "document"]
    uri: Optional[str] = None
    sha256: Optional[str] = Field(default=None, pattern=_SHA256_PATTERN)


class CreativeDirection(BaseModel):
    """Creative-direction references (notes + pointers), not inline media."""

    model_config = ConfigDict(extra="forbid")

    director_notes: str = ""
    treatment_refs: list[str] = Field(default_factory=list)
    style_id: Optional[str] = None
    genre: Optional[str] = None

    @field_validator("treatment_refs")
    @classmethod
    def _clean(cls, value: list[str]) -> list[str]:
        return [_strip_non_blank(v, "treatment_refs item") for v in value]


class LockState(BaseModel):
    """Which scopes of this version are locked against further edits."""

    model_config = ConfigDict(extra="forbid")

    content: bool = False
    visual: bool = False
    timing: bool = False
    voice: bool = False
    lock_id: Optional[str] = None
    locked_by: Optional[str] = None
    locked_at: Optional[str] = None


class PinnedProductionConfig(BaseModel):
    """Production inputs pinned at version time so a render is reproducible."""

    model_config = ConfigDict(extra="forbid")

    voice_provider: Optional[Literal["gemini"]] = None
    voice_actor: Optional[str] = None
    language: Optional[str] = None
    aspect_ratio: Optional[AspectRatio] = None
    style_id: Optional[str] = None
    script_model: Optional[str] = None


class OutputMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    aspect_ratio: Optional[AspectRatio] = None
    width: Optional[int] = Field(default=None, ge=1)
    height: Optional[int] = Field(default=None, ge=1)
    duration_seconds: Optional[float] = Field(default=None, ge=0)
    fps: Optional[int] = Field(default=None, ge=1)
    codec: Optional[str] = None


class RenderManifest(BaseModel):
    """Reproducibility manifest for a render (document line 91)."""

    model_config = ConfigDict(extra="forbid")

    video_spec_version: str = Field(min_length=1)
    asset_hashes: dict[str, str] = Field(default_factory=dict)
    fonts: list[str] = Field(default_factory=list)
    renderer_version: Optional[str] = None
    # skill name -> version, sourced from backend.agent.skills.version_manifest().
    skill_versions: dict[str, str] = Field(default_factory=dict)
    seeds: dict[str, int] = Field(default_factory=dict)
    output: OutputMetadata = Field(default_factory=OutputMetadata)

    @field_validator("asset_hashes")
    @classmethod
    def _hashes(cls, value: dict[str, str]) -> dict[str, str]:
        import re

        for asset_id, digest in value.items():
            if not asset_id.strip():
                raise ValueError("asset_hashes keys must be non-blank")
            if not re.fullmatch(_SHA256_PATTERN, digest):
                raise ValueError(f"asset_hashes[{asset_id!r}] must be lowercase sha256")
        return value


# ---------------------------------------------------------------------------
# SurfaceSpec - CLOSED discriminated union (document line 86)
# ---------------------------------------------------------------------------


class TextComponent(BaseModel):
    """Plain-text component. ``text`` is a bounded string; there is no field able
    to carry HTML/JS, and ``extra="forbid"`` blocks adding one."""

    model_config = ConfigDict(extra="forbid")

    component: Literal["text"] = "text"
    id: str = Field(min_length=1)
    text: str = Field(default="", max_length=500)
    role: Literal["title", "body", "caption", "label"] = "body"


class ButtonComponent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    component: Literal["button"] = "button"
    id: str = Field(min_length=1)
    label: str = Field(min_length=1, max_length=80)
    action_id: str = Field(min_length=1)


class SceneListComponent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    component: Literal["scene_list"] = "scene_list"
    id: str = Field(min_length=1)
    segment_ids: list[str] = Field(default_factory=list)


class PreviewComponent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    component: Literal["preview"] = "preview"
    id: str = Field(min_length=1)
    variant_name: Optional[str] = None


class StatusComponent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    component: Literal["status"] = "status"
    id: str = Field(min_length=1)


SurfaceComponent = Annotated[
    Union[
        TextComponent,
        ButtonComponent,
        SceneListComponent,
        PreviewComponent,
        StatusComponent,
    ],
    Field(discriminator="component"),
]

# A CLOSED set of bindable fields and data sources. No arbitrary path,
# expression, or script string can be bound.
SurfaceBindingField = Literal["text", "label", "variant_name", "segment_ids", "status"]
SurfaceBindingSource = Literal[
    "version.title",
    "version.variant_name",
    "version.language",
    "workflow.stage",
    "workflow.status",
    "budget.remaining_usd",
    "budget.paid_production_enabled",
]


class SurfaceBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    component_id: str = Field(min_length=1)
    field: SurfaceBindingField
    source: SurfaceBindingSource


class SurfaceAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action_id: str = Field(min_length=1)
    kind: Literal[
        "apply_command", "request_render", "approve_spend", "select_scene", "refresh"
    ]
    command_operation: Optional[CommandOperation] = None


class SurfaceSpec(BaseModel):
    """A closed UI surface description (document line 86).

    Implemented as a closed discriminated union of approved components plus a
    closed binding/action vocabulary and a schema version. This is deliberately
    NOT a general component-registry / binding DSL: the union is closed, every
    member forbids extra keys, and no member can carry executable markup.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: str = Field(default=SCHEMA_VERSION, min_length=1)
    components: list[SurfaceComponent] = Field(default_factory=list)
    bindings: list[SurfaceBinding] = Field(default_factory=list)
    actions: list[SurfaceAction] = Field(default_factory=list)

    @model_validator(mode="after")
    def _referential_integrity(self) -> "SurfaceSpec":
        component_ids = [c.id for c in self.components]
        if len(set(component_ids)) != len(component_ids):
            raise ValueError("component ids must be unique")
        action_ids = [a.action_id for a in self.actions]
        if len(set(action_ids)) != len(action_ids):
            raise ValueError("action ids must be unique")
        known_components = set(component_ids)
        for binding in self.bindings:
            if binding.component_id not in known_components:
                raise ValueError(
                    f"binding references unknown component {binding.component_id!r}"
                )
        known_actions = set(action_ids)
        for component in self.components:
            if component.component == "button" and component.action_id not in known_actions:
                raise ValueError(
                    f"button {component.id!r} references unknown action "
                    f"{component.action_id!r}"
                )
        return self


# ---------------------------------------------------------------------------
# Command payloads + ProjectCommand (document line 83)
# ---------------------------------------------------------------------------


class ScriptSegmentsPayload(BaseModel):
    """A replacement set of segments (reuses ``video_contract.ScriptSegment``)."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["script_segments"] = "script_segments"
    segments: list[ScriptSegment] = Field(min_length=1)


class SegmentTimingsPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["segment_timings"] = "segment_timings"
    timings: list[SegmentTiming] = Field(min_length=1)


class SurfacePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["surface"] = "surface"
    surface: SurfaceSpec


class RenderRequestPayload(BaseModel):
    """A paid render dispatch request. ``estimated_cost_usd`` is ``None`` when
    unknown (never ``0``)."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["render_request"] = "render_request"
    estimated_cost_usd: Optional[float] = Field(default=None, ge=0)
    manifest: Optional[RenderManifest] = None


CommandPayload = Annotated[
    Union[
        ScriptSegmentsPayload,
        SegmentTimingsPayload,
        SurfacePayload,
        RenderRequestPayload,
    ],
    Field(discriminator="kind"),
]


class ProjectCommand(BaseModel):
    """A single atomic, idempotent mutation request against a project spine."""

    model_config = ConfigDict(extra="forbid")

    project_id: str = Field(pattern=PROJECT_ID_PATTERN)
    base_version: int = Field(ge=0)
    actor: str = Field(min_length=1)
    operation: CommandOperation
    # Optional explicit scope; when omitted it is derived from the operation.
    scope: Optional[CommandScope] = None
    selection: Optional[Selection] = None
    payload: Optional[CommandPayload] = None
    idempotency_key: str = Field(min_length=1, max_length=128)
    requested_at: Optional[str] = None

    @field_validator("actor")
    @classmethod
    def _strip_actor(cls, value: str) -> str:
        return _strip_non_blank(value, "actor")

    @model_validator(mode="after")
    def _payload_matches_operation(self) -> "ProjectCommand":
        expected_kind = OPERATION_PAYLOAD_KIND.get(self.operation)
        if expected_kind is None:  # pragma: no cover - operation is a closed Literal
            raise ValueError(f"unsupported operation {self.operation!r}")
        if self.payload is None:
            raise ValueError(f"operation {self.operation!r} requires a payload")
        # A scene's duration is part of its editable segment contract.  Keep
        # the historic ``segment_timings`` payload for timeline edits while
        # also accepting a validated script-segment payload for direct scene
        # duration updates.
        if (
            self.operation == "edit_timing"
            and self.payload.kind in {"segment_timings", "script_segments"}
        ):
            return self
        if self.payload.kind != expected_kind:
            raise ValueError(
                f"operation {self.operation!r} requires payload kind "
                f"{expected_kind!r}, got {self.payload.kind!r}"
            )
        return self

    def effective_scope(self) -> Optional[str]:
        """The lock scope this command touches (explicit scope wins)."""
        return self.scope if self.scope is not None else scope_for_operation(self.operation)


# ---------------------------------------------------------------------------
# ProjectVersion (document line 84) - WRAPS VideoScript
# ---------------------------------------------------------------------------


class ProjectVersion(BaseModel):
    """One immutable version of a project.

    ``script`` **wraps** ``video_contract.VideoScript``; this model never
    duplicates a VideoScript field. Timing, assets, creative direction, locks,
    pinned production config and the optional render manifest / surface live here
    at the version layer.
    """

    model_config = ConfigDict(extra="forbid")

    project_id: str = Field(pattern=PROJECT_ID_PATTERN)
    version_no: int = Field(ge=1)
    parent_version: Optional[int] = None
    script: VideoScript  # wrapped, never duplicated
    locks: LockState = Field(default_factory=LockState)
    variant_name: Optional[str] = None
    pinned_production_config: PinnedProductionConfig = Field(
        default_factory=PinnedProductionConfig
    )
    creative_direction: CreativeDirection = Field(default_factory=CreativeDirection)
    segment_timings: list[SegmentTiming] = Field(default_factory=list)
    asset_references: list[AssetReference] = Field(default_factory=list)
    render_manifest: Optional[RenderManifest] = None
    surface: Optional[SurfaceSpec] = None
    # Provenance.
    actor: str = Field(min_length=1)
    created_at: str = Field(min_length=1)
    idempotency_key: Optional[str] = None
    # Fingerprint of the exact ProjectCommand that created this version.
    # Keeping it beside the idempotency key prevents a replay key from being
    # reused for a different operation or payload.
    command_fingerprint: Optional[str] = Field(default=None, pattern=_SHA256_PATTERN)
    applied_operations: list[str] = Field(default_factory=list)
    source_command_operation: Optional[CommandOperation] = None

    @field_validator("actor")
    @classmethod
    def _strip_actor(cls, value: str) -> str:
        return _strip_non_blank(value, "actor")

    @model_validator(mode="after")
    def _coherence(self) -> "ProjectVersion":
        if self.parent_version is None:
            if self.version_no != 1:
                raise ValueError("only version 1 may have a null parent_version")
        elif self.parent_version != self.version_no - 1:
            raise ValueError("parent_version must equal version_no - 1")

        segment_ids = [s.id for s in self.script.segments]
        known = set(segment_ids)
        seen: set[str] = set()
        for timing in self.segment_timings:
            if timing.segment_id not in known:
                raise ValueError(
                    f"segment_timings references unknown segment {timing.segment_id!r}"
                )
            if timing.segment_id in seen:
                raise ValueError("segment_timings must not repeat a segment_id")
            seen.add(timing.segment_id)

        asset_ids = [a.asset_id for a in self.asset_references]
        if len(set(asset_ids)) != len(asset_ids):
            raise ValueError("asset_references ids must be unique")
        return self

    def segment_ids(self) -> list[str]:
        """Stable ordered segment ids of the wrapped script."""
        return [s.id for s in self.script.segments]


# ---------------------------------------------------------------------------
# ChangeSet (document line 85)
# ---------------------------------------------------------------------------


class ValidationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    passed: bool = False
    errors: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _passed_has_no_errors(self) -> "ValidationResult":
        if self.passed and self.errors:
            raise ValueError("a passed ValidationResult must not carry errors")
        return self


class LockEffects(BaseModel):
    model_config = ConfigDict(extra="forbid")

    locks_added: list[CommandScope] = Field(default_factory=list)
    locks_released: list[CommandScope] = Field(default_factory=list)
    conflicts: list[CommandScope] = Field(default_factory=list)


class ApprovalEffects(BaseModel):
    model_config = ConfigDict(extra="forbid")

    required: bool = False
    operation: Optional[str] = None
    approved_spend_usd: Optional[float] = Field(default=None, ge=0)
    approval_id: Optional[str] = None
    decision: Optional[str] = None


class ChangeSet(BaseModel):
    """The proposed + applied effect of one command (document line 85).

    Invariant: when ``status == "applied"``, the approved diff equals the applied
    operations (``proposed_operations == applied_operations``).
    """

    model_config = ConfigDict(extra="forbid")

    project_id: str = Field(pattern=PROJECT_ID_PATTERN)
    base_version: int = Field(ge=0)
    target_version: Optional[int] = None
    status: Literal["applied", "rejected", "replayed", "rebased"] = "rejected"
    applied: bool = False
    replayed: bool = False
    proposed_operations: list[str] = Field(default_factory=list)
    applied_operations: list[str] = Field(default_factory=list)
    affected_segment_ids: list[str] = Field(default_factory=list)
    lock_effects: LockEffects = Field(default_factory=LockEffects)
    approval_effects: Optional[ApprovalEffects] = None
    estimated_spend_usd: Optional[float] = Field(default=None, ge=0)
    validation: ValidationResult = Field(default_factory=ValidationResult)
    reason: Optional[str] = None
    idempotency_key: Optional[str] = None

    @model_validator(mode="after")
    def _applied_consistency(self) -> "ChangeSet":
        if self.status == "applied":
            if not self.applied:
                raise ValueError("status='applied' requires applied=True")
            if self.target_version is None:
                raise ValueError("status='applied' requires a target_version")
            if self.proposed_operations != self.applied_operations:
                raise ValueError(
                    "approved diff must equal applied operations "
                    "(proposed_operations != applied_operations)"
                )
        if self.status == "replayed" and not self.replayed:
            raise ValueError("status='replayed' requires replayed=True")
        return self


# ---------------------------------------------------------------------------
# Approval (document line 87)
# ---------------------------------------------------------------------------


class Approval(BaseModel):
    """A spend-approval decision. Field-compatible with
    ``budget_store.record_approval`` / ``read_approvals`` records."""

    model_config = ConfigDict(extra="forbid")

    approval_id: str = Field(min_length=1)
    operation: str = Field(min_length=1)
    target_ref: Optional[str] = None
    approved_spend_usd: Optional[float] = Field(default=None, ge=0)
    decision: Literal["approved", "denied", "pending"] = "pending"
    actor: str = Field(min_length=1)
    timestamp: str = Field(min_length=1)


# ---------------------------------------------------------------------------
# Chat proposal lifecycle
# ---------------------------------------------------------------------------


ProposalStatus = Literal["proposed", "approved", "rejected", "expired", "revoked"]


class ProposalDiff(BaseModel):
    """The server-derived, reviewable effect of a proposed command.

    ``before`` and ``after`` intentionally contain only JSON-safe snapshots of
    fields touched by the command.  The authoritative command remains the
    validated :class:`ProjectCommand` persisted beside this summary.
    """

    model_config = ConfigDict(extra="forbid")

    operation: CommandOperation
    summary: str = Field(min_length=1)
    affected_fields: list[str] = Field(default_factory=list)
    before: dict[str, Any] = Field(default_factory=dict)
    after: dict[str, Any] = Field(default_factory=dict)


class ProjectProposal(BaseModel):
    """An immutable command proposal until one explicit terminal decision.

    Proposal records are separate from project versions: creating or rejecting
    one never changes the project head.  Approval metadata is filled only after
    the command has committed successfully under the same project lock.
    """

    model_config = ConfigDict(extra="forbid")

    proposal_id: str = Field(min_length=1, max_length=128)
    project_id: str = Field(pattern=PROJECT_ID_PATTERN)
    base_version: int = Field(ge=1)
    command: ProjectCommand
    diff: ProposalDiff
    affected_scopes: list[CommandScope] = Field(default_factory=list)
    affected_segment_ids: list[str] = Field(default_factory=list)
    actor: str = Field(min_length=1, max_length=80)
    status: ProposalStatus = "proposed"
    idempotency_key: str = Field(min_length=1, max_length=128)
    created_at: str = Field(min_length=1)
    updated_at: str = Field(min_length=1)
    expires_at: Optional[str] = None
    decision_actor: Optional[str] = None
    decision_at: Optional[str] = None
    target_version: Optional[int] = Field(default=None, ge=1)
    reason: Optional[str] = None

    @field_validator("actor", "proposal_id", "idempotency_key")
    @classmethod
    def _clean_identity(cls, value: str) -> str:
        return _strip_non_blank(value, "proposal identity")

    @model_validator(mode="after")
    def _coherent_identity_and_status(self) -> "ProjectProposal":
        if self.command.project_id != self.project_id:
            raise ValueError("proposal command project_id must match project_id")
        if self.command.base_version != self.base_version:
            raise ValueError("proposal command base_version must match base_version")
        if self.command.actor != self.actor:
            raise ValueError("proposal command actor must match actor")
        if self.command.idempotency_key != self.idempotency_key:
            raise ValueError("proposal command idempotency_key must match idempotency_key")
        if self.diff.operation != self.command.operation:
            raise ValueError("proposal diff operation must match command operation")
        if self.status == "approved" and self.target_version is None:
            raise ValueError("approved proposal requires target_version")
        if self.status != "approved" and self.target_version is not None:
            raise ValueError("only an approved proposal may carry target_version")
        return self


# Names used by callers in different stages of the project brief.  They are
# aliases, not duplicate schemas, so persisted JSON has one canonical shape.
ChangeProposal = ProjectProposal
Proposal = ProjectProposal


# ---------------------------------------------------------------------------
# WorkflowEvent (document line 88)
# ---------------------------------------------------------------------------

WorkflowEventType = Literal[
    "project_created",
    "version_appended",
    "command_rejected",
    "command_rebased",
    "command_replayed",
    "render_requested",
    "stage_changed",
    "approval_recorded",
]


class WorkflowEvent(BaseModel):
    """An append-only, monotonic workflow event (document line 88)."""

    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(min_length=1)
    project_id: str = Field(pattern=PROJECT_ID_PATTERN)
    version_no: Optional[int] = None
    run_id: Optional[str] = None
    job_id: Optional[str] = None
    event_type: WorkflowEventType
    stage: Optional[str] = None
    status: Literal[
        "pending", "running", "completed", "failed", "rejected", "rebased",
        "needs_attention",
    ] = "pending"
    sequence: int = Field(ge=1)
    progress_source: Literal["actual", "estimated"] = "actual"
    artifact_refs: list[str] = Field(default_factory=list)
    required_action: Optional[str] = None
    actor: Optional[str] = None
    idempotency_key: Optional[str] = None
    timestamp: str = Field(min_length=1)
