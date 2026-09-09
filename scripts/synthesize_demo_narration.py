#!/usr/bin/env python3
"""
Synthesize English Explanatory Voiceover for FYF Demo Video using Google Gemini-TTS.
Generates professional English narration synced to each beat of the 103-second demo video,
and mixes the audio track directly into fyf_agentic_cinema_demo.mp4.
"""

import os
import sys
import subprocess
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from voice_service.gemini_tts import generate_gemini_tts
OUTPUT_DIR = REPO_ROOT / "output"
NARRATION_DIR = OUTPUT_DIR / "demo_narration"
NARRATION_DIR.mkdir(parents=True, exist_ok=True)

DEMO_VIDEO = OUTPUT_DIR / "fyf_agentic_cinema_demo.mp4"
FINAL_VIDEO_WITH_AUDIO = OUTPUT_DIR / "fyf_agentic_cinema_demo_with_narration.mp4"

SECTIONS = [
    {
        "name": "part1_studio",
        "timing_key": "act1_studio",
        "offset": 0.5,
        "text": (
            "FYF Studio transforms briefs into production-ready video, "
            "supporting instant preset switching and multi-platform aspect ratios."
        ),
    },
    {
        "name": "part2_canvas",
        "timing_key": "act2_canvas",
        "offset": 0.5,
        "text": (
            "In Chat plus Canvas, operators collaborate with an AI Creative Director "
            "to refine scene scripts, lock story beats, and review visual drafts."
        ),
    },
    {
        "name": "part3_playback",
        "timing_key": "act3_playback",
        "offset": 0.5,
        "text": (
            "In the Approved Library, the Remotion engine renders frame-accurate motion graphics, "
            "synchronized character lip-sync, and native Burmese typography."
        ),
    },
    {
        "name": "part4_qa",
        "timing_key": "act4_qa",
        "offset": 0.5,
        "text": (
            "Every production must pass twenty automated quality gates, "
            "verifying visual composition, audio loudness, and phonetic alignment before release."
        ),
    },
    {
        "name": "part5_telemetry",
        "timing_key": "act5_telemetry",
        "offset": 0.8,
        "text": (
            "Telemetry streams in real time to ClickHouse Cloud to audit latency, tokens, and costs, "
            "with instant warehouse presets like Cost Intelligence."
        ),
    },
    {
        "name": "part6_officer",
        "timing_key": "act5_officer",
        "offset": 0.8,
        "text": (
            "Operators can ask our Google ADK Data Officer natural-language questions. "
            "Powered by ClickHouse MCP, it queries live warehouse data to deliver verified production intelligence at enterprise scale."
        ),
    },
]

def main():
    timings_file = OUTPUT_DIR / "demo_act_timings.json"
    timings = {
        "act1_studio": 1.0,
        "act2_canvas": 14.55,
        "act3_playback": 29.25,
        "act4_qa": 45.56,
        "act5_telemetry": 66.87,
        "act5_officer": 83.73,
    }
    if timings_file.exists():
        with open(timings_file) as f:
            timings.update(json.load(f))
        print(f"🎬 Loaded exact visual timestamps: {timings}")

    for section in SECTIONS:
        key = section["timing_key"]
        offset = section.get("offset", 0.5)
        section["start_sec"] = round(float(timings.get(key, 0.0)) + offset, 2)
        print(f"  Mapped {section['name']} to start at {section['start_sec']}s")

    skip_tts = "--skip-tts" in sys.argv
    print("\n🎙️ [1/3] Generating English narration using Google Gemini-TTS (Voice: Sadaltager)...")
    audio_files = []
    durations = {}

    for section in SECTIONS:
        out_wav = NARRATION_DIR / f"{section['name']}.wav"
        if not skip_tts or not out_wav.exists():
            print(f"  Generating {section['name']}...")
            generate_gemini_tts(
                text=section["text"],
                voice="Sadaltager",
                style="tech",
                language="en-US",
                output_path=str(out_wav),
            )
        else:
            print(f"  Reusing {section['name']}...")

        probe_cmd = [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(out_wav),
        ]
        dur = float(subprocess.check_output(probe_cmd).decode().strip())
        durations[section["name"]] = dur
        start = section["start_sec"]
        end = start + dur
        print(f"    -> Duration: {dur:.2f}s (Time: {start}s - {end:.2f}s)")
        audio_files.append((start, out_wav))

    # Verify no overlaps
    for i in range(len(audio_files) - 1):
        curr_start, curr_path = audio_files[i]
        curr_end = curr_start + durations[curr_path.stem]
        next_start, next_path = audio_files[i + 1]
        diff = next_start - curr_end
        if diff < 0:
            print(f"⚠️ WARNING: Overlap between {curr_path.stem} and {next_path.stem}: {abs(diff):.2f}s")
        else:
            print(f"✅ Safe pause between {curr_path.stem} and {next_path.stem}: {diff:.2f}s")

    # Get total video duration
    video_dur_cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(DEMO_VIDEO),
    ]
    raw_video_dur = float(subprocess.check_output(video_dur_cmd).decode().strip())
    max_audio_end = max(sec + durations[p.stem] for sec, p in audio_files)
    final_limit = min(raw_video_dur, round(max_audio_end + 3.0, 2))
    print(f"\n⏱️ Video duration: {raw_video_dur:.2f}s | Audio ends: {max_audio_end:.2f}s | Final cut: {final_limit:.2f}s")

    print("\n🎵 [2/3] Constructing synchronized master narration track with FFmpeg...")
    filter_parts = []
    inputs = []
    for idx, (start_sec, wav_path) in enumerate(audio_files):
        inputs.extend(["-i", str(wav_path)])
        delay_ms = int(start_sec * 1000)
        filter_parts.append(f"[{idx}:a]adelay={delay_ms}|{delay_ms}[a{idx}]")

    mix_inputs = "".join(f"[a{idx}]" for idx in range(len(audio_files)))
    filter_complex = f"{';'.join(filter_parts)};{mix_inputs}amix=inputs={len(audio_files)}:dropout_transition=0:normalize=0,volume=1.0[aout]"

    master_audio = OUTPUT_DIR / "demo_master_narration.wav"
    ffmpeg_mix_cmd = [
        "ffmpeg", "-y",
        *inputs,
        "-filter_complex", filter_complex,
        "-map", "[aout]",
        str(master_audio),
    ]
    subprocess.run(ffmpeg_mix_cmd, check=True)

    print(f"\n🎬 [3/3] Merging narration audio with video into {FINAL_VIDEO_WITH_AUDIO.name}...")
    ffmpeg_merge_cmd = [
        "ffmpeg", "-y",
        "-t", str(final_limit),
        "-i", str(DEMO_VIDEO),
        "-i", str(master_audio),
        "-filter_complex", "[1:a]apad[a]",
        "-map", "0:v",
        "-map", "[a]",
        "-c:v", "libx264",
        "-crf", "18",
        "-preset", "fast",
        "-c:a", "aac",
        "-b:a", "192k",
        "-t", str(final_limit),
        str(FINAL_VIDEO_WITH_AUDIO),
    ]
    subprocess.run(ffmpeg_merge_cmd, check=True)

    size_mb = FINAL_VIDEO_WITH_AUDIO.stat().st_size / (1024 * 1024)
    print(f"\n🎉 SUCCESS! Final Video with English Explanatory Audio is ready:")
    print(f"    File: {FINAL_VIDEO_WITH_AUDIO}")
    print(f"    Duration: {final_limit:.2f}s ({int(final_limit // 60)}m {int(final_limit % 60)}s)")
    print(f"    Size: {size_mb:.2f} MB")
    return 0

if __name__ == "__main__":
    sys.exit(main())
