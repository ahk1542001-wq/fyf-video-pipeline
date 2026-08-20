"""Vertex-owned generation and verification of per-job visual evidence assets."""

import copy
from contextvars import ContextVar
import json
import hashlib
import logging
import os
import re
import time
from pathlib import Path
from typing import Optional

from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from video_contract import EvidenceClaim, EvidenceShot, MotionGraphicSpec, VideoScript, VisualTreatment
from vertex_model_routing import model_for
from backend.creative_quality import failed_scene_ids, rebalance_creative_rhythm
from backend.director_context import DirectorPolicy, build_director_context
from backend.vertex_client import vertex_client_kwargs
from backend.vertex_telemetry import telemetry_retry_attempt, track_client
from backend.vertex_thinking import generation_config_for

DEFAULT_LOCATION = "global"
MAX_GENERATION_ATTEMPTS = 2
QUOTA_RETRY_ATTEMPTS = 4
DEFAULT_VERTEX_RETRY_BASE_SECONDS = 10.0
DEFAULT_VERTEX_RETRY_MAX_SECONDS = 60.0
QUALITY_ROUTE_RETRY_ATTEMPTS = 2
FINAL_REPAIR_PLAN_ATTEMPTS = 4
VIDEO_POLL_SECONDS = 10
VIDEO_TIMEOUT_SECONDS = 420
VISUAL_EVIDENCE_CONTRACT_VERSION = 2

logger = logging.getLogger(__name__)

_quota_retry_had_transient = ContextVar("fyf_quota_retry_had_transient", default=False)

MYANMAR_DIGITS = str.maketrans("၀၁၂၃၄၅၆၇၈၉", "0123456789")

FYF_STYLE = """
Premium handcrafted cut-paper diorama for a vertical 9:16 FYF educational video.
Warm ivory paper, deep olive structures, forest green accents, restrained coral only
for warnings, tactile paper fibers, directional studio light, one focal action, clean
upper negative space for Burmese captions. No generated text, letters, numbers, logos,
watermarks, UI cards, split screens, or photorealism.
""".strip()


class EvidenceVerification(BaseModel):
    model_config = ConfigDict(extra="forbid")
    passed: bool
    proved_claim_ids: list[str] = Field(default_factory=list)
    observed_values: list[str] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)


class MotionRepair(BaseModel):
    model_config = ConfigDict(extra="forbid")
    caption: str = Field(min_length=1)
    motion_spec: MotionGraphicSpec


class FinalVisualRepairPlan(BaseModel):
    """Brand-aware replacement plan selected from the existing visual palette."""

    model_config = ConfigDict(extra="forbid")
    media_type: str = Field(pattern=r"^(generated_image|generated_video|motion_graphic)$")
    screen_text: list[str] = Field(min_length=1, max_length=2)
    caption: str = Field(min_length=1)
    prompt: str = Field(min_length=1)
    motion_preset: str = Field(pattern=r"^(slow_push|pan_left|pan_right|drift|static)$")
    transition: str = Field(pattern=r"^(cut|crossfade|push|wipe)$")
    composition: str = Field(pattern=r"^(full_bleed|focal_center|split_stage)$")
    mascot_presence: str = Field(pattern=r"^(none|reaction|explain)$")
    motion_spec: MotionGraphicSpec | None = None


class RelationModeDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    relation_mode: str = Field(pattern=r"^(directional|bidirectional|non_replacement)$")


class TreatmentBatchItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    segment_id: str = Field(min_length=1)
    shot_id: str = Field(min_length=1)
    treatment: VisualTreatment


class TreatmentBatchResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[TreatmentBatchItem] = Field(min_length=1, max_length=8)


def _director_batch_size() -> int:
    try:
        batch_size = int(os.getenv("FYF_DIRECTOR_BATCH_SIZE", "5"))
    except ValueError as exc:
        raise ValueError("FYF_DIRECTOR_BATCH_SIZE must be an integer between 1 and 8") from exc
    if not 1 <= batch_size <= 8:
        raise ValueError("FYF_DIRECTOR_BATCH_SIZE must be between 1 and 8")
    return batch_size


def _motion_repair_attempts() -> int:
    try:
        attempts = int(os.getenv("FYF_MOTION_REPAIR_ATTEMPTS", "4"))
    except ValueError as exc:
        raise ValueError("FYF_MOTION_REPAIR_ATTEMPTS must be an integer between 1 and 6") from exc
    if not 1 <= attempts <= 6:
        raise ValueError("FYF_MOTION_REPAIR_ATTEMPTS must be between 1 and 6")
    return attempts


def _shot_identity(segment: dict, shot: dict) -> str:
    return f"{segment['id']}/{shot['shot_id']}"


def _director_shot_identities(script: dict, *, treated_only: bool = False) -> list[str]:
    identities = []
    for segment in script.get("segments", []):
        for shot in (segment.get("visual") or {}).get("evidence_shots") or []:
            if not treated_only or shot.get("treatment"):
                identities.append(_shot_identity(segment, shot))
    return identities


def _is_transient_vertex_error(error: BaseException) -> bool:
    """Recognize provider availability failures eligible for optional-layer fallback."""
    code = getattr(error, "code", None) or getattr(error, "status_code", None)
    if code in {429, 500, 502, 503, 504}:
        return True
    text = str(error).upper()
    return any(marker in text for marker in ("429", "RESOURCE_EXHAUSTED", "DEADLINE_EXCEEDED"))


def _deterministic_treatment_fallback(
    segment: dict,
    shot: dict,
    sequence_index: int,
) -> dict:
    """Build a brand-safe optional treatment without changing locked evidence."""
    visual = segment.get("visual") or {}
    claims = visual.get("evidence_claims") or []
    claim_text = next(
        (
            str(claim.get("statement", "")).strip()
            for claim in claims
            if isinstance(claim, dict) and str(claim.get("statement", "")).strip()
        ),
        "verified evidence",
    )
    focal_object = str(shot.get("caption") or claim_text).strip()[:120] or "verified evidence"
    motion_spec = shot.get("motion_spec") or {}
    layout = str(motion_spec.get("layout", ""))
    treatment_by_layout = {
        "count": ("editorial_data", "interface", "data"),
        "comparison": ("comparison_transform", "object", "data"),
        "sequence": ("object_action", "object", "caption"),
        "relationship": ("motion_diagram", "diagram", "label"),
        "directional_branch": ("ui_proof", "interface", "label"),
        "concept": ("object_action", "object", "caption"),
    }
    if shot.get("media_type") == "motion_graphic" and layout in treatment_by_layout:
        treatment_type, motion_family, text_mode = treatment_by_layout[layout]
    else:
        treatment_type = "story_scene" if sequence_index % 2 == 0 else "object_action"
        motion_family = "camera" if treatment_type == "story_scene" else "object"
        text_mode = "caption"
    return VisualTreatment.model_validate({
        "treatment_type": treatment_type,
        "focal_object": focal_object,
        "action": "reveals the locked evidence",
        "change": "the verified meaning becomes visible",
        "visual_world": "FYF paper evidence diorama",
        "motion_family": motion_family,
        "text_mode": text_mode,
        "attention_reset": sequence_index % 3 == 0,
        "director_reason": "Deterministic fallback preserves locked evidence during provider recovery",
    }).model_dump(mode="json")


def _write_director_checkpoint(
    path: Path,
    *,
    fingerprint: str,
    policy: DirectorPolicy,
    model_route: str,
    batch_size: int,
    total_shot_count: int,
    completed_batch_count: int,
    complete: bool,
    script: dict,
) -> None:
    payload = {
        "input_fingerprint": fingerprint,
        "policy_version": policy.version,
        "model_route": model_route,
        "batch_size": batch_size,
        "total_shot_count": total_shot_count,
        "completed_shot_ids": _director_shot_identities(script, treated_only=True),
        "completed_batch_count": completed_batch_count,
        "complete": complete,
        "script": script,
    }
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def _validate_treatment_batch(
    response_text: str,
    expected: list[tuple[str, str]],
) -> tuple[dict[tuple[str, str], VisualTreatment], dict[tuple[str, str], str]]:
    expected_set = set(expected)
    try:
        payload = json.loads(response_text)
    except json.JSONDecodeError as exc:
        return {}, {identity: f"invalid JSON: {exc}" for identity in expected}

    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        if len(expected) != 1:
            return {}, {identity: "response must contain an items list" for identity in expected}
        try:
            return {expected[0]: VisualTreatment.model_validate(payload)}, {}
        except Exception as exc:
            return {}, {expected[0]: str(exc)}

    accepted: dict[tuple[str, str], VisualTreatment] = {}
    errors: dict[tuple[str, str], str] = {}
    seen: set[tuple[str, str]] = set()
    unexpected: list[tuple[str, str]] = []
    for raw_item in payload["items"]:
        if not isinstance(raw_item, dict):
            continue
        identity = (str(raw_item.get("segment_id", "")), str(raw_item.get("shot_id", "")))
        if identity not in expected_set:
            unexpected.append(identity)
            continue
        if identity in seen:
            errors[identity] = "duplicate treatment identity"
            accepted.pop(identity, None)
            continue
        seen.add(identity)
        try:
            item = TreatmentBatchItem.model_validate(raw_item)
            accepted[identity] = item.treatment
        except Exception as exc:
            errors[identity] = str(exc)

    if unexpected:
        missing = sorted(expected_set - seen)
        raise ValueError(
            f"unexpected treatment identities; missing={missing}; unexpected={sorted(unexpected)}"
        )
    for identity in expected:
        if identity not in seen:
            errors[identity] = "missing treatment identity"
    return accepted, errors


def repair_creative_failures(script_data: dict, creative_report: dict, job_dir: str) -> dict:
    """Re-plan treatments only for scenes failed by the creative-quality audit."""
    script = VideoScript.model_validate(script_data).model_dump(mode="json")
    original = copy.deepcopy(script)
    failed = set(failed_scene_ids(creative_report))
    if not failed:
        return script

    def evidence_claims_snapshot(segment: dict) -> tuple:
        visual = segment.get("visual") or {}
        return (
            segment.get("id"),
            segment.get("text"),
            tuple(tuple(sorted(claim.items())) for claim in visual.get("evidence_claims") or []),
        )

    original_claims = [evidence_claims_snapshot(segment) for segment in original["segments"]]
    structural_codes = {
        "TREATMENT_RUN_REPEATED",
        "CENTER_CARD_SATURATION",
        "MOTION_DIAGRAM_SATURATION",
        "MASCOT_CADENCE_REPEATED",
        "TRANSITION_RUN_REPEATED",
    }
    reported_codes = set(creative_report.get("failure_codes") or [])
    if reported_codes and reported_codes <= structural_codes:
        repaired = VideoScript.model_validate(
            rebalance_creative_rhythm(script, scene_ids=failed)
        ).model_dump(mode="json")
        if [evidence_claims_snapshot(segment) for segment in repaired["segments"]] != original_claims:
            raise RuntimeError("Creative rhythm repair modified locked segment evidence claims")
        return repaired

    for segment in script["segments"]:
        if str(segment.get("id")) not in failed:
            continue
        for shot in (segment.get("visual") or {}).get("evidence_shots") or []:
            shot["treatment"] = None

    repaired = VideoScript.model_validate(plan_visual_treatments(script, job_dir)).model_dump(mode="json")
    repaired = VideoScript.model_validate(
        rebalance_creative_rhythm(repaired, scene_ids=failed)
    ).model_dump(mode="json")
    original_by_id = {str(segment.get("id")): segment for segment in original["segments"]}
    for segment in repaired["segments"]:
        segment_id = str(segment.get("id"))
        if segment_id not in failed and segment != original_by_id.get(segment_id):
            raise RuntimeError(f"Creative repair modified unrelated segment {segment_id}")
    if [evidence_claims_snapshot(segment) for segment in repaired["segments"]] != original_claims:
        raise RuntimeError("Creative repair modified locked segment evidence claims")
    return repaired


def plan_visual_treatments(script_data: dict, job_dir: str, policy: Optional[DirectorPolicy] = None) -> dict:
    """Assign structural shot treatments to evidence without modifying segments or locked facts."""
    policy = policy or DirectorPolicy()
    original = VideoScript.model_validate(script_data).model_dump(mode="json")
    script = original
    client = None
    root = Path(job_dir)

    fingerprint = _input_fingerprint(original) + f"-{policy.version}"
    checkpoint = root / "director_treatment_checkpoint.json"
    model_route = model_for("visual_direction")
    batch_size = _director_batch_size()
    expected_identities = _director_shot_identities(original)
    completed_batch_count = 0

    if checkpoint.is_file():
        try:
            saved = json.loads(checkpoint.read_text(encoding="utf-8"))
            if saved.get("input_fingerprint") == fingerprint:
                saved_script = VideoScript.model_validate(saved["script"]).model_dump(mode="json")
                saved_route = saved.get("model_route")
                saved_treated = _director_shot_identities(saved_script, treated_only=True)
                route_matches = saved_route == model_route
                legacy_partial = saved_route is None and len(saved_treated) < len(expected_identities)
                if route_matches or legacy_partial:
                    script = saved_script
                    completed_batch_count = int(saved.get("completed_batch_count", 0))
                if (
                    route_matches
                    and saved.get("complete") is True
                    and saved.get("completed_shot_ids") == expected_identities
                    and saved_treated == expected_identities
                ):
                    return script
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            pass

    untreated: list[tuple[int, dict, dict]] = []
    for segment_index, segment in enumerate(script["segments"]):
        for shot in (segment.get("visual") or {}).get("evidence_shots") or []:
            if not shot.get("treatment"):
                untreated.append((segment_index, segment, shot))

    for batch_start in range(0, len(untreated), batch_size):
        batch = untreated[batch_start:batch_start + batch_size]
        expected = [(str(segment["id"]), str(shot["shot_id"])) for _, segment, shot in batch]
        pending = {
            (str(segment["id"]), str(shot["shot_id"])): (segment_index, segment, shot)
            for segment_index, segment, shot in batch
        }
        validation_errors: dict[tuple[str, str], str] = {}
        batch_had_transient = False
        for attempt in range(MAX_GENERATION_ATTEMPTS):
            locked = [
                {
                    "segment": segment,
                    "shot_id": shot["shot_id"],
                    "director_context": build_director_context(
                        script["segments"], segment_index, policy
                    ).model_dump(mode="json"),
                }
                for segment_index, segment, shot in pending.values()
            ]
            pending_expected = list(pending)
            try:
                client = client or _client()
                response = _quota_retry(lambda: client.models.generate_content(
                    model=model_route,
                    contents=(
                        "Act as the FYF visual director. Generate one keyed structural treatment "
                        "for every locked shot. Do NOT change segment ids, shot ids, screen text, "
                        "or locked evidence claims. Return every requested identity exactly once.\n"
                        f"Locked shots and Director Context: {json.dumps(locked, ensure_ascii=False)}\n"
                        f"Validation errors: {json.dumps({f'{key[0]}/{key[1]}': value for key, value in validation_errors.items()}, ensure_ascii=False)}"
                    ),
                    config=generation_config_for("treatment",
                        response_mime_type="application/json",
                        response_json_schema=TreatmentBatchResponse.model_json_schema(),
                    ),
                ), label=f"director treatment batch {batch_start // batch_size + 1}")
                batch_had_transient = batch_had_transient or _quota_retry_had_transient.get()
                accepted, validation_errors = _validate_treatment_batch(
                    response.text or "", pending_expected
                )
                for key, treatment in accepted.items():
                    _, _, shot = pending[key]
                    shot["treatment"] = treatment.model_dump(mode="json")
                    pending.pop(key)

                if not pending:
                    break

                _write_director_checkpoint(
                    checkpoint,
                    fingerprint=fingerprint,
                    policy=policy,
                    model_route=model_route,
                    batch_size=batch_size,
                    total_shot_count=len(expected_identities),
                    completed_batch_count=completed_batch_count,
                    complete=False,
                    script=script,
                )
                validation_errors = {
                    key: validation_errors.get(key, "missing treatment identity")
                    for key in pending
                }
            except Exception as exc:
                batch_had_transient = batch_had_transient or _quota_retry_had_transient.get()
                if attempt + 1 == MAX_GENERATION_ATTEMPTS:
                    if batch_had_transient or _is_transient_vertex_error(exc):
                        logger.warning(
                            "Using deterministic treatment fallback for %s after transient Vertex error: %s",
                            ",".join(f"{segment_id}/{shot_id}" for segment_id, shot_id in pending),
                            type(exc).__name__,
                        )
                        for offset, (_, segment, shot) in enumerate(pending.values()):
                            shot["treatment"] = _deterministic_treatment_fallback(
                                segment,
                                shot,
                                batch_start + offset,
                            )
                        pending.clear()
                        validation_errors = {}
                        break
                    identities = ",".join(f"{segment_id}/{shot_id}" for segment_id, shot_id in expected)
                    raise RuntimeError(
                        f"Failed to plan visual treatment batch {identities}: {exc}"
                    ) from exc
                validation_errors = {key: str(exc) for key in pending}

            if attempt + 1 == MAX_GENERATION_ATTEMPTS and pending:
                if batch_had_transient:
                    logger.warning(
                        "Using deterministic treatment fallback for %s after a transient Vertex response",
                        ",".join(f"{segment_id}/{shot_id}" for segment_id, shot_id in pending),
                    )
                    for offset, (_, segment, shot) in enumerate(pending.values()):
                        shot["treatment"] = _deterministic_treatment_fallback(
                            segment,
                            shot,
                            batch_start + offset,
                        )
                    pending.clear()
                    continue
                _write_director_checkpoint(
                    checkpoint,
                    fingerprint=fingerprint,
                    policy=policy,
                    model_route=model_route,
                    batch_size=batch_size,
                    total_shot_count=len(expected_identities),
                    completed_batch_count=completed_batch_count,
                    complete=False,
                    script=script,
                )
                failed = ",".join(f"{segment_id}/{shot_id}" for segment_id, shot_id in pending)
                raise RuntimeError(f"Failed to plan visual treatment items: {failed}")

        completed_batch_count += 1
        _write_director_checkpoint(
            checkpoint,
            fingerprint=fingerprint,
            policy=policy,
            model_route=model_route,
            batch_size=batch_size,
            total_shot_count=len(expected_identities),
            completed_batch_count=completed_batch_count,
            complete=False,
            script=script,
        )

    completed_identities = _director_shot_identities(script, treated_only=True)
    if completed_identities != expected_identities:
        raise RuntimeError(
            "Visual treatment checkpoint is incomplete after planning; "
            f"completed={completed_identities}; expected={expected_identities}"
        )
    _write_director_checkpoint(
        checkpoint,
        fingerprint=fingerprint,
        policy=policy,
        model_route=model_route,
        batch_size=batch_size,
        total_shot_count=len(expected_identities),
        completed_batch_count=completed_batch_count,
        complete=True,
        script=script,
    )

    return VideoScript.model_validate(script).model_dump(mode="json")


def _classify_relation_mode(client: genai.Client, claims: list[dict], spec: dict) -> str:
    response = _quota_retry(lambda: client.models.generate_content(
        model=model_for("repair"),
        contents=(
            "Classify the visible relationship connector required by the locked claims. "
            "Use directional for cause, explanation, or one-way flow; bidirectional for "
            "interaction, feedback, or mutual control; non_replacement only when a claim "
            "explicitly says one actor cannot replace the other. Do not infer facts.\n"
            f"Claims: {json.dumps(claims, ensure_ascii=False)}\n"
            f"Visible spec: {json.dumps(spec, ensure_ascii=False)}"
        ),
        config=generation_config_for("relationship",
            response_mime_type="application/json",
            response_json_schema=RelationModeDecision.model_json_schema(),
        ),
    ), label="relationship mode classification")
    return RelationModeDecision.model_validate_json(response.text or "").relation_mode


def ensure_relationship_modes(script_data: dict, job_dir: str) -> dict:
    """Upgrade legacy relationship specs using Vertex semantics, preserving passed assets."""
    legacy_shots = {
        (str(segment.get("id")), str(shot.get("shot_id")))
        for segment in script_data.get("segments", [])
        for shot in ((segment.get("visual") or {}).get("evidence_shots") or [])
        if (shot.get("motion_spec") or {}).get("layout") == "relationship"
        and "relation_mode" not in (shot.get("motion_spec") or {})
    }
    script = VideoScript.model_validate(script_data).model_dump(mode="json")
    client = None
    changed = False
    for segment in script["segments"]:
        visual = segment.get("visual") or {}
        claims_by_id = {claim["claim_id"]: claim for claim in visual.get("evidence_claims") or []}
        for shot in visual.get("evidence_shots") or []:
            spec = shot.get("motion_spec") or {}
            if (segment["id"], shot["shot_id"]) not in legacy_shots:
                continue
            client = client or _client()
            claims = [claims_by_id[item] for item in shot["proves_claim_ids"]]
            try:
                spec["relation_mode"] = _classify_relation_mode(client, claims, spec)
            except Exception as exc:
                if not _is_transient_vertex_error(exc):
                    raise
                logger.warning(
                    "Using directional relationship fallback for %s/%s after transient Vertex error: %s",
                    segment["id"],
                    shot["shot_id"],
                    type(exc).__name__,
                )
                spec["relation_mode"] = "directional"
            changed = True
    if changed:
        _write_checkpoint(
            Path(job_dir) / "visual_evidence_checkpoint.json", _input_fingerprint(script), script
        )
    return VideoScript.model_validate(script).model_dump(mode="json")


def _plan_final_visual_repair(
    client: genai.Client,
    segment: dict,
    issues: list[str],
    repair_feedback: list[str] | None = None,
    *,
    model_stage: str = "repair",
) -> dict:
    response = _quota_retry(lambda: client.models.generate_content(
        model=model_for(model_stage),
        contents=(
            "Act as the FYF visual director repairing a final rendered composition. "
            "Preserve the approved FYF visual language: clean warm ivory/olive/green, one "
            "focal story action, staged reveals, selective mascot, and a varied mix of "
            "generated scenes, object motion, diagrams/data, UI proof, and deterministic "
            "motion graphics. Never collapse every repair into cards or paper popups. "
            "Choose generated_image/generated_video for concrete story action or emotional "
            "consequence; choose motion_graphic only when exact values, order, comparison, "
            "or relationship must be explicit. For one cause/actor producing multiple parallel "
            "outcomes, use directional_branch with the cause first and every outcome after it. "
            "Return one or two concise Burmese screen "
            "lines for a beginner. Do not change narration, facts, or claim IDs. Do not add "
            "unlocked facts. For comparison, limitation, or negation claims, every named "
            "actor, attribute, and relation (including cannot/not) must be visibly encoded "
            "in Burmese labels or values; color, accent, position, or inference alone is "
            "insufficient. If using motion_graphic, motion_spec is required; otherwise it "
            "must be null.\n"
            "For relationship motion graphics choose relation_mode: directional for cause or "
            "one-way explanation, bidirectional for interaction/control loops, and "
            "non_replacement only when the locked claim explicitly says one actor cannot "
            "replace the other. The renderer shows every relationship label as an ordered "
            "node chain. Therefore order labels from the true semantic source/cause/actor "
            "through any process to the target/result; never put a described object before "
            "the technology or actor that explains or changes it. If the claim needs three "
            "or more visible stages, include every stage as a label. Put all visible wording "
            "in beginner-friendly Burmese except established names such as AI or XAI. "
            "If rejected repair feedback says a motion layout still omits a cause, actor, "
            "branch, or outcome, do not repeat the same structure: switch to an ordered "
            "sequence whose first node is the cause/actor and whose remaining nodes visibly "
            "show every outcome, or choose a text-free generated action when exact wording is "
            "not needed. Never use generated English text.\n"
            f"Segment contract: {json.dumps(segment, ensure_ascii=False)}\n"
            f"Final QA issues: {json.dumps(issues, ensure_ascii=False)}\n"
            f"Rejected repair feedback: {json.dumps(repair_feedback or [], ensure_ascii=False)}"
        ),
        config=generation_config_for("final_visual_repair",
            response_mime_type="application/json",
            response_json_schema=FinalVisualRepairPlan.model_json_schema(),
        ),
    ), label=f"final visual repair plan {segment['id']}")
    return FinalVisualRepairPlan.model_validate_json(response.text or "").model_dump(mode="json")


def _safe_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "-", value).strip("-")
    if not cleaned:
        raise ValueError("Evidence shot ID cannot produce a safe filename")
    return cleaned[:80]


def _input_fingerprint(script: dict) -> str:
    payload = {"contract_version": VISUAL_EVIDENCE_CONTRACT_VERSION, "script": script}
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _write_checkpoint(path: Path, fingerprint: str, script: dict) -> None:
    temp = path.with_suffix(".tmp")
    temp.write_text(
        json.dumps({"input_fingerprint": fingerprint, "script": script}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temp.replace(path)


def _write_final_repair_checkpoint(path: Path, source_fingerprint: str, script: dict) -> None:
    temp = path.with_suffix(".tmp")
    temp.write_text(
        json.dumps({
            "source_fingerprint": source_fingerprint,
            "script": script,
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temp.replace(path)


def _passed_shot_is_usable(shot: dict, asset_dir: Path) -> bool:
    if shot.get("verification_status") != "passed":
        return False
    if shot.get("media_type") == "motion_graphic":
        return bool(shot.get("motion_spec"))
    asset = shot.get("asset_path")
    if not isinstance(asset, str):
        return False
    path = asset_dir / Path(asset).name
    return path.is_file() and path.stat().st_size > 0


def _normalized_evidence_token(value: object) -> str:
    return "".join(str(value).translate(MYANMAR_DIGITS).split()).casefold()


def _validate_motion_spec(required: list[dict], shot: dict) -> None:
    spec = shot.get("motion_spec") or {}
    visible_tokens = [
        _normalized_evidence_token(item)
        for item in (spec.get("labels", []) + spec.get("values", []))
    ]
    exact_values = [
        value for claim in required if claim.get("evidence_type") == "count"
        for value in claim.get("values", [])
    ]
    missing_values = [
        value for value in exact_values
        if not any(_normalized_evidence_token(value) in token for token in visible_tokens)
    ]
    if missing_values:
        raise ValueError(f"does not visibly encode claim values: {missing_values}")

    exact_count_values = [
        int(_normalized_evidence_token(value))
        for claim in required
        if claim.get("evidence_type") == "count"
        for value in claim.get("values", [])
        if _normalized_evidence_token(value).isdigit()
    ]
    if spec.get("layout") == "count" and exact_count_values:
        if len(exact_count_values) != 1 or spec.get("object_count") != exact_count_values[0]:
            raise ValueError("count layout object_count must equal its single locked count")
    if spec.get("layout") == "comparison" and len(exact_count_values) >= 2:
        if len(spec.get("values", [])) < len(exact_count_values):
            raise ValueError("comparison layout must show every locked count as a separate panel")


def _verify_motion_spec_semantics(client: genai.Client, required: list[dict], shot: dict) -> None:
    visible_spec = {
        "shot_id": shot["shot_id"],
        "proves_claim_ids": shot["proves_claim_ids"],
        "motion_spec": shot.get("motion_spec"),
    }
    response = _quota_retry(lambda: client.models.generate_content(
        model=model_for("visual_verification"),
        contents=(
            "Verify whether this deterministic motion graphic specification directly and "
            "unambiguously communicates every locked claim to a Burmese-speaking beginner. "
            "Burmese labels are required audience language; do not require English or reject "
            "correct Burmese wording merely because the claim contract is written in English. "
            "Require every visible label and value to use Burmese except established acronyms "
            "such as AI or XAI. Treat label order "
            "as visual order. Reject missing causes, constraints, states, or relationships. "
            "Do not infer meaning absent from the labels, values, objects, and layout. "
            "A caption is not visual evidence and is intentionally excluded. Reject a "
            "text-only concept if the labels do not show the actors/states/actions and their order.\n"
            "For a directional relationship, verify that the ordered labels follow the actual "
            "semantic arrow direction from source/cause/actor through process to target/result. "
            "Reject reversed subjects and objects. Require every relationship label to be a "
            "visible node, including third or later outcomes.\n"
            f"Claims: {json.dumps(required, ensure_ascii=False)}\n"
            f"Visible motion specification: {json.dumps(visible_spec, ensure_ascii=False)}"
        ),
        config=generation_config_for("visual_verification",
            response_mime_type="application/json",
            response_json_schema=EvidenceVerification.model_json_schema(),
        ),
    ), label=f"motion evidence verification {shot['shot_id']}")
    verification = EvidenceVerification.model_validate_json(response.text or "")
    if not verification.passed or set(verification.proved_claim_ids) != set(shot["proves_claim_ids"]):
        raise RuntimeError(
            "Motion graphic does not prove locked claims: "
            + "; ".join(verification.issues or ["claim IDs not covered"])
        )


def _client() -> genai.Client:
    try:
        timeout_ms = int(float(os.getenv("FYF_VERTEX_CALL_TIMEOUT_SECONDS", "120")) * 1000)
    except ValueError:
        timeout_ms = 90_000
    timeout_ms = max(10_000, min(timeout_ms, 300_000))
    client = genai.Client(
        **vertex_client_kwargs(location=os.getenv("FYF_VERTEX_MEDIA_LOCATION", DEFAULT_LOCATION)),
        http_options=types.HttpOptions(timeout=timeout_ms),
    )
    return track_client(client, stage="visual")


def _image_payload(response) -> tuple[bytes, str]:
    for candidate in getattr(response, "candidates", []) or []:
        content = getattr(candidate, "content", None)
        for part in getattr(content, "parts", []) or []:
            inline = getattr(part, "inline_data", None)
            data = getattr(inline, "data", None)
            if data:
                return bytes(data), (getattr(inline, "mime_type", None) or "image/png")
    raise RuntimeError("Vertex image model returned no image bytes")


def _video_generation_enabled() -> bool:
    return os.getenv("FYF_ENABLE_VERTEX_VIDEO", "1").strip().lower() not in {"0", "false", "no", "off"}


def _quota_retry(call, *, label: str, attempts: int = QUOTA_RETRY_ATTEMPTS):
    """Retry transient Vertex quota, availability, and deadline failures."""
    _quota_retry_had_transient.set(False)
    try:
        base_delay = float(os.getenv(
            "FYF_VERTEX_RETRY_BASE_SECONDS",
            str(DEFAULT_VERTEX_RETRY_BASE_SECONDS),
        ))
    except ValueError:
        base_delay = DEFAULT_VERTEX_RETRY_BASE_SECONDS
    try:
        max_delay = float(os.getenv(
            "FYF_VERTEX_RETRY_MAX_SECONDS",
            str(DEFAULT_VERTEX_RETRY_MAX_SECONDS),
        ))
    except ValueError:
        max_delay = DEFAULT_VERTEX_RETRY_MAX_SECONDS
    base_delay = max(0, base_delay)
    max_delay = max(0, max_delay)
    attempts = max(1, min(attempts, QUOTA_RETRY_ATTEMPTS))
    for attempt in range(attempts):
        try:
            with telemetry_retry_attempt(label, attempt + 1):
                return call()
        except genai_errors.APIError as exc:
            code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
            if code not in {429, 500, 502, 503, 504} or attempt + 1 == attempts:
                raise
            _quota_retry_had_transient.set(True)
            delay = min(base_delay * (2 ** attempt), max_delay)
            logger.warning("Transient Vertex %s for %s; retrying in %.1fs", code, label, delay)
            if delay:
                time.sleep(delay)
    raise RuntimeError(f"Vertex retry loop exhausted for {label}")


def _video_bytes(operation) -> bytes:
    response = getattr(operation, "response", None) or getattr(operation, "result", None)
    generated = getattr(response, "generated_videos", None) or []
    if not generated:
        raise RuntimeError("Vertex video model returned no generated video")
    video = getattr(generated[0], "video", None)
    data = getattr(video, "video_bytes", None)
    if not data:
        uri = getattr(video, "uri", None)
        detail = f" ({uri})" if uri else ""
        raise RuntimeError(f"Vertex video response did not include downloadable bytes{detail}")
    return bytes(data)


def _generate_verified_video(
    client: genai.Client,
    still_path: Path,
    destination: Path,
    required: list[dict],
    shot: dict,
) -> None:
    operation = _quota_retry(lambda: client.models.generate_videos(
        model=model_for("video_generation"),
        source=types.GenerateVideosSource(
            prompt=(
                FYF_STYLE + "\nAnimate this approved evidence frame with restrained, physically "
                "plausible movement. Preserve every visible object count, state, relationship, "
                "composition, and color. No camera cuts, text, logos, new objects, removed objects, "
                "or changed factual values.\n" + shot["prompt"]
            ),
            image=types.Image.from_file(location=str(still_path)),
        ),
        config=types.GenerateVideosConfig(
            number_of_videos=1,
            duration_seconds=4,
            aspect_ratio="9:16",
            resolution="720p",
            generate_audio=False,
            enhance_prompt=False,
        ),
    ), label="video generation")
    deadline = time.monotonic() + VIDEO_TIMEOUT_SECONDS
    while not getattr(operation, "done", False):
        if time.monotonic() >= deadline:
            raise TimeoutError("Vertex video generation timed out")
        time.sleep(VIDEO_POLL_SECONDS)
        operation = _quota_retry(
            lambda: client.operations.get(operation), label="video operation polling"
        )
    destination.write_bytes(_video_bytes(operation))

    verify_response = _quota_retry(lambda: client.models.generate_content(
        model=model_for("visual_verification"),
        contents=[
            _verification_prompt(required, shot).replace("attached image", "attached video from beginning to end"),
            types.Part.from_bytes(data=destination.read_bytes(), mime_type="video/mp4"),
        ],
        config=generation_config_for("visual_verification",
            response_mime_type="application/json",
            response_json_schema=EvidenceVerification.model_json_schema(),
        ),
    ), label="video evidence verification")
    verification = EvidenceVerification.model_validate_json(verify_response.text or "")
    if not verification.passed or set(verification.proved_claim_ids) != set(shot["proves_claim_ids"]):
        raise RuntimeError(
            "Generated video changed or failed required visual evidence: "
            + "; ".join(verification.issues or ["required evidence was not continuously visible"])
        )


def _verification_prompt(claims: list[dict], shot: dict) -> str:
    return (
        "Act as a strict visual evidence verifier. Inspect the attached image only. "
        "Pass only when an ordinary viewer can directly see every required claim and "
        "exact value/relationship without relying on a caption. Do not infer hidden facts. "
        "The audience is Burmese-speaking. Reject generated Latin-script prose or interface "
        "copy anywhere in the image; allow only established acronyms such as AI, XAI, and FYF. "
        "Prefer text-free imagery, and require any necessary explanatory wording to be Burmese.\n"
        f"Required claims: {json.dumps(claims, ensure_ascii=False)}\n"
        f"Shot requirement: {json.dumps(shot, ensure_ascii=False)}"
    )


def _deterministic_motion_graphic_fallback(
    required: list[dict],
    shot: dict,
) -> dict:
    """Convert locked claims into a deterministic evidence frame during provider outage."""
    evidence_types = {claim.get("evidence_type") for claim in required}
    if "sequence" in evidence_types:
        layout = "sequence"
    elif "relationship" in evidence_types:
        layout = "relationship"
    elif "comparison" in evidence_types:
        layout = "comparison"
    elif "count" in evidence_types:
        layout = "count"
    elif "state" in evidence_types:
        layout = "concept"
    else:
        layout = "concept"

    labels: list[str] = []
    caption = str(shot.get("caption") or "").strip()
    if caption:
        labels.append(caption[:120])
    for claim in required:
        statement = str(claim.get("statement") or "").strip()
        if statement and statement[:120] not in labels:
            labels.append(statement[:120])
        if len(labels) >= 6:
            break
    if not labels:
        labels = ["အချက်အလက်ကို စစ်ဆေးပါ"]

    values: list[str] = []
    for claim in required:
        for value in claim.get("values") or []:
            value_text = str(value).strip()
            if value_text and value_text not in values:
                values.append(value_text)
            if len(values) >= 6:
                break
        if len(values) >= 6:
            break
    count_values = [
        int(_normalized_evidence_token(value))
        for claim in required
        if claim.get("evidence_type") == "count"
        for value in claim.get("values") or []
        if _normalized_evidence_token(value).isdigit()
    ]
    motion_spec = MotionGraphicSpec.model_validate({
        "layout": layout,
        "labels": labels[:6],
        "values": values[:6],
        "object_count": count_values[0] if layout == "count" and len(count_values) == 1 and count_values[0] <= 30 else None,
        "accent_index": 0,
        "relation_mode": "directional" if layout == "relationship" else None,
    })
    shot.update({
        "caption": caption or "အချက်အလက်ကို စစ်ဆေးပါ",
        "media_type": "motion_graphic",
        "motion_preset": "static",
        "motion_spec": motion_spec.model_dump(mode="json"),
        "asset_path": None,
        "fallback_asset_path": None,
        "fallback_used": True,
        "verification_status": "passed",
    })
    return shot


def _locked_content_fingerprint(script: dict) -> str:
    payload = {
        "title": script.get("title"),
        "language": script.get("language"),
        "segments": [
            {
                "id": segment.get("id"),
                "text": segment.get("text"),
                "claims": (segment.get("visual") or {}).get("evidence_claims") or [],
                "shot_claims": [
                    {
                        "shot_id": shot.get("shot_id"),
                        "proves_claim_ids": shot.get("proves_claim_ids") or [],
                    }
                    for shot in ((segment.get("visual") or {}).get("evidence_shots") or [])
                ],
            }
            for segment in script.get("segments") or []
        ],
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _repair_as_motion_graphic(client: genai.Client, required: list[dict], shot: dict, issues: list[str]) -> dict:
    repair_feedback = list(issues)
    repair = None
    verification = None
    for attempt in range(_motion_repair_attempts()):
        repair_model = model_for("repair") if attempt == 0 else model_for("storyboard_direction")
        response = _quota_retry(lambda: client.models.generate_content(
            model=repair_model,
            contents=(
                "Convert this failed media shot into a deterministic FYF motion graphic. "
                "Return concise beginner-friendly Burmese labels. The ordered labels and "
                "values must directly show every claim without relying on inference. Use "
                "layout count, comparison, sequence, relationship, directional_branch, or concept. "
                "Use directional_branch when one cause/actor leads to multiple parallel outcomes. Do not add "
                "facts. A causal claim must name/show its trigger, action, and result in order. "
                "Treat every verifier issue as a mandatory visible correction: rewrite the labels "
                "so each missing actor, condition, negation, qualifier, relationship, and outcome "
                "is explicitly visible in Burmese. Do not merely acknowledge feedback in reasoning, "
                "and do not rely on the caption because captions are excluded from evidence. "
                "Keep object_count null unless exact repeated objects are required.\n"
                f"Locked claims: {json.dumps(required, ensure_ascii=False)}\n"
                f"Failed shot: {json.dumps(shot, ensure_ascii=False)}\n"
                f"Verifier issues: {json.dumps(repair_feedback, ensure_ascii=False)}"
            ),
            config=generation_config_for("motion_repair",
                response_mime_type="application/json",
                response_json_schema=MotionRepair.model_json_schema(),
            ),
        ), label=f"motion repair {shot['shot_id']}")
        try:
            repair = MotionRepair.model_validate_json(response.text or "")
        except ValidationError as exc:
            repair_feedback = [f"Motion repair contract invalid: {exc}"]
            continue
        labels = repair.motion_spec.labels + repair.motion_spec.values
        normalized = [_normalized_evidence_token(item) for item in labels]
        exact_values = [
            value for claim in required if claim.get("evidence_type") == "count"
            for value in claim.get("values", [])
        ]
        missing = [
            value for value in exact_values
            if not any(_normalized_evidence_token(value) in token for token in normalized)
        ]
        if missing:
            repair_feedback = [f"Motion repair omitted locked values: {missing}"]
            continue

        verify = _quota_retry(lambda: client.models.generate_content(
            model=model_for("visual_verification"),
            contents=(
                "Verify whether this deterministic motion graphic specification directly and "
                "unambiguously communicates every locked claim to a Burmese-speaking beginner. "
                "Burmese labels are required audience language; do not require English or reject "
                "correct Burmese wording merely because the claim contract is written in English. Do not infer a "
                "cause or sequence that is absent from labels/order.\n"
                f"Claims: {json.dumps(required, ensure_ascii=False)}\n"
                f"Motion spec: {repair.model_dump_json()}"
            ),
            config=generation_config_for("visual_verification",
                response_mime_type="application/json",
                response_json_schema=EvidenceVerification.model_json_schema(),
            ),
        ), label=f"motion repair verification {shot['shot_id']}")
        verification = EvidenceVerification.model_validate_json(verify.text or "")
        if verification.passed and set(verification.proved_claim_ids) == set(shot["proves_claim_ids"]):
            break
        repair_feedback = verification.issues or ["claim IDs not covered"]
    else:
        raise RuntimeError("Motion repair did not prove locked claims: " + "; ".join(repair_feedback))
    assert repair is not None
    shot.update({
        "caption": repair.caption,
        "media_type": "motion_graphic",
        "motion_preset": "static",
        "motion_spec": repair.motion_spec.model_dump(mode="json"),
        "asset_path": None,
        "fallback_asset_path": None,
        "fallback_used": True,
        "verification_status": "passed",
    })
    return shot


def generate_and_verify_visual_evidence(script_data: dict, job_dir: str) -> dict:
    """Generate every planned evidence shot, verify it, and return an updated script."""
    original = VideoScript.model_validate(script_data).model_dump(mode="json")
    fingerprint = _input_fingerprint(original)
    script = original
    client = _client()
    root = Path(job_dir)
    asset_dir = root / "visuals"
    asset_dir.mkdir(parents=True, exist_ok=True)
    checkpoint = root / "visual_evidence_checkpoint.json"
    checkpoint_matches = False
    if checkpoint.is_file():
        try:
            saved = json.loads(checkpoint.read_text(encoding="utf-8"))
            if saved.get("input_fingerprint") == fingerprint:
                script = VideoScript.model_validate(saved["script"]).model_dump(mode="json")
                checkpoint_matches = True
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            pass
    if not checkpoint_matches:
        # Replace stale/corrupt progress before any remote call so status never
        # reports completed shots from a different locked script.
        _write_checkpoint(checkpoint, fingerprint, script)

    for segment in script["segments"]:
        visual = segment.get("visual") or {}
        claims = visual.get("evidence_claims") or []
        shots = visual.get("evidence_shots") or []
        if not claims or not shots:
            raise ValueError(f"Segment {segment['id']} has no visual evidence plan")
        claims_by_id = {claim["claim_id"]: claim for claim in claims}

        for shot in shots:
            required = [claims_by_id[claim_id] for claim_id in shot["proves_claim_ids"]]
            if _passed_shot_is_usable(shot, asset_dir):
                continue
            if shot.get("media_type") == "motion_graphic":
                try:
                    _validate_motion_spec(required, shot)
                except ValueError as exc:
                    raise ValueError(
                        f"Motion graphic for segment={segment['id']} shot={shot['shot_id']} {exc}"
                    ) from exc
                try:
                    _verify_motion_spec_semantics(client, required, shot)
                except Exception as exc:
                    if _is_transient_vertex_error(exc):
                        logger.warning(
                            "Using deterministic motion fallback for %s/%s after transient Vertex error: %s",
                            segment["id"],
                            shot["shot_id"],
                            type(exc).__name__,
                        )
                        _deterministic_motion_graphic_fallback(required, shot)
                    else:
                        try:
                            _repair_as_motion_graphic(client, required, shot, [str(exc)])
                        except Exception as repair_exc:
                            if not _is_transient_vertex_error(repair_exc):
                                raise
                            logger.warning(
                                "Using deterministic motion fallback for %s/%s after repair quota error: %s",
                                segment["id"],
                                shot["shot_id"],
                                type(repair_exc).__name__,
                            )
                            _deterministic_motion_graphic_fallback(required, shot)
                shot["verification_status"] = "passed"
                _write_checkpoint(checkpoint, fingerprint, script)
                continue
            filename = f"{_safe_name(segment['id'])}-{_safe_name(shot['shot_id'])}.png"
            destination = asset_dir / filename
            last_issues: list[str] = []
            for attempt in range(MAX_GENERATION_ATTEMPTS):
                repair = ""
                if last_issues:
                    repair = "\nCorrect these verifier failures: " + "; ".join(last_issues)
                try:
                    response = _quota_retry(lambda: client.models.generate_content(
                        model=(
                            model_for("visual_generation")
                            if attempt == 0
                            else model_for("visual_generation_quality")
                        ),
                        contents=FYF_STYLE + "\n" + shot["prompt"] + repair,
                        config=types.GenerateContentConfig(
                            response_modalities=["IMAGE"],
                            image_config=types.ImageConfig(aspect_ratio="9:16"),
                        ),
                    ), label=f"image generation {segment['id']}/{shot['shot_id']}",
                        attempts=(QUOTA_RETRY_ATTEMPTS if attempt == 0 else QUALITY_ROUTE_RETRY_ATTEMPTS))
                except Exception as exc:
                    last_issues = [f"media generation unavailable: {exc}"]
                    if _is_transient_vertex_error(exc):
                        logger.warning(
                            "Using deterministic motion fallback for %s/%s after transient media error: %s",
                            segment["id"],
                            shot["shot_id"],
                            type(exc).__name__,
                        )
                        _deterministic_motion_graphic_fallback(required, shot)
                        break
                    try:
                        _repair_as_motion_graphic(client, required, shot, last_issues)
                    except Exception as repair_exc:
                        if _is_transient_vertex_error(repair_exc):
                            logger.warning(
                                "Using deterministic motion fallback for %s/%s after repair quota error: %s",
                                segment["id"],
                                shot["shot_id"],
                                type(repair_exc).__name__,
                            )
                            _deterministic_motion_graphic_fallback(required, shot)
                            break
                        raise RuntimeError(
                            f"Visual media unavailable for segment={segment['id']} "
                            f"shot={shot['shot_id']}; motion repair failed: {repair_exc}"
                        ) from repair_exc
                    break
                image_data, image_mime = _image_payload(response)
                if image_mime not in {"image/png", "image/jpeg", "image/webp"}:
                    raise RuntimeError(f"Unsupported Vertex image MIME type: {image_mime}")
                destination.write_bytes(image_data)
                verify_response = _quota_retry(lambda: client.models.generate_content(
                    model=model_for("visual_verification"),
                    contents=[
                        _verification_prompt(required, shot),
                        types.Part.from_bytes(data=destination.read_bytes(), mime_type=image_mime),
                    ],
                    config=generation_config_for("visual_verification",
                        response_mime_type="application/json",
                        response_json_schema=EvidenceVerification.model_json_schema(),
                    ),
                ), label=f"image evidence verification {segment['id']}/{shot['shot_id']}")
                verification = EvidenceVerification.model_validate_json(verify_response.text or "")
                expected_ids = set(shot["proves_claim_ids"])
                if verification.passed and set(verification.proved_claim_ids) == expected_ids:
                    shot["asset_path"] = f"job-visuals/{filename}"
                    # The verified still is also the fail-safe frame for an optional
                    # generated-video shot. Video generation may be unavailable or
                    # fail semantic QA without blocking an accurate production render.
                    if shot.get("media_type") == "generated_video":
                        shot["fallback_asset_path"] = f"job-visuals/{filename}"
                        if _video_generation_enabled():
                            video_filename = filename.removesuffix(".png") + ".mp4"
                            video_destination = asset_dir / video_filename
                            try:
                                _generate_verified_video(
                                    client, destination, video_destination, required, shot
                                )
                            except Exception as exc:
                                video_destination.unlink(missing_ok=True)
                                shot["media_type"] = "generated_image"
                                shot["fallback_used"] = True
                                logger.warning(
                                    "Falling back to verified still for segment=%s shot=%s: %s",
                                    segment["id"], shot["shot_id"], exc,
                                )
                            else:
                                shot["asset_path"] = f"job-visuals/{video_filename}"
                        else:
                            shot["media_type"] = "generated_image"
                            shot["fallback_used"] = True
                    shot["verification_status"] = "passed"
                    break
                last_issues = verification.issues or ["required evidence was not directly visible"]
            else:
                try:
                    _repair_as_motion_graphic(client, required, shot, last_issues)
                except Exception as repair_exc:
                    raise RuntimeError(
                        f"Visual evidence failed after {MAX_GENERATION_ATTEMPTS} attempts for "
                        f"segment={segment['id']} shot={shot['shot_id']}: {last_issues}; "
                        f"motion repair failed: {repair_exc}"
                    ) from repair_exc
            _write_checkpoint(checkpoint, fingerprint, script)

    return VideoScript.model_validate(script).model_dump(mode="json")


def repair_final_visual_failures(script_data: dict, report: dict, job_dir: str) -> dict:
    """Re-direct only final-QA-failed scenes from their locked claims and brand rules."""
    original = VideoScript.model_validate(script_data).model_dump(mode="json")
    script = original
    root = Path(job_dir)
    checkpoint = root / "visual_evidence_checkpoint.json"
    repair_checkpoint = root / "final_visual_repair_checkpoint.json"
    source_fingerprint = _input_fingerprint(original)
    try:
        saved_repair = json.loads(repair_checkpoint.read_text(encoding="utf-8"))
        saved_script = VideoScript.model_validate(saved_repair["script"]).model_dump(mode="json")
    except (OSError, KeyError, ValueError, json.JSONDecodeError):
        saved_repair = None
        saved_script = None
    if (
        saved_repair
        and saved_repair.get("source_fingerprint") == source_fingerprint
        and saved_script
        and _locked_content_fingerprint(saved_script) == _locked_content_fingerprint(original)
    ):
        script = saved_script
    failed = {
        str(item.get("segment_id")): list(item.get("issues") or [])
        for item in report.get("segments", [])
        if not item.get("passed")
    }
    if not failed:
        return script

    client = _client()
    assets = root / "visuals"
    assets.mkdir(parents=True, exist_ok=True)
    original_by_id = {segment["id"]: segment for segment in original["segments"]}
    for segment in script["segments"]:
        issues = failed.get(segment["id"])
        if issues is None:
            continue
        visual = segment.get("visual") or {}
        claims = visual.get("evidence_claims") or []
        shots = visual.get("evidence_shots") or []
        if not claims or not shots:
            raise ValueError(f"Final visual repair has no evidence contract for {segment['id']}")
        original_visual = (original_by_id.get(segment["id"]) or {}).get("visual") or {}
        if visual != original_visual and all(_passed_shot_is_usable(shot, assets) for shot in shots):
            continue
        repaired_screen_text: list[str] = []
        for shot in shots:
            required = [claim for claim in claims if claim["claim_id"] in shot["proves_claim_ids"]]
            feedback: list[str] = []
            repair_target = {**segment, "repair_target_shot_id": shot["shot_id"]}
            for attempt in range(FINAL_REPAIR_PLAN_ATTEMPTS):
                try:
                    plan = _plan_final_visual_repair(
                        client,
                        repair_target,
                        issues,
                        feedback,
                        model_stage="repair" if attempt == 0 else "storyboard_direction",
                    )
                except (ValueError, json.JSONDecodeError) as exc:
                    feedback = [f"Structured repair plan was invalid: {exc}"]
                    continue
                shot.update({key: plan[key] for key in (
                    "media_type", "caption", "prompt", "motion_preset", "transition",
                    "composition", "mascot_presence", "motion_spec",
                )})
                shot.update({
                    "asset_path": None,
                    "fallback_asset_path": None,
                    "fallback_used": False,
                    "verification_status": "planned",
                })
                if plan["media_type"] != "motion_graphic":
                    for line in plan["screen_text"]:
                        if line not in repaired_screen_text and len(repaired_screen_text) < 2:
                            repaired_screen_text.append(line)
                    break
                if plan["motion_spec"] is None:
                    feedback = ["motion plan is missing motion_spec"]
                    continue
                try:
                    _validate_motion_spec(required, shot)
                    _verify_motion_spec_semantics(client, required, shot)
                except (ValueError, RuntimeError) as exc:
                    feedback = [str(exc)]
                    continue
                shot["verification_status"] = "passed"
                for line in plan["screen_text"]:
                    if line not in repaired_screen_text and len(repaired_screen_text) < 2:
                        repaired_screen_text.append(line)
                _write_checkpoint(
                    root / "visual_evidence_checkpoint.json", _input_fingerprint(script), script
                )
                break
            else:
                raise RuntimeError(
                    f"Final visual repair failed for {segment['id']} shot {shot['shot_id']}: "
                    + "; ".join(feedback or issues)
                )
            _write_final_repair_checkpoint(
                repair_checkpoint, source_fingerprint, script
            )
        visual["screen_text"] = repaired_screen_text

    fingerprint = _input_fingerprint(script)
    _write_checkpoint(root / "visual_evidence_checkpoint.json", fingerprint, script)
    # Generated repair plans remain planned and flow through the existing
    # generation plus per-shot verifier. Motion plans have already passed it.
    repaired = generate_and_verify_visual_evidence(script, job_dir)
    _write_final_repair_checkpoint(repair_checkpoint, source_fingerprint, repaired)
    return repaired
