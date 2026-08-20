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
from backend.mouth_cues import build_render_input
from backend.output_qa import qa_job_directory
from backend.final_visual_qa_vertex import verify_final_rendered_meaning
from backend.creative_quality import audit_creative_quality
from backend.director_context import DirectorPolicy
from backend.visual_artifact_store import (
    claim_artifact,
    fail_artifact,
    materialize_artifact,
    seal_artifact,
    visual_artifact_key,
)
from backend.paired_visuals import load_adopted_visual_plan
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


def _record_stage_timing(job_dir: Path, stage: str, started_at: float) -> None:
    elapsed = max(0.0, time.monotonic() - started_at)
    current = read_job_status(job_dir).get("stage_timings") or {}
    timings = dict(current) if isinstance(current, dict) else {}
    previous = timings.get(stage, 0.0)
    if not isinstance(previous, (int, float)) or previous < 0:
        previous = 0.0
    timings[stage] = float(round(previous + elapsed, 3))
    update_job_status(job_dir, {"stage_timings": timings})


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
                produced = plan_visual_treatments(script_dict, str(artifact_dir), policy)
                produced = ensure_relationship_modes(produced, str(artifact_dir))
                produced = generate_and_verify_visual_evidence(produced, str(artifact_dir))
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
    """Render with the opt-in segment cache, preserving a monolithic fallback."""
    if os.getenv("FYF_SEGMENT_RENDER_ENABLED", "0").strip() == "1":
        try:
            report = render_segments_and_assemble(str(job_dir))
        except Exception as exc:
            logger.warning(
                "Segmented render failed for %s; falling back to monolithic render: %s",
                job_dir,
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

async def run_pipeline(
    job_id: str,
    script_dict: Dict[str, Any],
    provider: Literal["kaggle", "gemini"],
    jobs_root: Path,
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
            if (
                os.getenv("FYF_RUNTIME_MODE", "product").strip().lower() == "hackathon"
                and provider != "gemini"
            ):
                raise ValueError("Hackathon runtime permits only the Google AI voice route")
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
            if not local_visual_repair:
                adopted_visual_script = load_adopted_visual_plan(job_dir)
                if adopted_visual_script is not None:
                    logger.info(
                        "[%s] Reusing integrity-checked visual plan from paired voice job",
                        job_id,
                    )
                    script_dict = adopted_visual_script
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
                if _voice_checkpoint_is_usable(job_dir, script_dict, provider):
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
            if reuse_render_output:
                logger.info(f"[{job_id}] Reusing checkpointed render contract")
            else:
                render_input = build_render_input(script_dict, str(audio_path))
                write_json_atomically(mouth_cues_path, render_input["mouthCues"])
                write_json_atomically(render_input_path, render_input)
                _write_render_checkpoint(
                    job_dir, script_dict, audio_path, render_progress=render_progress
                )

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
                qa_report["attempts"] = 1
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
                    qa_report["attempts"] = 2
                write_json_atomically(job_dir / "qa_report.json", qa_report)
                if not qa_report.get("passed"):
                    failure_codes = qa_report.get("failure_codes", [])
                    raise RuntimeError(f"Output QA failed: {','.join(failure_codes)}")

                logger.info(f"[{job_id}] Running Vertex final rendered-meaning QA")
                final_visual_report = await asyncio.to_thread(
                    verify_final_rendered_meaning, str(job_dir)
                )
                write_json_atomically(job_dir / "final_visual_qa.json", final_visual_report)
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
                write_json_atomically(
                    job_dir / f"final_visual_qa.attempt-{semantic_attempt}.json",
                    final_visual_report,
                )
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
                write_json_atomically(job_dir / "creative_qa.json", creative_report)
                update_job_status(job_dir, {"status": "creative_qa", "creative_qa": creative_report})
                if creative_report.get("passed"):
                    break

                write_json_atomically(
                    job_dir / f"creative_qa.attempt-{creative_attempt}.json",
                    creative_report,
                )
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
                write_json_atomically(job_dir / "qa_report.json", qa_report)
                if not qa_report.get("passed"):
                    raise RuntimeError(f"Output QA failed: {','.join(qa_report.get('failure_codes', []))}")
                final_visual_report = await asyncio.to_thread(
                    verify_final_rendered_meaning, str(job_dir)
                )
                write_json_atomically(job_dir / "final_visual_qa.json", final_visual_report)
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
                "qa_report" in locals() and not qa_report.get("passed")
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
        telemetry_context.__exit__(None, None, None)
        release_job_lease(job_dir, lease_token)
