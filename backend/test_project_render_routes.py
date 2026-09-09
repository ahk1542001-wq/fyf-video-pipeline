from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import backend.main as main
from backend.job_store import update_job_status


PID = "b1c2d3e4"


def _script() -> dict:
    return {
        "title": "Studio render",
        "language": "my-MM",
        "segments": [
            {
                "id": "s1",
                "text": "မင်္ဂလာပါ",
                "visual_action": "title card",
                "scene_type": "whiteboard",
                "mascot_action": "present",
                "emotion": "warm",
            }
        ],
    }


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    jobs = tmp_path / "jobs"
    monkeypatch.setattr(main, "JOBS_ROOT", jobs)
    monkeypatch.setattr(main, "SCRIPT_JOBS_ROOT", tmp_path / "script-jobs")
    monkeypatch.setattr(main, "LOCKS_ROOT", tmp_path / "locks")
    monkeypatch.setenv("FYF_PROJECTS_ROOT", str(tmp_path / "projects"))
    monkeypatch.setenv("FYF_QUEUE_ROOT", str(tmp_path / "queue"))
    monkeypatch.setenv("FYF_BUDGET_LEDGER_PATH", str(tmp_path / "budget.json"))
    monkeypatch.setenv("FYF_DAILY_BUDGET_CAP_USD", "10")
    monkeypatch.setenv("FYF_TOTAL_BUDGET_CAP_USD", "10")
    monkeypatch.delenv("FYF_PUBLIC_DEPLOYMENT", raising=False)

    async def fake_pipeline(job_id: str, script_data: dict, voice_provider: str, jobs_root: Path):
        job_dir = Path(jobs_root) / job_id
        video = job_dir / "video.mp4"
        video.write_bytes(b"real-pipeline-boundary-test")
        manifest = {
            "manifest_contract_version": 1,
            "render_manifest": {
                "video_spec_version": "test-spec-v1",
                "asset_hashes": {},
                "fonts": [],
                "renderer_version": "test",
                "skill_versions": {},
                "seeds": {},
                "output": {
                    "aspect_ratio": script_data.get("aspect_ratio", "9:16"),
                    "width": 1080,
                    "height": 1920,
                    "duration_seconds": 1,
                    "fps": 30,
                    "codec": "h264",
                },
            },
            "video_sha256": main._sha256_path(video),
        }
        (job_dir / "render_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        update_job_status(job_dir, {"status": "completed", "video_url": f"/api/jobs/{job_id}/video"})

    monkeypatch.setattr(main, "run_pipeline", fake_pipeline)
    with TestClient(main.app) as test_client:
        yield test_client


def _create(client: TestClient) -> None:
    response = client.post(
        "/api/projects",
        json={"project_id": PID, "script": _script(), "actor": "owner"},
    )
    assert response.status_code == 201, response.text


def test_project_render_requires_explicit_approved_budget(client: TestClient):
    _create(client)
    response = client.post(
        f"/api/projects/{PID}/render",
        headers={"x-fyf-idempotency-key": "studio-render-1"},
        json={"base_version": 1, "approved_spend_usd": 0.01},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["error"] == "approval_below_estimate"
    assert client.get(f"/api/projects/{PID}/versions").json()["head"] == 1


def test_project_render_queues_real_pipeline_and_attaches_completed_result(client: TestClient):
    _create(client)
    response = client.post(
        f"/api/projects/{PID}/render",
        headers={"x-fyf-idempotency-key": "studio-render-2"},
        json={
            "base_version": 1,
            "approved_spend_usd": 3,
            "aspect_ratio": "9:16",
            "reduced_motion": True,
        },
    )
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["dispatched_version"] == 2
    assert body["job_id"]

    version = client.get(f"/api/projects/{PID}/versions/3").json()["version"]
    assert version["source_command_operation"] is None
    assert version["render_manifest"]["video_spec_version"] == "test-spec-v1"
    video_assets = [item for item in version["asset_references"] if item["kind"] == "video"]
    assert video_assets == [
        {
            "asset_id": f"job:{body['job_id']}:video",
            "kind": "video",
            "uri": f"/api/jobs/{body['job_id']}/video",
            "sha256": main._sha256_path(main.JOBS_ROOT / body["job_id"] / "video.mp4"),
        }
    ]

    stored_script = json.loads((main.JOBS_ROOT / body["job_id"] / "script.json").read_text())
    assert stored_script["reduced_motion"] is True


def test_project_render_replay_does_not_create_duplicate_version_or_job(client: TestClient):
    _create(client)
    request = {
        "base_version": 1,
        "approved_spend_usd": 3,
        "aspect_ratio": "9:16",
    }
    headers = {"x-fyf-idempotency-key": "studio-render-replay"}
    first = client.post(f"/api/projects/{PID}/render", headers=headers, json=request)
    replay = client.post(f"/api/projects/{PID}/render", headers=headers, json=request)
    assert first.status_code == 202, first.text
    assert replay.status_code == 202, replay.text
    assert replay.json()["job_id"] == first.json()["job_id"]
    assert client.get(f"/api/projects/{PID}/versions").json()["head"] == 3


def test_project_render_rejects_stale_base_before_recording_or_dispatch(client: TestClient):
    _create(client)
    client.post(
        f"/api/projects/{PID}/variants",
        json={"variant_name": "newer", "base_version": 1},
    )
    response = client.post(
        f"/api/projects/{PID}/render",
        headers={"x-fyf-idempotency-key": "stale-render"},
        json={"base_version": 1, "approved_spend_usd": 3},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "stale_version"


@pytest.mark.asyncio
async def test_attachment_integrity_failure_never_leaves_job_falsely_completed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    job_id = "d1e2f3a4"
    job_dir = tmp_path / job_id
    job_dir.mkdir()
    main.initialize_job_status(job_dir, job_id, "gemini")

    async def completed_pipeline(*_args, **_kwargs):
        update_job_status(job_dir, {"status": "completed", "video_url": f"/api/jobs/{job_id}/video"})

    monkeypatch.setattr(main, "run_pipeline", completed_pipeline)
    monkeypatch.setattr(
        main,
        "_attach_completed_project_result",
        lambda *_args: (_ for _ in ()).throw(ValueError("manifest mismatch")),
    )
    await main._run_video_pipeline_tracked(job_id, _script(), "gemini", tmp_path)
    status = main.read_job_status(job_dir)
    assert status["status"] == "needs_attention"
    assert status["video_url"] is None
    assert "integrity verification" in status["error"]
