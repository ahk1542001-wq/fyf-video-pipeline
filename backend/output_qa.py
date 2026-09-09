import os
import json
import hashlib
import subprocess
import math
from pathlib import Path
from typing import Any

from voice_service.audio_quality import analyze_pcm16_wav
from backend.job_store import write_json_atomically


OUTPUT_QA_REPORT_VERSION = 2

TOLERANCE_SECONDS = 0.5


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def qa_report_fingerprint(report: dict[str, Any]) -> str:
    """Return the immutable SHA-256 identity of one persisted QA payload.

    The fingerprint deliberately excludes itself, so a caller can verify a
    report after it has been read from disk or transported to the acceptance
    boundary.  Any change to the report's evidence, identity, or persistence
    metadata therefore invalidates the fingerprint instead of being accepted as
    a fresh automated pass.
    """

    if not isinstance(report, dict):
        raise ValueError("QA report must be an object")
    covered = {key: value for key, value in report.items() if key != "fingerprint"}
    return hashlib.sha256(_canonical_json(covered).encode("utf-8")).hexdigest()


def persist_qa_report(
    job_dir: str | Path,
    report: dict[str, Any],
    *,
    attempt: int | None = None,
    report_name: str = "qa_report",
) -> Path:
    """Persist both the attempt evidence and the latest QA report atomically.

    Every deterministic QA decision is durable before the caller routes it.
    Attempt files are append-only in normal operation while the canonical file
    remains the latest report consumed by existing callers.
    """

    if not isinstance(report, dict):
        raise ValueError("QA report must be an object")
    if not isinstance(report_name, str) or not report_name.strip() or "/" in report_name:
        raise ValueError("report_name must be a non-empty filename stem")
    if attempt is not None and (
        isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1
    ):
        raise ValueError("attempt must be a positive integer or None")

    root = Path(job_dir)
    payload = dict(report)
    payload.setdefault("report_version", OUTPUT_QA_REPORT_VERSION)
    if "passed" not in payload:
        overall = payload.get("overall")
        if isinstance(overall, dict) and isinstance(overall.get("passed"), bool):
            payload["passed"] = overall["passed"]
    payload.setdefault("passed", False)
    payload.setdefault("status", "passed" if payload["passed"] is True else "failed")
    payload.setdefault("failure_codes", [])
    if not isinstance(payload.get("failure_codes"), list):
        raise ValueError("QA report failure_codes must be a list")

    # These fields are written by the persistence boundary, not trusted from a
    # caller.  They bind acceptance to a concrete job/report/attempt and make a
    # bare {"passed": true} payload ineligible for human acceptance.
    persisted_attempt = attempt if attempt is not None else 1
    payload["job_id"] = root.name
    payload["report_name"] = report_name
    payload["attempt"] = persisted_attempt
    payload["qa_identity"] = {
        "job_id": root.name,
        "report_name": report_name,
        "attempt": persisted_attempt,
    }
    payload["persisted_at_source"] = "local_deterministic_qa"
    payload["fingerprint"] = qa_report_fingerprint(payload)
    latest_path = root / f"{report_name}.json"
    write_json_atomically(latest_path, payload)
    if attempt is None:
        return latest_path
    attempt_path = root / f"{report_name}.attempt-{attempt}.json"
    write_json_atomically(attempt_path, payload)
    return attempt_path


def audit_output_manifest(
    job_dir: str | Path, *, require_manifest: bool = False
) -> dict[str, Any]:
    """Verify the reproducibility manifest without inventing a passing result.

    Jobs created before manifest support may omit the file when the caller does
    not require it.  A production render that explicitly requires a manifest
    receives a deterministic failure instead; malformed or mismatched manifests
    are never treated as transient renderer failures.
    """

    root = Path(job_dir)
    path = root / "render_manifest.json"
    report: dict[str, Any] = {
        "passed": not require_manifest,
        "status": "not_required",
        "failure_codes": [],
        "integrity": None,
    }
    if not path.is_file() or path.stat().st_size == 0:
        report["status"] = "missing"
        if require_manifest:
            report["passed"] = False
            report["failure_codes"].append("MISSING_RENDER_MANIFEST")
        return report

    try:
        from backend.render_manifest import verify_render_manifest

        integrity = verify_render_manifest(root)
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        report.update({
            "passed": False,
            "status": "invalid",
            "failure_codes": ["RENDER_MANIFEST_INVALID"],
            "integrity": {"error": str(exc)},
        })
        return report

    report["integrity"] = integrity
    report["status"] = "verified" if integrity.get("verified") is True else "mismatch"
    if integrity.get("verified") is not True:
        report["passed"] = False
        report["failure_codes"].append("MANIFEST_INTEGRITY_FAILED")
    else:
        report["passed"] = True
    return report


# Additive alias for route integration and callers that use the renderer's
# vocabulary.  Keep one implementation so the integrity contract cannot drift.
verify_output_manifest = audit_output_manifest

def _get_ffprobe_info(filepath: str) -> dict:
    cmd = [
        "ffprobe",
        "-v", "quiet",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        filepath
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return json.loads(result.stdout)

def _get_duration(info: dict) -> float:
    fmt = info.get("format", {})
    duration = fmt.get("duration")
    if duration is not None:
        return float(duration)
    for stream in info.get("streams", []):
        if "duration" in stream:
            return float(stream["duration"])
    return 0.0

def _has_stream_type(info: dict, codec_type: str) -> bool:
    return any(stream.get("codec_type") == codec_type for stream in info.get("streams", []))


def _parse_frame_rate(value: Any) -> float | None:
    """Parse ffprobe's ``num/den`` or decimal frame-rate spelling safely."""

    if isinstance(value, bool):
        return None
    try:
        if isinstance(value, (int, float)):
            result = float(value)
        elif isinstance(value, str):
            text = value.strip()
            if not text:
                return None
            if "/" in text:
                numerator, denominator = text.split("/", 1)
                result = float(numerator) / float(denominator)
            else:
                result = float(text)
        else:
            return None
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    return result if math.isfinite(result) and result > 0 else None


def _audit_video_contract(
    video_info: dict[str, Any], render_input: dict[str, Any], video_duration: float
) -> tuple[list[tuple[str, bool, str]], dict[str, Any]]:
    """Compare measurable final-video metadata with the persisted render contract.

    Older fixtures and third-party ffprobe builds may omit optional stream
    fields, so a comparison is added only when both sides are measurable.  A
    production Remotion render declares these fields in ``render_input.json``;
    when its pixel format is not repeated there, the renderer's fixed
    ``yuv420p`` output contract remains the expected value.
    """

    streams = video_info.get("streams") if isinstance(video_info, dict) else None
    if not isinstance(streams, list) or not isinstance(render_input, dict):
        return [], {}
    video_streams = [
        stream for stream in streams
        if isinstance(stream, dict) and stream.get("codec_type") == "video"
    ]
    if len(video_streams) != 1:
        return [], {}
    stream = video_streams[0]
    checks: list[tuple[str, bool, str]] = []
    metrics: dict[str, Any] = {}

    def compare(
        name: str, expected: Any, actual: Any, failure_code: str, *, tolerance: float = 0.0
    ) -> None:
        if expected is None or actual is None:
            return
        if isinstance(expected, bool) or isinstance(actual, bool):
            passed = expected == actual
        elif tolerance:
            passed = abs(float(actual) - float(expected)) <= tolerance
        else:
            passed = actual == expected
        checks.append((name, passed, failure_code))

    expected_fps = render_input.get("fps")
    if isinstance(expected_fps, bool) or not isinstance(expected_fps, (int, float)) or expected_fps <= 0:
        expected_fps = None
    observed_fps = _parse_frame_rate(stream.get("avg_frame_rate") or stream.get("r_frame_rate"))
    if observed_fps is not None:
        metrics["video_fps"] = observed_fps
    compare("video_fps_matches_contract", expected_fps, observed_fps, "VIDEO_FPS_MISMATCH", tolerance=1e-6)

    expected_width = render_input.get("width")
    expected_height = render_input.get("height")
    if isinstance(expected_width, bool) or not isinstance(expected_width, int) or expected_width <= 0:
        expected_width = None
    if isinstance(expected_height, bool) or not isinstance(expected_height, int) or expected_height <= 0:
        expected_height = None
    actual_width = stream.get("width")
    actual_height = stream.get("height")
    if isinstance(actual_width, int) and not isinstance(actual_width, bool):
        metrics["video_width"] = actual_width
    if isinstance(actual_height, int) and not isinstance(actual_height, bool):
        metrics["video_height"] = actual_height
    compare("video_width_matches_contract", expected_width, actual_width, "VIDEO_WIDTH_MISMATCH")
    compare("video_height_matches_contract", expected_height, actual_height, "VIDEO_HEIGHT_MISMATCH")

    expected_pixel_format = render_input.get("pixel_format")
    if not isinstance(expected_pixel_format, str) or not expected_pixel_format.strip():
        if expected_fps is not None and expected_width is not None and expected_height is not None:
            expected_pixel_format = "yuv420p"
    actual_pixel_format = stream.get("pix_fmt")
    if isinstance(actual_pixel_format, str) and actual_pixel_format.strip():
        metrics["video_pixel_format"] = actual_pixel_format
    compare(
        "video_pixel_format_matches_contract",
        expected_pixel_format,
        actual_pixel_format,
        "VIDEO_PIXEL_FORMAT_MISMATCH",
    )

    expected_codec = render_input.get("video_codec") or render_input.get("codec")
    if not isinstance(expected_codec, str) or not expected_codec.strip():
        if expected_fps is not None and expected_width is not None and expected_height is not None:
            expected_codec = "h264"
    actual_codec = stream.get("codec_name")
    if isinstance(actual_codec, str) and actual_codec.strip():
        metrics["video_codec"] = actual_codec
    compare("video_codec_matches_contract", expected_codec, actual_codec, "VIDEO_CODEC_MISMATCH")

    expected_frames = render_input.get("durationInFrames")
    if isinstance(expected_frames, bool) or not isinstance(expected_frames, int) or expected_frames <= 0:
        expected_frames = None
    raw_frames = stream.get("nb_read_frames") or stream.get("nb_frames")
    actual_frames: int | None
    try:
        actual_frames = int(raw_frames) if raw_frames is not None else None
    except (TypeError, ValueError):
        actual_frames = None
    if actual_frames is not None and actual_frames > 0:
        metrics["video_frame_count"] = actual_frames
    compare("video_frame_count_matches_contract", expected_frames, actual_frames, "VIDEO_FRAME_COUNT_MISMATCH")

    if expected_frames is not None and expected_fps is not None and video_duration > 0:
        expected_duration = expected_frames / float(expected_fps)
        metrics["contract_duration"] = expected_duration
        duration_tolerance = max(1.0 / float(expected_fps), 0.01)
        if actual_frames == expected_frames:
            # MP4 audio/container timestamps can extend the reported format
            # duration slightly beyond the exact encoded video frame count.
            duration_tolerance = max(2.0 / float(expected_fps), 0.01)
        compare(
            "video_duration_matches_contract",
            expected_duration,
            video_duration,
            "VIDEO_DURATION_MISMATCH",
            tolerance=duration_tolerance,
        )
    return checks, metrics

def _extract_segments(data):
    """
    Recursively search for dictionaries that have an 'id' and a text representation.
    Returns a list of (id, text) tuples.
    """
    segments = []
    if isinstance(data, dict):
        has_id = "id" in data
        text_keys = ["text", "audio_text", "narration"]
        found_text = None
        for k in text_keys:
            if k in data and isinstance(data[k], str):
                found_text = data[k]
                break

        if has_id and isinstance(data["id"], (str, int)) and found_text is not None:
            segments.append((str(data["id"]), found_text))

        for v in data.values():
            segments.extend(_extract_segments(v))
    elif isinstance(data, list):
        for item in data:
            segments.extend(_extract_segments(item))

    return segments

def qa_job_directory(job_dir: str, *, require_manifest: bool = False) -> dict:
    """
    Deterministic local output QA function for a rendered job directory.
    Validates script.json, render_input.json, mouth_cues.json, video.mp4, and voice.wav.
    When ``require_manifest`` is true, a verified render manifest is part of the
    same deterministic gate; older unmanifested fixtures remain compatible by
    default.
    """
    report = {
        "passed": False,
        "report_version": OUTPUT_QA_REPORT_VERSION,
        "status": "available",
        "checks": [],
        "failure_codes": [],
        "metrics": {},
        "warnings": [],
    }

    def add_check(name: str, passed: bool, failure_code: str = None):
        report["checks"].append({"name": name, "passed": passed})
        if not passed and failure_code:
            report["failure_codes"].append(failure_code)

    try:
        # 1. Check for required files and non-empty status
        files_to_check = {
            "voice.wav": "MISSING_VOICE",
            "video.mp4": "MISSING_VIDEO",
            "script.json": "MISSING_SCRIPT",
            "render_input.json": "MISSING_RENDER_INPUT",
            "mouth_cues.json": "MISSING_MOUTH_CUES"
        }

        all_files_exist = True
        for filename, error_code in files_to_check.items():
            filepath = os.path.join(job_dir, filename)
            exists = os.path.isfile(filepath)
            is_nonempty = exists and os.path.getsize(filepath) > 0
            add_check(f"{filename.replace('.', '_')}_nonempty", is_nonempty, error_code)
            if not is_nonempty:
                all_files_exist = False

        if not all_files_exist:
            return report

        # 2. Probe voice.wav using ffprobe
        voice_path = os.path.join(job_dir, "voice.wav")
        voice_duration = 0.0
        try:
            voice_info = _get_ffprobe_info(voice_path)
            voice_duration = _get_duration(voice_info)
            has_voice_audio = _has_stream_type(voice_info, "audio")

            add_check("voice_has_audio_stream", has_voice_audio, "VOICE_NO_AUDIO_STREAM")
            add_check("voice_duration_positive", voice_duration > 0, "VOICE_ZERO_DURATION")
            report["metrics"]["voice_duration"] = voice_duration

            audio_metrics = analyze_pcm16_wav(voice_path)
            peak_dbfs = float(audio_metrics["peak_dbfs"])
            full_scale_samples = int(audio_metrics["full_scale_samples"])
            report["metrics"].update({
                "voice_peak_dbfs": peak_dbfs,
                "voice_full_scale_samples": full_scale_samples,
            })
            add_check(
                "voice_no_full_scale_clipping",
                full_scale_samples == 0,
                "VOICE_FULL_SCALE_CLIPPING",
            )
            add_check(
                "voice_peak_headroom",
                peak_dbfs <= -1.0,
                "VOICE_PEAK_HEADROOM_LOW",
            )
        except Exception:
            add_check("voice_probe_success", False, "VOICE_PROBE_FAILED")
            return report

        # 3. Probe video.mp4 using ffprobe
        video_path = os.path.join(job_dir, "video.mp4")
        video_duration = 0.0
        try:
            video_info = _get_ffprobe_info(video_path)
            video_duration = _get_duration(video_info)
            has_video_stream = _has_stream_type(video_info, "video")
            has_video_audio_stream = _has_stream_type(video_info, "audio")

            add_check("video_has_video_stream", has_video_stream, "VIDEO_NO_VIDEO_STREAM")
            add_check("video_has_audio_stream", has_video_audio_stream, "VIDEO_NO_AUDIO_STREAM")
            add_check("video_duration_positive", video_duration > 0, "VIDEO_ZERO_DURATION")
            report["metrics"]["video_duration"] = video_duration
        except Exception:
            add_check("video_probe_success", False, "VIDEO_PROBE_FAILED")
            return report

        # 4. Compare durations (video covers voice within tolerance)
        covers_voice = video_duration >= (voice_duration - TOLERANCE_SECONDS)
        add_check("video_covers_voice", covers_voice, "VIDEO_TOO_SHORT")

        # 5. Check mouth_cues.json validity
        try:
            with open(os.path.join(job_dir, "mouth_cues.json"), "r") as f:
                mouth_cues = json.load(f)

            is_list = isinstance(mouth_cues, list)
            add_check("mouth_cues_is_list", is_list, "MOUTH_CUES_NOT_LIST")
            cue_values = set()

            if is_list:
                valid_cues = True
                max_end = 0.0
                for cue in mouth_cues:
                    start = cue.get("start")
                    end = cue.get("end")
                    value = cue.get("value")
                    if start is None or end is None or not math.isfinite(start) or not math.isfinite(end):
                        valid_cues = False
                        break
                    if value not in {"A", "B", "C", "D", "E", "F", "G", "H", "X"}:
                        valid_cues = False
                        break
                    cue_values.add(value)
                    if start < 0 or end < 0 or end < start:
                        valid_cues = False
                        break
                    if end > max_end:
                        max_end = end

                add_check("mouth_cues_valid_values", valid_cues, "MOUTH_CUES_INVALID_VALUES")

                cues_within_duration = max_end <= (voice_duration + TOLERANCE_SECONDS)
                add_check("mouth_cues_within_duration", cues_within_duration, "MOUTH_CUES_OUT_OF_BOUNDS")
        except Exception:
            add_check("mouth_cues_parse_success", False, "MOUTH_CUES_PARSE_FAILED")

        # 6. Check script.json and render_input.json consistency
        try:
            with open(os.path.join(job_dir, "script.json"), "r") as f:
                script_data = json.load(f)
            with open(os.path.join(job_dir, "render_input.json"), "r") as f:
                render_data = json.load(f)

            contract_checks, contract_metrics = _audit_video_contract(
                video_info, render_data, video_duration
            )
            report["metrics"].update(contract_metrics)
            for check_name, passed, failure_code in contract_checks:
                add_check(check_name, passed, failure_code)

            script_segments = _extract_segments(script_data)
            render_segments = _extract_segments(render_data)

            render_ids = [s[0] for s in render_segments]
            has_duplicates = len(render_ids) != len(set(render_ids))
            add_check("render_no_duplicate_ids", not has_duplicates, "RENDER_HAS_DUPLICATES")

            script_dict = dict(script_segments)
            render_dict = dict(render_segments)

            segments_match = (script_dict == render_dict)
            add_check("render_matches_script_segments", segments_match, "SEGMENTS_MISMATCH")

            visual_shots = [
                shot for segment in script_data.get("segments", [])
                for shot in ((segment.get("visual") or {}).get("evidence_shots") or [])
                if isinstance(shot, dict)
            ] if isinstance(script_data, dict) else []
            if visual_shots:
                fallback_count = sum(bool(shot.get("fallback_used")) for shot in visual_shots)
                generated_count = sum(
                    shot.get("media_type") in {"generated_image", "generated_video"}
                    for shot in visual_shots
                )
                fallback_ratio = fallback_count / len(visual_shots)
                report["metrics"].update({
                    "visual_shots": len(visual_shots),
                    "visual_fallbacks": fallback_count,
                    "visual_fallback_ratio": round(fallback_ratio, 4),
                    "generated_media_shots": generated_count,
                })
                if fallback_ratio > 0.4:
                    report["warnings"].append("VISUAL_FALLBACK_RATIO_HIGH")
                if len(script_data.get("segments", [])) >= 8:
                    add_check(
                        "long_form_has_generated_media_diversity",
                        generated_count >= 2,
                        "VISUAL_MEDIA_DIVERSITY_LOW",
                    )

            cue_source = render_data.get("mouthCueSource") if isinstance(render_data, dict) else None
            if cue_source is not None:
                supported_source = cue_source in {
                    "rhubarb-phonetic", "burmese-text-audio", "amplitude-fallback"
                }
                add_check("mouth_cue_source_supported", supported_source, "MOUTH_CUE_SOURCE_INVALID")
                report["metrics"]["mouth_cue_source"] = cue_source
                if cue_source in {"rhubarb-phonetic", "burmese-text-audio"}:
                    has_phonetic_variety = len(cue_values - {"X"}) >= 3
                    add_check(
                        "mouth_cues_have_phonetic_variety",
                        has_phonetic_variety,
                        "MOUTH_CUES_LOW_VARIETY",
                    )

        except Exception:
            add_check("json_parse_success", False, "JSON_PARSE_FAILED")

    except Exception:
        add_check("unhandled_exception", False, "UNHANDLED_EXCEPTION")

    manifest_path = os.path.join(job_dir, "render_manifest.json")
    if require_manifest or os.path.isfile(manifest_path):
        manifest_report = audit_output_manifest(job_dir, require_manifest=require_manifest)
        report["manifest_integrity"] = manifest_report
        add_check(
            "render_manifest_integrity",
            bool(manifest_report.get("passed")),
            (manifest_report.get("failure_codes") or ["MANIFEST_INTEGRITY_FAILED"])[0],
        )

    report["passed"] = len(report["failure_codes"]) == 0
    return report
