import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import time
from pathlib import Path
from typing import Dict, Any, Literal

from backend.job_store import (
    acquire_job_lease,
    begin_job_attempt,
    read_job_status,
    release_job_lease,
    update_job_status,
    write_json_atomically,
)
from backend.latency_metrics import record_latency_sample
from backend.mouth_cues import build_render_input
from backend.output_qa import (
    audit_output_manifest,
    persist_qa_report,
    qa_job_directory,
)
from backend.render_contract import (
    repair_caption_audio_plan,
    route_caption_audio_qa,
    run_caption_audio_qa,
)
from backend.visual_artifact_store import (
    claim_artifact,
    fail_artifact,
    materialize_artifact,
    seal_artifact,
    visual_artifact_key,
)
from backend.final_visual_qa_vertex import verify_final_rendered_meaning
from backend.creative_quality import audit_creative_quality
from backend.director_context import DirectorPolicy
from backend.render_video import render_video_remotion
from backend.segment_render_cache import render_segments_and_assemble
from voice_service.voice_generator import generate_voice
from voice_service.audio_quality import master_voice_audio
from visual_evidence_vertex import (
    ensure_relationship_modes,
    generate_and_verify_visual_evidence,
    plan_visual_treatments,
    repair_creative_failures,
    repair_final_visual_failures,
)
from vertex_model_routing import model_for
from backend.vertex_telemetry import telemetry_scope
from video_contract import ASPECT_RATIO_DIMENSIONS, RenderControls, VideoScript

logger = logging.getLogger(__name__)

# process-local asyncio.Semaphore(1)
_pipeline_semaphore = asyncio.Semaphore(1)
_RENDER_RETRYABLE_QA_CODES = {
    "MISSING_VIDEO",
    "VIDEO_PROBE_FAILED",
    "VIDEO_NO_VIDEO_STREAM",
    "VIDEO_NO_AUDIO_STREAM",
    "VIDEO_ZERO_DURATION",
    "VIDEO_TOO_SHORT",
}
RENDER_CHECKPOINT_VERSION = 2
MAX_FINAL_VISUAL_ATTEMPTS = 3
MAX_CREATIVE_ATTEMPTS = 2
MAX_CAPTION_AUDIO_REPAIRS = 1
REMOTION_SOURCE_ROOT = Path(__file__).resolve().parents[1] / "remotion" / "src"


def _visual_model_routes() -> dict[str, str]:
    return {
        stage: model_for(stage)
        for stage in (
            "visual_direction",
            "visual_generation",
            "visual_generation_quality",
            "visual_verification",
        )
    }


# D8: which pipeline stage publishes which latency metric.  Stages with no entry
# still accumulate stage_timings but publish no sample, because a sample without
# a defined target would be noise rather than measurement.
_STAGE_LATENCY_METRIC: Dict[str, str] = {
    "voice": "draft_to_animatic",
    "animatic": "draft_to_animatic",
    "render": "final_render",
}


def _mark_stage_cache_state(job_dir: Path, stage: str, warm: bool) -> None:
    """Record whether a stage is about to run cold or warm (D8 condition tag).

    The cache state is written BEFORE the stage runs so the latency sample taken
    when it finishes is tagged with the truth rather than guessed at afterwards.
    """

    status = read_job_status(job_dir)
    current = status.get("stage_cache_state")
    states = dict(current) if isinstance(current, dict) else {}
    states[stage] = "warm" if warm else "cold"
    update_job_status(job_dir, {"stage_cache_state": states})


def _stage_latency_conditions(job_dir: Path) -> Dict[str, Any]:
    """Scene count, aspect ratio and language for a latency sample's conditions."""

    conditions: Dict[str, Any] = {"scene_count": 0, "aspect_ratio": None, "language": None}
    for name in ("render_input.json", "script.json"):
        path = job_dir / name
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        segments = payload.get("segments")
        if isinstance(segments, list) and segments:
            conditions["scene_count"] = len(segments)
        language = payload.get("language")
        if isinstance(language, str) and language:
            conditions["language"] = language
        controls = payload.get("render_controls")
        ratio = None
        if isinstance(controls, dict):
            ratio = controls.get("aspect_ratio")
        if not isinstance(ratio, str):
            ratio = payload.get("aspect_ratio")
        if isinstance(ratio, str) and ratio:
            conditions["aspect_ratio"] = ratio
        if conditions["scene_count"]:
            break
    return conditions


def _record_stage_timing(
    job_dir: Path,
    stage: str,
    started_at: float,
    *,
    job_id: str | None = None,
    provider_queue_depth: int | None = None,
) -> None:
    elapsed = max(0.0, time.monotonic() - started_at)
    status = read_job_status(job_dir)
    current = status.get("stage_timings") or {}
    timings = dict(current) if isinstance(current, dict) else {}
    previous = timings.get(stage, 0.0)
    if not isinstance(previous, (int, float)) or previous < 0:
        previous = 0.0
    timings[stage] = float(round(previous + elapsed, 3))
    update_job_status(job_dir, {"stage_timings": timings})

    # D8: the same elapsed value is published as a latency SAMPLE tagged with the
    # conditions it was measured under.  Recording is best-effort by design — a
    # metrics store problem must never fail a render — but it is logged, never
    # silently swallowed.
    metric = _STAGE_LATENCY_METRIC.get(stage)
    if metric is None:
        return
    states = status.get("stage_cache_state")
    cache_state = states.get(stage) if isinstance(states, dict) else None
    if cache_state not in ("cold", "warm"):
        logger.warning(
            "[%s] stage %s has no recorded cache state; latency sample not published "
            "rather than published mislabelled",
            job_id or job_dir.name,
            stage,
        )
        return
    conditions = _stage_latency_conditions(job_dir)
    try:
        record_latency_sample(
            metric,
            elapsed,
            cache_state=cache_state,
            scene_count=int(conditions["scene_count"]),
            aspect_ratio=conditions["aspect_ratio"],
            language=conditions["language"],
            provider_queue_depth=provider_queue_depth,
            job_id=job_id or job_dir.name,
            stage=stage,
        )
    except (OSError, ValueError) as exc:
        logger.warning("[%s] latency sample for %s was not recorded: %s", job_id or job_dir.name, stage, exc)


def _write_timed_animatic(job_dir: Path, render_input: Dict[str, Any], *, has_music: bool = False) -> Dict[str, Any]:
    """D4: persist the voice-timed animatic plan next to the render contract.

    Pure planning over the approved draft plus the measured voice track — no
    provider call, no cost.  The document itself declares whether its timings
    were measured from real audio or fell back to text-weight estimates.
    """

    from voice_service.timed_animatic import build_timed_animatic, write_timed_animatic

    segments = render_input.get("segments") if isinstance(render_input, dict) else None
    if not isinstance(segments, list) or not segments:
        # An animatic plan cannot exist without scenes.  Rather than fail the
        # render for a derived artifact, or invent one, persist an explicit
        # not-planned marker so the absence is visible and auditable.
        document = {
            "status": "not_planned",
            "reason": "render input carries no segments, so no scene can be timed",
            "generation_enabled": False,
        }
        write_timed_animatic(job_dir, document)
        return document

    timing_source = render_input.get("segmentTimingSource")
    try:
        document = build_timed_animatic(
            render_input,
            has_music=has_music,
            voice_measured=timing_source in {"wav-silence-snap", "single-segment"},
        )
    except ValueError as exc:
        document = {
            "status": "not_planned",
            "reason": f"animatic planning rejected the render input: {exc}",
            "generation_enabled": False,
        }
    document.setdefault("status", "planned")
    write_timed_animatic(job_dir, document)
    return document


def _run_caption_audio_quality(job_dir: Path, render_input: Dict[str, Any]) -> Dict[str, Any] | None:
    """Run D7 technical/creative lanes against the persisted animatic.

    Legacy test fixtures without scenes do not have a caption contract and are
    left to the existing deterministic output QA. Production render inputs
    always carry scenes; for those, a missing/corrupt animatic is a real defect
    and fails rather than being treated as a pass.
    """

    segments = render_input.get("segments")
    if not isinstance(segments, list) or not segments:
        return None

    def _blocked(code: str, detail: str) -> Dict[str, Any]:
        report = {
            "report_version": 1,
            "status": "blocked",
            "passed": False,
            "technical_gate": {
                "passed": False,
                "lane": "technical",
                "checks": [{"id": code, "passed": False, "detail": detail}],
                "failure_codes": [code],
            },
            "creative_gate": {
                "passed": False,
                "lane": "creative",
                "checks": [],
                "failure_codes": [],
                "route": "needs_human_review",
            },
            "human_acceptance": {
                "lane": "human_acceptance",
                "required": False,
                "pending_ids": [],
                "statement": (
                    "human acceptance is a separate decision; nothing in this report "
                    "approves the work on a human's behalf"
                ),
            },
            "overall": {
                "passed": False,
                "technical_passed": False,
                "creative_passed": False,
            },
            "lanes_independent": True,
            "caption_cues": 0,
        }
        write_json_atomically(job_dir / "caption_audio_qa.json", report)
        return report

    animatic_path = job_dir / "animatic.json"
    if not animatic_path.is_file():
        return _blocked("ANIMATIC_MISSING", "Caption/audio QA requires animatic.json")
    try:
        animatic = json.loads(animatic_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return _blocked("ANIMATIC_UNREADABLE", f"Caption/audio QA could not read animatic.json: {exc}")
    if not isinstance(animatic, dict) or animatic.get("status") == "not_planned":
        return _blocked("ANIMATIC_NOT_PLANNED", "Caption/audio QA requires a planned animatic")
    qa_input = dict(render_input)
    mix = animatic.get("mix")
    if isinstance(mix, dict):
        qa_input["mix"] = mix
    cues = animatic.get("captions")
    try:
        report = run_caption_audio_qa(
            qa_input,
            cues=[dict(cue) for cue in cues if isinstance(cue, dict)]
            if isinstance(cues, list)
            else None,
        )
    except (TypeError, ValueError, KeyError) as exc:
        return _blocked("CAPTION_AUDIO_CONTRACT_INVALID", str(exc))
    write_json_atomically(job_dir / "caption_audio_qa.json", report)
    return report


def _repair_caption_audio_quality(
    job_dir: Path, render_input: Dict[str, Any], report: Dict[str, Any]
) -> bool:
    """Apply one real deterministic caption/audio repair, if it is safe.

    A creative ``repair`` route is not permission to run the same QA against
    unchanged inputs.  The persisted animatic is the only mutable plan at this
    stage; when no supported, observable change can be made, return ``False``
    so the caller escalates to human review without a duplicate report.
    """

    animatic_path = job_dir / "animatic.json"
    try:
        animatic = json.loads(animatic_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(animatic, dict):
        return False
    repaired = repair_caption_audio_plan(render_input, animatic, report)
    if repaired is None or repaired == animatic:
        return False
    write_json_atomically(animatic_path, repaired)
    return True


def _caption_audio_plan_fingerprint(job_dir: Path) -> str | None:
    """Return the persisted animatic identity used to prove a repair changed state."""

    path = job_dir / "animatic.json"
    try:
        if not path.is_file() or path.stat().st_size == 0:
            return None
        return _sha256_file(path)
    except OSError:
        return None


def _run_caption_audio_preflight(
    job_dir: Path,
    render_input: Dict[str, Any],
    *,
    max_repairs: int = MAX_CAPTION_AUDIO_REPAIRS,
) -> tuple[Dict[str, Any] | None, Dict[str, Any] | None]:
    """Run and persist caption/audio QA, applying only changed bounded repairs."""

    if isinstance(max_repairs, bool) or not isinstance(max_repairs, int) or max_repairs < 0:
        raise ValueError("max_repairs must be a non-negative integer")
    if not isinstance(render_input.get("segments"), list) or not render_input.get("segments"):
        return None, None

    report: Dict[str, Any] | None = None
    route: Dict[str, Any] | None = None
    for caption_attempt in range(1, max_repairs + 2):
        report = _run_caption_audio_quality(job_dir, render_input)
        if report is None:
            break
        persist_qa_report(
            job_dir,
            report,
            attempt=caption_attempt,
            report_name="caption_audio_qa",
        )
        route = route_caption_audio_qa(
            report,
            attempt=caption_attempt - 1,
            max_repairs=max_repairs,
        )
        if route["route"] != "repair":
            break
        plan_before = _caption_audio_plan_fingerprint(job_dir)
        if not _repair_caption_audio_quality(job_dir, render_input, report):
            route = {
                **route,
                "route": "needs_human_review",
                "reason": "caption_audio_repair_unavailable",
                "repair_applied": False,
                "human_acceptance_required": True,
            }
            break
        plan_after = _caption_audio_plan_fingerprint(job_dir)
        if plan_before is None or plan_after is None or plan_before == plan_after:
            route = {
                **route,
                "route": "needs_human_review",
                "reason": "caption_audio_repair_noop",
                "repair_applied": False,
                "human_acceptance_required": True,
            }
            break
        route = {**route, "repair_applied": True}
    return report, route


def _is_transient_render_failure(error: BaseException) -> bool:
    """Return whether a segmented-render error is safe to retry/fallback.

    A segmented renderer may fall back only for explicitly transient transport
    or availability failures.  Contract, manifest, and assembly errors are
    deterministic defects and must surface to the caller instead of being
    hidden by a monolithic render.
    """

    if isinstance(error, (TimeoutError, ConnectionError)):
        return True
    if isinstance(error, (ValueError, json.JSONDecodeError)):
        return False
    message = str(error).upper()
    transient_markers = (
        "TIMEOUT",
        "TIMED OUT",
        "TEMPORARY",
        "TRANSIENT",
        "UNAVAILABLE",
        "RESOURCE_EXHAUSTED",
        "DEADLINE_EXCEEDED",
        "HTTP 429",
        "HTTP 500",
        "HTTP 502",
        "HTTP 503",
        "HTTP 504",
        "STATUS 429",
        "STATUS 500",
        "STATUS 502",
        "STATUS 503",
        "STATUS 504",
    )
    return any(marker in message for marker in transient_markers)


def _migrate_best_director_checkpoint(job_dir: Path, artifact_dir: Path) -> None:
    destination = artifact_dir / "director_treatment_checkpoint.json"
    if destination.exists():
        return

    local = job_dir / "director_treatment_checkpoint.json"
    try:
        local_payload = json.loads(local.read_text(encoding="utf-8"))
        fingerprint = local_payload.get("input_fingerprint")
    except (OSError, json.JSONDecodeError, AttributeError):
        return
    if not isinstance(fingerprint, str) or not fingerprint:
        return

    candidates: list[tuple[int, Path]] = []
    for sibling in job_dir.parent.iterdir():
        candidate = sibling / "director_treatment_checkpoint.json"
        if not sibling.is_dir() or candidate.is_symlink() or not candidate.is_file():
            continue
        try:
            payload = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if payload.get("input_fingerprint") != fingerprint:
            continue
        completed = payload.get("completed_shot_ids") or []
        if isinstance(completed, list) and completed:
            score = len(completed)
        else:
            checkpoint_script = payload.get("script") or {}
            segments = checkpoint_script.get("segments") or [] if isinstance(checkpoint_script, dict) else []
            score = sum(
                bool(shot.get("treatment"))
                for segment in segments
                if isinstance(segment, dict)
                for shot in ((segment.get("visual") or {}).get("evidence_shots") or [])
                if isinstance(shot, dict)
            )
        candidates.append((score, candidate))

    if not candidates:
        return
    source = max(candidates, key=lambda item: item[0])[1]
    temporary = destination.with_suffix(".migration-tmp")
    shutil.copyfile(source, temporary)
    os.replace(temporary, destination)


def _prepare_visual_artifact(
    job_id: str,
    job_dir: Path,
    script_dict: Dict[str, Any],
    artifacts_root: Path,
) -> Dict[str, Any]:
    policy = DirectorPolicy()
    story_fields = set(VideoScript.model_fields)
    # Keep the planner boundary free of render-only metadata without forcing a
    # second validation pass here.  The production request was already
    # validated, while tests and resume checkpoints may intentionally carry a
    # minimal pre-planner shape that the planner itself enriches.
    story_script = {
        key: value for key, value in script_dict.items() if key in story_fields
    }
    render_metadata = {
        key: value for key, value in script_dict.items() if key not in story_fields
    }
    persisted_key = read_job_status(job_dir).get("visual_artifact_key")
    if isinstance(persisted_key, str) and persisted_key:
        try:
            materialize_artifact(artifacts_root, persisted_key, job_dir)
        except (OSError, ValueError):
            pass
        else:
            update_job_status(job_dir, {
                "visual_artifact_key": persisted_key,
                "visual_cache_state": "hit",
            })
            return json.loads((job_dir / "script.json").read_text(encoding="utf-8"))
    key = visual_artifact_key(script_dict, policy.version, _visual_model_routes())
    update_job_status(job_dir, {"visual_artifact_key": key})
    try:
        wait_seconds = int(os.getenv("FYF_VISUAL_ARTIFACT_WAIT_SECONDS", "900"))
    except ValueError:
        wait_seconds = 900
    wait_seconds = max(1, min(wait_seconds, 3600))
    deadline = time.monotonic() + wait_seconds

    while True:
        state = claim_artifact(artifacts_root, key, job_id)
        update_job_status(job_dir, {"visual_cache_state": state})
        if state == "hit":
            materialize_artifact(artifacts_root, key, job_dir)
            return json.loads((job_dir / "script.json").read_text(encoding="utf-8"))
        if state == "producer":
            artifact_dir = artifacts_root / key
            try:
                _migrate_best_director_checkpoint(job_dir, artifact_dir)
                # Visual planning consumes the strict story contract only. Render
                # controls are job-local output settings and are restored after
                # the visual evidence stages complete.
                produced = plan_visual_treatments(story_script, str(artifact_dir), policy)
                produced = ensure_relationship_modes(produced, str(artifact_dir))
                produced = generate_and_verify_visual_evidence(produced, str(artifact_dir))
                produced.update(render_metadata)
                write_json_atomically(artifact_dir / "script.json", produced)
                files = ["script.json"]
                for name in ("director_treatment_checkpoint.json", "visual_evidence_checkpoint.json"):
                    if (artifact_dir / name).is_file():
                        files.append(name)
                visual_dir = artifact_dir / "visuals"
                if visual_dir.is_dir():
                    files.extend(
                        str(path.relative_to(artifact_dir))
                        for path in sorted(visual_dir.rglob("*"))
                        if path.is_file() and not path.is_symlink()
                    )
                seal_artifact(artifacts_root, key, job_id, {
                    "fingerprint_inputs": {
                        "policy_version": policy.version,
                        "model_routes": _visual_model_routes(),
                    },
                    "files": files,
                })
            except Exception:
                try:
                    fail_artifact(artifacts_root, key, job_id, "visual_production_failed")
                except Exception:
                    logger.exception("[%s] Could not release failed visual artifact", job_id)
                raise
            materialize_artifact(artifacts_root, key, job_dir)
            return json.loads((job_dir / "script.json").read_text(encoding="utf-8"))
        if time.monotonic() >= deadline:
            raise TimeoutError("Timed out waiting for shared visual artifact")
        time.sleep(1)


def _voice_fingerprint(script_dict: Dict[str, Any], provider: str) -> str:
    narration = [
        {"id": segment.get("id"), "text": segment.get("text")}
        for segment in script_dict.get("segments", [])
    ]
    payload = json.dumps(
        {"provider": provider, "language": script_dict.get("language"), "narration": narration},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _legacy_voice_fingerprint(script_dict: Dict[str, Any], provider: str) -> str:
    payload = json.dumps(
        {"provider": provider, "script": script_dict},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _render_fingerprint(script_dict: Dict[str, Any], audio_path: Path) -> str:
    digest = hashlib.sha256(json.dumps(
        script_dict, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8"))
    digest.update(str(audio_path.stat().st_size).encode("ascii"))
    digest.update(_sha256_file(audio_path).encode("ascii"))
    if REMOTION_SOURCE_ROOT.is_dir():
        for path in sorted(
            item for item in REMOTION_SOURCE_ROOT.rglob("*")
            if item.is_file() and item.suffix in {".ts", ".tsx", ".css", ".json"}
        ):
            digest.update(path.relative_to(REMOTION_SOURCE_ROOT).as_posix().encode("utf-8"))
            digest.update(path.read_bytes())
    return digest.hexdigest()


def _script_render_controls(script_dict: Dict[str, Any]) -> dict[str, Any] | None:
    """Return a validated control snapshot when the job carries one."""
    raw = script_dict.get("render_controls")
    if raw is None:
        names = (
            "cta_text",
            "retention_progress_bar",
            "animated_lower_thirds",
            "aspect_ratio",
        )
        if not any(name in script_dict for name in names):
            return None
        raw = {name: script_dict[name] for name in names if name in script_dict}
    return RenderControls.model_validate(raw).model_dump(mode="json")


def _render_input_matches_controls(job_dir: Path, script_dict: Dict[str, Any]) -> bool:
    """Prevent a resume/cache hit when persisted Remotion props drift."""
    expected = _script_render_controls(script_dict)
    if expected is None:
        return True
    try:
        render_input = json.loads((job_dir / "render_input.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(render_input, dict):
        return False
    raw = render_input.get("render_controls")
    if raw is None:
        names = tuple(expected)
        raw = {name: render_input[name] for name in names if name in render_input}
    try:
        actual = RenderControls.model_validate(raw).model_dump(mode="json")
    except ValueError:
        return False
    if actual != expected:
        return False
    dimensions = ASPECT_RATIO_DIMENSIONS[expected["aspect_ratio"]]
    return (render_input.get("width"), render_input.get("height")) == dimensions


def _render_checkpoint_is_usable(job_dir: Path, script_dict: Dict[str, Any], audio_path: Path) -> bool:
    checkpoint_path = job_dir / "render_checkpoint.json"
    video_path = job_dir / "video.mp4"
    required = [
        job_dir / "render_input.json",
        job_dir / "mouth_cues.json",
        checkpoint_path,
        video_path,
    ]
    if not all(path.is_file() for path in required):
        return False
    try:
        saved = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    version = saved.get("version", 1)
    if version not in {1, RENDER_CHECKPOINT_VERSION}:
        return False
    if version == RENDER_CHECKPOINT_VERSION:
        saved_strategy = saved.get("strategy")
        if saved_strategy not in {"segmented", "monolithic", "monolithic-fallback"}:
            return False
        if saved_strategy == "segmented":
            return False
        try:
            current_progress = read_job_status(job_dir).get("render_progress") or {}
        except (FileNotFoundError, ValueError):
            current_progress = {}
        current_strategy = current_progress.get("strategy")
        if current_strategy and current_strategy != saved_strategy:
            return False
        current_manifest = current_progress.get("manifest_fingerprint")
        saved_manifest = saved.get("manifest_fingerprint")
        if current_manifest and saved_manifest != current_manifest:
            return False
    if not _render_input_matches_controls(job_dir, script_dict):
        return False
    return (
        saved.get("complete") is True
        and saved.get("fingerprint") == _render_fingerprint(script_dict, audio_path)
        and saved.get("video_bytes") == video_path.stat().st_size
        and saved.get("video_sha256") == _sha256_file(video_path)
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_render_checkpoint(
    job_dir: Path,
    script_dict: Dict[str, Any],
    audio_path: Path,
    video_path: Path | None = None,
    render_progress: Dict[str, Any] | None = None,
) -> None:
    progress = render_progress
    if progress is None:
        try:
            progress = read_job_status(job_dir).get("render_progress")
        except (FileNotFoundError, ValueError):
            progress = None
    if not isinstance(progress, dict):
        progress = {}
    strategy = progress.get("strategy")
    if strategy not in {"segmented", "monolithic", "monolithic-fallback"}:
        strategy = "segmented" if os.getenv("FYF_SEGMENT_RENDER_ENABLED", "0").strip() == "1" else "monolithic"
    payload: Dict[str, Any] = {
        "version": RENDER_CHECKPOINT_VERSION,
        "strategy": strategy,
        "fingerprint": _render_fingerprint(script_dict, audio_path),
        "complete": False,
    }
    manifest_fingerprint = progress.get("manifest_fingerprint")
    if isinstance(manifest_fingerprint, str) and re.fullmatch(r"[0-9a-f]{64}", manifest_fingerprint):
        payload["manifest_fingerprint"] = manifest_fingerprint
    if video_path is not None and video_path.is_file() and video_path.stat().st_size > 0:
        payload.update({
            "complete": True,
            "video_bytes": video_path.stat().st_size,
            "video_sha256": _sha256_file(video_path),
        })
    write_json_atomically(job_dir / "render_checkpoint.json", payload)


def _render_with_configured_strategy(job_dir: Path) -> tuple[Path, Dict[str, Any]]:
    """Render with the opt-in segment cache and a transient-only fallback.

    Direct callers that predate the persisted render contract are kept
    compatible with the historical fallback behaviour.  ``run_pipeline``
    writes ``render_input.json`` before this function is reached, so every
    production render takes the strict path and deterministic failures cannot
    be hidden by a monolithic retry.
    """
    if os.getenv("FYF_SEGMENT_RENDER_ENABLED", "0").strip() == "1":
        try:
            report = render_segments_and_assemble(str(job_dir))
        except Exception as exc:
            legacy_uncontracted_call = not (job_dir / "render_input.json").is_file()
            if not _is_transient_render_failure(exc) and not legacy_uncontracted_call:
                raise
            logger.warning(
                "Segmented render failed for %s; falling back to monolithic render%s: %s",
                job_dir,
                " (legacy uncontracted call)" if legacy_uncontracted_call else "",
                exc,
            )
            output_path = Path(render_video_remotion(str(job_dir)))
            progress = {
                "strategy": "monolithic-fallback",
                "total": 0,
                "rendered": 0,
                "cache_hits": 0,
                "fallback_reason": f"{type(exc).__name__}: {exc}",
            }
        else:
            output_path = Path(report.output_path)
            progress = {
                "strategy": "segmented",
                "total": report.total_segments,
                "rendered": report.rendered_segments,
                "cache_hits": report.cache_hits,
                "manifest_fingerprint": report.manifest_fingerprint,
            }
    else:
        output_path = Path(render_video_remotion(str(job_dir)))
        progress = {
            "strategy": "monolithic",
            "total": 0,
            "rendered": 0,
            "cache_hits": 0,
        }

    update_job_status(job_dir, {"render_progress": progress})
    return output_path, progress


def _voice_checkpoint_is_usable(
    job_dir: Path, script_dict: Dict[str, Any], provider: str
) -> bool:
    audio_path = job_dir / "voice.wav"
    checkpoint_path = job_dir / "voice_checkpoint.json"
    if not audio_path.is_file() or audio_path.stat().st_size == 0 or not checkpoint_path.is_file():
        return False
    try:
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    fingerprint = checkpoint.get("fingerprint")
    return (
        fingerprint in {
            _voice_fingerprint(script_dict, provider),
            _legacy_voice_fingerprint(script_dict, provider),
        }
        and checkpoint.get("bytes") == audio_path.stat().st_size
    )


def _load_resumable_rendered_script(
    job_dir: Path,
    provider: str,
) -> Dict[str, Any] | None:
    """Return the exact job-local script only when its rendered media is reusable."""
    video_path = job_dir / "video.mp4"
    script_path = job_dir / "script.json"
    qa_path = job_dir / "qa_report.json"
    if not video_path.is_file() or video_path.stat().st_size == 0:
        return None
    try:
        persisted_script = json.loads(script_path.read_text(encoding="utf-8"))
        deterministic_qa = json.loads(qa_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not deterministic_qa.get("passed"):
        return None
    audio_path = job_dir / "voice.wav"
    if not _voice_checkpoint_is_usable(job_dir, persisted_script, provider):
        return None
    if not _render_checkpoint_is_usable(job_dir, persisted_script, audio_path):
        return None
    return persisted_script


def _load_approved_local_visual_script(
    job_dir: Path,
    incoming_script: Dict[str, Any],
) -> Dict[str, Any] | None:
    """Reuse a locally approved visual plan even when audio invalidates its render."""
    try:
        persisted_script = json.loads((job_dir / "script.json").read_text(encoding="utf-8"))
        reports = [
            json.loads((job_dir / name).read_text(encoding="utf-8"))
            for name in ("qa_report.json", "creative_qa.json", "final_visual_qa.json")
        ]
    except (OSError, json.JSONDecodeError):
        return None
    if not all(report.get("passed") is True for report in reports):
        return None
    if _voice_fingerprint(persisted_script, "narration") != _voice_fingerprint(
        incoming_script, "narration"
    ):
        return None
    return persisted_script

def _mirror_job_telemetry_to_clickhouse(job_id: str, job_dir: Path, script_dict: Dict[str, Any]) -> None:
    """Best-effort dual-write of terminal job metrics into ClickHouse Cloud.

    Reads back the persisted per-job telemetry snapshot so every finished run
    (success or failure) lands in video_pipeline_jobs for the Data Officer.
    Never raises into the pipeline; failures are logged at warning level.
    """
    try:
        from backend.telemetry_store import record_job_telemetry

        telemetry_file = job_dir / "telemetry.json"
        if not telemetry_file.is_file():
            return
        snapshot = json.loads(telemetry_file.read_text(encoding="utf-8"))
        summary = snapshot.get("summary") or {}
        status_row = read_job_status(job_dir) or {}

        timings = status_row.get("stage_timings") or {}
        total_duration_ms = int(
            sum(float(v) for v in timings.values() if isinstance(v, (int, float))) * 1000
        )
        qa_report = status_row.get("qa_report") or {}
        final_qa = status_row.get("final_visual_qa") or {}
        observed_models = {
            call.get("model", "").strip()
            for call in (snapshot.get("calls") or [])
            if isinstance(call, dict)
            and isinstance(call.get("model"), str)
            and call.get("model", "").strip()
        }
        observed_model = next(iter(observed_models)) if len(observed_models) == 1 else None
        script_metadata = {
            field: script_dict[field]
            for field in ("studio_name", "language", "genre")
            if field in script_dict and script_dict[field] is not None
        }

        record_job_telemetry(
            job_id,
            {
                "title": str(script_dict.get("title", "")),
                # A job can contain multiple provider models (for example
                # Vertex visual calls plus Gemini TTS). Keep the legacy
                # mirror model field unknown unless telemetry observed one
                # unambiguous model rather than inventing a default.
                "model_name": observed_model,
                "input_tokens": int(summary.get("total_input_tokens") or 0),
                "output_tokens": int(summary.get("total_output_tokens") or 0),
                "model_call_count": int(
                    summary.get("billable_calls") or summary.get("total_calls") or 0
                ),
                "retry_count": int(summary.get("job_retry_count") or 0),
                "total_duration_ms": total_duration_ms,
                "render_duration_ms": int(float(timings.get("render") or 0) * 1000),
                "status": str(status_row.get("status") or snapshot.get("job_status") or "completed"),
                "qa_passed": bool(qa_report.get("passed", False) or final_qa.get("passed", False)),
                "calls": snapshot.get("calls", []),
                **script_metadata,
            },
        )
    except Exception:
        logger.warning("[%s] ClickHouse telemetry mirror failed", job_id, exc_info=True)


async def run_pipeline(
    job_id: str,
    script_dict: Dict[str, Any],
    provider: Literal["gemini"] = "gemini",
    jobs_root: Path = Path("jobs"),
    visual_artifacts_root: Path | None = None,
) -> None:
    job_dir = jobs_root / job_id
    lease_token = acquire_job_lease(job_dir)
    if lease_token is None:
        logger.info(f"[{job_id}] Another worker owns the persisted job lease; skipping duplicate run")
        return

    telemetry_context = telemetry_scope(job_id, "video", job_dir)
    telemetry_collector = telemetry_context.__enter__()
    try:
        async with _pipeline_semaphore:
            if provider != "gemini":
                provider = "gemini"
            begin_job_attempt(job_dir)
            local_visual_repair = False
            previous_final_path = job_dir / "final_visual_qa.json"
            if previous_final_path.is_file():
                try:
                    previous_final = json.loads(previous_final_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    previous_final = None
                if previous_final and not previous_final.get("passed"):
                    logger.info(f"[{job_id}] Repairing final-QA-failed scenes from their locked claims")
                    audio_path = job_dir / "voice.wav"
                    if _voice_checkpoint_is_usable(job_dir, script_dict, provider):
                        write_json_atomically(job_dir / "voice_checkpoint.json", {
                            "provider": provider,
                            "fingerprint": _voice_fingerprint(script_dict, provider),
                            "bytes": audio_path.stat().st_size,
                        })
                    script_dict = await asyncio.to_thread(
                        repair_final_visual_failures, script_dict, previous_final, str(job_dir)
                    )
                    local_visual_repair = True
                    write_json_atomically(job_dir / "script.json", script_dict)
                    previous_final_path.unlink(missing_ok=True)
            previous_creative_path = job_dir / "creative_qa.json"
            if not local_visual_repair and previous_creative_path.is_file():
                try:
                    previous_creative = json.loads(previous_creative_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    previous_creative = None
                if previous_creative and not previous_creative.get("passed"):
                    logger.info(f"[{job_id}] Repairing persisted creative rhythm before artifact reuse")
                    script_dict = await asyncio.to_thread(
                        repair_creative_failures,
                        script_dict,
                        previous_creative,
                        str(job_dir),
                    )
                    local_visual_repair = True
                    write_json_atomically(job_dir / "script.json", script_dict)
                    previous_creative_path.unlink(missing_ok=True)
            if not local_visual_repair:
                approved_local_script = _load_approved_local_visual_script(
                    job_dir, script_dict
                )
                if approved_local_script is not None:
                    logger.info(f"[{job_id}] Reusing approved job-local visual plan")
                    script_dict = approved_local_script
                    local_visual_repair = True
            if not local_visual_repair:
                persisted_render_script = _load_resumable_rendered_script(job_dir, provider)
                if persisted_render_script is not None:
                    logger.info(f"[{job_id}] Resuming QA from verified job-local rendered output")
                    script_dict = persisted_render_script
                    local_visual_repair = True
            update_job_status(job_dir, {"status": "visuals", "error": None})
            logger.info(f"[{job_id}] Generating and verifying Vertex visual evidence")
            visuals_started = time.monotonic()
            try:
                if local_visual_repair:
                    logger.info(f"[{job_id}] Reusing verified job-local visual repairs")
                else:
                    script_dict = await asyncio.to_thread(
                        _prepare_visual_artifact,
                        job_id,
                        job_dir,
                        script_dict,
                        visual_artifacts_root or (
                            jobs_root.parent / "visual-artifacts"
                            if jobs_root.name == "jobs"
                            else jobs_root / ".visual-artifacts"
                        ),
                    )
            finally:
                _record_stage_timing(job_dir, "visuals", visuals_started)
            write_json_atomically(job_dir / "script.json", script_dict)

            update_job_status(job_dir, {"status": "voice"})
            audio_path = job_dir / "voice.wav"
            voice_started = time.monotonic()
            try:
                voice_warm = _voice_checkpoint_is_usable(job_dir, script_dict, provider)
                _mark_stage_cache_state(job_dir, "voice", voice_warm)
                if voice_warm:
                    logger.info(f"[{job_id}] Reusing checkpointed {provider} voice")
                else:
                    logger.info(f"[{job_id}] Starting voice generation with {provider}")
                    await asyncio.to_thread(
                        generate_voice,
                        script_json=script_dict,
                        provider=provider,
                        output_path=str(audio_path)
                    )
                    if not audio_path.exists() or audio_path.stat().st_size == 0:
                        raise RuntimeError("Voice output is missing or empty")

                master_report = await asyncio.to_thread(master_voice_audio, audio_path)
                write_json_atomically(job_dir / "voice_checkpoint.json", {
                    "provider": provider,
                    "fingerprint": _voice_fingerprint(script_dict, provider),
                    "bytes": audio_path.stat().st_size,
                    "audio_master_version": master_report["version"],
                    "audio_peak_dbfs": master_report["after"]["peak_dbfs"],
                    "audio_full_scale_samples": master_report["after"]["full_scale_samples"],
                })
            finally:
                _record_stage_timing(job_dir, "voice", voice_started)

            if not audio_path.exists() or audio_path.stat().st_size == 0:
                raise RuntimeError("Voice output is missing or empty")

            render_input_path = job_dir / "render_input.json"
            mouth_cues_path = job_dir / "mouth_cues.json"
            render_progress = read_job_status(job_dir).get("render_progress")
            reuse_render_output = _render_checkpoint_is_usable(
                job_dir, script_dict, audio_path
            )
            _mark_stage_cache_state(job_dir, "render", bool(reuse_render_output))
            if reuse_render_output:
                logger.info(f"[{job_id}] Reusing checkpointed render contract")
            else:
                render_input = build_render_input(script_dict, str(audio_path))
                write_json_atomically(mouth_cues_path, render_input["mouthCues"])
                write_json_atomically(render_input_path, render_input)
                _write_timed_animatic(job_dir, render_input)
                _write_render_checkpoint(
                    job_dir, script_dict, audio_path, render_progress=render_progress
                )

            try:
                render_input = json.loads(render_input_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise RuntimeError("Render input contract is missing or unreadable") from exc
            if not isinstance(render_input, dict):
                raise RuntimeError("Render input contract must be an object")

            # D7 caption/audio QA is deterministic preflight. Run it before the
            # first expensive render and persist every bounded re-check.
            caption_audio_report, caption_audio_route = _run_caption_audio_preflight(
                job_dir,
                render_input,
                max_repairs=MAX_CAPTION_AUDIO_REPAIRS,
            )
            if isinstance(render_input.get("segments"), list) and render_input.get("segments"):
                if caption_audio_route and caption_audio_route["route"] == "blocked":
                    failures = caption_audio_route.get("failure_codes") or []
                    raise RuntimeError(
                        "Caption/audio technical QA failed: "
                        + ",".join(str(item) for item in failures)
                    )
                if caption_audio_route and caption_audio_route["route"] == "needs_human_review":
                    update_job_status(job_dir, {
                        "status": "needs_human_review",
                        "video_url": None,
                        "restart_resumable": False,
                    })
                    return

            qa_attempt_number = 0

            for semantic_attempt in range(1, MAX_FINAL_VISUAL_ATTEMPTS + 1):
                update_job_status(job_dir, {"status": "rendering"})
                logger.info(f"[{job_id}] Starting Remotion render, semantic attempt {semantic_attempt}")

                render_started = time.monotonic()
                existing_output = job_dir / "video.mp4"
                try:
                    if (
                        reuse_render_output
                        and existing_output.is_file()
                        and existing_output.stat().st_size > 0
                    ):
                        logger.info(f"[{job_id}] Reusing rendered output for QA")
                        output_mp4 = str(existing_output)
                    else:
                        output_mp4, render_progress = await asyncio.to_thread(
                            _render_with_configured_strategy, job_dir
                        )
                    reuse_render_output = False
                finally:
                    _record_stage_timing(job_dir, "render", render_started)

                out_path = Path(output_mp4)
                if not out_path.exists() or out_path.stat().st_size == 0:
                    raise RuntimeError("Render output is missing or empty")
                _write_render_checkpoint(
                    job_dir, script_dict, audio_path, out_path, render_progress
                )

                update_job_status(job_dir, {"status": "qa"})
                logger.info(f"[{job_id}] Running deterministic output QA")
                qa_started = time.monotonic()
                qa_report = await asyncio.to_thread(qa_job_directory, str(job_dir))
                qa_attempt_number += 1
                qa_report["attempts"] = 1
                # Persist the first report before deciding whether a bounded
                # renderer retry is warranted.  A retry must not erase the
                # evidence for the failed attempt.
                persist_qa_report(job_dir, qa_report, attempt=qa_attempt_number)
                failure_codes = set(qa_report.get("failure_codes", []))
                if failure_codes and failure_codes.issubset(_RENDER_RETRYABLE_QA_CODES):
                    logger.warning(f"[{job_id}] Retrying render after QA failure: {sorted(failure_codes)}")
                    update_job_status(job_dir, {"status": "rendering"})
                    output_mp4, render_progress = await asyncio.to_thread(
                        _render_with_configured_strategy, job_dir
                    )
                    out_path = Path(output_mp4)
                    if not out_path.exists() or out_path.stat().st_size == 0:
                        raise RuntimeError("Render retry output is missing or empty")
                    _write_render_checkpoint(
                        job_dir, script_dict, audio_path, out_path, render_progress
                    )
                    update_job_status(job_dir, {"status": "qa"})
                    qa_report = await asyncio.to_thread(qa_job_directory, str(job_dir))
                    qa_attempt_number += 1
                    qa_report["attempts"] = 2
                    persist_qa_report(job_dir, qa_report, attempt=qa_attempt_number)
                if not qa_report.get("passed"):
                    failure_codes = qa_report.get("failure_codes", [])
                    raise RuntimeError(f"Output QA failed: {','.join(failure_codes)}")

                # A production render input must carry a verified reproducibility
                # manifest. Missing/mismatched manifests are deterministic
                # integrity failures, never a renderer fallback condition.
                manifest_report = audit_output_manifest(
                    job_dir,
                    require_manifest=bool(render_input.get("segments")),
                )
                if not manifest_report.get("passed"):
                    qa_report["manifest_integrity"] = manifest_report
                    qa_report.setdefault("failure_codes", []).extend(
                        code for code in manifest_report.get("failure_codes", [])
                        if code not in qa_report.get("failure_codes", [])
                    )
                    qa_report["passed"] = False
                    persist_qa_report(job_dir, qa_report, attempt=qa_attempt_number)
                    raise RuntimeError(
                        "Output QA failed: "
                        + ",".join(str(item) for item in qa_report.get("failure_codes", []))
                    )

                logger.info(f"[{job_id}] Running Vertex final rendered-meaning QA")
                final_visual_report = await asyncio.to_thread(
                    verify_final_rendered_meaning, str(job_dir)
                )
                persist_qa_report(
                    job_dir,
                    final_visual_report,
                    attempt=semantic_attempt,
                    report_name="final_visual_qa",
                )
                _record_stage_timing(job_dir, "qa", qa_started)
                if final_visual_report.get("passed"):
                    break
                if semantic_attempt >= MAX_FINAL_VISUAL_ATTEMPTS:
                    failed = [
                        f"{item.get('segment_id')}: {', '.join(item.get('issues') or []) or 'claims not proved'}"
                        for item in final_visual_report.get("segments", [])
                        if not item.get("passed")
                    ]
                    raise RuntimeError("Final rendered visual meaning QA failed: " + "; ".join(failed))

                logger.info(f"[{job_id}] Dynamically repairing failed scenes for semantic retry")
                out_path.replace(job_dir / f"rejected-video.attempt-{semantic_attempt}.mp4")
                script_dict = await asyncio.to_thread(
                    repair_final_visual_failures, script_dict, final_visual_report, str(job_dir)
                )
                script_dict = await asyncio.to_thread(
                    ensure_relationship_modes, script_dict, str(job_dir)
                )
                write_json_atomically(job_dir / "script.json", script_dict)
                render_input = build_render_input(script_dict, str(audio_path))
                write_json_atomically(mouth_cues_path, render_input["mouthCues"])
                write_json_atomically(render_input_path, render_input)
                _write_timed_animatic(job_dir, render_input)
                _write_render_checkpoint(
                    job_dir, script_dict, audio_path, render_progress=render_progress
                )
                update_job_status(job_dir, {
                    "status": "visuals", "qa_report": None, "final_visual_qa": None,
                })

            for creative_attempt in range(1, MAX_CREATIVE_ATTEMPTS + 1):
                render_input = json.loads(render_input_path.read_text(encoding="utf-8"))
                update_job_status(job_dir, {"status": "creative_qa"})
                qa_started = time.monotonic()
                creative_report = await asyncio.to_thread(audit_creative_quality, render_input)
                _record_stage_timing(job_dir, "qa", qa_started)
                persist_qa_report(
                    job_dir,
                    creative_report,
                    attempt=creative_attempt,
                    report_name="creative_qa",
                )
                update_job_status(job_dir, {"status": "creative_qa", "creative_qa": creative_report})
                if creative_report.get("passed"):
                    break

                rendered = job_dir / "video.mp4"
                if rendered.is_file():
                    rendered.replace(job_dir / f"rejected-creative.attempt-{creative_attempt}.mp4")
                if creative_attempt >= MAX_CREATIVE_ATTEMPTS:
                    logger.error(f"[{job_id}] Creative QA failed after {creative_attempt} attempts")
                    update_job_status(job_dir, {
                        "status": "needs_human_review",
                        "video_url": None,
                        "creative_qa": creative_report,
                        "restart_resumable": False,
                    })
                    return

                logger.info(f"[{job_id}] Repairing creative QA failures for retry")
                script_dict = await asyncio.to_thread(
                    repair_creative_failures, script_dict, creative_report, str(job_dir)
                )
                write_json_atomically(job_dir / "script.json", script_dict)
                render_input = build_render_input(script_dict, str(audio_path))
                write_json_atomically(mouth_cues_path, render_input["mouthCues"])
                write_json_atomically(render_input_path, render_input)
                _write_timed_animatic(job_dir, render_input)
                _write_render_checkpoint(job_dir, script_dict, audio_path)

                update_job_status(job_dir, {"status": "rendering"})
                output_mp4, render_progress = await asyncio.to_thread(
                    _render_with_configured_strategy, job_dir
                )
                out_path = Path(output_mp4)
                if not out_path.exists() or out_path.stat().st_size == 0:
                    raise RuntimeError("Creative retry render output is missing or empty")
                _write_render_checkpoint(
                    job_dir, script_dict, audio_path, out_path, render_progress
                )
                update_job_status(job_dir, {"status": "qa"})
                qa_report = await asyncio.to_thread(qa_job_directory, str(job_dir))
                qa_report["attempts"] = 1
                qa_attempt_number += 1
                persist_qa_report(job_dir, qa_report, attempt=qa_attempt_number)
                if not qa_report.get("passed"):
                    raise RuntimeError(f"Output QA failed: {','.join(qa_report.get('failure_codes', []))}")
                manifest_report = audit_output_manifest(
                    job_dir,
                    require_manifest=bool(render_input.get("segments")),
                )
                if not manifest_report.get("passed"):
                    qa_report["manifest_integrity"] = manifest_report
                    qa_report.setdefault("failure_codes", []).extend(
                        code for code in manifest_report.get("failure_codes", [])
                        if code not in qa_report.get("failure_codes", [])
                    )
                    qa_report["passed"] = False
                    persist_qa_report(job_dir, qa_report, attempt=qa_attempt_number)
                    raise RuntimeError(
                        "Output QA failed: "
                        + ",".join(str(item) for item in qa_report.get("failure_codes", []))
                    )
                final_visual_report = await asyncio.to_thread(
                    verify_final_rendered_meaning, str(job_dir)
                )
                persist_qa_report(
                    job_dir,
                    final_visual_report,
                    attempt=semantic_attempt,
                    report_name="final_visual_qa",
                )
                if not final_visual_report.get("passed"):
                    raise RuntimeError("Final rendered visual meaning QA failed after creative repair")

            # Update status to completed
            update_job_status(job_dir, {
                "status": "completed",
                "video_url": f"/api/jobs/{job_id}/video",
                "qa_report": qa_report,
                "final_visual_qa": final_visual_report,
                "creative_qa": creative_report,
            })
            logger.info(f"[{job_id}] Pipeline completed successfully")

    except Exception as e:
        logger.exception(f"[{job_id}] Pipeline failed:")
        safe_error = "An internal error occurred during video generation."
        if str(e).startswith("Output QA failed:"):
            safe_error = str(e)
        if str(e).startswith("Caption/audio technical QA failed:"):
            safe_error = str(e)
        if str(e).startswith("Final rendered visual meaning QA failed:"):
            safe_error = str(e)
        try:
            rendered = job_dir / "video.mp4"
            final_semantic_rejection = (
                "final_visual_report" in locals()
                and isinstance(final_visual_report, dict)
                and not final_visual_report.get("passed")
            )
            if rendered.is_file() and (
                str(e).startswith("Output QA failed:") or final_semantic_rejection
            ):
                attempt_number = int(locals().get("semantic_attempt", 1))
                rendered.replace(job_dir / f"rejected-video.attempt-{attempt_number}.mp4")
            deterministic_qa_failed = (
                ("qa_report" in locals() and not qa_report.get("passed"))
                or str(e).startswith("Caption/audio technical QA failed:")
                or str(e).startswith("Render input contract")
            )
            failure_update = {
                "status": "failed",
                "error": safe_error,
                # Invalid contracts and deterministic output failures require a
                # code/input correction. Transient failures after deterministic
                # QA, and bounded final-semantic failures, can resume from the
                # persisted render and repair checkpoints.
                "restart_resumable": not isinstance(e, ValueError) and not deterministic_qa_failed,
            }
            if "qa_report" in locals():
                failure_update["qa_report"] = qa_report
            if "final_visual_report" in locals():
                failure_update["final_visual_qa"] = final_visual_report
            update_job_status(job_dir, {
                **failure_update,
            })
        except Exception:
            pass
    finally:
        telemetry_collector.persist()
        _mirror_job_telemetry_to_clickhouse(job_id, job_dir, script_dict)
        telemetry_context.__exit__(None, None, None)
        release_job_lease(job_dir, lease_token)
