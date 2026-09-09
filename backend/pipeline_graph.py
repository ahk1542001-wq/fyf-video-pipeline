"""Dependency-aware recompute planning for the FYF pipeline (Stage D3).

This module promotes the per-segment content-hash cache into an EXPLICIT node
graph — ``script -> visual -> voice -> render -> qa`` per scene — so that a
single-scene text edit can be shown to recompute only that scene's downstream
nodes while untouched scenes report ``cache_hits``.

DELIBERATE SCOPE LIMIT (Leader decision): this is NOT a general DAG scheduler.
It plans invalidation and reports it; execution stays exactly where it already
is (``backend.pipeline`` and ``backend.segment_render_cache``), which keeps the
existing fingerprint invalidation, cancellation checkpoints and lock scopes as
the single source of truth.  Nothing here starts a thread, calls a provider or
writes an artifact.

Two boundary systems are honoured rather than reimplemented:

* ``backend.cancellation`` — planning passes through a cooperative checkpoint,
  so a cancelled job stops before it is handed a recompute plan.
* ``backend.lock_store`` — a node whose stage maps to a locked granular scope is
  reported as ``blocked`` instead of being quietly scheduled.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from backend import cancellation
from backend.lock_store import GRANULAR_LOCK_SCOPES, locked_scopes


STAGES: tuple[str, ...] = ("script", "visual", "voice", "render", "qa")

#: stage -> granular lock scope (None means the stage is not lock-gated).
STAGE_LOCK_SCOPE: dict[str, str | None] = {
    "script": "content",
    "visual": "visual",
    "voice": "voice",
    "render": None,
    "qa": None,
}

#: Version of the hashing rules.  Changing any rule below MUST bump this, so a
#: plan computed under older rules is never compared against a newer one.
GRAPH_CONTRACT_VERSION = 1

#: Bumped when the QA policy changes, so QA re-runs without a render re-run.
QA_POLICY_VERSION = "1"

_MASTER = "master"


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(*parts: Any) -> str:
    return hashlib.sha256(_canonical_json({"v": GRAPH_CONTRACT_VERSION, "parts": list(parts)}).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class PipelineNode:
    """One unit of recomputation with an explicit content hash and edges."""

    node_id: str
    stage: str
    segment_id: str | None
    content_hash: str
    depends_on: tuple[str, ...] = ()
    lock_scope: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "stage": self.stage,
            "segment_id": self.segment_id,
            "content_hash": self.content_hash,
            "depends_on": list(self.depends_on),
            "lock_scope": self.lock_scope,
        }


@dataclass(frozen=True)
class PipelineGraph:
    """The explicit ``script -> visual -> voice -> render -> qa`` node graph."""

    nodes: dict[str, PipelineNode] = field(default_factory=dict)
    segment_ids: tuple[str, ...] = ()
    per_segment_voice: bool = False

    def __len__(self) -> int:
        return len(self.nodes)

    def stage_nodes(self, stage: str) -> list[PipelineNode]:
        return [node for node in self.nodes.values() if node.stage == stage]

    def topological_order(self) -> list[str]:
        """Stage-ordered node ids (the graph is layered, so stage order is a
        valid topological order and no general scheduler is needed)."""

        order = {stage: index for index, stage in enumerate(STAGES)}
        return sorted(
            self.nodes,
            key=lambda node_id: (order[self.nodes[node_id].stage], self.nodes[node_id].segment_id or "", node_id),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "graph_contract_version": GRAPH_CONTRACT_VERSION,
            "per_segment_voice": self.per_segment_voice,
            "segment_ids": list(self.segment_ids),
            "nodes": [self.nodes[node_id].as_dict() for node_id in self.topological_order()],
        }


@dataclass(frozen=True)
class RecomputePlan:
    """What must be recomputed, what is a cache hit, and why."""

    dirty: list[str]
    cache_hits: list[str]
    blocked: list[str]
    reasons: dict[str, str]
    dirty_by_stage: dict[str, list[str]]
    cache_hits_by_stage: dict[str, list[str]]
    graph: PipelineGraph

    def as_dict(self) -> dict[str, Any]:
        return {
            "graph_contract_version": GRAPH_CONTRACT_VERSION,
            "dirty": list(self.dirty),
            "cache_hits": list(self.cache_hits),
            "blocked": list(self.blocked),
            "reasons": dict(self.reasons),
            "dirty_by_stage": {stage: list(ids) for stage, ids in self.dirty_by_stage.items()},
            "cache_hits_by_stage": {stage: list(ids) for stage, ids in self.cache_hits_by_stage.items()},
            "nodes": len(self.graph),
        }


def _segment_identity(segment: Mapping[str, Any]) -> str:
    for key in ("id", "segment_id"):
        value = segment.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    raise ValueError("every script segment requires an id")


def _narrative_payload(segment: Mapping[str, Any]) -> dict[str, Any]:
    """Only the words that get spoken or read — never the visual or the timing."""

    keys = (
        "text",
        "caption",
        "narration",
        "voiceover",
        "voice",
        "screen_text",
        "numbers",
        "claims",
        "evidence_claims",
        "label",
    )
    return {key: segment.get(key) for key in keys if key in segment}


def _visual_payload(segment: Mapping[str, Any]) -> dict[str, Any]:
    keys = ("visual", "visual_spec", "shot", "shots", "motion", "treatment", "b_roll")
    return {key: segment.get(key) for key in keys if key in segment}


def _timing_payload(segment: Mapping[str, Any], timings: Mapping[str, Any]) -> dict[str, Any]:
    keys = ("startFrame", "endFrame", "start_frame", "end_frame", "duration_seconds", "pause_after")
    local = {key: segment.get(key) for key in keys if key in segment}
    declared = timings.get(_segment_identity(segment))
    return {"local": local, "declared": declared}


def build_pipeline_graph(
    script: Mapping[str, Any],
    *,
    segment_timings: Mapping[str, Any] | None = None,
    observed: Mapping[str, str] | None = None,
    per_segment_voice: bool = True,
    renderer_identity: str | None = None,
    voice_identity: str | None = None,
    visual_policy: str | None = None,
) -> PipelineGraph:
    """Hash a script into the explicit five-stage node graph.

    ``observed`` may carry real artifact digests keyed by node id (for example
    ``{"voice:s2": "<sha256 of the recorded segment wav>"}``).  Observed digests
    are folded into the node hash so that replacing an artifact on disk
    invalidates the node even when the script text is unchanged.  Nothing is
    invented: absent keys simply contribute ``None``.
    """

    if not isinstance(script, Mapping):
        raise ValueError("script must be an object")
    segments = script.get("segments")
    if not isinstance(segments, list) or not segments:
        raise ValueError("script.segments must be a non-empty list")
    timings = dict(segment_timings or {})
    seen_observed = dict(observed or {})

    script_identity = {
        key: script.get(key)
        for key in ("language", "title", "voice_actor", "style_id", "story_mode", "aspect_ratio")
        if key in script
    }
    voice_config = {"identity": voice_identity, "config": script_identity}

    nodes: dict[str, PipelineNode] = {}
    normalized_segments: list[Mapping[str, Any]] = []
    segment_ids: list[str] = []
    for segment in segments:
        if not isinstance(segment, Mapping):
            raise ValueError("script segment must be an object")
        segment_id = _segment_identity(segment)
        if segment_id in segment_ids:
            raise ValueError(f"duplicate segment id: {segment_id}")
        segment_ids.append(segment_id)
        normalized_segments.append(segment)

    # Materialise every script node first. A single-track voice node depends on
    # the narration of every scene, including scenes that appear later in the
    # document, so building nodes in one per-scene pass would reference nodes
    # that do not exist yet.
    for segment, segment_id in zip(normalized_segments, segment_ids):
        script_node = f"script:{segment_id}"
        nodes[script_node] = PipelineNode(
            node_id=script_node,
            stage="script",
            segment_id=segment_id,
            content_hash=_digest(_narrative_payload(segment), script_identity.get("language"),
                                 seen_observed.get(script_node)),
            depends_on=(),
            lock_scope=STAGE_LOCK_SCOPE["script"],
        )

    all_script_dependencies = tuple(f"script:{sid}" for sid in segment_ids)
    for segment, segment_id in zip(normalized_segments, segment_ids):
        script_node = f"script:{segment_id}"
        visual_node = f"visual:{segment_id}"
        nodes[visual_node] = PipelineNode(
            node_id=visual_node,
            stage="visual",
            segment_id=segment_id,
            content_hash=_digest(nodes[script_node].content_hash, _visual_payload(segment),
                                 visual_policy, seen_observed.get(visual_node)),
            depends_on=(script_node,),
            lock_scope=STAGE_LOCK_SCOPE["visual"],
        )

        timing = _timing_payload(segment, timings)
        voice_dependencies: tuple[str, ...]
        if per_segment_voice:
            voice_dependencies = (script_node,)
        else:
            # One synthesised track for the whole script: every scene's voice
            # node depends on every scene's words.  Modelling that honestly is
            # what stops the planner from promising a cheap partial recompute
            # the pipeline cannot actually deliver.
            voice_dependencies = all_script_dependencies
        voice_node = f"voice:{segment_id}"
        nodes[voice_node] = PipelineNode(
            node_id=voice_node,
            stage="voice",
            segment_id=segment_id,
            content_hash=_digest(
                [nodes[dependency].content_hash for dependency in voice_dependencies],
                timing,
                voice_config,
                seen_observed.get(voice_node),
            ),
            depends_on=voice_dependencies,
            lock_scope=STAGE_LOCK_SCOPE["voice"],
        )

        render_node = f"render:{segment_id}"
        nodes[render_node] = PipelineNode(
            node_id=render_node,
            stage="render",
            segment_id=segment_id,
            content_hash=_digest(
                nodes[visual_node].content_hash,
                nodes[voice_node].content_hash,
                renderer_identity,
                seen_observed.get(render_node),
            ),
            depends_on=(visual_node, voice_node),
            lock_scope=STAGE_LOCK_SCOPE["render"],
        )

        qa_node = f"qa:{segment_id}"
        nodes[qa_node] = PipelineNode(
            node_id=qa_node,
            stage="qa",
            segment_id=segment_id,
            content_hash=_digest(nodes[render_node].content_hash, QA_POLICY_VERSION, seen_observed.get(qa_node)),
            depends_on=(render_node,),
            lock_scope=STAGE_LOCK_SCOPE["qa"],
        )

    return PipelineGraph(
        nodes=nodes,
        segment_ids=tuple(segment_ids),
        per_segment_voice=per_segment_voice,
    )


def plan_recompute(
    before: PipelineGraph,
    after: PipelineGraph,
    *,
    job_id: str | None = None,
    job_dir: str | Path | None = None,
    locks_root: str | Path | None = None,
    project_id: str | None = None,
    force_segment_ids: Sequence[str] = (),
    scene_locks: Mapping[str, Sequence[str]] | None = None,
) -> RecomputePlan:
    """Diff two graphs into dirty nodes, cache hits and lock-blocked nodes.

    A node is dirty when its own content hash changed OR when any transitive
    dependency is dirty.  Because every downstream hash already folds in its
    upstream hashes, the two agree; the transitive pass exists so the plan can
    state a *reason* per node instead of only a hash difference.
    """

    if job_id is not None:
        # Cooperative cancellation boundary: a cancelled job never receives a
        # recompute plan, matching the boundary the render stage already uses.
        cancellation.checkpoint(
            job_id,
            job_dir=Path(job_dir) if job_dir is not None else None,
            boundary="pipeline_graph_plan",
        )

    locked: set[str] = set()
    if locks_root is not None and project_id is not None:
        for scope in locked_scopes(Path(locks_root), project_id):
            if scope in GRANULAR_LOCK_SCOPES:
                locked.add(scope)

    reasons: dict[str, str] = {}
    dirty: set[str] = set()
    order = after.topological_order()
    forced_segments = {str(segment_id) for segment_id in force_segment_ids}

    for node_id in order:
        node = after.nodes[node_id]
        previous = before.nodes.get(node_id)
        if node.stage == "script" and node.segment_id in forced_segments:
            dirty.add(node_id)
            reasons[node_id] = "selected scene regeneration requested"
            continue
        if previous is None:
            dirty.add(node_id)
            reasons[node_id] = "new node (no prior content hash)"
            continue
        if previous.content_hash != node.content_hash:
            dirty.add(node_id)
            upstream = [dependency for dependency in node.depends_on if dependency in dirty]
            if upstream:
                reasons[node_id] = f"{node.stage} inputs changed; upstream dirty: {', '.join(sorted(upstream))}"
            else:
                reasons[node_id] = f"{node.stage} content hash changed"
            continue
        upstream = [dependency for dependency in node.depends_on if dependency in dirty]
        if upstream:
            dirty.add(node_id)
            reasons[node_id] = f"upstream dirty: {', '.join(sorted(upstream))}"

    removed = [node_id for node_id in before.nodes if node_id not in after.nodes]
    for node_id in removed:
        reasons[node_id] = "node removed from the graph"

    blocked = sorted(
        node_id
        for node_id in dirty
        if node_id in after.nodes
        and (
            after.nodes[node_id].lock_scope in locked
            or (
                after.nodes[node_id].segment_id is not None
                and after.nodes[node_id].lock_scope is not None
                and after.nodes[node_id].segment_id in (scene_locks or {})
                and after.nodes[node_id].lock_scope
                in set(scene_locks.get(after.nodes[node_id].segment_id, ()))
            )
        )
    )

    def by_stage(node_ids: Sequence[str]) -> dict[str, list[str]]:
        grouped: dict[str, list[str]] = {stage: [] for stage in STAGES}
        for node_id in node_ids:
            node = after.nodes.get(node_id)
            if node is not None:
                grouped[node.stage].append(node_id)
        return {stage: sorted(ids) for stage, ids in grouped.items()}

    dirty_ids = sorted(dirty)
    cache_hit_ids = [node_id for node_id in order if node_id not in dirty]

    return RecomputePlan(
        dirty=dirty_ids,
        cache_hits=cache_hit_ids,
        blocked=blocked,
        reasons=reasons,
        dirty_by_stage=by_stage(dirty_ids),
        cache_hits_by_stage=by_stage(cache_hit_ids),
        graph=after,
    )


def plan_recompute_for_edit(
    script: Mapping[str, Any],
    *,
    edited_segment_ids: Sequence[str],
    previous_graph: PipelineGraph,
    edited_script: Mapping[str, Any] | None = None,
    **kwargs: Any,
) -> RecomputePlan:
    """Convenience wrapper: build the post-edit graph and diff it.

    ``edited_script`` defaults to ``script``; pass both when the caller already
    holds the pre-edit and post-edit documents.
    """

    after = build_pipeline_graph(edited_script if edited_script is not None else script, **kwargs)
    for segment_id in edited_segment_ids:
        if f"script:{segment_id}" not in after.nodes:
            raise ValueError(f"edited segment is not in the graph: {segment_id}")
    return plan_recompute(
        previous_graph,
        after,
    )


def plan_recompute_for_regeneration(
    script: Mapping[str, Any],
    *,
    selected_segment_ids: Sequence[str],
    previous_graph: PipelineGraph | None = None,
    segment_timings: Mapping[str, Any] | None = None,
    scene_locks: Mapping[str, Sequence[str]] | None = None,
    locks_root: str | Path | None = None,
    project_id: str | None = None,
    **kwargs: Any,
) -> RecomputePlan:
    """Plan a selected-scene regeneration without changing project state.

    Regeneration deliberately forces the selected scene's script node dirty
    even when its source hash is unchanged. The ordinary graph traversal then
    marks only that scene's descendants (or the honest shared voice descendants
    when ``per_segment_voice=False``). The caller supplies the current version's
    graph when it already has one; otherwise an identical pre-edit graph is
    built from the same canonical script.
    """

    selected = tuple(dict.fromkeys(str(scene_id) for scene_id in selected_segment_ids))
    if not selected:
        raise ValueError("selected_segment_ids must not be empty")
    after = build_pipeline_graph(script, segment_timings=segment_timings, **kwargs)
    missing = [scene_id for scene_id in selected if f"script:{scene_id}" not in after.nodes]
    if missing:
        raise ValueError(f"selected segment is not in the graph: {missing[0]}")
    before = previous_graph or build_pipeline_graph(
        script,
        segment_timings=segment_timings,
        **kwargs,
    )
    return plan_recompute(
        before,
        after,
        locks_root=locks_root,
        project_id=project_id,
        force_segment_ids=selected,
        scene_locks=scene_locks,
    )
