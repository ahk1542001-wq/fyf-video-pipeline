"""Deterministic export builders (Stage D5).

Every builder here is a PURE function: same inputs, same bytes, no provider call,
no network, no cost.  The only side effecting helper is :func:`run_export`, which
either writes the bytes a pure builder produced or executes the ffmpeg command a
pure builder specified — it never decides *what* to export.

Two rules shape the module:

1. **Only produce what the user selected.**  :func:`plan_exports` iterates the
   requested kinds and nothing else; there is no "convenience" default set that
   silently renders extra artifacts.
2. **Unknown or absent inputs are reported, never fabricated.**  An unrecognised
   kind raises :class:`UnknownExportKindError`; a missing source video or a
   missing render input raises :class:`ExportInputMissing`.  No placeholder file
   is ever written in place of a real export.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from backend.mouth_cues import FPS as MOUTH_CUES_FPS
from backend.render_manifest import read_render_manifest, verify_render_manifest
from backend.segment_render_cache import FFMPEG_BIN, FFPROBE_BIN
from voice_service.timed_animatic import (
    build_caption_cues,
    characters_per_second_cap,
    is_burmese,
)


EXPORT_CONTRACT_VERSION = 1

#: Brand ivory from remotion/src/theme.ts, used as the letterbox/pillarbox fill
#: so a reframe never crops content out of the safe zone.
PAD_COLOR = "0xF4F0E6"


class UnknownExportKindError(ValueError):
    """Raised for a kind the registry does not define (never guessed at)."""


class ExportInputMissing(ValueError):
    """Raised when a required input is absent; no placeholder is produced."""


@dataclass(frozen=True)
class ExportSpec:
    kind: str
    family: str
    extension: str
    media_type: str
    description: str
    container: str | None = None
    video_codec: str | None = None
    audio_codec: str | None = None
    aspect_ratio: str | None = None
    width: int | None = None
    height: int | None = None
    captions: bool | None = None


_EXPORT_SPECS: tuple[ExportSpec, ...] = tuple(
    [
        ExportSpec(
            kind=f"video-1080p-{tag}",
            family="video",
            extension=".mp4",
            media_type="video/mp4",
            description=f"1080p H.264/AAC MP4 at {ratio}, no burned-in captions",
            container="mp4",
            video_codec="h264",
            audio_codec="aac",
            aspect_ratio=ratio,
            width=width,
            height=height,
            captions=False,
        )
        for tag, ratio, width, height in (
            ("9x16", "9:16", 1080, 1920),
            ("16x9", "16:9", 1920, 1080),
            ("1x1", "1:1", 1080, 1080),
        )
    ]
    + [
        ExportSpec(
            kind=f"video-1080p-{tag}-captions",
            family="video",
            extension=".mp4",
            media_type="video/mp4",
            description=f"1080p H.264/AAC MP4 at {ratio} with burned-in captions",
            container="mp4",
            video_codec="h264",
            audio_codec="aac",
            aspect_ratio=ratio,
            width=width,
            height=height,
            captions=True,
        )
        for tag, ratio, width, height in (
            ("9x16", "9:16", 1080, 1920),
            ("16x9", "16:9", 1920, 1080),
            ("1x1", "1:1", 1080, 1080),
        )
    ]
    + [
        ExportSpec(
            kind="captions-srt",
            family="captions",
            extension=".srt",
            media_type="application/x-subrip",
            description="SubRip captions timed from backend.mouth_cues",
        ),
        ExportSpec(
            kind="captions-vtt",
            family="captions",
            extension=".vtt",
            media_type="text/vtt",
            description="WebVTT captions timed from backend.mouth_cues",
        ),
        ExportSpec(
            kind="thumbnail",
            family="thumbnail",
            extension=".jpg",
            media_type="image/jpeg",
            description="Single deterministic keyframe from the assembled video",
        ),
        ExportSpec(
            kind="transcript",
            family="transcript",
            extension=".md",
            media_type="text/markdown",
            description="Human-readable narration transcript with measured timings",
        ),
        ExportSpec(
            kind="provenance",
            family="provenance",
            extension=".md",
            media_type="text/markdown",
            description="Human-readable reproducibility and provenance report",
        ),
    ]
)

EXPORT_SPECS: dict[str, ExportSpec] = {spec.kind: spec for spec in _EXPORT_SPECS}
EXPORT_KINDS: tuple[str, ...] = tuple(spec.kind for spec in _EXPORT_SPECS)


def list_export_kinds() -> list[dict[str, Any]]:
    """The full selectable catalogue (the UI shows exactly this)."""

    return [
        {
            "kind": spec.kind,
            "family": spec.family,
            "extension": spec.extension,
            "media_type": spec.media_type,
            "description": spec.description,
            "aspect_ratio": spec.aspect_ratio,
            "width": spec.width,
            "height": spec.height,
            "captions": spec.captions,
        }
        for spec in _EXPORT_SPECS
    ]


def resolve_export_spec(kind: str) -> ExportSpec:
    spec = EXPORT_SPECS.get(str(kind))
    if spec is None:
        raise UnknownExportKindError(
            f"unknown export kind {kind!r}; supported kinds: {', '.join(EXPORT_KINDS)}"
        )
    return spec


def plan_exports(kinds: Sequence[str]) -> list[ExportSpec]:
    """Validate a user selection.  ONLY the selected kinds come back.

    Duplicates are collapsed and order is preserved; an unknown kind fails the
    whole selection rather than being skipped, because silently dropping a
    requested export is the same lie as fabricating one.
    """

    if not isinstance(kinds, Sequence) or isinstance(kinds, (str, bytes)):
        raise UnknownExportKindError("export kinds must be a sequence of strings")
    selected: list[ExportSpec] = []
    seen: set[str] = set()
    for kind in kinds:
        spec = resolve_export_spec(kind)
        if spec.kind not in seen:
            seen.add(spec.kind)
            selected.append(spec)
    return selected


# --------------------------------------------------------------------------- #
# Caption timings — reuse backend.mouth_cues
# --------------------------------------------------------------------------- #

#: A cue start may be pulled forward to the first mouth cue inside this window.
MAX_CUE_SNAP_SECONDS = 0.45


def caption_cues_from_render_input(
    render_input: Mapping[str, Any], *, language: str | None = None
) -> list[dict[str, Any]]:
    """Caption cues whose timings come from ``backend.mouth_cues``.

    Segment frame ranges are converted at :data:`backend.mouth_cues.FPS` (the
    same 30fps contract the mouth cues were measured against) and each cue start
    is snapped forward to the first amplitude mouth cue inside the segment, so a
    caption appears when the mouth actually starts moving instead of when the
    frame range begins.  Line breaking and the reading-speed cap come from the
    D4 animatic planner, which is Burmese-syllable aware.
    """

    if not isinstance(render_input, Mapping):
        raise ExportInputMissing("render input must be an object")
    segments = render_input.get("segments")
    if not isinstance(segments, list) or not segments:
        raise ExportInputMissing("render input has no segments; captions cannot be timed")
    resolved_language = language if isinstance(language, str) else render_input.get("language")

    raw_cues = render_input.get("mouthCues")
    mouth_cues: list[dict[str, float]] = []
    if isinstance(raw_cues, list):
        for cue in raw_cues:
            if isinstance(cue, Mapping):
                start, end = cue.get("start"), cue.get("end")
                if isinstance(start, (int, float)) and isinstance(end, (int, float)):
                    mouth_cues.append({"start": float(start), "end": float(end)})
    mouth_cues.sort(key=lambda cue: cue["start"])

    fps = render_input.get("fps")
    resolved_fps = float(fps) if isinstance(fps, (int, float)) and not isinstance(fps, bool) and fps > 0 else float(MOUTH_CUES_FPS)

    segment_spans: dict[str, tuple[float, float]] = {}
    for segment in segments:
        if isinstance(segment, Mapping):
            segment_id = str(segment.get("id") or segment.get("segment_id") or "")
            start, end = segment.get("startFrame"), segment.get("endFrame")
            if segment_id and isinstance(start, int) and isinstance(end, int):
                segment_spans[segment_id] = (start / resolved_fps, end / resolved_fps)

    cues: list[dict[str, Any]] = []
    for cue in build_caption_cues(segments, fps=resolved_fps, language=resolved_language):
        span = segment_spans.get(cue.segment_id, (cue.start, cue.end))
        snapped = cue.start
        for mouth in mouth_cues:
            if mouth["start"] < span[0] - 1e-6 or mouth["start"] >= cue.end:
                continue
            if mouth["start"] > cue.start and (mouth["start"] - cue.start) <= MAX_CUE_SNAP_SECONDS:
                snapped = round(mouth["start"], 3)
                break
        cues.append(
            {
                "segment_id": cue.segment_id,
                "start": snapped,
                "end": cue.end,
                "text": cue.text,
                "lines": list(cue.lines),
                "characters_per_second": cue.characters_per_second,
                "readable": cue.readable,
                "timing_source": "mouth_cues",
            }
        )
    return cues


def _timestamp(seconds: float, *, vtt: bool) -> str:
    if seconds < 0:
        raise ValueError("timestamp seconds must be non-negative")
    total_ms = int(round(seconds * 1000))
    hours, remainder = divmod(total_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    separator = "." if vtt else ","
    return f"{hours:02d}:{minutes:02d}:{secs:02d}{separator}{millis:03d}"


def build_srt(cues: Sequence[Mapping[str, Any]]) -> str:
    """SubRip text.  Deterministic: no clock, no random ids, LF newlines."""

    blocks: list[str] = []
    for index, cue in enumerate(cues, start=1):
        lines = [str(line) for line in (cue.get("lines") or [cue.get("text", "")]) if str(line).strip()]
        blocks.append(
            "\n".join(
                [
                    str(index),
                    f"{_timestamp(float(cue['start']), vtt=False)} --> {_timestamp(float(cue['end']), vtt=False)}",
                    *(lines or [""]),
                ]
            )
        )
    return ("\n\n".join(blocks) + "\n") if blocks else ""


def build_vtt(cues: Sequence[Mapping[str, Any]]) -> str:
    """WebVTT text with per-cue identifiers derived from the segment id."""

    out = ["WEBVTT", ""]
    for index, cue in enumerate(cues, start=1):
        lines = [str(line) for line in (cue.get("lines") or [cue.get("text", "")]) if str(line).strip()]
        identifier = f"{cue.get('segment_id') or 'cue'}-{index}"
        out.append(identifier)
        out.append(
            f"{_timestamp(float(cue['start']), vtt=True)} --> {_timestamp(float(cue['end']), vtt=True)}"
        )
        out.extend(lines or [""])
        out.append("")
    return "\n".join(out)


def build_transcript(
    render_input: Mapping[str, Any], *, cues: Sequence[Mapping[str, Any]] | None = None
) -> str:
    """Human-readable narration transcript with measured timings."""

    resolved = list(cues) if cues is not None else caption_cues_from_render_input(render_input)
    language = render_input.get("language")
    lines = [
        "# FYF narration transcript",
        "",
        f"- language: {language if isinstance(language, str) else 'unspecified'}",
        f"- fps: {render_input.get('fps') or MOUTH_CUES_FPS}",
        f"- duration frames: {render_input.get('durationInFrames')}",
        f"- timing source: {render_input.get('segmentTimingSource') or 'unmeasured'}",
        f"- caption cues: {len(resolved)}",
        "",
    ]
    if not resolved:
        lines.append("_No narration text was present in the render input; nothing is transcribed._")
        return "\n".join(lines) + "\n"
    for cue in resolved:
        lines.append(f"## {cue.get('segment_id')} — {cue.get('start')}s to {cue.get('end')}s")
        lines.append("")
        lines.append(str(cue.get("text") or ""))
        if not cue.get("readable", True):
            lines.append("")
            lines.append(
                f"> readability warning: {cue.get('characters_per_second')} characters/second "
                f"exceeds the {characters_per_second_cap(language)} cap for this language"
            )
        lines.append("")
    return "\n".join(lines) + "\n"


def build_provenance_report(
    *,
    render_input: Mapping[str, Any] | None = None,
    manifest_document: Mapping[str, Any] | None = None,
    version: Mapping[str, Any] | None = None,
    budget: Mapping[str, Any] | None = None,
    export_kinds: Sequence[str] = (),
) -> str:
    """Human-readable provenance report.

    States plainly what is reproducible and what is not.  Absent inputs are
    listed as absent; nothing is filled in with a plausible-looking value.
    """

    lines = [
        "# FYF render provenance report",
        "",
        f"export contract version: {EXPORT_CONTRACT_VERSION}",
        f"requested exports: {', '.join(export_kinds) if export_kinds else 'none'}",
        "",
        "## Reproducibility scope",
        "",
        "This video is reproducible **from stored assets only**. Model",
        "regeneration (narration drafting, image generation, TTS synthesis) is",
        "**not bit-identical**: the same prompt can return different bytes on a",
        "later run, so re-running the creative pipeline is not guaranteed to",
        "reproduce this output. Re-rendering the recorded assets is.",
        "",
        "## Render manifest",
        "",
    ]
    if manifest_document:
        manifest = manifest_document.get("render_manifest") or {}
        lines.extend(
            [
                f"- manifest fingerprint: {manifest_document.get('manifest_fingerprint')}",
                f"- video spec version: {manifest.get('video_spec_version')}",
                f"- renderer version: {manifest.get('renderer_version')}",
                f"- renderer source hash: {manifest_document.get('renderer_source_hash')}",
                f"- composition id: {manifest_document.get('composition_id')}",
                f"- motion mode: {manifest_document.get('motion_mode')}",
                f"- generation enabled at render time: {manifest_document.get('generation_enabled')}",
                f"- recorded at: {manifest_document.get('created_at')}",
                "",
                "### Fonts",
                "",
            ]
        )
        fonts = manifest.get("fonts") or []
        lines.extend([f"- {entry}" for entry in fonts] or ["- none recorded"])
        lines.extend(["", "### Skill versions", ""])
        skills = manifest.get("skill_versions") or {}
        lines.extend([f"- {name}: {value}" for name, value in sorted(skills.items())] or ["- none recorded"])
        lines.extend(["", "### Seeds", ""])
        seeds = manifest.get("seeds") or {}
        lines.extend(
            [f"- {name}: {value}" for name, value in sorted(seeds.items())]
            or ["- none declared (the render pipeline is deterministic and seed-free)"]
        )
        lines.extend(["", "### Asset hashes", ""])
        assets = manifest.get("asset_hashes") or {}
        lines.extend([f"- {name}: {digest}" for name, digest in sorted(assets.items())] or ["- none recorded"])
        lines.extend(["", "### Segments", ""])
        for entry in manifest_document.get("segments") or []:
            lines.append(
                f"- {entry.get('segment_id')}: {entry.get('frame_count')} frames, "
                f"sha256={entry.get('sha256')}, cache_hit={entry.get('cache_hit')}"
            )
        if not manifest_document.get("segments"):
            lines.append("- none recorded")
    else:
        lines.append("_No render manifest was supplied; reproducibility cannot be asserted._")

    lines.extend(["", "## Output geometry", ""])
    if render_input:
        controls = render_input.get("render_controls") if isinstance(render_input.get("render_controls"), Mapping) else {}
        lines.extend(
            [
                f"- aspect ratio: {controls.get('aspect_ratio') or render_input.get('aspect_ratio') or 'unspecified'}",
                f"- width x height: {render_input.get('width')} x {render_input.get('height')}",
                f"- fps: {render_input.get('fps')}",
                f"- duration frames: {render_input.get('durationInFrames')}",
                f"- reduced motion: {bool(render_input.get('reduced_motion'))}",
                f"- language: {render_input.get('language') or 'unspecified'}",
            ]
        )
    else:
        lines.append("_No render input was supplied._")

    if version:
        lines.extend(
            [
                "",
                "## Project version",
                "",
                f"- project: {version.get('project_id')}",
                f"- version: {version.get('version_no')}",
                f"- actor: {version.get('actor')}",
                f"- created at: {version.get('created_at')}",
                f"- applied operations: {', '.join(version.get('applied_operations') or []) or 'none'}",
                f"- source command operation: {version.get('source_command_operation') or 'none'}",
            ]
        )

    lines.extend(["", "## Cost", ""])
    if budget:
        lines.append(f"- committed spend reported by the budget ledger: {budget.get('committed_usd', budget)}")
        lines.append("- unknown costs are reported as null by the ledger and are never shown as 0")
    else:
        lines.append("_No budget ledger state was supplied; spend is unmeasured here._")

    lines.extend(["", "## What this report does not claim", ""])
    lines.append("- It does not claim creative acceptance; technical QA and human")
    lines.append("  acceptance are separate reports produced by the qa skill lanes.")
    lines.append("- It does not claim provider latency figures; those are measured")
    lines.append("  separately and reported as measured-and-met, measured-and-missed")
    lines.append("  or unmeasured.")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# Artifact construction
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ExportArtifact:
    """A fully specified export: bytes for text kinds, a command for media kinds."""

    kind: str
    filename: str
    media_type: str
    content: bytes | None = None
    command: list[str] | None = None
    inputs: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def is_text(self) -> bool:
        return self.content is not None


def _load_render_input(job_dir: Path) -> dict[str, Any]:
    path = job_dir / "render_input.json"
    if not path.is_file():
        raise ExportInputMissing(f"render_input.json not found in {job_dir}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ExportInputMissing("render_input.json is corrupt") from exc
    if not isinstance(payload, dict):
        raise ExportInputMissing("render_input.json must contain an object")
    return payload


def resolve_source_video(job_dir: Path, *, version: Mapping[str, Any] | None = None) -> Path:
    """Locate the assembled video for a job, optionally via a version's assets."""

    candidate = job_dir / "video.mp4"
    if candidate.is_file() and candidate.stat().st_size > 0:
        return candidate
    for reference in (version or {}).get("asset_references") or []:
        if not isinstance(reference, Mapping) or reference.get("kind") != "video":
            continue
        uri = reference.get("uri")
        if not isinstance(uri, str) or not uri.strip():
            continue
        path = Path(uri)
        if not path.is_absolute():
            path = job_dir / path
        if path.is_file() and path.stat().st_size > 0:
            return path
    raise ExportInputMissing(
        "no assembled video is available for this version; run a render before exporting"
    )


def build_video_command(
    source: Path,
    output: Path,
    spec: ExportSpec,
    *,
    subtitles_path: Path | None = None,
    canonical_settings: Mapping[str, Any] | None = None,
) -> list[str]:
    """ffmpeg command for a 1080p H.264/AAC MP4 export.

    Reframing is SAFE-ZONE AWARE: the source is scaled to fit entirely inside
    the target frame and padded with the brand ivory, never cropped, so no
    caption, lower third or subject is cut off by a ratio change.  A true
    re-composition is the ``reframe_aspect`` command's job (D6), not an export's.
    """

    width = int(spec.width or 1080)
    height = int(spec.height or 1920)
    settings = dict(canonical_settings or {})
    raw_fps = settings.get("fps", 30)
    if not isinstance(raw_fps, (int, float)) or isinstance(raw_fps, bool) or raw_fps <= 0:
        raise ExportInputMissing("verified manifest has an invalid fps")
    fps = str(int(raw_fps) if float(raw_fps).is_integer() else float(raw_fps))
    codec = settings.get("codec", "h264")
    if codec != "h264":
        raise ExportInputMissing(f"unsupported verified-manifest video codec: {codec!r}")
    pixel_format = settings.get("pixel_format", "yuv420p")
    if pixel_format != "yuv420p":
        raise ExportInputMissing(
            f"unsupported verified-manifest pixel format: {pixel_format!r}"
        )
    video_filters = [
        f"scale={width}:{height}:force_original_aspect_ratio=decrease",
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color={PAD_COLOR}",
        "setsar=1",
        "format=yuv420p",
    ]
    if subtitles_path is not None:
        escaped = str(subtitles_path).replace("\\", "/").replace(":", "\\:")
        video_filters.append(f"subtitles='{escaped}'")
    return [
        FFMPEG_BIN,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(source),
        "-vf",
        ",".join(video_filters),
        "-r",
        fps,
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "18",
        "-pix_fmt",
        pixel_format,
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-ar",
        "48000",
        "-movflags",
        "+faststart",
        str(output),
    ]


def build_thumbnail_command(source: Path, output: Path, *, at_seconds: float) -> list[str]:
    """Deterministic single-frame thumbnail at a computed timestamp."""

    return [
        FFMPEG_BIN,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        f"{max(0.0, round(at_seconds, 3))}",
        "-i",
        str(source),
        "-frames:v",
        "1",
        "-q:v",
        "2",
        str(output),
    ]


def thumbnail_timestamp(render_input: Mapping[str, Any]) -> float:
    """Midpoint of the first segment that has narration — deterministic."""

    fps = render_input.get("fps")
    resolved_fps = float(fps) if isinstance(fps, (int, float)) and not isinstance(fps, bool) and fps > 0 else float(MOUTH_CUES_FPS)
    for segment in render_input.get("segments") or []:
        if not isinstance(segment, Mapping):
            continue
        start, end = segment.get("startFrame"), segment.get("endFrame")
        if isinstance(start, int) and isinstance(end, int) and end > start:
            return round(((start + end) / 2.0) / resolved_fps, 3)
    return 1.0


def build_export_artifact(
    kind: str,
    *,
    job_dir: str | Path,
    render_input: Mapping[str, Any] | None = None,
    manifest_document: Mapping[str, Any] | None = None,
    version: Mapping[str, Any] | None = None,
    budget: Mapping[str, Any] | None = None,
    output_dir: str | Path | None = None,
) -> ExportArtifact:
    """Build exactly one export artifact for ``kind`` (pure, no execution)."""

    spec = resolve_export_spec(kind)
    root = Path(job_dir)
    if not root.is_dir():
        raise ExportInputMissing(f"job directory not found: {root}")
    resolved_input = dict(render_input) if render_input is not None else _load_render_input(root)
    verified_document: dict[str, Any] | None = None
    canonical_settings: dict[str, Any] = {}
    source_geometry: dict[str, Any] = {}
    if manifest_document is not None:
        try:
            verification = verify_render_manifest(root, manifest_document)
        except (FileNotFoundError, OSError, TypeError, ValueError) as exc:
            raise ExportInputMissing("render manifest is unreadable") from exc
        if not verification.get("verified"):
            raise ExportInputMissing("render manifest failed integrity verification")
        verified_document = dict(manifest_document)
        raw_settings = verified_document.get("output_settings")
        raw_geometry = verified_document.get("source_geometry")
        if not isinstance(raw_settings, Mapping) or not isinstance(raw_geometry, Mapping):
            raise ExportInputMissing("verified render manifest is missing output settings")
        canonical_settings = {
            "fps": raw_settings.get("fps"),
            "codec": raw_settings.get("codec"),
            "pixel_format": raw_settings.get("pixel_format"),
        }
        source_geometry = {
            "width": raw_geometry.get("width"),
            "height": raw_geometry.get("height"),
        }
    destination = Path(output_dir) if output_dir is not None else root / "exports"
    filename = f"{spec.kind}{spec.extension}"
    warnings: list[str] = []

    if spec.family == "captions":
        cues = caption_cues_from_render_input(resolved_input)
        if not cues:
            raise ExportInputMissing("render input produced no caption cues")
        text = build_vtt(cues) if spec.kind == "captions-vtt" else build_srt(cues)
        unreadable = [cue for cue in cues if not cue.get("readable", True)]
        if unreadable:
            warnings.append(f"{len(unreadable)} caption cue(s) exceed the reading-speed cap")
        return ExportArtifact(spec.kind, filename, spec.media_type, content=text.encode("utf-8"), warnings=warnings)

    if spec.family == "transcript":
        cues = caption_cues_from_render_input(resolved_input)
        return ExportArtifact(
            spec.kind,
            filename,
            spec.media_type,
            content=build_transcript(resolved_input, cues=cues).encode("utf-8"),
            warnings=warnings,
        )

    if spec.family == "provenance":
        text = build_provenance_report(
            render_input=resolved_input,
            manifest_document=verified_document,
            version=version,
            budget=budget,
            export_kinds=[spec.kind],
        )
        if manifest_document is None:
            warnings.append("no render manifest supplied; the report states reproducibility cannot be asserted")
        return ExportArtifact(spec.kind, filename, spec.media_type, content=text.encode("utf-8"), warnings=warnings)

    source = resolve_source_video(root, version=version)
    if spec.family == "thumbnail":
        output = destination / filename
        return ExportArtifact(
            spec.kind,
            filename,
            spec.media_type,
            command=build_thumbnail_command(source, output, at_seconds=thumbnail_timestamp(resolved_input)),
            inputs={"source": str(source), "at_seconds": thumbnail_timestamp(resolved_input)},
            warnings=warnings,
        )

    output = destination / filename
    subtitles_path: Path | None = None
    if spec.captions:
        cues = caption_cues_from_render_input(resolved_input)
        if not cues:
            raise ExportInputMissing("burned-in captions requested but the render input produced no cues")
        subtitles_path = destination / f"{spec.kind}.srt"
        warnings.append(f"captions burned in from {subtitles_path.name}")
    return ExportArtifact(
        spec.kind,
        filename,
        spec.media_type,
        command=build_video_command(
            source,
            output,
            spec,
            subtitles_path=subtitles_path,
            canonical_settings=canonical_settings,
        ),
        inputs={
            "source": str(source),
            "subtitles": str(subtitles_path) if subtitles_path else None,
            "aspect_ratio": spec.aspect_ratio,
            "width": spec.width,
            "height": spec.height,
            "source_geometry": source_geometry,
            "canonical_settings": canonical_settings,
        },
        warnings=warnings,
    )


def run_export(
    kind: str,
    *,
    job_dir: str | Path,
    output_dir: str | Path | None = None,
    render_input: Mapping[str, Any] | None = None,
    manifest_document: Mapping[str, Any] | None = None,
    version: Mapping[str, Any] | None = None,
    budget: Mapping[str, Any] | None = None,
    timeout_seconds: int = 1800,
) -> dict[str, Any]:
    """Materialise exactly one selected export.  Reports honestly on failure."""

    spec = resolve_export_spec(kind)
    root = Path(job_dir)
    try:
        persisted_document = read_render_manifest(root)
    except (OSError, TypeError, ValueError) as exc:
        raise ExportInputMissing("render manifest is unreadable") from exc
    if persisted_document is None:
        raise ExportInputMissing("render manifest is required before exporting")
    if manifest_document is not None and dict(manifest_document) != persisted_document:
        raise ExportInputMissing("supplied render manifest does not match the persisted manifest")
    verified_document = persisted_document
    if not verified_document.get("video_sha256") or not verified_document.get("video_bytes"):
        raise ExportInputMissing("a sealed final video is required before exporting")
    try:
        verification = verify_render_manifest(root, verified_document)
    except (FileNotFoundError, OSError, TypeError, ValueError) as exc:
        raise ExportInputMissing("render manifest is unreadable") from exc
    if not verification.get("verified"):
        raise ExportInputMissing("render manifest failed integrity verification")

    destination = Path(output_dir) if output_dir is not None else root / "exports"
    staging = Path(tempfile.mkdtemp(prefix=".export-", dir=root))
    try:
        artifact = build_export_artifact(
            kind,
            job_dir=root,
            render_input=render_input,
            manifest_document=verified_document,
            version=version,
            budget=budget,
            output_dir=staging,
        )
        staged_output = staging / artifact.filename

        if artifact.is_text:
            staged_output.write_bytes(artifact.content or b"")
        else:
            if spec.captions:
                cues = caption_cues_from_render_input(
                    render_input if render_input is not None else _load_render_input(root)
                )
                srt_path = staging / f"{spec.kind}.srt"
                srt_path.write_text(build_srt(cues), encoding="utf-8")
            subprocess.run(
                list(artifact.command or []),
                check=True,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
            )
        if not staged_output.is_file() or staged_output.stat().st_size == 0:
            raise ExportInputMissing(f"export {kind} produced no output")
        destination.mkdir(parents=True, exist_ok=True)
        output_path = destination / artifact.filename
        os.replace(staged_output, output_path)
    except FileNotFoundError as exc:
        raise ExportInputMissing(f"required export tool or input is unavailable: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ExportInputMissing(f"export {kind} timed out after {timeout_seconds}s") from exc
    except subprocess.CalledProcessError as exc:
        raise ExportInputMissing(
            f"export {kind} failed: {(exc.stderr or '').strip()[:400] or 'ffmpeg exited non-zero'}"
        ) from exc
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    result = {
        "kind": spec.kind,
        "family": spec.family,
        "path": str(output_path),
        "filename": artifact.filename,
        "media_type": spec.media_type,
        "bytes": output_path.stat().st_size,
        "sha256": hashlib.sha256(output_path.read_bytes()).hexdigest(),
        "warnings": list(artifact.warnings),
        "export_contract_version": EXPORT_CONTRACT_VERSION,
    }
    if spec.family == "video":
        canonical = artifact.inputs.get("canonical_settings") or {}
        result["output_geometry"] = {
            "aspect_ratio": spec.aspect_ratio,
            "width": spec.width,
            "height": spec.height,
            "fps": canonical.get("fps"),
            "codec": canonical.get("codec"),
            "pixel_format": canonical.get("pixel_format"),
            "transform": "scale-to-fit-and-pad",
            "parity": "derived-not-canonical",
        }
    return result
