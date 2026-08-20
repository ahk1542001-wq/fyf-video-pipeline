import json
import math
import os
import re
import wave
from typing import Any


KNOWN_VISUAL_KINDS = {
    "generic",
    "inventory_mismatch",
    "approval_gate",
    "inventory_correction",
    "auto_action",
    "consequence",
    "process_timeline",
    "human_verification",
    "approval_record",
    "balance_pair",
    "outro",
}
KNOWN_PHASES = {"setup", "in_progress", "completed", "alert"}
KNOWN_MOUTH_VALUES = set("ABCDEFGHX")


def _positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _standalone_number_present(text: str, value: Any) -> bool:
    return re.search(rf"(?<![0-9]){re.escape(str(value))}(?![0-9])", text) is not None


def _validate_labeled_numbers(index: int, visual: dict[str, Any], text: str) -> None:
    kind = visual["kind"]
    fields: tuple[str, ...] = ()
    if kind == "inventory_mismatch":
        fields = ("physical_stock", "system_stock")
    elif kind == "inventory_correction":
        fields = ("from_value", "to_value")

    for field in fields:
        value = visual.get(field)
        if not _positive_int(value):
            raise ValueError(f"Segment {index} visual.{field} must be a positive integer")
        if not _standalone_number_present(text, value):
            raise ValueError(
                f"Segment {index} visual.{field} value {value} must appear as a standalone labeled number in screen_text"
            )


def _validate_visual_evidence(index: int, visual: dict[str, Any], job_dir: str | None) -> None:
    if job_dir is None:
        return
    claims = visual.get("evidence_claims")
    shots = visual.get("evidence_shots")
    if not isinstance(claims, list) or not claims:
        raise ValueError(f"Segment {index} visual evidence_claims must be non-empty")
    if not isinstance(shots, list) or not shots:
        raise ValueError(f"Segment {index} visual evidence_shots must be non-empty")
    claim_ids = {claim.get("claim_id") for claim in claims if isinstance(claim, dict)}
    covered = set()
    for shot in shots:
        if not isinstance(shot, dict):
            raise ValueError(f"Segment {index} evidence shot must be an object")
        covered.update(shot.get("proves_claim_ids") or [])
        if shot.get("verification_status") != "passed":
            raise ValueError(f"Segment {index} evidence shot is not verified")
        if shot.get("media_type") == "motion_graphic":
            spec = shot.get("motion_spec")
            if not isinstance(spec, dict) or not spec.get("labels"):
                raise ValueError(f"Segment {index} motion graphic has no deterministic spec")
            continue
        asset = shot.get("asset_path")
        if not isinstance(asset, str) or not asset.startswith("job-visuals/"):
            raise ValueError(f"Segment {index} evidence asset_path must use job-visuals/")
        if shot.get("media_type") == "generated_video" and not asset.lower().endswith(".mp4"):
            raise ValueError(f"Segment {index} generated video must resolve to an MP4 asset")
        if shot.get("media_type") == "generated_video":
            fallback = shot.get("fallback_asset_path")
            if not isinstance(fallback, str) or not fallback.startswith("job-visuals/"):
                raise ValueError(f"Segment {index} generated video must retain a verified fallback")
        if job_dir:
            local = os.path.join(job_dir, "visuals", os.path.basename(asset))
            if not os.path.isfile(local) or os.path.getsize(local) == 0:
                raise ValueError(f"Segment {index} evidence asset is missing or empty: {asset}")
            if shot.get("media_type") == "generated_video":
                fallback_local = os.path.join(job_dir, "visuals", os.path.basename(shot["fallback_asset_path"]))
                if not os.path.isfile(fallback_local) or os.path.getsize(fallback_local) == 0:
                    raise ValueError(f"Segment {index} generated video fallback is missing or empty")
    if covered != claim_ids:
        raise ValueError(f"Segment {index} evidence shots do not cover every claim")


def validate_render_input(data: dict[str, Any], job_dir: str | None = None) -> None:
    """Fail closed when production render props are incomplete or contradictory."""
    if not isinstance(data, dict):
        raise ValueError("Render input must be an object")

    fps = data.get("fps")
    duration = data.get("durationInFrames")
    if not _positive_int(fps):
        raise ValueError("fps must be a positive integer")
    if not _positive_int(duration):
        raise ValueError("durationInFrames must be a positive integer")

    audio_src = data.get("audioSrc")
    if not isinstance(audio_src, str) or not audio_src.strip():
        raise ValueError("audioSrc must be a non-blank string")

    segments = data.get("segments")
    if not isinstance(segments, list) or not segments:
        raise ValueError("segments must be a non-empty list")

    expected_start = 0
    for index, segment in enumerate(segments):
        if not isinstance(segment, dict):
            raise ValueError(f"Segment {index} must be an object")
        start = segment.get("startFrame")
        end = segment.get("endFrame")
        if not isinstance(start, int) or isinstance(start, bool):
            raise ValueError(f"Segment {index} startFrame must be an integer")
        if not isinstance(end, int) or isinstance(end, bool):
            raise ValueError(f"Segment {index} endFrame must be an integer")
        if start != expected_start:
            raise ValueError(
                f"Segments are not contiguous: segment {index} starts at {start}, expected {expected_start}"
            )
        if end <= start:
            raise ValueError(f"Segment {index} endFrame must be greater than startFrame")
        expected_start = end

        visual = segment.get("visual")
        if not isinstance(visual, dict):
            raise ValueError(f"Segment {index} must contain a typed visual object")
        kind = visual.get("kind")
        if kind not in KNOWN_VISUAL_KINDS:
            raise ValueError(f"Segment {index} has unknown visual kind: {kind}")
        phase = visual.get("phase")
        if phase not in KNOWN_PHASES:
            raise ValueError(f"Segment {index} has unknown visual phase: {phase}")

        screen_text = visual.get("screen_text")
        if not isinstance(screen_text, list) or not 1 <= len(screen_text) <= 2:
            raise ValueError(f"Segment {index} visual.screen_text must contain 1 or 2 lines")
        if any(not isinstance(line, str) or not line.strip() for line in screen_text):
            raise ValueError(f"Segment {index} visual.screen_text lines must be non-blank strings")
        normalized_screen_text = " ".join(" ".join(screen_text).split())
        narration = segment.get("text")
        if not isinstance(narration, str) or not narration.strip():
            raise ValueError(f"Segment {index} text must be a non-blank narration string")
        if normalized_screen_text == " ".join(narration.split()):
            raise ValueError(f"Segment {index} screen_text exactly duplicates narration")
        if phase == "in_progress" and visual.get("completion_ui"):
            raise ValueError(f"Segment {index} cannot show completion_ui while in_progress")
        _validate_labeled_numbers(index, visual, normalized_screen_text)
        _validate_visual_evidence(index, visual, job_dir)

    if expected_start != duration:
        raise ValueError(
            f"Segments do not cover durationInFrames: final end {expected_start}, duration {duration}"
        )

    cues = data.get("mouthCues")
    if not isinstance(cues, list):
        raise ValueError("mouthCues must be a list")
    previous_end = 0.0
    duration_seconds = duration / fps
    for index, cue in enumerate(cues):
        if not isinstance(cue, dict):
            raise ValueError(f"Mouth cue {index} must be an object")
        start = cue.get("start")
        end = cue.get("end")
        value = cue.get("value")
        if (
            not isinstance(start, (int, float))
            or isinstance(start, bool)
            or not isinstance(end, (int, float))
            or isinstance(end, bool)
            or not math.isfinite(float(start))
            or not math.isfinite(float(end))
        ):
            raise ValueError(f"Mouth cue {index} must have finite numeric start and end")
        if value not in KNOWN_MOUTH_VALUES:
            raise ValueError(f"Mouth cue {index} has invalid value: {value}")
        if start < 0 or end <= start:
            raise ValueError(f"Mouth cue {index} must have 0 <= start < end")
        if start < previous_end:
            raise ValueError(f"Mouth cues overlap or are not sorted at index {index}")
        if end > duration_seconds + 1e-6:
            raise ValueError(f"Mouth cue {index} ends after video duration")
        previous_end = float(end)

    if job_dir is None:
        return
    audio_path = audio_src if os.path.isabs(audio_src) else os.path.join(job_dir, audio_src)
    if not os.path.isfile(audio_path):
        raise ValueError(f"audioSrc does not resolve to a local file: {audio_src}")
    if not audio_path.lower().endswith(".wav"):
        raise ValueError("Production audioSrc must resolve to a WAV file")
    try:
        with wave.open(audio_path, "rb") as wav_file:
            rate = wav_file.getframerate()
            frames = wav_file.getnframes()
    except (OSError, EOFError, wave.Error) as exc:
        raise ValueError(f"audioSrc is not a readable WAV file: {audio_src}") from exc
    if rate <= 0:
        raise ValueError("WAV sample rate must be positive")
    audio_duration = frames / rate
    tolerance = 1 / fps
    if abs(audio_duration - duration_seconds) > tolerance + 1e-6:
        raise ValueError(
            "WAV duration does not match video duration within one frame: "
            f"audio={audio_duration:.6f}s video={duration_seconds:.6f}s tolerance={tolerance:.6f}s"
        )


def validate_render_input_file(path: str, job_dir: str | None = None) -> None:
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    validate_render_input(data, job_dir=job_dir)
