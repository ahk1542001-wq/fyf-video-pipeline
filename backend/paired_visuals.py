"""Integrity-checked handoff of one approved visual plan between voice jobs."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Any

from backend.job_store import read_job_status, write_json_atomically


CHECKPOINT_NAME = "paired_visual_checkpoint.json"
CHECKPOINT_VERSION = "fyf-paired-visual-v1"


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def narration_fingerprint(script: dict[str, Any]) -> str:
    segments = script.get("segments")
    if not isinstance(segments, list) or not segments:
        raise ValueError("Paired visual adoption requires non-empty narration segments")
    locked = []
    for segment in segments:
        if not isinstance(segment, dict):
            raise ValueError("Paired visual adoption requires valid narration segments")
        segment_id = segment.get("id")
        text = segment.get("text")
        if not isinstance(segment_id, str) or not segment_id:
            raise ValueError("Paired visual adoption requires stable segment IDs")
        if not isinstance(text, str) or not text:
            raise ValueError("Paired visual adoption requires locked narration text")
        locked.append({"id": segment_id, "text": text})
    return _sha256_bytes(_canonical_json({
        "language": script.get("language"),
        "segments": locked,
    }))


def _approved_source(source_job_dir: Path, script: dict[str, Any]) -> None:
    status = read_job_status(source_job_dir)
    if status.get("status") != "completed":
        raise ValueError("Source job must be completed before visual adoption")
    for report_name in ("qa_report", "creative_qa", "final_visual_qa"):
        report = status.get(report_name)
        if not isinstance(report, dict) or report.get("passed") is not True:
            raise ValueError(f"Source job lacks approved {report_name}")
    final_segments = status["final_visual_qa"].get("segments")
    expected_ids = [segment["id"] for segment in script["segments"]]
    if not isinstance(final_segments, list) or [
        item.get("segment_id") for item in final_segments if isinstance(item, dict)
    ] != expected_ids or not all(
        isinstance(item, dict) and item.get("passed") is True for item in final_segments
    ):
        raise ValueError("Source job final visual QA does not cover every narration segment")


def _referenced_visual_files(script: dict[str, Any]) -> list[str]:
    found: set[str] = set()
    for segment in script.get("segments", []):
        visual = segment.get("visual") if isinstance(segment, dict) else None
        shots = visual.get("evidence_shots", []) if isinstance(visual, dict) else []
        for shot in shots:
            if not isinstance(shot, dict):
                continue
            for field in ("asset_path", "fallback_asset_path"):
                value = shot.get(field)
                if value is None:
                    continue
                if not isinstance(value, str) or not value.startswith("job-visuals/"):
                    raise ValueError("Approved visual asset path must use job-visuals/")
                relative = Path(value.removeprefix("job-visuals/"))
                if not relative.parts or relative.is_absolute() or ".." in relative.parts:
                    raise ValueError("Approved visual asset path is unsafe")
                found.add(relative.as_posix())
    return sorted(found)


def _safe_job_visual(job_dir: Path, relative: str, *, must_exist: bool) -> Path:
    visual_root = (job_dir / "visuals").resolve()
    candidate = visual_root / relative
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(visual_root)
    except ValueError as exc:
        raise ValueError("Paired visual asset escapes its job directory") from exc
    if candidate.is_symlink() or (must_exist and not candidate.is_file()):
        raise ValueError(f"Paired visual asset is missing or unsafe: {relative}")
    return candidate


def adopt_completed_visual_plan(source_job_dir: Path, target_job_dir: Path) -> dict[str, Any]:
    """Copy a completed job's approved visuals without touching target voice state."""
    source_job_dir = Path(source_job_dir)
    target_job_dir = Path(target_job_dir)
    source_script = json.loads((source_job_dir / "script.json").read_text(encoding="utf-8"))
    target_script = json.loads((target_job_dir / "script.json").read_text(encoding="utf-8"))
    source_narration = narration_fingerprint(source_script)
    if narration_fingerprint(target_script) != source_narration:
        raise ValueError("Source and target narration locks do not match")
    _approved_source(source_job_dir, source_script)

    assets = []
    for relative in _referenced_visual_files(source_script):
        source = _safe_job_visual(source_job_dir, relative, must_exist=True)
        target = _safe_job_visual(target_job_dir, relative, must_exist=False)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + ".paired-tmp")
        shutil.copyfile(source, temporary)
        digest = _sha256_file(source)
        if temporary.stat().st_size != source.stat().st_size or _sha256_file(temporary) != digest:
            temporary.unlink(missing_ok=True)
            raise ValueError(f"Paired visual asset failed integrity validation: {relative}")
        os.replace(temporary, target)
        assets.append({
            "path": relative,
            "bytes": target.stat().st_size,
            "sha256": digest,
        })

    script_bytes = _canonical_json(source_script)
    script_tmp = target_job_dir / "script.json.paired-tmp"
    script_tmp.write_bytes(script_bytes)
    os.replace(script_tmp, target_job_dir / "script.json")
    checkpoint = {
        "version": CHECKPOINT_VERSION,
        "source_job_id": source_job_dir.name,
        "narration_fingerprint": source_narration,
        "script_sha256": _sha256_bytes(script_bytes),
        "assets": assets,
    }
    write_json_atomically(target_job_dir / CHECKPOINT_NAME, checkpoint)
    return source_script


def load_adopted_visual_plan(job_dir: Path) -> dict[str, Any] | None:
    """Return an adopted plan only when its script and every asset still match."""
    job_dir = Path(job_dir)
    try:
        checkpoint = json.loads((job_dir / CHECKPOINT_NAME).read_text(encoding="utf-8"))
        script = json.loads((job_dir / "script.json").read_text(encoding="utf-8"))
        script_bytes = _canonical_json(script)
        if checkpoint.get("version") != CHECKPOINT_VERSION:
            return None
        if checkpoint.get("script_sha256") != _sha256_bytes(script_bytes):
            return None
        if checkpoint.get("narration_fingerprint") != narration_fingerprint(script):
            return None
        assets = checkpoint.get("assets")
        if not isinstance(assets, list):
            return None
        for item in assets:
            if not isinstance(item, dict):
                return None
            path = _safe_job_visual(job_dir, item.get("path"), must_exist=True)
            if path.stat().st_size != item.get("bytes") or _sha256_file(path) != item.get("sha256"):
                return None
        return script
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None
