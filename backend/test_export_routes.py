from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import backend.main as main
from backend.job_store import initialize_job_status, update_job_status
from backend.render_manifest import (
    build_render_manifest,
    manifest_document,
    seal_manifest_document,
    write_render_manifest,
)


JOB_ID = "c1d2e3f4"


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    jobs = tmp_path / "jobs"
    monkeypatch.setattr(main, "JOBS_ROOT", jobs)
    monkeypatch.delenv("FYF_PUBLIC_DEPLOYMENT", raising=False)
    job = jobs / JOB_ID
    job.mkdir(parents=True)
    initialize_job_status(job, JOB_ID, "gemini")
    update_job_status(job, {"status": "completed", "video_url": f"/api/jobs/{JOB_ID}/video"})
    (job / "video.mp4").write_bytes(b"video")
    render_input = {
        "language": "en-US",
        "fps": 30,
        "width": 1080,
        "height": 1920,
        "durationInFrames": 60,
        "segments": [
            {"id": "s1", "text": "Hello world", "startFrame": 0, "endFrame": 60}
        ],
    }
    (job / "render_input.json").write_text(json.dumps(render_input), encoding="utf-8")
    manifest = build_render_manifest(
        job,
        render_input,
        renderer_version="test-remotion",
        skill_versions={"export": "1"},
        include_asset_hashes=False,
    )
    document = manifest_document(
        manifest,
        results=[],
        manifest_fingerprint="0" * 64,
        renderer_source_hash="test-renderer",
        composition_id="VisualSystemV3Full",
        remotion_version="test-remotion",
        video_sha256=hashlib.sha256(b"video").hexdigest(),
        video_bytes=len(b"video"),
    )
    write_render_manifest(job, seal_manifest_document(document))
    with TestClient(main.app) as test_client:
        yield test_client


def test_export_catalog_and_selected_only_materialization(client: TestClient):
    catalog = client.get("/api/export-kinds")
    assert catalog.status_code == 200
    kinds = {item["kind"] for item in catalog.json()["exports"]}
    assert {"captions-srt", "transcript", "provenance"}.issubset(kinds)

    response = client.post(
        f"/api/jobs/{JOB_ID}/exports",
        json={"kinds": ["captions-srt", "transcript"]},
    )
    assert response.status_code == 201, response.text
    assert [item["kind"] for item in response.json()["exports"]] == [
        "captions-srt",
        "transcript",
    ]
    export_dir = main.JOBS_ROOT / JOB_ID / "exports"
    assert {path.name for path in export_dir.iterdir()} == {"captions-srt.srt", "transcript.md"}


def test_export_download_rejects_path_traversal(client: TestClient):
    response = client.get(f"/api/jobs/{JOB_ID}/exports/%2E%2E%2Fstatus.json")
    assert response.status_code in {400, 404}


def test_export_unknown_kind_fails_whole_request_without_output(client: TestClient):
    response = client.post(
        f"/api/jobs/{JOB_ID}/exports",
        json={"kinds": ["transcript", "not-real"]},
    )
    assert response.status_code == 422
    assert not (main.JOBS_ROOT / JOB_ID / "exports").exists()


def test_export_runtime_failure_leaves_no_partial_selection(client: TestClient):
    response = client.post(
        f"/api/jobs/{JOB_ID}/exports",
        json={"kinds": ["transcript", "video-1080p-9x16"]},
    )
    assert response.status_code == 422
    assert not (main.JOBS_ROOT / JOB_ID / "exports").exists()
