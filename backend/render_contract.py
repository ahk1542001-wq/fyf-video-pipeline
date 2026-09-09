import json
import math
import os
import re
import wave
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from video_contract import ASPECT_RATIO_DIMENSIONS, RenderControls


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
        if shot.get("semantic_verification_status") == "unverified" or shot.get("semantic_verified") is False:
            raise ValueError(f"Segment {index} evidence shot has unverified semantic fallback")
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

    raw_controls = data.get("render_controls")
    if raw_controls is None:
        raw_controls = {
            name: data[name]
            for name in (
                "cta_text",
                "retention_progress_bar",
                "animated_lower_thirds",
                "aspect_ratio",
            )
            if name in data
        }
    try:
        controls = RenderControls.model_validate(raw_controls).model_dump(mode="json")
    except ValueError as exc:
        raise ValueError(f"render controls are invalid: {exc}") from exc

    nested_controls = data.get("render_controls")
    if nested_controls is not None and not isinstance(nested_controls, dict):
        raise ValueError("render_controls must be an object")
    explicit_aspect_ratio = "aspect_ratio" in data or (
        isinstance(nested_controls, dict) and "aspect_ratio" in nested_controls
    )
    for name, expected in controls.items():
        if name in data and data[name] != expected:
            raise ValueError(f"render input {name} does not match render_controls")

    width = data.get("width")
    height = data.get("height")
    if (width is None) != (height is None):
        raise ValueError("width and height must be supplied together")
    if explicit_aspect_ratio and width is None:
        raise ValueError("width and height are required when aspect_ratio is supplied")
    if width is not None:
        if not _positive_int(width) or not _positive_int(height):
            raise ValueError("width and height must be positive integers")
        expected_width, expected_height = ASPECT_RATIO_DIMENSIONS[controls["aspect_ratio"]]
        if (width, height) != (expected_width, expected_height):
            raise ValueError(
                "width and height do not match the render aspect_ratio: "
                f"expected {expected_width}x{expected_height}"
            )

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


# --------------------------------------------------------------------------- #
# D6 — reframe and duration re-edit as SEPARATE commands (pure planners)
# --------------------------------------------------------------------------- #

#: The three supported output ratios, taken from the render contract itself.
ASPECT_RATIOS: tuple[str, ...] = tuple(sorted(ASPECT_RATIO_DIMENSIONS))

#: Fractional inset from every edge of the target frame that must stay clear of
#: captions, lower thirds and the progress bar.  6% of the short edge.
SAFE_ZONE_MARGIN_RATIO = 0.06

#: A scene shorter than this cannot be read, so a duration re-edit that would
#: push any scene below it is infeasible as an edit and becomes a story change.
MIN_SCENE_SECONDS = 1.2


@dataclass(frozen=True)
class ReframePlan:
    """Safe-zone-aware aspect adaptation plan.

    ``crop`` is typed ``None`` and ``crops_content`` is a constant ``False``:
    reframing in this pipeline NEVER crops, because cropping is how a subject, a
    caption or a lower third silently leaves the frame.  When the source
    composition does not fit the target safe zone the plan says so and asks for a
    real re-composition instead of pretending a pad fixed it.
    """

    source_aspect_ratio: str
    target_aspect_ratio: str
    source_width: int
    source_height: int
    target_width: int
    target_height: int
    strategy: str
    scale: float
    content_box: dict[str, int]
    safe_zone: dict[str, int]
    fits_safe_zone: bool
    requires_recomposition: bool
    requires_story_reedit: bool
    crop: None = None
    crops_content: bool = False
    warnings: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_aspect_ratio": self.source_aspect_ratio,
            "target_aspect_ratio": self.target_aspect_ratio,
            "source_width": self.source_width,
            "source_height": self.source_height,
            "target_width": self.target_width,
            "target_height": self.target_height,
            "strategy": self.strategy,
            "scale": round(self.scale, 6),
            "content_box": dict(self.content_box),
            "safe_zone": dict(self.safe_zone),
            "fits_safe_zone": self.fits_safe_zone,
            "requires_recomposition": self.requires_recomposition,
            "requires_story_reedit": self.requires_story_reedit,
            "crop": None,
            "crops_content": False,
            "warnings": list(self.warnings),
        }


def plan_reframe(
    *,
    source_aspect_ratio: str,
    target_aspect_ratio: str,
    source_width: int | None = None,
    source_height: int | None = None,
    safe_zone_margin: float = SAFE_ZONE_MARGIN_RATIO,
) -> ReframePlan:
    """Plan an aspect adaptation as a safe-zone-aware composition change.

    This is the ``reframe_aspect`` command's planner.  It is deliberately NOT
    shared with ``reedit_duration``: the two operations answer different
    questions and merging them is how a "resize" button ends up quietly
    time-stretching narration.
    """

    for name, value in (
        ("source_aspect_ratio", source_aspect_ratio),
        ("target_aspect_ratio", target_aspect_ratio),
    ):
        if value not in ASPECT_RATIO_DIMENSIONS:
            raise ValueError(
                f"{name} {value!r} is not supported; supported ratios: {', '.join(ASPECT_RATIOS)}"
            )
    if not 0.0 <= float(safe_zone_margin) < 0.25:
        raise ValueError("safe_zone_margin must be in [0, 0.25)")

    source_default = ASPECT_RATIO_DIMENSIONS[source_aspect_ratio]
    source_width = int(source_width or source_default[0])
    source_height = int(source_height or source_default[1])
    if source_width < 1 or source_height < 1:
        raise ValueError("source dimensions must be positive")
    target_width, target_height = ASPECT_RATIO_DIMENSIONS[target_aspect_ratio]

    if source_aspect_ratio == target_aspect_ratio and (source_width, source_height) == (
        target_width,
        target_height,
    ):
        return ReframePlan(
            source_aspect_ratio, target_aspect_ratio, source_width, source_height,
            target_width, target_height, "identity", 1.0,
            {"x": 0, "y": 0, "width": target_width, "height": target_height},
            _safe_zone(target_width, target_height, safe_zone_margin),
            True, False, False,
            warnings=("source already matches the target frame; nothing to reframe",),
        )

    scale = min(target_width / source_width, target_height / source_height)
    content_width = int(round(source_width * scale))
    content_height = int(round(source_height * scale))
    content_box = {
        "x": (target_width - content_width) // 2,
        "y": (target_height - content_height) // 2,
        "width": content_width,
        "height": content_height,
    }
    zone = _safe_zone(target_width, target_height, safe_zone_margin)
    fits = (
        content_box["x"] >= zone["left"]
        and content_box["y"] >= zone["top"]
        and content_box["x"] + content_box["width"] <= target_width - zone["right"]
        and content_box["y"] + content_box["height"] <= target_height - zone["bottom"]
    )

    warnings: list[str] = []
    if source_aspect_ratio != target_aspect_ratio:
        pad_axis = "pillarbox" if content_width < target_width else "letterbox"
        warnings.append(
            f"{pad_axis} padding to {target_aspect_ratio} with the brand ivory fill; "
            "no content is cropped"
        )
    if not fits:
        warnings.append(
            "the fitted composition overruns the target safe zone, so captions, lower "
            "thirds or the progress bar would sit in the unsafe margin; this needs a "
            "real re-composition of the visuals, not a pad"
        )
    warnings.append(
        "reframing changes the render contract, so every segment fingerprint "
        "invalidates and the video must be re-rendered"
    )

    return ReframePlan(
        source_aspect_ratio, target_aspect_ratio, source_width, source_height,
        target_width, target_height,
        "pad" if fits else "recompose_required",
        scale, content_box, zone, fits, not fits, False,
        warnings=tuple(warnings),
    )


def _safe_zone(width: int, height: int, margin: float) -> dict[str, int]:
    inset = int(round(min(width, height) * float(margin)))
    return {"left": inset, "top": inset, "right": inset, "bottom": inset}


@dataclass(frozen=True)
class DurationReeditPlan:
    """A duration change expressed ONLY as a story re-edit.

    ``speed_factor`` is typed ``None`` with a ``None`` default, so the plan is
    structurally incapable of carrying a playback-rate change.  That is the whole
    point of D6: shortening a video is a re-edit of the approved story and
    therefore requires re-approval, never a time-stretch of the existing audio.
    """

    current_duration_seconds: float
    requested_duration_seconds: float
    scene_count: int
    strategy: str
    feasible: bool
    requires_story_reedit: bool
    requires_reapproval: bool
    min_scene_seconds: float
    readability_floor_seconds: float
    segment_plan: tuple[dict[str, Any], ...]
    speed_factor: None = None
    reason: str = ""
    warnings: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "current_duration_seconds": round(self.current_duration_seconds, 3),
            "requested_duration_seconds": round(self.requested_duration_seconds, 3),
            "scene_count": self.scene_count,
            "strategy": self.strategy,
            "feasible": self.feasible,
            "requires_story_reedit": self.requires_story_reedit,
            "requires_reapproval": self.requires_reapproval,
            "min_scene_seconds": self.min_scene_seconds,
            "readability_floor_seconds": round(self.readability_floor_seconds, 3),
            "segment_plan": [dict(entry) for entry in self.segment_plan],
            # Explicit and always null: a duration re-edit has no speed factor.
            "speed_factor": None,
            "reason": self.reason,
            "warnings": list(self.warnings),
        }


def _segment_narration_weight(segment: dict[str, Any]) -> float:
    text = str(segment.get("text") or segment.get("narration") or "").strip()
    return float(max(1, len(text)))


def plan_duration_reedit(
    render_input: dict[str, Any],
    *,
    requested_duration_seconds: float,
    fps: int | float | None = None,
    min_scene_seconds: float = MIN_SCENE_SECONDS,
) -> DurationReeditPlan:
    """Plan a duration change as a story re-edit that requires re-approval.

    This is the ``reedit_duration`` command's planner.  It never returns a speed
    factor and never claims one was applied: shortening below the readability
    floor is reported infeasible with the reason, because the only honest way to
    get shorter is to cut or merge scenes and re-approve the result.
    """

    if not isinstance(render_input, dict):
        raise ValueError("render_input must be an object")
    segments = render_input.get("segments")
    if not isinstance(segments, list) or not segments:
        raise ValueError("render_input.segments must be a non-empty list")
    if not isinstance(requested_duration_seconds, (int, float)) or isinstance(
        requested_duration_seconds, bool
    ) or requested_duration_seconds <= 0:
        raise ValueError("requested_duration_seconds must be positive")
    if not isinstance(min_scene_seconds, (int, float)) or min_scene_seconds <= 0:
        raise ValueError("min_scene_seconds must be positive")

    resolved_fps = render_input.get("fps") if fps is None else fps
    if not isinstance(resolved_fps, (int, float)) or isinstance(resolved_fps, bool) or resolved_fps <= 0:
        resolved_fps = 30
    resolved_fps = float(resolved_fps)

    total_frames = render_input.get("durationInFrames")
    if not isinstance(total_frames, int) or isinstance(total_frames, bool) or total_frames <= 0:
        total_frames = max(int(segment.get("endFrame") or 0) for segment in segments)
    current = round(total_frames / resolved_fps, 3)
    requested = round(float(requested_duration_seconds), 3)
    scene_count = len(segments)
    floor = round(scene_count * float(min_scene_seconds), 3)

    if abs(requested - current) < 0.001:
        return DurationReeditPlan(
            current, requested, scene_count, "identity", True, False, False,
            float(min_scene_seconds), floor, (),
            reason="the requested duration equals the current duration; no story change is needed",
            warnings=("nothing to re-edit; no re-approval required",),
        )

    if requested > current:
        return DurationReeditPlan(
            current, requested, scene_count, "story_extension", True, True, True,
            float(min_scene_seconds), floor,
            _allocate_scene_seconds(segments, requested, resolved_fps, min_scene_seconds),
            reason=(
                "lengthening adds narration beats; that is a story change and must be "
                "re-approved before it is committed"
            ),
            warnings=("extending the runtime is a story re-edit, not a slow-down of the existing audio",),
        )

    if requested < floor:
        return DurationReeditPlan(
            current, requested, scene_count, "story_reedit_required", False, True, True,
            float(min_scene_seconds), floor, (),
            reason=(
                f"{scene_count} scenes need at least {floor}s to stay readable "
                f"({min_scene_seconds}s each); the requested {requested}s is below that floor. "
                "Getting there requires removing or merging scenes, which is a story "
                "re-edit and must be re-approved. It is never a speed-up: time-stretching "
                "the existing narration would change the approved performance and is not "
                "representable in this plan."
            ),
            warnings=(
                "infeasible as an edit; propose a shorter story instead",
                "speed_factor is not representable and was not applied",
            ),
        )

    plan = _allocate_scene_seconds(segments, requested, resolved_fps, min_scene_seconds)
    shortened = [
        entry["segment_id"]
        for entry in plan
        if entry["target_seconds"] < entry["current_seconds"] - 0.001
    ]
    return DurationReeditPlan(
        current, requested, scene_count, "story_reedit_required", True, True, True,
        float(min_scene_seconds), floor, tuple(plan),
        reason=(
            "shortening is delivered by re-editing the story to the new per-scene budget "
            "and re-approving it; the playback rate is untouched"
        ),
        warnings=(
            f"{len(shortened)} scene(s) lose narration time and may need their text trimmed: "
            + ", ".join(shortened),
            "re-approval is required before this version may be committed",
            "speed_factor is not representable and was not applied",
        ),
    )


def _allocate_scene_seconds(
    segments: list[dict[str, Any]],
    total_seconds: float,
    fps: float,
    min_scene_seconds: float,
) -> list[dict[str, Any]]:
    """Narration-weighted per-scene budget with a readability floor.

    Largest-remainder rounding keeps the scene budgets summing exactly to the
    requested duration in whole frames, so the plan can be committed without a
    drift correction pass.
    """

    weights = [_segment_narration_weight(segment) for segment in segments]
    weight_total = sum(weights) or float(len(segments))
    floor_frames = int(math.ceil(float(min_scene_seconds) * fps))
    total_frames = int(round(float(total_seconds) * fps))
    if total_frames < floor_frames * len(segments):
        return []

    raw = [total_frames * weight / weight_total for weight in weights]
    frames = [max(floor_frames, int(math.floor(value))) for value in raw]
    remainder = total_frames - sum(frames)
    order = sorted(
        range(len(segments)),
        key=lambda index: (-(raw[index] - math.floor(raw[index])), index),
    )
    cursor = 0
    while remainder > 0 and cursor < len(order) * 4:
        index = order[cursor % len(order)]
        frames[index] += 1
        remainder -= 1
        cursor += 1
    while remainder < 0:
        donor = max(range(len(segments)), key=lambda index: frames[index])
        if frames[donor] <= floor_frames:
            break
        frames[donor] -= 1
        remainder += 1

    plan: list[dict[str, Any]] = []
    start = 0
    for index, segment in enumerate(segments):
        end = start + frames[index]
        segment_id = str(segment.get("id") or segment.get("segment_id") or f"segment-{index}")
        plan.append(
            {
                "segment_id": segment_id,
                "start_frame": start,
                "end_frame": end,
                "target_seconds": round(frames[index] / fps, 3),
                "current_seconds": round(
                    (int(segment.get("endFrame") or 0) - int(segment.get("startFrame") or 0)) / fps, 3
                ),
                "narration_characters": int(weights[index]),
            }
        )
        start = end
    return plan


# --------------------------------------------------------------------------- #
# D7 — caption / audio QA and reduced-motion, in SEPARATE lanes
# --------------------------------------------------------------------------- #

QA_LANE_TECHNICAL = "technical"
QA_LANE_CREATIVE = "creative"
QA_LANE_HUMAN = "human_acceptance"

#: Photosensitivity guard: more than this many hard visual alternations inside
#: the window below is flagged as a flashing risk.
FLASHING_WINDOW_SECONDS = 1.0
FLASHING_MAX_ALTERNATIONS = 3

#: Reading-speed caps, mirrored from voice_service.timed_animatic so the caption
#: QA and the caption builder can never disagree.
CAPTION_CPS_CAP = {"latin": 20.0, "myanmar": 17.0}
CAPTION_MAX_LINES = 2
CAPTION_MAX_LINE_CHARACTERS = {"latin": 42, "myanmar": 34}


def _caption_language_bucket(render_input: dict[str, Any]) -> str:
    from voice_service.timed_animatic import is_burmese

    language = render_input.get("language")
    return "myanmar" if is_burmese(language if isinstance(language, str) else None) else "latin"


def _resolve_cues(render_input: dict[str, Any], cues: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    if cues is not None:
        return [dict(cue) for cue in cues if isinstance(cue, dict)]
    from backend.exports import caption_cues_from_render_input

    return caption_cues_from_render_input(render_input)


def audit_caption_readability(
    render_input: dict[str, Any], *, cues: list[dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    """TECHNICAL lane: can the captions actually be read in the time given?"""

    bucket = _caption_language_bucket(render_input)
    cps_cap = CAPTION_CPS_CAP[bucket]
    max_chars = CAPTION_MAX_LINE_CHARACTERS[bucket]
    resolved = _resolve_cues(render_input, cues)
    checks: list[dict[str, Any]] = []
    if not resolved:
        checks.append(
            {
                "id": "captions_present",
                "passed": False,
                "detail": "no caption cues could be derived from the render input",
            }
        )
        return checks
    checks.append({"id": "captions_present", "passed": True, "detail": f"{len(resolved)} cue(s)"})

    unreadable = [cue for cue in resolved if float(cue.get("characters_per_second") or 0) > cps_cap]
    checks.append(
        {
            "id": "caption_reading_speed",
            "passed": not unreadable,
            "detail": (
                f"{len(unreadable)} cue(s) exceed the {cps_cap} characters/second cap"
                if unreadable
                else f"all cues within the {cps_cap} characters/second cap"
            ),
            "offenders": [cue.get("segment_id") for cue in unreadable],
        }
    )

    too_long = [
        cue
        for cue in resolved
        for line in (cue.get("lines") or [])
        if len(str(line)) > max_chars
    ]
    checks.append(
        {
            "id": "caption_line_length",
            "passed": not too_long,
            "detail": (
                f"{len(too_long)} line(s) exceed {max_chars} characters"
                if too_long
                else f"all lines within {max_chars} characters"
            ),
        }
    )

    too_many = [cue for cue in resolved if len(cue.get("lines") or []) > CAPTION_MAX_LINES]
    checks.append(
        {
            "id": "caption_line_count",
            "passed": not too_many,
            "detail": (
                f"{len(too_many)} cue(s) exceed {CAPTION_MAX_LINES} lines"
                if too_many
                else f"all cues within {CAPTION_MAX_LINES} lines"
            ),
        }
    )

    overlapping: list[str] = []
    ordered = sorted(resolved, key=lambda cue: float(cue.get("start") or 0))
    for previous, current in zip(ordered, ordered[1:]):
        if float(current.get("start") or 0) < float(previous.get("end") or 0) - 1e-6:
            overlapping.append(str(current.get("segment_id")))
    checks.append(
        {
            "id": "caption_no_overlap",
            "passed": not overlapping,
            "detail": "cues overlap in time" if overlapping else "no cue overlaps the next",
            "offenders": overlapping,
        }
    )
    return checks


def audit_sound_off_comprehension(
    render_input: dict[str, Any], *, cues: list[dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    """TECHNICAL lane: does the video still communicate with the sound off?"""

    segments = render_input.get("segments") or []
    resolved = _resolve_cues(render_input, cues)
    captioned = {str(cue.get("segment_id")) for cue in resolved}

    narrated_without_caption: list[str] = []
    no_screen_text: list[str] = []
    numbers_off_screen: list[str] = []
    for segment in segments:
        if not isinstance(segment, dict):
            continue
        segment_id = str(segment.get("id") or segment.get("segment_id") or "")
        text = str(segment.get("text") or "").strip()
        visual = segment.get("visual") if isinstance(segment.get("visual"), dict) else {}
        # Render-input scenes keep typed visual information nested under
        # ``visual``.  Accept the legacy top-level spelling as well, but do not
        # let a nested screen-text contract disappear from sound-off QA.
        screen_text = segment.get("screen_text") or visual.get("screen_text")
        if text and segment_id not in captioned:
            narrated_without_caption.append(segment_id)
        if not screen_text:
            no_screen_text.append(segment_id)
        for number in re.findall(r"\d+(?:\.\d+)?", text):
            on_screen = " ".join(str(item) for item in (screen_text or [])) + " " + json.dumps(
                visual, ensure_ascii=False, sort_keys=True
            )
            if not _standalone_number_present(on_screen, number):
                numbers_off_screen.append(f"{segment_id}:{number}")

    return [
        {
            "id": "sound_off_captions_cover_narration",
            "passed": not narrated_without_caption,
            "detail": (
                f"{len(narrated_without_caption)} narrated scene(s) have no caption"
                if narrated_without_caption
                else "every narrated scene is captioned"
            ),
            "offenders": narrated_without_caption,
        },
        {
            "id": "sound_off_screen_text_present",
            "passed": not no_screen_text,
            "detail": (
                f"{len(no_screen_text)} scene(s) carry no on-screen text"
                if no_screen_text
                else "every scene carries on-screen text"
            ),
            "offenders": no_screen_text,
        },
        {
            "id": "sound_off_numbers_visible",
            "passed": not numbers_off_screen,
            "detail": (
                f"{len(numbers_off_screen)} spoken number(s) never appear on screen"
                if numbers_off_screen
                else "every spoken number is visible on screen"
            ),
            "offenders": numbers_off_screen,
        },
    ]


def audit_flashing(render_input: dict[str, Any]) -> list[dict[str, Any]]:
    """TECHNICAL lane: photosensitive flashing and strobe risk."""

    fps = render_input.get("fps")
    resolved_fps = float(fps) if isinstance(fps, (int, float)) and not isinstance(fps, bool) and fps > 0 else 30.0
    segments = render_input.get("segments") or []

    explicit: list[str] = []
    transitions: list[tuple[float, str]] = []
    for segment in segments:
        if not isinstance(segment, dict):
            continue
        segment_id = str(segment.get("id") or segment.get("segment_id") or "")
        visual = segment.get("visual") if isinstance(segment.get("visual"), dict) else {}
        blob = json.dumps(visual, ensure_ascii=False, sort_keys=True).lower()
        if any(token in blob for token in ("strobe", "flash", "blink", "flicker")):
            explicit.append(segment_id)
        start = segment.get("startFrame")
        if isinstance(start, int) and not isinstance(start, bool):
            kind = str(visual.get("kind") or "unknown")
            phase = str(visual.get("phase") or "")
            transitions.append((start / resolved_fps, f"{kind}:{phase}"))

    transitions.sort()
    rapid: list[str] = []
    window = FLASHING_WINDOW_SECONDS
    for index in range(len(transitions)):
        alternations = 0
        for offset in range(1, len(transitions) - index):
            if transitions[index + offset][0] - transitions[index][0] > window:
                break
            if transitions[index + offset][1] != transitions[index + offset - 1][1]:
                alternations += 1
        if alternations > FLASHING_MAX_ALTERNATIONS:
            rapid.append(f"{transitions[index][0]:.2f}s")

    return [
        {
            "id": "no_explicit_strobe",
            "passed": not explicit,
            "detail": (
                f"{len(explicit)} scene(s) declare a strobe/flash/blink visual"
                if explicit
                else "no scene declares a strobe, flash or blink visual"
            ),
            "offenders": explicit,
        },
        {
            "id": "no_rapid_visual_alternation",
            "passed": not rapid,
            "detail": (
                f"more than {FLASHING_MAX_ALTERNATIONS} hard visual alternations within "
                f"{window}s at {rapid}"
                if rapid
                else f"no more than {FLASHING_MAX_ALTERNATIONS} alternations within {window}s"
            ),
            "offenders": rapid,
        },
    ]


def audit_reduced_motion(render_input: dict[str, Any]) -> list[dict[str, Any]]:
    """TECHNICAL lane: was the reduced-motion output flag honoured?"""

    reduced = bool(render_input.get("reduced_motion"))
    controls = render_input.get("render_controls") if isinstance(render_input.get("render_controls"), dict) else {}
    animated = bool(controls.get("animated_lower_thirds", True))
    if not reduced:
        return [
            {
                "id": "reduced_motion_available",
                "passed": True,
                "detail": "reduced-motion output was not requested; full motion is expected",
            }
        ]
    return [
        {
            "id": "reduced_motion_requested",
            "passed": True,
            "detail": "reduced-motion output requested via the top-level reduced_motion flag",
        },
        {
            "id": "reduced_motion_suppresses_animation",
            "passed": not animated,
            "detail": (
                "animated_lower_thirds is still enabled while reduced motion is requested; "
                "the renderer must resolve the flag and suppress animation"
                if animated
                else "animated lower thirds are suppressed under reduced motion"
            ),
        },
    ]


def audit_caption_audio_creative(
    render_input: dict[str, Any], *, cues: list[dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    """CREATIVE lane: caption rhythm and audio balance judgement.

    This lane NEVER auto-approves.  A failure here routes to repair or
    ``needs_human_review`` exactly as the qa skill's invariants require; passing
    the technical lane above says nothing about this one.
    """

    resolved = _resolve_cues(render_input, cues)
    if not resolved:
        return [
            {
                "id": "creative_caption_rhythm",
                "passed": False,
                "detail": "no captions to judge; creative acceptance cannot be inferred",
                "route": "needs_human_review",
            }
        ]

    gaps: list[float] = []
    ordered = sorted(resolved, key=lambda cue: float(cue.get("start") or 0))
    for previous, current in zip(ordered, ordered[1:]):
        gaps.append(round(float(current.get("start") or 0) - float(previous.get("end") or 0), 3))
    breathless = [gap for gap in gaps if 0 < gap < 0.35]
    durations = [round(float(cue.get("end") or 0) - float(cue.get("start") or 0), 3) for cue in ordered]
    too_short = [duration for duration in durations if duration < 0.6]

    checks = [
        {
            "id": "creative_caption_rhythm",
            "passed": not breathless,
            "detail": (
                f"{len(breathless)} caption transition(s) are under 0.35s and read as breathless"
                if breathless
                else "caption transitions leave breathing room"
            ),
            "route": "repair" if breathless else None,
        },
        {
            "id": "creative_caption_dwell",
            "passed": not too_short,
            "detail": (
                f"{len(too_short)} cue(s) dwell under 0.6s, too brief to feel deliberate"
                if too_short
                else "every cue dwells long enough to feel deliberate"
            ),
            "route": "repair" if too_short else None,
        },
    ]
    mix = render_input.get("mix") if isinstance(render_input.get("mix"), dict) else None
    music_present = bool(mix.get("music_present")) if mix is not None else None
    balance_passed = bool(
        mix is not None and (not music_present or mix.get("ducking_enabled"))
    )
    checks.append(
        {
            "id": "creative_voice_music_balance",
            "passed": balance_passed,
            "detail": (
                "no mix plan is attached to the render input; voice/music balance is "
                "unjudged and cannot be assumed acceptable"
                if mix is None
                else (
                    "music ducks under narration"
                    if mix.get("ducking_enabled")
                    else (
                        "no music bed is present; narration is loudness-normalised"
                        if not music_present
                        else "music bed is present without ducking"
                    )
                )
            ),
            "route": "needs_human_review" if mix is None else None,
        }
    )
    return checks


def run_caption_audio_qa(
    render_input: dict[str, Any], *, cues: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """Caption and audio QA with technical and creative/human lanes kept apart.

    The report shape mirrors the qa skill's documented output
    (``technical_gate`` / ``creative_gate`` / ``overall`` / ``lanes_independent``)
    and the two gates are computed from disjoint check sets, so passing one can
    never be read as passing the other.
    """

    if not isinstance(render_input, dict):
        raise ValueError("render_input must be an object")
    resolved_cues = _resolve_cues(render_input, cues)

    technical_checks: list[dict[str, Any]] = []
    for check in (
        audit_caption_readability(render_input, cues=resolved_cues),
        audit_sound_off_comprehension(render_input, cues=resolved_cues),
        audit_flashing(render_input),
        audit_reduced_motion(render_input),
    ):
        technical_checks.extend(check)

    creative_checks = audit_caption_audio_creative(render_input, cues=resolved_cues)

    technical_passed = all(bool(check["passed"]) for check in technical_checks)
    creative_passed = all(bool(check["passed"]) for check in creative_checks)
    human_review = [
        check["id"] for check in creative_checks if check.get("route") == "needs_human_review"
    ] or (
        [] if creative_passed else [check["id"] for check in creative_checks if not check["passed"]]
    )

    return {
        "technical_gate": {
            "passed": technical_passed,
            "lane": QA_LANE_TECHNICAL,
            "checks": technical_checks,
            "failure_codes": [check["id"] for check in technical_checks if not check["passed"]],
        },
        "creative_gate": {
            "passed": creative_passed,
            "lane": QA_LANE_CREATIVE,
            "checks": creative_checks,
            "failure_codes": [check["id"] for check in creative_checks if not check["passed"]],
            # Creative failure never auto-approves: it routes to repair, and
            # anything unjudged routes to a human.
            "route": (
                "approved_for_review"
                if creative_passed
                else ("needs_human_review" if human_review else "repair")
            ),
        },
        "human_acceptance": {
            "lane": QA_LANE_HUMAN,
            "required": bool(human_review) or not creative_passed,
            "pending_ids": human_review,
            "statement": (
                "human acceptance is a separate decision; nothing in this report "
                "approves the work on a human's behalf"
            ),
        },
        "overall": {
            "passed": technical_passed and creative_passed,
            "technical_passed": technical_passed,
            "creative_passed": creative_passed,
        },
        "lanes_independent": True,
        "reduced_motion_requested": bool(render_input.get("reduced_motion")),
        "caption_cues": len(resolved_cues),
    }


def repair_caption_audio_plan(
    render_input: dict[str, Any],
    animatic: dict[str, Any],
    report: dict[str, Any],
) -> dict[str, Any] | None:
    """Apply one bounded, deterministic repair to a caption/audio plan.

    The repair is deliberately local: it may extend a short cue within its
    scene, trim a preceding cue to create the intentional-silence floor, and
    replace an unsafe music mix with the canonical side-chain ducking plan.  It
    never changes narration text or scene boundaries.  ``None`` means that the
    reported creative failure has no safe deterministic repair; callers must
    escalate that state instead of running the same QA report again.
    """

    if not isinstance(render_input, dict) or not isinstance(animatic, dict):
        return None
    if not isinstance(report, dict):
        return None
    technical = report.get("technical_gate")
    creative = report.get("creative_gate")
    if (
        not isinstance(technical, dict)
        or technical.get("passed") is not True
        or not isinstance(creative, dict)
        or creative.get("passed") is True
        or creative.get("route") != "repair"
    ):
        return None
    raw_codes = creative.get("failure_codes")
    failure_codes = [str(code) for code in raw_codes] if isinstance(raw_codes, list) else []
    supported_codes = {
        "creative_caption_dwell",
        "creative_caption_rhythm",
        "creative_voice_music_balance",
    }
    if not failure_codes or not set(failure_codes).issubset(supported_codes):
        return None

    captions = animatic.get("captions")
    if not isinstance(captions, list):
        captions = []
    repaired = deepcopy(animatic)
    repaired_captions = [cue for cue in repaired.get("captions", []) if isinstance(cue, dict)]
    if len(repaired_captions) != len(captions):
        return None

    fps = render_input.get("fps")
    if isinstance(fps, bool) or not isinstance(fps, (int, float)) or fps <= 0:
        fps = 30.0
    scene_bounds: dict[str, tuple[float, float]] = {}
    for segment in render_input.get("segments") or []:
        if not isinstance(segment, dict):
            continue
        segment_id = str(segment.get("id") or segment.get("segment_id") or "")
        try:
            start_frame = float(segment.get("startFrame"))
            end_frame = float(segment.get("endFrame"))
        except (TypeError, ValueError):
            continue
        if not segment_id or not math.isfinite(start_frame) or not math.isfinite(end_frame):
            continue
        start = start_frame / float(fps)
        end = end_frame / float(fps)
        if end > start:
            scene_bounds[segment_id] = (start, end)

    ordered: list[dict[str, Any]] = []
    for cue in repaired_captions:
        try:
            start = float(cue.get("start"))
            end = float(cue.get("end"))
        except (TypeError, ValueError):
            return None
        if not math.isfinite(start) or not math.isfinite(end) or end <= start:
            return None
        cue["start"] = start
        cue["end"] = end
        ordered.append(cue)
    ordered.sort(key=lambda cue: (float(cue["start"]), float(cue["end"]), str(cue.get("segment_id") or "")))

    changed = False

    def set_end(cue: dict[str, Any], end: float) -> None:
        nonlocal changed
        if abs(end - float(cue["end"])) <= 1e-6:
            return
        cue["end"] = round(end, 3)
        changed = True

    # First make a short cue readable when its scene and the next caption leave
    # enough room.  A cue that cannot receive the minimum dwell is escalated.
    if "creative_caption_dwell" in failure_codes:
        for index, cue in enumerate(ordered):
            start = float(cue["start"])
            end = float(cue["end"])
            if end - start >= 0.6 - 1e-6:
                continue
            segment_end = scene_bounds.get(str(cue.get("segment_id") or ""), (start, end))[1]
            next_start = float(ordered[index + 1]["start"]) if index + 1 < len(ordered) else segment_end
            candidate = min(start + 0.6, segment_end, next_start)
            if candidate - start >= 0.6 - 1e-6:
                set_end(cue, candidate)

    # Then create intentional silence by trimming a preceding cue only when its
    # own readable dwell remains intact.  Overlap is a technical failure and is
    # intentionally left for the technical gate to block.
    if "creative_caption_rhythm" in failure_codes:
        for previous, current in zip(ordered, ordered[1:]):
            gap = float(current["start"]) - float(previous["end"])
            if 0 < gap < 0.35:
                candidate = float(current["start"]) - 0.35
                if candidate - float(previous["start"]) >= 0.6 - 1e-6:
                    set_end(previous, candidate)

    if "creative_voice_music_balance" in failure_codes:
        mix = repaired.get("mix")
        if isinstance(mix, dict) and mix.get("music_present") is True and mix.get("ducking_enabled") is not True:
            from voice_service.timed_animatic import build_mix_plan

            repaired["mix"] = build_mix_plan(has_music=True).as_dict()
            changed = True

    if not changed:
        return None

    from voice_service.timed_animatic import characters_per_second_cap

    for cue in repaired_captions:
        duration = max(float(cue["end"]) - float(cue["start"]), 1e-6)
        text = str(cue.get("text") or "")
        cps = len(text.replace(" ", "")) / duration
        cue["characters_per_second"] = round(cps, 2)
        cue["readable"] = not bool(cue.get("overflowed")) and cps <= characters_per_second_cap(
            render_input.get("language") if isinstance(render_input.get("language"), str) else None
        ) * 1.15

    history = repaired.get("repair_history")
    history = list(history) if isinstance(history, list) else []
    history.append({
        "kind": "caption_audio",
        "failure_codes": failure_codes,
        "strategy": "deterministic_caption_reflow_and_mix_ducking",
    })
    repaired["repair_history"] = history
    return repaired if repaired != animatic else None


def route_caption_audio_qa(
    report: dict[str, Any], *, attempt: int = 0, max_repairs: int = 1
) -> dict[str, Any]:
    """Route one caption/audio QA report through bounded deterministic policy.

    Technical failures are integrity failures and are therefore blocked before
    rendering can continue.  Creative failures may take one explicitly bounded
    repair route; once that budget is exhausted (or the report asks for human
    judgement), the result is ``needs_human_review``.  A passing automated
    report remains only ``pass``/``approved_for_review``: it never records a
    human acceptance decision.
    """

    if not isinstance(report, dict):
        return {
            "route": "blocked",
            "reason": "caption_audio_report_invalid",
            "attempt": attempt,
            "max_repairs": max_repairs,
            "automated_passed": False,
            "human_acceptance_required": False,
        }
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 0:
        raise ValueError("attempt must be a non-negative integer")
    if isinstance(max_repairs, bool) or not isinstance(max_repairs, int) or max_repairs < 0:
        raise ValueError("max_repairs must be a non-negative integer")

    technical = report.get("technical_gate")
    if not isinstance(technical, dict) or technical.get("passed") is not True:
        return {
            "route": "blocked",
            "reason": "technical_gate_failed",
            "failure_codes": list(technical.get("failure_codes") or [])
            if isinstance(technical, dict)
            else ["technical_gate_missing"],
            "attempt": attempt,
            "max_repairs": max_repairs,
            "automated_passed": False,
            "human_acceptance_required": False,
        }

    creative = report.get("creative_gate")
    if not isinstance(creative, dict):
        return {
            "route": "needs_human_review",
            "reason": "creative_gate_missing",
            "attempt": attempt,
            "max_repairs": max_repairs,
            "automated_passed": False,
            "human_acceptance_required": True,
        }
    overall = report.get("overall")
    if (
        isinstance(overall, dict)
        and overall.get("passed") is False
        and creative.get("passed") is True
    ):
        return {
            "route": "blocked",
            "reason": "caption_audio_report_inconsistent",
            "failure_codes": ["QA_REPORT_INCONSISTENT"],
            "attempt": attempt,
            "max_repairs": max_repairs,
            "automated_passed": False,
            "human_acceptance_required": False,
        }
    if creative.get("passed") is True:
        return {
            "route": "pass",
            "reason": "technical_and_creative_qa_passed",
            "attempt": attempt,
            "max_repairs": max_repairs,
            "automated_passed": True,
            "human_acceptance_required": True,
        }

    failure_codes = list(creative.get("failure_codes") or [])
    requested_route = creative.get("route")
    if requested_route == "repair" and attempt < max_repairs:
        return {
            "route": "repair",
            "reason": "creative_qa_repair_requested",
            "failure_codes": failure_codes,
            "attempt": attempt,
            "max_repairs": max_repairs,
            "automated_passed": False,
            "human_acceptance_required": False,
        }
    return {
        "route": "needs_human_review",
        "reason": (
            "creative_qa_requires_human_review"
            if requested_route == "needs_human_review"
            else "creative_repair_budget_exhausted"
        ),
        "failure_codes": failure_codes,
        "attempt": attempt,
        "max_repairs": max_repairs,
        "automated_passed": False,
        "human_acceptance_required": True,
    }


def caption_audio_quality_checks() -> list[Any]:
    """The D7 checks as qa-skill ``QualityCheck`` delegations, lanes intact.

    Reusing the skill's descriptor type means these checks are registered with
    the same ``kind`` vocabulary (technical / creative / human_acceptance) the
    rest of the acceptance boundary uses, rather than inventing a parallel one.
    """

    from backend.agent.skills.base import QualityCheck

    return [
        QualityCheck(
            name="caption_readability",
            module="backend.render_contract",
            attribute="audit_caption_readability",
            description="Caption reading speed, line length, line count and overlap.",
            kind=QA_LANE_TECHNICAL,
        ),
        QualityCheck(
            name="sound_off_comprehension",
            module="backend.render_contract",
            attribute="audit_sound_off_comprehension",
            description="Captions cover narration; screen text and numbers are visible.",
            kind=QA_LANE_TECHNICAL,
        ),
        QualityCheck(
            name="flashing_risk",
            module="backend.render_contract",
            attribute="audit_flashing",
            description="Photosensitive strobe and rapid visual alternation checks.",
            kind=QA_LANE_TECHNICAL,
        ),
        QualityCheck(
            name="reduced_motion_honoured",
            module="backend.render_contract",
            attribute="audit_reduced_motion",
            description="The optional reduced-motion output flag is honoured.",
            kind=QA_LANE_TECHNICAL,
        ),
        QualityCheck(
            name="caption_audio_creative",
            module="backend.render_contract",
            attribute="audit_caption_audio_creative",
            description="Caption rhythm and voice/music balance; never auto-approved.",
            kind=QA_LANE_CREATIVE,
        ),
    ]
