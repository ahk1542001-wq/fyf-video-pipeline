"""Repeatable post-processing for the approved FYF Burmese production voice."""

from __future__ import annotations

import math
import os
import re
import shutil
import subprocess
import tempfile
import wave


APPROVED_SPEED = 0.96
MIN_SPEED = 0.90
MAX_SPEED = 1.10


def build_production_filter(speed: float = APPROVED_SPEED) -> str:
    """Return the approved less-nasal, loudness-matched, pitch-safe filter."""
    if not isinstance(speed, (int, float)) or not math.isfinite(speed):
        raise ValueError("speed must be a finite number")
    if not MIN_SPEED <= speed <= MAX_SPEED:
        raise ValueError(f"speed must be between {MIN_SPEED:.2f} and {MAX_SPEED:.2f}")

    return ",".join(
        (
            "highpass=f=65",
            "equalizer=f=280:width_type=q:width=1.0:g=-0.8",
            "equalizer=f=850:width_type=q:width=1.15:g=-1.8",
            "equalizer=f=1250:width_type=q:width=1.2:g=-0.9",
            "equalizer=f=3400:width_type=q:width=1.0:g=0.4",
            "volume=0.7dB",
            f"atempo={speed:g}",
        )
    )


AI_PRONUNCIATION = "အေ အိုင်"
AI_TARGET_GAP_SECONDS = 0.030


def _ai_gap_positions(text: str, duration: float) -> list[float]:
    """Estimate acoustic positions for the space inside each AI pronunciation."""
    if not text or duration <= 0:
        return []
    positions = []
    offset = 0
    while True:
        index = text.find(AI_PRONUNCIATION, offset)
        if index < 0:
            return positions
        gap_index = index + len("အေ")
        positions.append((gap_index / max(1, len(text))) * duration)
        offset = index + len(AI_PRONUNCIATION)


def _parse_silence_intervals(stderr: str) -> list[tuple[float, float]]:
    """Pair silencedetect events in order without misaligning unmatched events."""
    intervals = []
    open_start = None
    for match in re.finditer(r"silence_(start|end):\s+([\d.]+)", stderr):
        event, raw_value = match.groups()
        value = float(raw_value)
        if event == "start":
            open_start = value
        elif open_start is not None and value >= open_start:
            intervals.append((open_start, value))
            open_start = None
    return intervals


def _select_ai_gap_cuts(
    text: str,
    duration: float,
    silences: list[tuple[float, float]],
) -> list[tuple[float, float]]:
    """Match each AI occurrence to one nearby short silence and cut its center."""
    expected_positions = _ai_gap_positions(text, duration)
    if not expected_positions:
        return []

    plausible = [
        (start, end)
        for start, end in silences
        if 0.045 <= end - start <= 0.140
    ]
    available = set(range(len(plausible)))
    max_position_error = min(0.45, max(0.20, duration * 0.075))
    cuts = []

    for expected in expected_positions:
        candidates = [
            index
            for index in available
            if abs(((plausible[index][0] + plausible[index][1]) / 2) - expected)
            <= max_position_error
        ]
        if not candidates:
            continue
        selected = min(
            candidates,
            key=lambda index: abs(
                ((plausible[index][0] + plausible[index][1]) / 2) - expected
            ),
        )
        available.remove(selected)
        start, end = plausible[selected]
        if end - start <= AI_TARGET_GAP_SECONDS:
            continue
        half_target = AI_TARGET_GAP_SECONDS / 2
        cuts.append((start + half_target, end - half_target))

    return sorted(cuts)


def _wav_duration(path: str) -> float:
    try:
        with wave.open(path, "rb") as wav_file:
            return wav_file.getnframes() / wav_file.getframerate()
    except (wave.Error, OSError, ZeroDivisionError) as exc:
        raise RuntimeError(f"Could not read processed WAV duration: {path}") from exc


def tighten_ai_silences(input_wav: str, text: str, ffmpeg_bin: str = "ffmpeg") -> int:
    """Tighten only dynamically matched gaps inside ``အေ အိုင်`` to about 30 ms."""
    if AI_PRONUNCIATION not in text:
        return 0

    ffmpeg_path = shutil.which(ffmpeg_bin)
    if not ffmpeg_path:
        raise RuntimeError(f"ffmpeg is unavailable: {ffmpeg_bin!r}")

    # Detect silences in the audio file
    sd_command = [
        ffmpeg_path,
        "-hide_banner", "-loglevel", "info",
        "-i", input_wav,
        "-af", "silencedetect=noise=-35dB:d=0.035",
        "-f", "null", "-",
    ]
    try:
        res = subprocess.run(sd_command, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "unknown ffmpeg error").strip()
        raise RuntimeError(f"ffmpeg silencedetect failed: {detail}") from exc

    silences = _parse_silence_intervals(res.stderr)
    total_duration = _wav_duration(input_wav)
    to_cut = _select_ai_gap_cuts(text, total_duration, silences)
    if not to_cut:
        return 0

    chunks = []
    last_end = 0.0
    for cut_start, cut_end in to_cut:
        chunks.append((last_end, cut_start))
        last_end = cut_end
    chunks.append((last_end, total_duration))

    filter_complex = []
    for i, (start, end) in enumerate(chunks):
        filter_complex.append(
            f"[0:a]atrim=start={start:.6f}:end={end:.6f},asetpts=PTS-STARTPTS[a{i}]"
        )

    concat_inputs = "".join(f"[a{i}]" for i in range(len(chunks)))
    filter_complex.append(f"{concat_inputs}concat=n={len(chunks)}:v=0:a=1[outa]")

    filter_str = ";".join(filter_complex)
    destination_dir = os.path.dirname(os.path.abspath(input_wav))
    handle = tempfile.NamedTemporaryFile(
        prefix=".fyf-voice-tighten-",
        suffix=".wav",
        dir=destination_dir,
        delete=False,
    )
    temp_wav = handle.name
    handle.close()

    cut_command = [
        ffmpeg_path,
        "-hide_banner",
        "-loglevel", "error",
        "-y",
        "-i", input_wav,
        "-filter_complex", filter_str,
        "-map", "[outa]",
        "-c:a", "pcm_s16le",
        temp_wav,
    ]

    try:
        subprocess.run(cut_command, check=True, capture_output=True, text=True)
        if not os.path.isfile(temp_wav) or os.path.getsize(temp_wav) == 0:
            raise RuntimeError("ffmpeg tighten completed without a non-empty WAV output")
        os.replace(temp_wav, input_wav)
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "unknown ffmpeg error").strip()
        raise RuntimeError(f"ffmpeg tighten post-processing failed: {detail}") from exc
    finally:
        if os.path.exists(temp_wav):
            try:
                os.remove(temp_wav)
            except OSError:
                pass
    return len(to_cut)


def apply_production_postprocessing(
    input_wav: str,
    output_wav: str | None = None,
    *,
    speed: float = APPROVED_SPEED,
    ffmpeg_bin: str = "ffmpeg",
    script_text: str = "",
) -> str:
    """Apply the approved preset atomically, including safe in-place use."""
    if not os.path.isfile(input_wav):
        raise FileNotFoundError(f"Input WAV not found: {input_wav}")

    ffmpeg_path = shutil.which(ffmpeg_bin)
    if not ffmpeg_path:
        raise RuntimeError(f"ffmpeg is unavailable: {ffmpeg_bin!r}")

    filter_chain = build_production_filter(speed)
    destination = output_wav or input_wav
    destination_dir = os.path.dirname(os.path.abspath(destination))
    os.makedirs(destination_dir, exist_ok=True)

    handle = tempfile.NamedTemporaryFile(
        prefix=".fyf-voice-",
        suffix=".wav",
        dir=destination_dir,
        delete=False,
    )
    temp_wav = handle.name
    handle.close()

    command = [
        ffmpeg_path,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        input_wav,
        "-af",
        filter_chain,
        "-c:a",
        "pcm_s16le",
        temp_wav,
    ]

    try:
        subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
        )
        if not os.path.isfile(temp_wav) or os.path.getsize(temp_wav) == 0:
            raise RuntimeError("ffmpeg completed without a non-empty WAV output")
        if script_text:
            tighten_ai_silences(temp_wav, script_text, ffmpeg_bin)
        os.replace(temp_wav, destination)
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "unknown ffmpeg error").strip()
        raise RuntimeError(f"ffmpeg post-processing failed: {detail}") from exc
    finally:
        if os.path.exists(temp_wav):
            try:
                os.remove(temp_wav)
            except OSError:
                pass

    return destination
