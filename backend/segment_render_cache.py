"""Content-addressed cache primitives for deterministic video segments.

The cache deliberately knows nothing about a topic or a particular job's
narration.  A segment is reusable only when its local render contract and the
bytes of its referenced assets still match the stored checkpoint.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any, Mapping, Sequence

from backend.job_store import write_json_atomically
from backend.render_contract import validate_render_input
from backend.render_video import (
    REMOTION_COMPOSITION_ID,
    REPO_ROOT,
    render_video_segment,
)


CACHE_CONTRACT_VERSION = 1
SEGMENT_CHECKPOINT_FILENAME = "segment_render_checkpoint.json"
_SEGMENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


@dataclass(frozen=True)
class SegmentRenderResult:
    segment_id: str
    fingerprint: str
    path: Path
    cache_hit: bool
    frame_count: int


@dataclass(frozen=True)
class RenderAssemblyReport:
    output_path: Path
    total_segments: int
    rendered_segments: int
    cache_hits: int
    manifest_fingerprint: str


def normalize_segment_id(segment_id: str) -> str:
    """Return a path-safe segment ID or fail closed.

    Segment IDs are data, not shell or filesystem syntax.  Rejecting path
    separators and dot components is safer than trying to repair a caller's
    identity silently.
    """

    if not isinstance(segment_id, str) or not segment_id.strip():
        raise ValueError("segment ID must be a non-blank string")
    value = segment_id.strip()
    if value in {".", ".."} or "/" in value or "\\" in value:
        raise ValueError(f"unsafe segment ID: {segment_id!r}")
    if not _SEGMENT_ID_PATTERN.fullmatch(value):
        raise ValueError(f"unsafe segment ID: {segment_id!r}")
    return value


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _segment_identifier(segment: Mapping[str, Any]) -> str | None:
    value = segment.get("id")
    if value is None:
        value = segment.get("segment_id")
    return value if isinstance(value, str) else None


def _find_segment(render_input: Mapping[str, Any], segment_id: str) -> Mapping[str, Any]:
    segments = render_input.get("segments")
    if not isinstance(segments, list):
        raise ValueError("render input segments must be a list")

    target = normalize_segment_id(segment_id)
    matches: list[Mapping[str, Any]] = []
    for segment in segments:
        if not isinstance(segment, Mapping):
            raise ValueError("render input segment must be an object")
        identifier = _segment_identifier(segment)
        if identifier is not None and identifier == target:
            matches.append(segment)
    if len(matches) != 1:
        raise ValueError(f"render input must contain exactly one segment with ID {target!r}")
    return matches[0]


def _intersecting_mouth_cues(
    render_input: Mapping[str, Any], segment: Mapping[str, Any]
) -> list[Mapping[str, Any]]:
    fps = render_input.get("fps")
    start_frame = segment.get("startFrame")
    end_frame = segment.get("endFrame")
    if not isinstance(fps, (int, float)) or isinstance(fps, bool) or fps <= 0:
        raise ValueError("render input fps must be positive")
    if (
        not isinstance(start_frame, int)
        or isinstance(start_frame, bool)
        or not isinstance(end_frame, int)
        or isinstance(end_frame, bool)
        or end_frame <= start_frame
    ):
        raise ValueError("segment frame range must be a positive interval")

    segment_start = start_frame / float(fps)
    segment_end = end_frame / float(fps)
    cues = render_input.get("mouthCues", [])
    if not isinstance(cues, list):
        raise ValueError("render input mouthCues must be a list")

    selected: list[Mapping[str, Any]] = []
    for cue in cues:
        if not isinstance(cue, Mapping):
            raise ValueError("mouth cue must be an object")
        cue_start = cue.get("start")
        cue_end = cue.get("end")
        if (
            not isinstance(cue_start, (int, float))
            or isinstance(cue_start, bool)
            or not isinstance(cue_end, (int, float))
            or isinstance(cue_end, bool)
        ):
            raise ValueError("mouth cue start and end must be numeric")
        if cue_end > segment_start and cue_start < segment_end:
            selected.append(cue)
    return selected


def _asset_hashes(asset_paths: Sequence[str | Path]) -> list[dict[str, str]]:
    assets: list[dict[str, str]] = []
    for raw_path in asset_paths:
        path = Path(raw_path)
        if not path.is_file():
            raise FileNotFoundError(f"referenced render asset not found: {path}")
        assets.append({"name": path.name, "sha256": _sha256_file(path)})
    return sorted(assets, key=lambda item: (item["name"], item["sha256"]))


def segment_render_fingerprint(
    render_input: Mapping[str, Any],
    *,
    segment_id: str,
    renderer_source_hash: str,
    remotion_version: str,
    composition_id: str,
    output_settings: Mapping[str, Any] | None = None,
    asset_paths: Sequence[str | Path] = (),
) -> str:
    """Hash only the inputs that can change one segment's rendered bytes."""

    target_id = normalize_segment_id(segment_id)
    segment = _find_segment(render_input, target_id)
    if not isinstance(renderer_source_hash, str) or not renderer_source_hash:
        raise ValueError("renderer_source_hash must be a non-blank string")
    if not isinstance(remotion_version, str) or not remotion_version:
        raise ValueError("remotion_version must be a non-blank string")
    if not isinstance(composition_id, str) or not composition_id:
        raise ValueError("composition_id must be a non-blank string")

    payload = {
        "cache_contract_version": CACHE_CONTRACT_VERSION,
        "segment_id": target_id,
        "composition_id": composition_id,
        "remotion_version": remotion_version,
        "renderer_source_hash": renderer_source_hash,
        "output_settings": dict(output_settings or {}),
        "render_fps": render_input.get("fps"),
        "render_width": render_input.get("width"),
        "render_height": render_input.get("height"),
        "frame_range": [segment.get("startFrame"), segment.get("endFrame")],
        "segment": segment,
        "mouth_cues": _intersecting_mouth_cues(render_input, segment),
        "referenced_assets": _asset_hashes(asset_paths),
    }
    encoded = _canonical_json(payload).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _job_dir_path(job_dir: str | Path) -> Path:
    path = Path(job_dir)
    if not path.is_dir():
        raise FileNotFoundError(f"job directory not found: {path}")
    return path


def _cache_root(job_dir: Path) -> Path:
    root = job_dir / "render-segments"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _resolve_cache_path(
    job_dir: Path,
    raw_path: str | Path,
    *,
    allow_absolute: bool = False,
) -> Path:
    if not isinstance(raw_path, (str, Path)):
        raise ValueError("segment checkpoint path must be a string")
    raw = Path(raw_path)
    if raw.is_absolute() and not allow_absolute:
        raise ValueError("segment checkpoint path must be relative")
    cache_root = _cache_root(job_dir).resolve()
    candidate = (job_dir / raw).resolve()
    try:
        candidate.relative_to(cache_root)
    except ValueError as exc:
        raise ValueError("segment checkpoint path escapes render-segments") from exc
    return candidate


def _checkpoint_entry_from_result(job_dir: Path, result: SegmentRenderResult) -> dict[str, Any]:
    segment_id = normalize_segment_id(result.segment_id)
    if not isinstance(result.fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", result.fingerprint):
        raise ValueError("segment fingerprint must be a lowercase SHA-256 hex digest")
    if not isinstance(result.frame_count, int) or isinstance(result.frame_count, bool) or result.frame_count <= 0:
        raise ValueError("segment frame_count must be a positive integer")

    path = _resolve_cache_path(job_dir, result.path, allow_absolute=True)
    if not path.is_file():
        raise FileNotFoundError(f"segment render does not exist: {path}")
    return {
        "segment_id": segment_id,
        "fingerprint": result.fingerprint,
        "path": path.relative_to(job_dir.resolve()).as_posix(),
        "frame_count": result.frame_count,
        "size_bytes": path.stat().st_size,
        "sha256": _sha256_file(path),
        "complete": True,
    }


def write_segment_checkpoint(
    job_dir: str | Path,
    segments: Sequence[SegmentRenderResult],
    *,
    complete: bool,
    manifest_fingerprint: str | None = None,
    video_path: str | Path | None = None,
) -> dict[str, Any]:
    """Atomically persist ordered segment integrity metadata."""

    root = _job_dir_path(job_dir)
    if not isinstance(complete, bool):
        raise ValueError("checkpoint complete must be a boolean")
    raw_segment_ids = [normalize_segment_id(result.segment_id) for result in segments]
    if len(raw_segment_ids) != len(set(raw_segment_ids)):
        raise ValueError("checkpoint contains duplicate segment IDs")
    entries = [_checkpoint_entry_from_result(root, result) for result in segments]
    segment_ids = [entry["segment_id"] for entry in entries]
    if len(segment_ids) != len(set(segment_ids)):
        raise ValueError("checkpoint contains duplicate segment IDs")
    if manifest_fingerprint is not None and not re.fullmatch(r"[0-9a-f]{64}", manifest_fingerprint):
        raise ValueError("manifest_fingerprint must be a lowercase SHA-256 hex digest")
    if video_path is not None:
        if not complete:
            raise ValueError("video seal is only valid for a complete checkpoint")
        final_video = Path(video_path).resolve()
        if not final_video.is_file() or final_video.stat().st_size <= 0:
            raise ValueError("complete checkpoint video must be a non-empty file")

    checkpoint: dict[str, Any] = {
        "version": CACHE_CONTRACT_VERSION,
        "complete": complete,
        "segment_ids": segment_ids,
        "segments": entries,
    }
    if manifest_fingerprint is not None:
        checkpoint["manifest_fingerprint"] = manifest_fingerprint
    if video_path is not None:
        checkpoint["video_bytes"] = final_video.stat().st_size
        checkpoint["video_sha256"] = _sha256_file(final_video)
    write_json_atomically(root / SEGMENT_CHECKPOINT_FILENAME, checkpoint)
    return checkpoint


def _read_checkpoint(job_dir: Path) -> dict[str, Any] | None:
    path = job_dir / SEGMENT_CHECKPOINT_FILENAME
    if not path.is_file():
        return None
    try:
        checkpoint = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("invalid segment render checkpoint JSON") from exc
    if not isinstance(checkpoint, dict):
        raise ValueError("segment render checkpoint must be an object")
    if checkpoint.get("version") != CACHE_CONTRACT_VERSION:
        raise ValueError("unsupported segment render checkpoint version")
    if not isinstance(checkpoint.get("complete"), bool):
        raise ValueError("segment render checkpoint completion state is invalid")
    entries = checkpoint.get("segments")
    if not isinstance(entries, list):
        raise ValueError("segment render checkpoint segments must be a list")
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("segment render checkpoint entry must be an object")
        segment_id = normalize_segment_id(entry.get("segment_id"))
        if segment_id in seen:
            raise ValueError(f"checkpoint contains duplicate segment ID: {segment_id}")
        seen.add(segment_id)
        _resolve_cache_path(job_dir, entry.get("path"))
    if checkpoint.get("segment_ids") != [entry["segment_id"] for entry in entries]:
        raise ValueError("segment render checkpoint order does not match segment IDs")
    return checkpoint


def load_reusable_segment(
    job_dir: str | Path,
    *,
    segment_id: str,
    fingerprint: str,
    expected_frame_count: int | None = None,
) -> SegmentRenderResult | None:
    """Return an integrity-checked cache hit, otherwise ``None``."""

    root = _job_dir_path(job_dir)
    target_id = normalize_segment_id(segment_id)
    if not isinstance(fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
        raise ValueError("segment fingerprint must be a lowercase SHA-256 hex digest")
    checkpoint = _read_checkpoint(root)
    if checkpoint is None:
        return None

    entry = next(
        (candidate for candidate in checkpoint["segments"] if candidate["segment_id"] == target_id),
        None,
    )
    if entry is None or entry.get("complete") is not True:
        return None
    if entry.get("fingerprint") != fingerprint:
        return None
    frame_count = entry.get("frame_count")
    if not isinstance(frame_count, int) or isinstance(frame_count, bool) or frame_count <= 0:
        return None
    if expected_frame_count is not None and frame_count != expected_frame_count:
        return None

    path = _resolve_cache_path(root, entry.get("path"))
    try:
        stat = path.stat()
    except OSError:
        return None
    if not path.is_file() or stat.st_size <= 0:
        return None
    if entry.get("size_bytes") != stat.st_size:
        return None
    expected_sha = entry.get("sha256")
    if not isinstance(expected_sha, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_sha):
        return None
    if _sha256_file(path) != expected_sha:
        return None
    return SegmentRenderResult(
        segment_id=target_id,
        fingerprint=fingerprint,
        path=path,
        cache_hit=True,
        frame_count=frame_count,
    )


FFMPEG_BIN = shutil.which("ffmpeg") or "ffmpeg"
FFPROBE_BIN = shutil.which("ffprobe") or "ffprobe"


def _segment_render_concurrency() -> int:
    raw = os.environ.get("FYF_SEGMENT_RENDER_CONCURRENCY", "2")
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("FYF_SEGMENT_RENDER_CONCURRENCY must be an integer from 1 to 4") from exc
    if not 1 <= value <= 4:
        raise ValueError("FYF_SEGMENT_RENDER_CONCURRENCY must be an integer from 1 to 4")
    return value


def segment_cache_path(job_dir: str | Path, segment_id: str, fingerprint: str) -> Path:
    root = _job_dir_path(job_dir)
    safe_id = normalize_segment_id(segment_id)
    if not isinstance(fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
        raise ValueError("segment fingerprint must be a lowercase SHA-256 hex digest")
    return _cache_root(root) / f"{safe_id}-{fingerprint[:16]}.mp4"


def _parse_rate(value: Any) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if not isinstance(value, str) or not value.strip():
        raise ValueError("FPS is missing from FFprobe output")
    try:
        if "/" in value:
            numerator, denominator = value.split("/", 1)
            return float(Fraction(int(numerator), int(denominator)))
        return float(value)
    except (ValueError, ZeroDivisionError) as exc:
        raise ValueError(f"FPS is invalid in FFprobe output: {value!r}") from exc


def _ffprobe_json(path: Path, *, count_frames: bool) -> dict[str, Any]:
    if not path.is_file() or path.stat().st_size <= 0:
        raise ValueError(f"media file is missing or empty: {path}")
    command = [
        FFPROBE_BIN,
        "-v",
        "error",
    ]
    if count_frames:
        command.append("-count_frames")
    command.extend([
        "-print_format",
        "json",
        "-show_streams",
        "-show_format",
        str(path),
    ])
    try:
        completed = subprocess.run(
            command,
            shell=False,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ValueError(f"FFprobe failed for media file: {path}") from exc
    try:
        payload = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError(f"FFprobe returned invalid JSON for media file: {path}") from exc
    if not isinstance(payload, dict):
        raise ValueError("FFprobe response must be an object")
    return payload


def validate_segment_media(
    path: str | Path,
    expected_frames: int,
    fps: int | float,
    width: int,
    height: int,
) -> dict[str, Any]:
    """Require the exact muted H.264 contract used by segment assembly."""

    media_path = Path(path).resolve()
    payload = _ffprobe_json(media_path, count_frames=True)
    streams = payload.get("streams")
    if not isinstance(streams, list):
        raise ValueError("FFprobe response has no video stream")
    video_streams = [stream for stream in streams if isinstance(stream, dict) and stream.get("codec_type") == "video"]
    if len(video_streams) != 1:
        raise ValueError("segment must contain exactly one video stream")
    if any(isinstance(stream, dict) and stream.get("codec_type") == "audio" for stream in streams):
        raise ValueError("segment must not contain an audio stream")

    video = video_streams[0]
    if video.get("codec_name") != "h264":
        raise ValueError("segment codec must be h264")
    if video.get("width") != width:
        raise ValueError(f"segment width must be {width}")
    if video.get("height") != height:
        raise ValueError(f"segment height must be {height}")
    if video.get("pix_fmt") != "yuv420p":
        raise ValueError("segment pixel format must be yuv420p")
    observed_fps = _parse_rate(video.get("avg_frame_rate") or video.get("r_frame_rate"))
    if abs(observed_fps - float(fps)) > 1e-6:
        raise ValueError(f"segment FPS must be {fps}")
    raw_frame_count = video.get("nb_read_frames") or video.get("nb_frames")
    try:
        frame_count = int(raw_frame_count)
    except (TypeError, ValueError) as exc:
        raise ValueError("segment frame count is missing or invalid") from exc
    if frame_count != expected_frames:
        raise ValueError(f"segment frame count must be {expected_frames}")
    return {
        "path": str(media_path),
        "frame_count": frame_count,
        "fps": observed_fps,
        "width": video["width"],
        "height": video["height"],
        "codec": video["codec_name"],
        "pixel_format": video["pix_fmt"],
    }


def _validate_final_output(path: Path, fps: int | float, width: int, height: int) -> None:
    payload = _ffprobe_json(path, count_frames=False)
    streams = payload.get("streams")
    if not isinstance(streams, list):
        raise ValueError("assembled output has no streams")
    video_streams = [stream for stream in streams if isinstance(stream, dict) and stream.get("codec_type") == "video"]
    audio_streams = [stream for stream in streams if isinstance(stream, dict) and stream.get("codec_type") == "audio"]
    if len(video_streams) != 1 or not audio_streams:
        raise ValueError("assembled output must contain one video stream and an audio stream")
    video = video_streams[0]
    if video.get("codec_name") != "h264":
        raise ValueError("assembled output codec must be h264")
    if video.get("width") != width or video.get("height") != height:
        raise ValueError("assembled output dimensions do not match the render contract")
    if abs(_parse_rate(video.get("avg_frame_rate") or video.get("r_frame_rate")) - float(fps)) > 1e-6:
        raise ValueError("assembled output FPS does not match the render contract")


def _run_ffmpeg(command: list[str]) -> None:
    try:
        subprocess.run(
            command,
            shell=False,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError("FFmpeg segment assembly failed") from exc


def _installed_remotion_version() -> str:
    candidates = [
        Path(REPO_ROOT) / "remotion" / "node_modules" / "remotion" / "package.json",
        Path(REPO_ROOT) / "remotion" / "package.json",
    ]
    for candidate in candidates:
        try:
            payload = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if candidate.name == "package.json" and candidate.parent.name == "remotion":
            dependency = (payload.get("dependencies") or {}).get("remotion")
            if isinstance(dependency, str):
                return dependency
        version = payload.get("version")
        if isinstance(version, str) and version:
            return version
    return "unknown"


def _renderer_source_hash() -> str:
    root = Path(REPO_ROOT) / "remotion"
    digest = hashlib.sha256()
    files = []
    source_root = root / "src"
    if source_root.is_dir():
        files.extend(
            path for path in source_root.rglob("*")
            if path.is_file() and path.suffix in {".ts", ".tsx", ".css", ".json"}
        )
    config = root / "remotion.config.ts"
    if config.is_file():
        files.append(config)
    for path in sorted(set(files)):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _resolve_asset_reference(job_dir: Path, reference: str) -> Path:
    if reference.startswith("job-visuals/"):
        relative = Path(reference.removeprefix("job-visuals/"))
        root = (job_dir / "visuals").resolve()
        candidate = (root / relative).resolve()
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"visual asset path escapes the job visuals directory: {reference}") from exc
        return candidate
    if reference.startswith("fyf-v2/"):
        root = (Path(REPO_ROOT) / "remotion" / "public").resolve()
        candidate = (root / reference).resolve()
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"approved asset path escapes the public directory: {reference}") from exc
        return candidate
    candidate = Path(reference)
    if not candidate.is_absolute():
        candidate = job_dir / candidate
    return candidate.resolve()


def _segment_asset_paths(job_dir: Path, render_input: Mapping[str, Any], segment: Mapping[str, Any], index: int) -> list[Path]:
    references: list[str] = []

    def collect(value: Any, key: str | None = None) -> None:
        if isinstance(value, Mapping):
            for child_key, child_value in value.items():
                collect(child_value, str(child_key))
        elif isinstance(value, list):
            for child in value:
                collect(child, key)
        elif key in {"asset_path", "fallback_asset_path"} and isinstance(value, str):
            references.append(value)

    collect(segment)
    all_scene_assets = render_input.get("v3SceneAssets")
    if isinstance(all_scene_assets, list) and index < len(all_scene_assets):
        scene_assets = all_scene_assets[index]
        if isinstance(scene_assets, list):
            references.extend(asset for asset in scene_assets if isinstance(asset, str))

    public_root = Path(REPO_ROOT) / "remotion" / "public"
    references.extend([
        "fyf-mascot-presenting.png",
        "fyf-mascot-talking-atlas.png",
        "fyf-cut-paper-world.png",
    ])
    paths: list[Path] = []
    seen: set[Path] = set()
    for reference in references:
        path = _resolve_asset_reference(job_dir, reference)
        if not path.is_file():
            if reference in {"fyf-mascot-presenting.png", "fyf-mascot-talking-atlas.png", "fyf-cut-paper-world.png"}:
                path = (public_root / reference).resolve()
            else:
                raise FileNotFoundError(f"referenced segment asset not found: {reference}")
        if path not in seen:
            seen.add(path)
            paths.append(path)
    return paths


def _manifest_fingerprint(results: Sequence[SegmentRenderResult]) -> str:
    payload = {
        "cache_contract_version": CACHE_CONTRACT_VERSION,
        "segments": [
            {
                "segment_id": result.segment_id,
                "fingerprint": result.fingerprint,
                "frame_count": result.frame_count,
                "sha256": _sha256_file(result.path),
            }
            for result in results
        ],
    }
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _write_progress_checkpoint(
    job_dir: Path,
    ordered_specs: Sequence[tuple[str, int, int, str, int]],
    completed: Mapping[str, SegmentRenderResult],
    *,
    complete: bool,
    manifest_fingerprint: str | None = None,
    video_path: Path | None = None,
) -> None:
    results = [completed[spec[0]] for spec in ordered_specs if spec[0] in completed]
    write_segment_checkpoint(
        job_dir,
        results,
        complete=complete,
        manifest_fingerprint=manifest_fingerprint,
        video_path=video_path,
    )


def _render_one_segment(
    job_dir: Path,
    segment_id: str,
    start_frame: int,
    end_frame: int,
    fingerprint: str,
    fps: int | float,
    width: int,
    height: int,
) -> SegmentRenderResult:
    final_path = segment_cache_path(job_dir, segment_id, fingerprint)
    temporary_path = final_path.with_name(f".{final_path.stem}-{uuid.uuid4().hex}.tmp.mp4")
    try:
        rendered_path = render_video_segment(
            str(job_dir),
            segment_id=segment_id,
            start_frame=start_frame,
            end_frame=end_frame,
            output_path=str(temporary_path),
        )
        if Path(rendered_path).resolve() != temporary_path.resolve():
            raise RuntimeError("segment renderer returned an unexpected output path")
        validate_segment_media(temporary_path, end_frame - start_frame, fps, width, height)
        os.replace(temporary_path, final_path)
    finally:
        temporary_path.unlink(missing_ok=True)
    return SegmentRenderResult(
        segment_id=segment_id,
        fingerprint=fingerprint,
        path=final_path.resolve(),
        cache_hit=False,
        frame_count=end_frame - start_frame,
    )


def _assemble_segments(
    job_dir: Path,
    results: Sequence[SegmentRenderResult],
    *,
    audio_path: Path,
    fps: int | float,
    width: int,
    height: int,
) -> Path:
    video_path = (job_dir / "video.mp4").resolve()
    concat_list_path = Path(tempfile.mkstemp(prefix=".segment-concat-", suffix=".txt", dir=job_dir)[1])
    concat_video_path = job_dir / f".segments-{uuid.uuid4().hex}.mp4"
    muxed_video_path = job_dir / f".video-{uuid.uuid4().hex}.mp4"
    try:
        with concat_list_path.open("w", encoding="utf-8") as handle:
            for result in results:
                escaped = str(result.path.resolve()).replace("'", "'\\''")
                handle.write(f"file '{escaped}'\n")
        _run_ffmpeg([
            FFMPEG_BIN,
            "-y",
            "-v",
            "error",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_list_path),
            "-c",
            "copy",
            str(concat_video_path),
        ])
        _run_ffmpeg([
            FFMPEG_BIN,
            "-y",
            "-v",
            "error",
            "-i",
            str(concat_video_path),
            "-i",
            str(audio_path),
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            str(muxed_video_path),
        ])
        _validate_final_output(muxed_video_path, fps, width, height)
        os.replace(muxed_video_path, video_path)
        return video_path
    finally:
        concat_list_path.unlink(missing_ok=True)
        concat_video_path.unlink(missing_ok=True)
        muxed_video_path.unlink(missing_ok=True)


def render_segments_and_assemble(job_dir: str) -> RenderAssemblyReport:
    """Render missing segments, resume valid cache entries, then mux once."""

    root = _job_dir_path(job_dir)
    concurrency = _segment_render_concurrency()
    render_input_path = root / "render_input.json"
    try:
        render_input = json.loads(render_input_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("render_input.json is missing or invalid") from exc
    if not isinstance(render_input, dict):
        raise ValueError("render_input.json must contain an object")
    validate_render_input(render_input, job_dir=str(root))

    segments = render_input.get("segments")
    if not isinstance(segments, list) or not segments:
        raise ValueError("render input segments must be a non-empty list")
    fps = render_input.get("fps")
    width = int(render_input.get("width") or 1080)
    height = int(render_input.get("height") or 1920)
    if not isinstance(fps, (int, float)) or isinstance(fps, bool) or fps <= 0:
        raise ValueError("render input fps must be positive")

    renderer_source_hash = _renderer_source_hash()
    remotion_version = _installed_remotion_version()
    output_settings = {
        "codec": "h264",
        "pixel_format": "yuv420p",
        "fps": fps,
        "width": width,
        "height": height,
    }
    ordered_specs: list[tuple[str, int, int, str, int]] = []
    seen_ids: set[str] = set()
    for index, segment in enumerate(segments):
        if not isinstance(segment, Mapping):
            raise ValueError("render input segment must be an object")
        raw_id = segment.get("id") or segment.get("segment_id")
        segment_id = normalize_segment_id(raw_id)
        if segment_id in seen_ids:
            raise ValueError(f"duplicate segment ID: {segment_id}")
        seen_ids.add(segment_id)
        start_frame = segment.get("startFrame")
        end_frame = segment.get("endFrame")
        if (
            not isinstance(start_frame, int)
            or isinstance(start_frame, bool)
            or not isinstance(end_frame, int)
            or isinstance(end_frame, bool)
            or end_frame <= start_frame
        ):
            raise ValueError(f"segment {segment_id} has an invalid frame range")
        fingerprint = segment_render_fingerprint(
            render_input,
            segment_id=segment_id,
            renderer_source_hash=renderer_source_hash,
            remotion_version=remotion_version,
            composition_id=REMOTION_COMPOSITION_ID,
            output_settings=output_settings,
            asset_paths=_segment_asset_paths(root, render_input, segment, index),
        )
        ordered_specs.append((segment_id, start_frame, end_frame, fingerprint, end_frame - start_frame))

    completed: dict[str, SegmentRenderResult] = {}
    cache_hits = 0
    for segment_id, start_frame, end_frame, fingerprint, frame_count in ordered_specs:
        try:
            cached = load_reusable_segment(
                root,
                segment_id=segment_id,
                fingerprint=fingerprint,
                expected_frame_count=frame_count,
            )
        except ValueError:
            cached = None
        if cached is not None:
            try:
                validate_segment_media(cached.path, frame_count, fps, width, height)
            except (OSError, ValueError):
                cached = None
        if cached is not None:
            completed[segment_id] = cached
            cache_hits += 1

    if len(completed) == len(ordered_specs):
        ordered_results = [completed[spec[0]] for spec in ordered_specs]
        manifest = _manifest_fingerprint(ordered_results)
        checkpoint = _read_checkpoint(root)
        video_path = (root / "video.mp4").resolve()
        if (
            checkpoint
            and checkpoint.get("complete") is True
            and checkpoint.get("manifest_fingerprint") == manifest
            and video_path.is_file()
        ):
            try:
                seal_matches = (
                    checkpoint.get("video_bytes") == video_path.stat().st_size
                    and checkpoint.get("video_sha256") == _sha256_file(video_path)
                )
                if not seal_matches:
                    raise ValueError("assembled output seal does not match checkpoint")
                _validate_final_output(video_path, fps, width, height)
            except (OSError, ValueError):
                pass
            else:
                return RenderAssemblyReport(
                    output_path=video_path,
                    total_segments=len(ordered_specs),
                    rendered_segments=0,
                    cache_hits=cache_hits,
                    manifest_fingerprint=manifest,
                )

    missing_specs = [spec for spec in ordered_specs if spec[0] not in completed]
    rendered_segments = 0
    failures: list[BaseException] = []
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = {
            executor.submit(
                _render_one_segment,
                root,
                segment_id,
                start_frame,
                end_frame,
                fingerprint,
                fps,
                width,
                height,
            ): segment_id
            for segment_id, start_frame, end_frame, fingerprint, _frame_count in missing_specs
        }
        for future in as_completed(futures):
            try:
                result = future.result()
            except BaseException as exc:
                failures.append(exc)
                for pending in futures:
                    if pending is not future:
                        pending.cancel()
                break
            completed[result.segment_id] = result
            rendered_segments += 1
            _write_progress_checkpoint(root, ordered_specs, completed, complete=False)
    if failures:
        raise failures[0]

    ordered_results = [completed[spec[0]] for spec in ordered_specs]
    manifest = _manifest_fingerprint(ordered_results)
    audio_src = render_input.get("audioSrc")
    if not isinstance(audio_src, str) or not audio_src.strip():
        raise ValueError("render input audioSrc must be a non-blank string")
    audio_path = Path(audio_src) if os.path.isabs(audio_src) else root / audio_src
    audio_path = audio_path.resolve()
    if not audio_path.is_file() or audio_path.stat().st_size <= 0:
        raise FileNotFoundError(f"render input audioSrc is missing or empty: {audio_path}")
    output_path = _assemble_segments(
        root,
        ordered_results,
        audio_path=audio_path,
        fps=fps,
        width=width,
        height=height,
    )
    _write_progress_checkpoint(
        root,
        ordered_specs,
        completed,
        complete=True,
        manifest_fingerprint=manifest,
        video_path=output_path,
    )
    return RenderAssemblyReport(
        output_path=output_path,
        total_segments=len(ordered_specs),
        rendered_segments=rendered_segments,
        cache_hits=cache_hits,
        manifest_fingerprint=manifest,
    )
