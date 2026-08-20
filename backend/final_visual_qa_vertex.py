"""Final rendered-frame semantic QA owned by Vertex multimodal verification."""

import hashlib
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

from google import genai
from google.genai import types
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from video_contract import VideoScript
from vertex_model_routing import model_for
from visual_evidence_vertex import DEFAULT_LOCATION, EvidenceVerification, _quota_retry
from backend.vertex_client import vertex_client_kwargs
from backend.job_store import write_json_atomically
from backend.job_store import update_job_status
from backend.vertex_telemetry import track_client
from backend.vertex_thinking import generation_config_for


CHECKPOINT_FILENAME = "final_visual_qa_checkpoint.json"
QA_CHECKPOINT_VERSION = 2
QA_PROMPT_VERSION = 1
_SAFE_SEGMENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class FinalVisualBatchItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    segment_id: str
    passed: bool
    proved_claim_ids: list[str]
    observed_values: list[str]
    issues: list[str]


class FinalVisualBatchResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[FinalVisualBatchItem] = Field(min_length=1, max_length=6)


def _final_qa_batch_size() -> int:
    raw = os.getenv("FYF_FINAL_QA_BATCH_SIZE", "4")
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("FYF_FINAL_QA_BATCH_SIZE must be an integer from 1 to 6") from exc
    if not 1 <= value <= 6:
        raise ValueError("FYF_FINAL_QA_BATCH_SIZE must be an integer from 1 to 6")
    return value


def _qa_fingerprint(root: Path) -> str:
    digest = hashlib.sha256()
    digest.update(model_for("visual_verification").encode("utf-8"))
    digest.update(model_for("visual_verification_fallback").encode("utf-8"))
    for name in ("script.json", "render_input.json", "video.mp4"):
        digest.update(name.encode("utf-8"))
        with (root / name).open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def segment_qa_fingerprint(
    *,
    segment_id: str,
    claims: list[dict],
    render_segment: dict,
    media_fingerprint: str,
    media_source: str,
) -> str:
    """Hash only the semantic-QA inputs for one rendered scene."""
    if not isinstance(segment_id, str) or not segment_id:
        raise ValueError("segment_id must be a non-blank string")
    if not isinstance(media_fingerprint, str) or not re.fullmatch(
        r"[0-9a-f]{64}", media_fingerprint
    ):
        raise ValueError("media_fingerprint must be a lowercase SHA-256 hex digest")
    payload = {
        "qa_checkpoint_version": QA_CHECKPOINT_VERSION,
        "qa_prompt_version": QA_PROMPT_VERSION,
        "primary_model": model_for("visual_verification"),
        "fallback_model": model_for("visual_verification_fallback"),
        "segment_id": segment_id,
        "locked_evidence_claims": claims,
        "render_input_segment": render_segment,
        "media_source": media_source,
        "media_fingerprint": media_fingerprint,
    }
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _render_progress_strategy(root: Path) -> str:
    try:
        status = json.loads((root / "status.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        status = {}
    progress = status.get("render_progress")
    if isinstance(progress, dict):
        strategy = progress.get("strategy")
        if strategy in {"monolithic", "monolithic-fallback"}:
            return "monolithic"
        if strategy == "segmented":
            return "segmented"
    return "segmented" if os.getenv("FYF_SEGMENT_RENDER_ENABLED", "0").strip() == "1" else "monolithic"


def _cached_segment_media(root: Path, segment_id: str) -> Path | None:
    if not _SAFE_SEGMENT_ID.fullmatch(segment_id):
        return None
    cache_root = (root / "render-segments").resolve()
    if not cache_root.is_dir():
        return None

    checkpoint_path = root / "segment_render_checkpoint.json"
    try:
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(checkpoint, dict):
        return None
    if checkpoint.get("version") != 1 or checkpoint.get("complete") is not True:
        return None
    entries = checkpoint.get("segments")
    if not isinstance(entries, list):
        return None
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("segment_id") != segment_id:
            continue
        raw_path = entry.get("path")
        if not isinstance(raw_path, str) or Path(raw_path).is_absolute():
            return None
        candidate = (root / raw_path).resolve()
        try:
            candidate.relative_to(cache_root)
        except ValueError:
            return None
        if (
            candidate.is_file()
            and candidate.stat().st_size > 0
            and entry.get("complete") is True
            and entry.get("size_bytes") == candidate.stat().st_size
            and entry.get("sha256") == _sha256_file(candidate)
        ):
            return candidate
        return None
    return None


def _qa_source(root: Path, segment_id: str) -> tuple[Path, str]:
    if _render_progress_strategy(root) == "segmented":
        cached = _cached_segment_media(root, segment_id)
        if cached is not None:
            return cached, "cached-segment"
    return root / "video.mp4", "full-video"


def _segment_media_fingerprint(
    media_path: Path,
    *,
    media_source: str,
    render_segment: dict,
    fps: int | float,
) -> str:
    if media_source == "cached-segment":
        return _sha256_file(media_path)
    payload = {
        "full_video_sha256": _sha256_file(media_path),
        "start_frame": render_segment.get("startFrame"),
        "end_frame": render_segment.get("endFrame"),
        "fps": fps,
    }
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _request_verification(client: genai.Client, parts: list, model: str, label: str) -> EvidenceVerification:
    response = _quota_retry(lambda: client.models.generate_content(
        model=model,
        contents=parts,
        config=generation_config_for("final_visual_qa",
            response_mime_type="application/json",
            response_json_schema=EvidenceVerification.model_json_schema(),
        ),
    ), label=label)
    return EvidenceVerification.model_validate_json(response.text or "")


def _validate_batch_response(
    response: FinalVisualBatchResponse,
    expected_segment_ids: list[str],
) -> FinalVisualBatchResponse:
    if not isinstance(response, FinalVisualBatchResponse):
        try:
            response = FinalVisualBatchResponse.model_validate(response)
        except ValidationError as exc:
            raise ValueError("Final visual QA batch response schema is invalid") from exc
    response_ids = [item.segment_id for item in response.items]
    if (
        len(response_ids) != len(set(response_ids))
        or set(response_ids) != set(expected_segment_ids)
        or len(response_ids) != len(expected_segment_ids)
    ):
        raise ValueError("Final visual QA batch response IDs do not match the requested batch")
    return response


def _request_batch_verification(
    client: genai.Client,
    parts: list,
    expected_segment_ids: list[str],
) -> FinalVisualBatchResponse:
    response = _quota_retry(lambda: client.models.generate_content(
        model=model_for("visual_verification"),
        contents=parts,
        config=generation_config_for("final_visual_qa",
            response_mime_type="application/json",
            response_json_schema=FinalVisualBatchResponse.model_json_schema(),
        ),
    ), label=f"final rendered QA batch {','.join(expected_segment_ids)}")
    try:
        parsed = FinalVisualBatchResponse.model_validate_json(response.text or "")
    except ValidationError as exc:
        raise ValueError("Final visual QA batch response JSON is invalid") from exc
    return _validate_batch_response(parsed, expected_segment_ids)


def _scene_prompt(segment_id: str, claims: list[dict]) -> str:
    return (
        f"QA contract version {QA_PROMPT_VERSION}. Segment ID: {segment_id}. "
        "Act as the final FYF rendered-video evidence gate. The attached images are "
        "chronological frames from one narrated segment. Pass only when an ordinary "
        "Burmese-speaking beginner can directly understand every locked claim from the actual final "
        "composition. Reject missing values, wrong counts, misleading relations, "
        "duplicate/overlapping content, unreadable focal evidence, or a claim visible "
        "only in a decorative caption. Burmese script is the required audience language "
        "and must never be rejected merely for not being English. Reject English or other "
        "foreign-language explanatory text generated inside the visual when it is needed "
        "to understand the comparison, relationship, value, or action. Allow only established "
        "technical names such as AI, XAI, API, or a proper name. Generated media should be "
        "text-free whenever the deterministic Burmese overlay carries the explanation. "
        "Repeated sampled frames are acceptable for a deliberately held static shot; reject "
        "duplication only when elements overlap or contradict each other.\nLocked claims: "
        + json.dumps(claims, ensure_ascii=False)
    )


def _safe_frame_stem(segment_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", segment_id)


def _build_scene_parts(
    segment_id: str,
    claims: list[dict],
    media_path: Path,
    sample_times: list[float],
    temp: Path,
) -> list:
    parts = [_scene_prompt(segment_id, claims)]
    for index, timestamp in enumerate(sample_times):
        frame = temp / f"{_safe_frame_stem(segment_id)}-{index}.jpg"
        _extract_frame(media_path, timestamp, frame)
        parts.append(types.Part.from_bytes(data=frame.read_bytes(), mime_type="image/jpeg"))
    return parts


def _build_batch_parts(
    scene_parts: dict[str, list],
    scene_data: dict[str, dict],
    batch_ids: list[str],
) -> list:
    parts = [
        f"QA contract version {QA_PROMPT_VERSION}. Verify every requested segment exactly once. "
        "Return one strict JSON item per segment, preserving the requested order. "
        "Each delimited segment below has locked claims followed by three chronological frames. "
        "Requested segments: " + ", ".join(f"SEGMENT {segment_id}" for segment_id in batch_ids)
    ]
    for segment_id in batch_ids:
        parts.append(
            f"--- SEGMENT {segment_id} START ---\n"
            f"Locked claims: {json.dumps(scene_data[segment_id]['claims'], ensure_ascii=False)}\n"
            "The next three images are chronological frames for this segment."
        )
        parts.extend(scene_parts[segment_id][1:])
        parts.append(f"--- SEGMENT {segment_id} END ---")
    return parts


def _verification_is_complete(verification: EvidenceVerification | FinalVisualBatchItem, expected: set[str]) -> bool:
    proved = verification.proved_claim_ids
    return bool(verification.passed) and len(proved) == len(set(proved)) and set(proved) == expected


def _verify_scene_individually(
    client: genai.Client,
    parts: list,
    expected: set[str],
    segment_id: str,
) -> EvidenceVerification:
    used_fallback = False
    try:
        verification = _request_verification(
            client,
            parts,
            model_for("visual_verification"),
            f"final rendered QA {segment_id}",
        )
    except Exception:
        used_fallback = True
        verification = _request_verification(
            client,
            parts,
            model_for("visual_verification_fallback"),
            f"final rendered QA Pro fallback {segment_id}",
        )
    if not used_fallback and not _verification_is_complete(verification, expected):
        verification = _request_verification(
            client,
            parts,
            model_for("visual_verification_fallback"),
            f"final rendered QA Pro adjudication {segment_id}",
        )
    return verification


def _persist_qa_progress(root: Path, progress: dict[str, int]) -> None:
    if (root / "status.json").is_file():
        update_job_status(root, {"qa_progress": progress})


def _load_checkpoint(
    path: Path,
    expected_segment_ids: list[str],
    fingerprints: dict[str, str],
) -> dict[str, dict]:
    try:
        checkpoint = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if checkpoint.get("version") != QA_CHECKPOINT_VERSION:
        return {}
    results = checkpoint.get("results")
    completed = checkpoint.get("completed_segment_ids")
    if not isinstance(results, list) or not isinstance(completed, list):
        return {}
    expected = set(expected_segment_ids)
    result_map: dict[str, dict] = {}
    for result in results:
        if not isinstance(result, dict):
            return {}
        segment_id = result.get("segment_id")
        if segment_id not in expected or segment_id in result_map:
            return {}
        segment_fingerprint = result.get("segment_fingerprint")
        if not isinstance(segment_fingerprint, str) or not re.fullmatch(
            r"[0-9a-f]{64}", segment_fingerprint
        ):
            return {}
        result_map[segment_id] = result
    if completed != [
        segment_id for segment_id in expected_segment_ids if segment_id in result_map
    ]:
        return {}
    if checkpoint.get("complete") is not (len(result_map) == len(expected_segment_ids)):
        return {}
    if checkpoint.get("total_segment_count") != len(expected_segment_ids):
        return {}
    return {
        segment_id: result
        for segment_id, result in result_map.items()
        if fingerprints.get(segment_id) == result.get("segment_fingerprint")
    }


def _write_checkpoint(
    path: Path,
    expected_segment_ids: list[str],
    result_map: dict[str, dict],
) -> None:
    ordered_results = [
        result_map[segment_id]
        for segment_id in expected_segment_ids
        if segment_id in result_map
    ]
    write_json_atomically(path, {
        "version": QA_CHECKPOINT_VERSION,
        "qa_prompt_version": QA_PROMPT_VERSION,
        "complete": len(ordered_results) == len(expected_segment_ids),
        "completed_segment_ids": [result["segment_id"] for result in ordered_results],
        "total_segment_count": len(expected_segment_ids),
        "results": ordered_results,
    })


def _client() -> genai.Client:
    try:
        timeout_seconds = int(os.getenv("FYF_VERTEX_CALL_TIMEOUT_SECONDS", "120"))
    except ValueError:
        timeout_seconds = 120
    timeout_ms = max(10, min(300, timeout_seconds)) * 1000
    client = genai.Client(
        **vertex_client_kwargs(location=os.getenv("FYF_VERTEX_MEDIA_LOCATION", DEFAULT_LOCATION)),
        http_options=types.HttpOptions(timeout=timeout_ms),
    )
    return track_client(client, stage="final_visual_qa")


def _extract_frame(video_path: Path, seconds: float, output: Path) -> None:
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-ss", f"{seconds:.3f}", "-i", str(video_path),
            "-frames:v", "1", "-vf", "scale=540:-2", "-y", str(output),
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    if not output.is_file() or output.stat().st_size == 0:
        raise RuntimeError(f"Could not extract final QA frame at {seconds:.3f}s")


def verify_final_rendered_meaning(job_dir: str) -> dict:
    """Fail closed unless sampled final frames visibly prove each locked segment claim."""
    root = Path(job_dir)
    script = VideoScript.model_validate_json((root / "script.json").read_text(encoding="utf-8"))
    render_input = json.loads((root / "render_input.json").read_text(encoding="utf-8"))
    timing = {str(segment["id"]): segment for segment in render_input["segments"]}
    expected_segment_ids = [segment.id for segment in script.segments]
    fps = render_input["fps"]
    sources: dict[str, tuple[Path, str, list[float], str]] = {}
    scene_data: dict[str, dict] = {}
    fingerprints: dict[str, str] = {}
    for segment in script.segments:
        segment_timing = timing.get(segment.id)
        if not segment_timing:
            raise ValueError(f"Final visual QA has no render timing for segment {segment.id}")
        media_path, media_source = _qa_source(root, segment.id)
        if not media_path.is_file():
            raise FileNotFoundError(f"Final visual QA media is missing for segment {segment.id}: {media_path}")
        start = segment_timing["startFrame"] / fps
        end = segment_timing["endFrame"] / fps
        span = max(0.05, end - start)
        sample_times = [
            span * fraction if media_source == "cached-segment" else start + span * fraction
            for fraction in (0.2, 0.5, 0.8)
        ]
        claims = [claim.model_dump(mode="json") for claim in segment.visual.evidence_claims]
        media_fingerprint = _segment_media_fingerprint(
            media_path,
            media_source=media_source,
            render_segment=segment_timing,
            fps=fps,
        )
        fingerprints[segment.id] = segment_qa_fingerprint(
            segment_id=segment.id,
            claims=claims,
            render_segment=segment_timing,
            media_fingerprint=media_fingerprint,
            media_source=media_source,
        )
        sources[segment.id] = (media_path, media_source, sample_times, fingerprints[segment.id])
        scene_data[segment.id] = {
            "claims": claims,
            "expected": {claim["claim_id"] for claim in claims},
        }

    checkpoint_path = root / CHECKPOINT_FILENAME
    result_map = _load_checkpoint(checkpoint_path, expected_segment_ids, fingerprints)
    qa_progress = {
        "total": len(expected_segment_ids),
        "verified": len(result_map),
        "cache_hits": len(result_map),
        "batches": 0,
    }
    _persist_qa_progress(root, qa_progress)
    batch_size = _final_qa_batch_size()
    client = _client() if len(result_map) < len(expected_segment_ids) else None
    unfinished_ids = [
        segment_id for segment_id in expected_segment_ids if segment_id not in result_map
    ]

    with tempfile.TemporaryDirectory(prefix="fyf-final-visual-qa-") as temp_dir:
        temp = Path(temp_dir)
        for offset in range(0, len(unfinished_ids), batch_size):
            batch_ids = unfinished_ids[offset:offset + batch_size]
            scene_parts: dict[str, list] = {}
            for segment_id in batch_ids:
                segment = next(item for item in script.segments if item.id == segment_id)
                media_path, _media_source, sample_times, _segment_fingerprint = sources[segment_id]
                scene_parts[segment_id] = _build_scene_parts(
                    segment_id,
                    scene_data[segment_id]["claims"],
                    media_path,
                    sample_times,
                    temp,
                )
            batch_parts = _build_batch_parts(scene_parts, scene_data, batch_ids)

            batch_results: dict[str, dict] = {}
            try:
                batch_response = _request_batch_verification(client, batch_parts, batch_ids)
                batch_response = _validate_batch_response(batch_response, batch_ids)
            except Exception:
                # A transport/schema failure falls back to the existing individual
                # verifier for this batch only. No partial batch is checkpointed.
                for segment_id in batch_ids:
                    verification = _verify_scene_individually(
                        client,
                        scene_parts[segment_id],
                        scene_data[segment_id]["expected"],
                        segment_id,
                    )
                    media_path, media_source, sample_times, segment_fingerprint = sources[segment_id]
                    batch_results[segment_id] = {
                        "segment_id": segment_id,
                        "segment_fingerprint": segment_fingerprint,
                        "passed": _verification_is_complete(
                            verification, scene_data[segment_id]["expected"]
                        ),
                        "proved_claim_ids": verification.proved_claim_ids,
                        "observed_values": verification.observed_values,
                        "issues": verification.issues,
                        "sample_times": sample_times,
                        "media_source": media_source,
                    }
            else:
                items = {item.segment_id: item for item in batch_response.items}
                for segment_id in batch_ids:
                    item = items[segment_id]
                    expected = scene_data[segment_id]["expected"]
                    if _verification_is_complete(item, expected):
                        verification = item
                    else:
                        verification = _request_verification(
                            client,
                            scene_parts[segment_id],
                            model_for("visual_verification_fallback"),
                            f"final rendered QA Pro adjudication {segment_id}",
                        )
                    _media_path, media_source, sample_times, segment_fingerprint = sources[segment_id]
                    batch_results[segment_id] = {
                        "segment_id": segment_id,
                        "segment_fingerprint": segment_fingerprint,
                        "passed": _verification_is_complete(verification, expected),
                        "proved_claim_ids": verification.proved_claim_ids,
                        "observed_values": verification.observed_values,
                        "issues": verification.issues,
                        "sample_times": sample_times,
                        "media_source": media_source,
                    }

            result_map.update(batch_results)
            _write_checkpoint(checkpoint_path, expected_segment_ids, result_map)
            qa_progress["verified"] = len(result_map)
            qa_progress["batches"] += 1
            _persist_qa_progress(root, qa_progress)

    results = [result_map[segment_id] for segment_id in expected_segment_ids]
    failed = [result for result in results if not result["passed"]]
    return {"passed": not failed, "segments": results}
