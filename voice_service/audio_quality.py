"""Deterministic PCM voice analysis and conditional mastering."""

from __future__ import annotations

from array import array
import math
import os
from pathlib import Path
import subprocess
import sys
import wave


DEFAULT_MAX_PEAK_DBFS = -1.0
DEFAULT_TARGET_LUFS = -19.0
DEFAULT_TARGET_TRUE_PEAK_DBFS = -1.5
AUDIO_MASTER_VERSION = 1


def analyze_pcm16_wav(path: str | Path) -> dict[str, float | int]:
    """Return deterministic peak/clipping metrics for a PCM16 WAV file."""
    wav_path = Path(path)
    with wave.open(str(wav_path), "rb") as handle:
        channels = handle.getnchannels()
        sample_width = handle.getsampwidth()
        sample_rate = handle.getframerate()
        frame_count = handle.getnframes()
        compression = handle.getcomptype()
        if sample_width != 2 or compression != "NONE":
            raise ValueError("Voice mastering requires uncompressed PCM16 WAV audio")
        samples = array("h", handle.readframes(frame_count))

    if sys.byteorder != "little":
        samples.byteswap()
    absolute_peak = max((abs(value) for value in samples), default=0)
    full_scale_samples = sum(value in {-32768, 32767} for value in samples)
    peak_dbfs = (
        20.0 * math.log10(absolute_peak / 32768.0)
        if absolute_peak > 0
        else float("-inf")
    )
    return {
        "channels": channels,
        "sample_rate": sample_rate,
        "frames": frame_count,
        "duration_seconds": frame_count / sample_rate if sample_rate else 0.0,
        "peak_dbfs": round(peak_dbfs, 4),
        "full_scale_samples": full_scale_samples,
    }


def master_voice_audio(path: str | Path) -> dict:
    """Master only unsafe voice audio, preserving already-safe source quality."""
    wav_path = Path(path)
    before = analyze_pcm16_wav(wav_path)
    max_peak = float(os.getenv("FYF_AUDIO_MAX_PEAK_DBFS", DEFAULT_MAX_PEAK_DBFS))
    if before["full_scale_samples"] == 0 and before["peak_dbfs"] <= max_peak:
        return {"changed": False, "version": AUDIO_MASTER_VERSION, "before": before, "after": before}

    target_lufs = float(os.getenv("FYF_AUDIO_TARGET_LUFS", DEFAULT_TARGET_LUFS))
    target_peak = float(
        os.getenv("FYF_AUDIO_TARGET_TRUE_PEAK_DBFS", DEFAULT_TARGET_TRUE_PEAK_DBFS)
    )
    temp_path = wav_path.with_name(f".{wav_path.stem}.mastering.wav")
    command = [
        "ffmpeg", "-v", "error", "-y", "-i", str(wav_path),
        "-af", f"loudnorm=I={target_lufs}:LRA=11:TP={target_peak}",
        "-ar", str(before["sample_rate"]),
        "-ac", str(before["channels"]),
        "-c:a", "pcm_s16le", str(temp_path),
    ]
    try:
        subprocess.run(command, capture_output=True, text=True, check=True, timeout=300)
        after = analyze_pcm16_wav(temp_path)
        if after["full_scale_samples"] != 0 or after["peak_dbfs"] > max_peak:
            raise RuntimeError("Mastered voice still violates peak safety limits")
        if abs(after["duration_seconds"] - before["duration_seconds"]) > 0.02:
            raise RuntimeError("Mastering changed voice duration beyond tolerance")
        os.replace(temp_path, wav_path)
    finally:
        temp_path.unlink(missing_ok=True)
    return {"changed": True, "version": AUDIO_MASTER_VERSION, "before": before, "after": after}
