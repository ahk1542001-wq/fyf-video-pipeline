"""Deterministic WAV timing and amplitude-driven mascot mouth cues."""

from __future__ import annotations

import math
import wave
import json
import os
import subprocess
import tempfile
import unicodedata
from pathlib import Path
from typing import Any, Iterable

from backend.creative_quality import enforce_attention_reset_cadence

FPS = 30



def read_wav_duration(wav_path: str | Path) -> float:
    try:
        with wave.open(str(wav_path), "rb") as wav_file:
            rate = wav_file.getframerate()
            if rate <= 0:
                raise ValueError("WAV sample rate must be positive")
            return wav_file.getnframes() / rate
    except (OSError, wave.Error) as exc:
        raise ValueError(f"Could not read PCM WAV: {wav_path}") from exc


def _decode_pcm_sample(raw: bytes, sample_width: int) -> float:
    if sample_width == 1:
        return (raw[0] - 128) / 128.0
    if sample_width == 3:
        sign = b"\xff" if raw[2] & 0x80 else b"\x00"
        return int.from_bytes(raw + sign, "little", signed=True) / 8_388_608.0
    if sample_width in (2, 4):
        value = int.from_bytes(raw, "little", signed=True)
        return value / float(1 << (sample_width * 8 - 1))
    raise ValueError(f"Unsupported PCM sample width: {sample_width}")


def _window_rms(
    raw: bytes, *, sample_width: int, channels: int, frames_per_window: int
) -> list[float]:
    samples_per_window = max(1, frames_per_window) * channels
    values: list[float] = []
    square_sum = 0.0
    sample_count = 0
    for offset in range(0, len(raw), sample_width):
        sample = _decode_pcm_sample(raw[offset : offset + sample_width], sample_width)
        square_sum += sample * sample
        sample_count += 1
        if sample_count == samples_per_window:
            values.append(math.sqrt(square_sum / sample_count))
            square_sum = 0.0
            sample_count = 0
    if sample_count:
        values.append(math.sqrt(square_sum / sample_count))
    return values


def _coalesce(cues: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    for cue in cues:
        if merged and merged[-1]["value"] == cue["value"]:
            merged[-1]["end"] = cue["end"]
        else:
            merged.append(dict(cue))
    return merged


def generate_amplitude_mouth_cues(
    wav_path: str | Path, *, window_seconds: float = 0.05
) -> list[dict[str, Any]]:
    """Map real WAV energy to Rhubarb-compatible X/A/C/D cue values."""
    if window_seconds <= 0:
        raise ValueError("window_seconds must be positive")
    try:
        with wave.open(str(wav_path), "rb") as wav_file:
            if wav_file.getcomptype() != "NONE":
                raise ValueError("Only uncompressed PCM WAV is supported")
            rate = wav_file.getframerate()
            channels = wav_file.getnchannels()
            sample_width = wav_file.getsampwidth()
            frame_count = wav_file.getnframes()
            raw = wav_file.readframes(frame_count)
    except (OSError, wave.Error) as exc:
        raise ValueError(f"Could not read PCM WAV: {wav_path}") from exc

    if rate <= 0 or channels <= 0 or frame_count <= 0:
        raise ValueError("WAV must contain audio frames")
    duration = frame_count / rate
    rms_values = _window_rms(
        raw,
        sample_width=sample_width,
        channels=channels,
        frames_per_window=max(1, round(rate * window_seconds)),
    )
    peak = max(rms_values, default=0.0)
    silence_threshold = max(0.006, peak * 0.09)

    cues: list[dict[str, Any]] = []
    for index, rms in enumerate(rms_values):
        start = index * window_seconds
        end = min(duration, (index + 1) * window_seconds)
        if rms <= silence_threshold or peak <= silence_threshold:
            value = "X"
        else:
            openness = (rms - silence_threshold) / max(peak - silence_threshold, 1e-9)
            value = "A" if openness < 0.34 else "C" if openness < 0.68 else "D"
        cues.append({"start": round(start, 3), "end": round(end, 3), "value": value})

    merged = _coalesce(cues)
    if not merged:
        return [{"start": 0.0, "end": round(duration, 3), "value": "X"}]
    merged[-1]["end"] = round(duration, 3)
    return merged


_BURMESE_LABIALS = set("ပဖဗဘမ")
_BURMESE_ROUNDED_MARKS = set("ုူ")
_BURMESE_OPEN_MARKS = set("ာါ")
_BURMESE_WIDE_MARKS = set("ိီေဲ")
_BURMESE_TONGUE_CONSONANTS = set("တထဒဓနလရသဠ")


def _burmese_grapheme_visemes(text: str) -> list[str]:
    """Approximate Burmese articulation as Rhubarb-compatible cartoon visemes.

    This is deliberately a visual G2P heuristic, not a linguistic transcript:
    consonant place supplies the onset and dependent vowels supply the nucleus.
    """
    clusters: list[str] = []
    current = ""
    for char in text:
        code = ord(char)
        is_myanmar = 0x1000 <= code <= 0x109F or 0xAA60 <= code <= 0xAA7F
        if not is_myanmar:
            if current:
                clusters.append(current)
                current = ""
            if char.isalpha():
                clusters.append(char.lower())
            continue
        if not current or unicodedata.combining(char) == 0:
            if current:
                clusters.append(current)
            current = char
        else:
            current += char
    if current:
        clusters.append(current)

    visemes: list[str] = []
    for cluster in clusters:
        base = cluster[0]
        if base in _BURMESE_LABIALS:
            visemes.append("B")
        elif base in _BURMESE_TONGUE_CONSONANTS:
            visemes.append("H")
        else:
            visemes.append("C")

        marks = set(cluster[1:])
        if marks & _BURMESE_ROUNDED_MARKS:
            visemes.append("E")
        elif marks & _BURMESE_OPEN_MARKS or base in {"အ", "ဩ", "ဪ"}:
            visemes.append("D")
        elif marks & _BURMESE_WIDE_MARKS:
            visemes.append("C")
        else:
            visemes.append("A")
    return visemes


def generate_burmese_text_audio_mouth_cues(
    wav_path: str | Path,
    timed_segments: list[dict[str, Any]],
    *,
    fps: int = FPS,
    window_seconds: float = 0.05,
) -> list[dict[str, Any]]:
    """Align Burmese-derived mouth shapes to real voiced WAV windows per segment."""
    amplitude = generate_amplitude_mouth_cues(wav_path, window_seconds=window_seconds)
    duration = read_wav_duration(wav_path)
    expanded: list[dict[str, Any]] = []
    for cue in amplitude:
        cursor = float(cue["start"])
        while cursor < float(cue["end"]) - 1e-9:
            end = min(float(cue["end"]), cursor + window_seconds)
            expanded.append({"start": cursor, "end": end, "value": cue["value"]})
            cursor = end

    output: list[dict[str, Any]] = []
    for segment in timed_segments:
        start = segment["startFrame"] / fps
        end = min(duration, segment["endFrame"] / fps)
        voiced = [cue for cue in expanded if cue["end"] > start and cue["start"] < end and cue["value"] != "X"]
        plan = _burmese_grapheme_visemes(str(segment.get("text", "")))
        if not voiced or not plan:
            continue
        for index, cue in enumerate(voiced):
            plan_index = min(len(plan) - 1, index * len(plan) // len(voiced))
            cue["value"] = plan[plan_index]

    for cue in expanded:
        output.append({
            "start": round(float(cue["start"]), 3),
            "end": round(min(duration, float(cue["end"])), 3),
            "value": cue["value"],
        })
    merged = _coalesce(output)
    if not merged:
        raise ValueError("No Burmese text-audio mouth cues generated")
    merged[-1]["end"] = round(duration, 3)
    return merged


def _burmese_or_amplitude_cues(
    wav_path: str | Path,
    timed_segments: list[dict[str, Any]],
    *,
    fps: int,
) -> tuple[list[dict[str, Any]], str]:
    try:
        cues = generate_burmese_text_audio_mouth_cues(
            wav_path, timed_segments, fps=fps
        )
        return cues, "burmese-text-audio"
    except (ValueError, OSError, wave.Error):
        return generate_amplitude_mouth_cues(wav_path), "amplitude-fallback"


def generate_rhubarb_mouth_cues(
    wav_path: str | Path,
    dialog_text: str | None = None,
    rhubarb_bin: str | Path | None = None,
    timeout_seconds: int = 120,
) -> list[dict[str, Any]]:
    """Generate phonetic visemes via Rhubarb Lip Sync.
    Raises ValueError on invalid input or fallback conditions.
    """
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")

    if not Path(wav_path).is_absolute():
        raise ValueError("wav_path must be absolute")
    if rhubarb_bin and not Path(rhubarb_bin).is_absolute():
        raise ValueError("rhubarb_bin must be absolute")

    if dialog_text is not None:
        if not isinstance(dialog_text, str):
            raise ValueError("dialog_text must be a string")
        if not dialog_text.strip():
            raise ValueError("dialog_text cannot be empty")

    wav_path_obj = Path(wav_path).resolve()
    if not wav_path_obj.is_file() or wav_path_obj.stat().st_size == 0:
        raise ValueError(f"WAV file is missing or empty: {wav_path}")

    if not rhubarb_bin:
        raise ValueError("rhubarb_bin path is required")

    rhubarb_path = Path(rhubarb_bin).resolve()
    if not rhubarb_path.is_file():
        raise ValueError(f"Rhubarb binary not found at: {rhubarb_bin}")
    if not os.access(rhubarb_path, os.X_OK):
        raise ValueError(f"Rhubarb binary is not executable at: {rhubarb_bin}")

    duration = read_wav_duration(wav_path_obj)

    cmd = [str(rhubarb_path), "-r", "phonetic", "-f", "json"]

    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            if dialog_text is not None:
                dialog_path = Path(temp_dir) / "dialog.txt"
                dialog_path.write_text(dialog_text, encoding="utf-8")
                cmd.extend(["-d", str(dialog_path)])

            cmd.append(str(wav_path_obj))

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=True,
                timeout=timeout_seconds,
                shell=False
            )

            if not result.stdout:
                raise ValueError("Rhubarb output is empty")

            try:
                data = json.loads(result.stdout)
            except json.JSONDecodeError:
                raise ValueError("Failed to parse Rhubarb JSON output")

            if not isinstance(data, dict):
                raise ValueError("Invalid Rhubarb JSON: output is not a dictionary")

            if "mouthCues" not in data or not isinstance(data["mouthCues"], list):
                raise ValueError("Invalid Rhubarb JSON: missing mouthCues list")

            cues = []
            expected_start = 0.0
            known_values = {"A", "B", "C", "D", "E", "F", "G", "H", "X"}

            for raw_cue in data["mouthCues"]:
                if not isinstance(raw_cue, dict):
                    raise ValueError("Invalid cue format: cue is not a dict")

                start = raw_cue.get("start")
                end = raw_cue.get("end")
                value = raw_cue.get("value")

                if isinstance(start, bool) or isinstance(end, bool):
                    raise ValueError("Invalid cue format: bool is not numeric")

                if not isinstance(start, (int, float)) or not isinstance(end, (int, float)) or not isinstance(value, str):
                    raise ValueError("Invalid cue format")

                if not math.isfinite(start) or not math.isfinite(end):
                    raise ValueError("Invalid cue format: non-finite timing")

                if value not in known_values:
                    raise ValueError(f"Unknown mouth cue value: {value}")

                if start < 0 or start >= end:
                    raise ValueError(f"Invalid cue timing: {start} -> {end}")

                if end > duration + 0.001:
                    raise ValueError(f"Invalid cue timing: end {end} beyond duration {duration}")

                # Monotonic non-overlap with small float rounding tolerance
                if start < expected_start - 0.001:
                    raise ValueError(f"Overlapping or out-of-order cues: {start} < {expected_start}")

                if start > expected_start + 0.001:
                    # Fill gap with closed mouth
                    cues.append({"start": round(expected_start, 3), "end": round(start, 3), "value": "X"})

                # Clamp end strictly to duration if it slightly exceeds it due to rounding tolerance
                end = min(end, duration)

                cues.append({"start": round(start, 3), "end": round(end, 3), "value": value})
                expected_start = end

            if not cues:
                raise ValueError("No mouth cues generated")

            # Fill to end of file if needed
            if expected_start < duration - 0.001:
                cues.append({"start": round(expected_start, 3), "end": round(duration, 3), "value": "X"})

            cues = _coalesce(cues)
            if cues:
                cues[-1]["end"] = round(duration, 3)
            return cues

    except (subprocess.TimeoutExpired, subprocess.CalledProcessError, OSError) as e:
        raise ValueError(f"Rhubarb execution failed") from e


def _text_weight(text: str) -> int:
    return max(1, sum(not char.isspace() for char in text))


def allocate_segment_frames(
    segments: list[dict[str, Any]], duration_seconds: float, *, fps: int = FPS
) -> tuple[list[dict[str, Any]], int]:
    """Allocate exact, contiguous frames while Vertex remains timing-free."""
    if not segments:
        raise ValueError("At least one segment is required")
    if duration_seconds <= 0 or fps <= 0:
        raise ValueError("duration_seconds and fps must be positive")
    # The video timeline must never be shorter than its audio. Rounding down can
    # place the final mouth cue a few milliseconds beyond the render boundary.
    total_frames = max(len(segments), math.ceil(duration_seconds * fps))
    weights = [_text_weight(str(segment.get("text", ""))) for segment in segments]
    distributable = total_frames - len(segments)
    raw_extras = [distributable * weight / sum(weights) for weight in weights]
    frame_counts = [1 + math.floor(value) for value in raw_extras]
    remainder = total_frames - sum(frame_counts)
    ranked = sorted(
        range(len(segments)), key=lambda index: raw_extras[index] % 1, reverse=True
    )
    for index in ranked[:remainder]:
        frame_counts[index] += 1

    timed: list[dict[str, Any]] = []
    cursor = 0
    for segment, frame_count in zip(segments, frame_counts):
        end = cursor + frame_count
        timed.append({**segment, "startFrame": cursor, "endFrame": end})
        cursor = end
    return timed, total_frames


def _wav_silence_centers(
    wav_path: str | Path, *, window_seconds: float = 0.02, minimum_silence: float = 0.18
) -> list[float]:
    """Return centers of real inter-phrase silence regions from a PCM WAV."""
    with wave.open(str(wav_path), "rb") as wav_file:
        rate = wav_file.getframerate()
        channels = wav_file.getnchannels()
        sample_width = wav_file.getsampwidth()
        raw = wav_file.readframes(wav_file.getnframes())
    rms = _window_rms(
        raw,
        sample_width=sample_width,
        channels=channels,
        frames_per_window=max(1, round(rate * window_seconds)),
    )
    peak = max(rms, default=0.0)
    threshold = max(0.004, peak * 0.055)
    centers: list[float] = []
    start: int | None = None
    for index, value in enumerate(rms + [peak + 1]):
        if value <= threshold and start is None:
            start = index
        elif value > threshold and start is not None:
            end = index
            if (end - start) * window_seconds >= minimum_silence:
                centers.append((start + end) * window_seconds / 2)
            start = None
    duration = len(rms) * window_seconds
    return [center for center in centers if 0.12 < center < duration - 0.12]


def allocate_segment_frames_from_wav(
    segments: list[dict[str, Any]], wav_path: str | Path, *, fps: int = FPS
) -> tuple[list[dict[str, Any]], int, str]:
    """Snap text-estimated segment boundaries to nearby audible pause centers."""
    duration = read_wav_duration(wav_path)
    baseline, total_frames = allocate_segment_frames(segments, duration, fps=fps)
    if len(segments) == 1:
        return baseline, total_frames, "single-segment"
    centers = _wav_silence_centers(wav_path)
    if not centers:
        return baseline, total_frames, "text-weight-fallback"

    boundaries: list[int] = []
    previous = 0
    for segment in baseline[:-1]:
        estimated = segment["endFrame"] / fps
        eligible = [
            center for center in centers
            if abs(center - estimated) <= 2.25 and round(center * fps) > previous + 1
        ]
        chosen = min(eligible, key=lambda center: abs(center - estimated)) if eligible else estimated
        frame = min(total_frames - 1, max(previous + 1, round(chosen * fps)))
        boundaries.append(frame)
        previous = frame

    timed: list[dict[str, Any]] = []
    start = 0
    for index, segment in enumerate(segments):
        end = boundaries[index] if index < len(boundaries) else total_frames
        timed.append({**segment, "startFrame": start, "endFrame": end})
        start = end
    return timed, total_frames, "wav-silence-snap"


def build_render_input(
    script_data: dict[str, Any],
    wav_path: str | Path,
    audio_src: str = "voice.wav",
    *,
    fps: int = FPS,
    rhubarb_bin: str | Path | None = None,
    rhubarb_timeout_seconds: int | None = None,
) -> dict[str, Any]:
    duration = read_wav_duration(wav_path)
    timed_segments, total_frames, segment_timing_source = allocate_segment_frames_from_wav(
        script_data["segments"], wav_path, fps=fps
    )

    mouth_cues = []
    mouth_cue_source = "amplitude-fallback"

    bin_path = rhubarb_bin or os.environ.get("RHUBARB_BIN")

    if bin_path:
        # Determine timeout
        if rhubarb_timeout_seconds is not None:
            if not isinstance(rhubarb_timeout_seconds, int):
                raise ValueError("rhubarb_timeout_seconds must be an integer")
            timeout = rhubarb_timeout_seconds
        else:
            env_timeout = os.environ.get("RHUBARB_TIMEOUT_SECONDS")
            try:
                timeout = int(env_timeout) if env_timeout is not None else 300
            except ValueError:
                timeout = 300

        timeout = max(30, min(1800, timeout))

        # Extract full dialog text for optional Rhubarb phonetic hint
        dialog_text = " ".join(seg.get("text", "") for seg in script_data["segments"])
        try:
            mouth_cues = generate_rhubarb_mouth_cues(wav_path, dialog_text=dialog_text, rhubarb_bin=bin_path, timeout_seconds=timeout)
            mouth_cue_source = "rhubarb-phonetic"
        except (ValueError, OSError, subprocess.SubprocessError):
            mouth_cues, mouth_cue_source = _burmese_or_amplitude_cues(
                wav_path, timed_segments, fps=fps
            )
    else:
        mouth_cues, mouth_cue_source = _burmese_or_amplitude_cues(
            wav_path, timed_segments, fps=fps
        )

    render_input = {
        "title": script_data["title"],
        "language": script_data.get("language", "my-MM"),
        "fps": fps,
        "durationInFrames": total_frames,
        "audioSrc": audio_src,
        "segments": timed_segments,
        "mouthCues": mouth_cues,
        "mouthCueSource": mouth_cue_source,
        "segmentTimingSource": segment_timing_source,
    }
    # Frames exist from here on, so deterministic creative QA can now be satisfied
    # before render/QA regardless of which upstream path produced the script.
    return enforce_attention_reset_cadence(render_input)
