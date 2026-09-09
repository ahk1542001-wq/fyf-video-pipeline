from __future__ import annotations

import math
from pathlib import Path

import pytest

from backend.exports import (
    UnknownExportKindError,
    build_export_artifact,
    build_video_command,
    plan_exports,
    resolve_export_spec,
)
from backend.latency_metrics import record_latency_sample, report_latency
from backend.render_contract import plan_duration_reedit, plan_reframe, run_caption_audio_qa
from backend.render_manifest import (
    build_render_manifest,
    manifest_document,
    resolve_asset_hashes,
    seal_manifest_document,
    verify_render_manifest,
    write_render_manifest,
)
from backend.segment_render_cache import SegmentRenderResult
from voice_service.timed_animatic import build_timed_animatic


def _render_input(language: str = "en-US") -> dict:
    return {
        "title": "Measured demo",
        "language": language,
        "fps": 30,
        "width": 1080,
        "height": 1920,
        "durationInFrames": 120,
        "segmentTimingSource": "wav-silence-snap",
        "render_controls": {
            "cta_text": "Learn more",
            "retention_progress_bar": True,
            "animated_lower_thirds": False,
            "aspect_ratio": "9:16",
        },
        "reduced_motion": True,
        "mouthCues": [{"start": 0.1, "end": 0.4, "value": "A"}],
        "segments": [
            {
                "id": "s1",
                "startFrame": 0,
                "endFrame": 60,
                "text": "Revenue grew 15 percent",
                "screen_text": ["Revenue grew 15 percent"],
                "visual": {"kind": "generic", "screen_text": ["15"]},
            },
            {
                "id": "s2",
                "startFrame": 60,
                "endFrame": 120,
                "text": "Ship the launch",
                "screen_text": ["Ship the launch"],
                "visual": {"kind": "generic"},
            },
        ],
    }


def test_timed_animatic_and_caption_qa_keep_measurement_and_acceptance_separate():
    render_input = _render_input()
    animatic = build_timed_animatic(render_input, voice_measured=True, has_music=True)
    assert animatic["voice_timed"] is True
    assert animatic["generation_enabled"] is False
    assert animatic["mix"]["ducking_enabled"] is True

    render_input["mix"] = animatic["mix"]
    report = run_caption_audio_qa(render_input, cues=animatic["captions"])
    assert report["lanes_independent"] is True
    assert report["human_acceptance"]["statement"]
    assert report["reduced_motion_requested"] is True

    no_music = build_timed_animatic(render_input, voice_measured=True, has_music=False)
    render_input["mix"] = no_music["mix"]
    no_music_report = run_caption_audio_qa(render_input, cues=no_music["captions"])
    balance = next(
        check
        for check in no_music_report["creative_gate"]["checks"]
        if check["id"] == "creative_voice_music_balance"
    )
    assert balance["passed"] is True


def test_reframe_never_crops_and_duration_reedit_never_speeds_up():
    reframe = plan_reframe(source_aspect_ratio="9:16", target_aspect_ratio="16:9")
    assert reframe.crops_content is False
    assert reframe.crop is None
    duration = plan_duration_reedit(_render_input(), requested_duration_seconds=3.0)
    assert duration.requires_reapproval is True
    assert duration.speed_factor is None


def test_manifest_detects_segment_tampering(tmp_path: Path):
    segment_dir = tmp_path / "render-segments"
    segment_dir.mkdir()
    segment_path = segment_dir / ("s1-" + "a" * 16 + ".mp4")
    segment_path.write_bytes(b"segment-v1")
    result = SegmentRenderResult("s1", "a" * 64, segment_path, False, 60)
    manifest = build_render_manifest(
        tmp_path,
        _render_input(),
        renderer_version="test-remotion",
        skill_versions={"qa": "1"},
        include_asset_hashes=False,
    )
    document = manifest_document(
        manifest,
        results=[result],
        manifest_fingerprint="b" * 64,
        renderer_source_hash="renderer-source",
        composition_id="VisualSystemV3Full",
        remotion_version="test-remotion",
        reduced_motion=True,
    )
    write_render_manifest(tmp_path, seal_manifest_document(document))
    assert verify_render_manifest(tmp_path)["verified"] is True
    segment_path.write_bytes(b"tampered")
    verification = verify_render_manifest(tmp_path)
    assert verification["verified"] is False
    assert verification["segment_mismatches"][0]["segment_id"] == "s1"


def test_manifest_rejects_ambiguous_duplicate_asset_names(tmp_path: Path, monkeypatch):
    left = tmp_path / "left" / "frame.png"
    right = tmp_path / "right" / "frame.png"
    left.parent.mkdir()
    right.parent.mkdir()
    left.write_bytes(b"left")
    right.write_bytes(b"right")
    monkeypatch.setattr(
        "backend.render_manifest._segment_asset_paths",
        lambda *_args, **_kwargs: [left, right],
    )
    with pytest.raises(ValueError, match="ambiguous asset name"):
        resolve_asset_hashes(tmp_path, {"segments": [{"id": "s1"}]})


def test_exports_are_selected_only_and_video_reframe_uses_pad_not_crop(tmp_path: Path):
    assert [spec.kind for spec in plan_exports(["captions-srt", "captions-srt", "transcript"])] == [
        "captions-srt",
        "transcript",
    ]
    with pytest.raises(UnknownExportKindError):
        plan_exports(["not-real"])

    artifact = build_export_artifact(
        "captions-srt", job_dir=tmp_path, render_input=_render_input()
    )
    assert artifact.content and b"Revenue grew" in artifact.content
    command = build_video_command(
        tmp_path / "video.mp4",
        tmp_path / "out.mp4",
        resolve_export_spec("video-1080p-16x9"),
    )
    filters = command[command.index("-vf") + 1]
    assert "force_original_aspect_ratio=decrease" in filters
    assert "pad=1920:1080" in filters
    assert "crop" not in filters


def test_latency_rejects_non_finite_values_and_reports_real_samples(tmp_path: Path):
    with pytest.raises(ValueError):
        record_latency_sample(
            "chat_round_trip", math.inf, cache_state="cold", scene_count=1, root=tmp_path
        )
    record_latency_sample(
        "chat_round_trip",
        2.5,
        cache_state="cold",
        scene_count=2,
        aspect_ratio="9:16",
        language="en-US",
        root=tmp_path,
        now_fn=lambda: "2026-09-08T00:00:00Z",
    )
    report = report_latency(tmp_path)
    assert report["metrics"]["chat_round_trip"]["status"] == "measured-and-met"
    assert report["metrics"]["final_render"]["status"] == "unmeasured"
