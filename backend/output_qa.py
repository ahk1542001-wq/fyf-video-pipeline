import os
import json
import subprocess
import math

from voice_service.audio_quality import analyze_pcm16_wav

TOLERANCE_SECONDS = 0.5

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

def qa_job_directory(job_dir: str) -> dict:
    """
    Deterministic local output QA function for a rendered job directory.
    Validates script.json, render_input.json, mouth_cues.json, video.mp4, and voice.wav.
    """
    report = {
        "passed": False,
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

    report["passed"] = len(report["failure_codes"]) == 0
    return report
