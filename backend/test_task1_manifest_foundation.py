from __future__ import annotations

import hashlib
import json
import subprocess
from copy import deepcopy
from pathlib import Path

import pytest

from backend.exports import ExportInputMissing, build_export_artifact, run_export
from backend.render_manifest import (
    MANIFEST_CONTRACT_VERSION,
    build_render_manifest,
    compute_manifest_fingerprint,
    manifest_document,
    rerender_from_manifest,
    seal_manifest_document,
    verify_render_manifest,
    write_render_manifest,
)
from backend.render_video import render_video_remotion
from backend.segment_render_cache import SegmentRenderResult


def _render_input(*, fps: int = 24, width: int = 1920, height: int = 1080) -> dict:
    return {
        "title": "Manifest fixture",
        "language": "en-US",
        "fps": fps,
        "width": width,
        "height": height,
        "durationInFrames": 48,
        "audioSrc": "voice.wav",
        "segments": [
            {
                "id": "s1",
                "startFrame": 0,
                "endFrame": 48,
                "text": "Explain the result",
                "visual": {
                    "kind": "generic",
                    "phase": "setup",
                    "screen_text": ["Result"],
                },
            }
        ],
        "mouthCues": [],
    }


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _sealed_document(tmp_path: Path, *, include_video: bool = True) -> tuple[dict, dict]:
    render_input = _render_input()
    segment_dir = tmp_path / "render-segments"
    segment_dir.mkdir(exist_ok=True)
    fingerprint = "a" * 64
    segment_path = segment_dir / f"s1-{fingerprint[:16]}.mp4"
    segment_path.write_bytes(b"segment-bytes")
    result = SegmentRenderResult("s1", fingerprint, segment_path, False, 48)
    if include_video:
        (tmp_path / "video.mp4").write_bytes(b"assembled-video")
    manifest = build_render_manifest(
        tmp_path,
        render_input,
        renderer_version="remotion-test",
        skill_versions={"render": "1"},
        total_frames=48,
        include_asset_hashes=False,
    )
    document = manifest_document(
        manifest,
        results=[result],
        manifest_fingerprint="0" * 64,
        renderer_source_hash="renderer-source",
        composition_id="VisualSystemV3Full",
        remotion_version="remotion-test",
        reduced_motion=False,
        created_at="2026-09-08T00:00:00+00:00",
        video_sha256=(hashlib.sha256(b"assembled-video").hexdigest() if include_video else None),
        video_bytes=(len(b"assembled-video") if include_video else None),
    )
    document = seal_manifest_document(document)
    write_render_manifest(tmp_path, document)
    return render_input, document


def test_manifest_verification_uses_the_real_sixteen_character_segment_filename(tmp_path: Path):
    """A valid cache entry is named with fingerprint[:16], not the old [:12]."""

    _render_input_value, document = _sealed_document(tmp_path)

    assert (tmp_path / "render-segments" / ("s1-" + "a" * 16 + ".mp4")).is_file()
    verification = verify_render_manifest(tmp_path, document)
    assert verification["verified"] is True


@pytest.mark.parametrize(
    "mutation",
    [
        lambda doc: doc.__setitem__("composition_id", "OtherComposition"),
        lambda doc: doc["render_manifest"].__setitem__("video_spec_version", "forged-spec"),
        lambda doc: doc["render_manifest"]["output"].__setitem__("width", 1080),
    ],
)
def test_manifest_verification_rejects_wrapper_config_and_output_tampering(
    tmp_path: Path, mutation
):
    _render_input_value, document = _sealed_document(tmp_path)
    tampered = deepcopy(document)
    mutation(tampered)

    assert verify_render_manifest(tmp_path, tampered)["verified"] is False


def test_manifest_verification_rejects_segment_identity_substitution(tmp_path: Path):
    _render_input_value, document = _sealed_document(tmp_path)
    tampered = deepcopy(document)
    tampered["segments"][0]["segment_id"] = "s2"
    # Keep a same-bytes file at the substituted identity so a hash-only check
    # cannot accidentally accept the wrong scene.
    (tmp_path / "render-segments" / ("s2-" + "a" * 16 + ".mp4")).write_bytes(
        b"segment-bytes"
    )

    assert verify_render_manifest(tmp_path, tampered)["verified"] is False


def test_manifest_writer_rejects_unsealed_wrapper(tmp_path: Path):
    _render_input_value, document = _sealed_document(tmp_path)
    unsealed = deepcopy(document)
    unsealed["manifest_fingerprint"] = "0" * 64

    with pytest.raises(ValueError, match="unsealed"):
        write_render_manifest(tmp_path, unsealed)


def test_manifest_identity_ignores_created_at_but_covers_render_strategy(tmp_path: Path):
    _render_input_value, document = _sealed_document(tmp_path)
    later = deepcopy(document)
    later["created_at"] = "2026-09-09T00:00:00+00:00"
    assert compute_manifest_fingerprint(later) == compute_manifest_fingerprint(document)
    later["render_strategy"] = "monolithic"
    assert compute_manifest_fingerprint(later) != compute_manifest_fingerprint(document)


def test_rerender_uses_segmented_strategy_and_compares_sealed_manifest_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _render_input_value, document = _sealed_document(tmp_path)

    class Assembly:
        output_path = tmp_path / "video.mp4"
        manifest_fingerprint = "assembly-cache-fingerprint"
        cache_hits = 1
        rendered_segments = 0

    def rerender_segmented(job_dir: str):
        assert Path(job_dir) == tmp_path
        # A warm-cache render rewrites operational metadata but preserves the
        # sealed document identity because cache_hit is excluded from it.
        rewritten = deepcopy(document)
        rewritten["segments"][0]["cache_hit"] = True
        write_render_manifest(tmp_path, rewritten)
        return Assembly()

    monkeypatch.setattr(
        "backend.segment_render_cache.render_segments_and_assemble",
        rerender_segmented,
    )
    monkeypatch.setattr(
        "backend.render_video.render_video_remotion",
        lambda *_args, **_kwargs: pytest.fail("monolithic renderer must not run"),
    )

    report = rerender_from_manifest(tmp_path)

    assert report["reproduced"] is True
    assert report["render_strategy"] == "segmented"
    assert report["actual_manifest_fingerprint"] == document["manifest_fingerprint"]
    assert report["assembly_manifest_fingerprint"] == "assembly-cache-fingerprint"


def test_rerender_uses_monolithic_strategy_without_switching_manifest_type(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _render_input_value, segmented = _sealed_document(tmp_path)
    monolithic = deepcopy(segmented)
    monolithic["segments"] = []
    monolithic["render_strategy"] = "monolithic"
    monolithic = seal_manifest_document(monolithic)
    write_render_manifest(tmp_path, monolithic)

    def rerender_monolithic(job_dir: str):
        assert Path(job_dir) == tmp_path
        write_render_manifest(tmp_path, monolithic)
        return str(tmp_path / "video.mp4")

    monkeypatch.setattr("backend.render_video.render_video_remotion", rerender_monolithic)
    monkeypatch.setattr(
        "backend.segment_render_cache.render_segments_and_assemble",
        lambda *_args, **_kwargs: pytest.fail("segmented renderer must not run"),
    )

    report = rerender_from_manifest(tmp_path)

    assert report["reproduced"] is True
    assert report["render_strategy"] == "monolithic"
    assert report["actual_manifest_fingerprint"] == monolithic["manifest_fingerprint"]
    assert "assembly_manifest_fingerprint" not in report


@pytest.mark.parametrize("manifest_state", ["missing", "corrupt", "mismatched"])
def test_exports_block_without_leaving_artifacts_for_invalid_manifest(
    tmp_path: Path, manifest_state: str
):
    render_input = _render_input()
    (tmp_path / "render_input.json").write_text(json.dumps(render_input), encoding="utf-8")
    if manifest_state == "corrupt":
        (tmp_path / "render_manifest.json").write_text("not-json", encoding="utf-8")
    elif manifest_state == "mismatched":
        _render_input_value, document = _sealed_document(tmp_path)
        document["video_bytes"] += 1
        (tmp_path / "render_manifest.json").write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ExportInputMissing):
        run_export("transcript", job_dir=tmp_path)
    assert not (tmp_path / "exports").exists()


def test_export_requires_the_persisted_manifest_even_if_caller_supplies_a_copy(tmp_path: Path):
    render_input, document = _sealed_document(tmp_path)
    (tmp_path / "render_input.json").write_text(json.dumps(render_input), encoding="utf-8")
    (tmp_path / "render_manifest.json").unlink()

    with pytest.raises(ExportInputMissing, match="required"):
        run_export("transcript", job_dir=tmp_path, manifest_document=document)


def test_export_requires_a_final_video_seal(tmp_path: Path):
    render_input, document = _sealed_document(tmp_path)
    (tmp_path / "render_input.json").write_text(json.dumps(render_input), encoding="utf-8")
    unsealed_video = deepcopy(document)
    unsealed_video.pop("video_sha256")
    unsealed_video.pop("video_bytes")
    write_render_manifest(tmp_path, seal_manifest_document(unsealed_video))

    with pytest.raises(ExportInputMissing, match="sealed final video"):
        run_export("transcript", job_dir=tmp_path)


def test_failed_captioned_video_export_leaves_no_sidecar_or_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    render_input, _document = _sealed_document(tmp_path)
    (tmp_path / "render_input.json").write_text(json.dumps(render_input), encoding="utf-8")

    def fail_export(*_args, **_kwargs):
        raise subprocess.CalledProcessError(1, ["ffmpeg"], stderr="failed")

    monkeypatch.setattr("backend.exports.subprocess.run", fail_export)
    with pytest.raises(ExportInputMissing, match="failed"):
        run_export("video-1080p-9x16-captions", job_dir=tmp_path)
    assert not (tmp_path / "exports").exists()
    assert not list(tmp_path.glob(".export-*"))


def test_monolithic_render_seals_manifest_from_staged_props_and_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    (tmp_path / "voice.wav").write_bytes(b"voice")
    render_input = _render_input(fps=30, width=1080, height=1920)
    render_input["reduced_motion"] = True
    (tmp_path / "render_input.json").write_text(json.dumps(render_input), encoding="utf-8")

    monkeypatch.setattr("backend.render_video.validate_render_input", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("backend.render_video.approved_visual_preset", lambda *_args, **_kwargs: None)

    def render_side_effect(command: list[str], **_kwargs):
        props_path = Path(command[command.index("--props") + 1])
        assert json.loads(props_path.read_text(encoding="utf-8"))["reduced_motion"] is True
        Path(command[command.index("VisualSystemV3Full") + 1]).write_bytes(b"monolithic-video")

    monkeypatch.setattr("backend.render_video.subprocess.run", render_side_effect)

    assert render_video_remotion(str(tmp_path)) == str(tmp_path / "video.mp4")
    document = json.loads((tmp_path / "render_manifest.json").read_text(encoding="utf-8"))
    assert document["manifest_contract_version"] == MANIFEST_CONTRACT_VERSION
    assert document["motion_mode"] == "reduced"
    assert document["video_bytes"] == len(b"monolithic-video")
    assert document["video_sha256"] == hashlib.sha256(b"monolithic-video").hexdigest()
    assert document["render_manifest"]["output"] == {
        "aspect_ratio": None,
        "width": 1080,
        "height": 1920,
        "duration_seconds": 1.6,
        "fps": 30,
        "codec": "h264",
    }
    verification = verify_render_manifest(tmp_path, document)
    assert not verification["integrity_mismatches"], verification["integrity_mismatches"]
    assert not verification["asset_mismatches"], verification["asset_mismatches"]
    assert not verification["missing_assets"], verification["missing_assets"]
    assert not verification["segment_mismatches"], verification["segment_mismatches"]
    assert not verification["missing_segments"], verification["missing_segments"]
    assert verification["verified"] is True, verification


def test_export_derives_canonical_settings_and_records_derived_geometry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    render_input, document = _sealed_document(tmp_path)
    (tmp_path / "render_input.json").write_text(json.dumps(render_input), encoding="utf-8")

    artifact = build_export_artifact(
        "video-1080p-9x16",
        job_dir=tmp_path,
        manifest_document=document,
    )
    assert artifact.inputs["source_geometry"] == {"width": 1920, "height": 1080}
    assert artifact.inputs["canonical_settings"] == {
        "fps": 24,
        "codec": "h264",
        "pixel_format": "yuv420p",
    }
    command = artifact.command or []
    assert command[command.index("-r") + 1] == "24"
    assert command[command.index("-pix_fmt") + 1] == "yuv420p"

    def export_side_effect(command: list[str], **_kwargs):
        Path(command[-1]).write_bytes(b"derived-video")

    monkeypatch.setattr("backend.exports.subprocess.run", export_side_effect)
    result = run_export(
        "video-1080p-9x16",
        job_dir=tmp_path,
        manifest_document=document,
    )
    assert result["output_geometry"] == {
        "aspect_ratio": "9:16",
        "width": 1080,
        "height": 1920,
        "fps": 24,
        "codec": "h264",
        "pixel_format": "yuv420p",
        "transform": "scale-to-fit-and-pad",
        "parity": "derived-not-canonical",
    }
