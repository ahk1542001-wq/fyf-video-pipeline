"""Focused Task 4/5 API integration tests.

These tests cover only the shared FastAPI/API-state seam.  Core QA, budget and
telemetry behavior is exercised by their owning focused suites; this module
asserts that the API composes those contracts without weakening them.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import backend.main as main
import backend.runtime_limits as runtime_limits
from backend.output_qa import OUTPUT_QA_REPORT_VERSION, persist_qa_report, qa_report_fingerprint
from backend.job_store import initialize_job_status, update_job_status


PROJECT_ID = "1234abcd"
JOB_ID = "a1b2c3d4"


def _script() -> dict:
    return {
        "title": "Integration test",
        "language": "en-US",
        "segments": [
            {
                "id": "s1",
                "text": "Hello world",
                "visual_action": "show the title",
                "scene_type": "whiteboard",
                "mascot_action": "present",
                "emotion": "neutral",
                "emphasis": [],
            }
        ],
    }


def _qa_evidence() -> dict:
    evidence = {
        "report_version": OUTPUT_QA_REPORT_VERSION,
        "passed": True,
        "status": "passed",
        "failure_codes": [],
        "job_id": JOB_ID,
        "report_name": "qa_report",
        "attempt": 1,
        "qa_identity": {"job_id": JOB_ID, "report_name": "qa_report", "attempt": 1},
        "persisted_at_source": "local_deterministic_qa",
        "checks": [{"name": "integration", "passed": True}],
    }
    evidence["fingerprint"] = qa_report_fingerprint(evidence)
    return evidence


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(main, "JOBS_ROOT", tmp_path / "jobs")
    monkeypatch.setattr(main, "PROJECTS_ROOT", tmp_path / "projects")
    monkeypatch.setattr(main, "LOCKS_ROOT", tmp_path / "locks")
    monkeypatch.setattr(main, "SCRIPT_JOBS_ROOT", tmp_path / "script-jobs")
    monkeypatch.setattr(main, "QUEUE_ROOT", tmp_path / "queue")
    monkeypatch.setattr(main, "TELEMETRY_ROOT", tmp_path / "telemetry")
    monkeypatch.setenv("FYF_QUEUE_ROOT", str(tmp_path / "queue"))
    monkeypatch.setenv("FYF_BUDGET_LEDGER_PATH", str(tmp_path / "budget.json"))
    monkeypatch.setenv("FYF_DAILY_BUDGET_CAP_USD", "20")
    monkeypatch.setenv("FYF_TOTAL_BUDGET_CAP_USD", "20")
    monkeypatch.delenv("FYF_PROJECTS_ROOT", raising=False)
    monkeypatch.delenv("FYF_PUBLIC_DEPLOYMENT", raising=False)
    with TestClient(main.app, raise_server_exceptions=False) as test_client:
        yield test_client


def _create_project(client: TestClient) -> None:
    response = client.post(
        "/api/projects",
        json={"project_id": PROJECT_ID, "script": _script(), "actor": "owner"},
    )
    assert response.status_code == 201, response.text


def test_project_budget_api_is_project_scoped_and_customizable(client: TestClient):
    _create_project(client)

    configured = client.post(
        f"/api/projects/{PROJECT_ID}/budget",
        json={"budget_usd": 1.25, "actor": "owner"},
    )
    assert configured.status_code == 200, configured.text
    assert configured.json()["budget"]["project_budget_usd"] == 1.25

    status = client.get("/api/budget", params={"project_id": PROJECT_ID})
    assert status.status_code == 200, status.text
    assert status.json()["budget"]["project_id"] == PROJECT_ID
    assert status.json()["budget"]["project_budget_usd"] == 1.25


def test_guardrail_reservation_carries_project_identity(client: TestClient):
    _create_project(client)
    lease = runtime_limits.acquire_guardrail_lease(
        operation_id="project-budget-lease",
        estimated_charge_usd=0.25,
        root_dir=Path(main.JOBS_ROOT).parent / "budget-root",
        job_roots=(main.JOBS_ROOT, main.SCRIPT_JOBS_ROOT),
        project_id=PROJECT_ID,
    )
    try:
        status = runtime_limits.get_budget_status(
            root_dir=Path(main.JOBS_ROOT).parent / "budget-root",
            project_id=PROJECT_ID,
        )
        assert status["project_id"] == PROJECT_ID
        assert status["project_reserved_usd"] == 0.25
    finally:
        lease.release()


def test_acceptance_api_is_bound_to_existing_project_version(client: TestClient):
    _create_project(client)

    client_only_qa = client.post(
        f"/api/projects/{PROJECT_ID}/versions/1/acceptance",
        json={
            "actor": "owner",
            "accepted": True,
            "automated_qa": _qa_evidence(),
        },
    )
    assert client_only_qa.status_code == 409, client_only_qa.text
    assert client_only_qa.json()["detail"]["error"] == "automated_qa_unavailable"

    missing_version = client.post(
        f"/api/projects/{PROJECT_ID}/versions/2/acceptance",
        json={"actor": "owner", "accepted": False},
    )
    assert missing_version.status_code == 404

    readiness = client.get(f"/api/projects/{PROJECT_ID}/versions/1/acceptance")
    assert readiness.status_code == 200
    assert readiness.json()["readiness"]["human_accepted"] is False
    assert readiness.json()["readiness"]["automated_qa_passed"] is False
    assert readiness.json()["readiness"]["download_ready"] is False


def test_acceptance_api_rejects_client_only_qa_evidence(client: TestClient):
    """A caller cannot manufacture final acceptance from a forged QA payload."""
    _create_project(client)
    response = client.post(
        f"/api/projects/{PROJECT_ID}/versions/1/acceptance",
        json={
            "actor": "owner",
            "accepted": True,
            "automated_qa": _qa_evidence(),
        },
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["error"] == "automated_qa_unavailable"


def test_acceptance_api_uses_current_server_qa_instead_of_client_payload(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    _create_project(client)
    job_dir = main.JOBS_ROOT / JOB_ID
    job_dir.mkdir(parents=True)
    persisted_path = persist_qa_report(
        job_dir,
        {
            "passed": True,
            "status": "passed",
            "checks": [{"name": "server-owned", "passed": True}],
            "failure_codes": [],
        },
        attempt=1,
    )
    server_qa = json.loads(persisted_path.read_text(encoding="utf-8"))
    forged_qa = dict(server_qa)
    forged_qa["checks"] = [{"name": "client-forged", "passed": True}]
    forged_qa["fingerprint"] = qa_report_fingerprint(forged_qa)
    monkeypatch.setattr(
        main,
        "_project_media_readiness",
        lambda *_args, **_kwargs: {
            "project_id": PROJECT_ID,
            "version_no": 1,
            "current_version": 1,
            "current": True,
            "video_current": True,
            "manifest_verified": True,
            "job_id": JOB_ID,
            "status": "ready",
            "reason": None,
        },
    )

    accepted = client.post(
        f"/api/projects/{PROJECT_ID}/versions/1/acceptance",
        json={"actor": "owner", "accepted": True, "automated_qa": forged_qa},
    )
    assert accepted.status_code == 200, accepted.text
    body = accepted.json()
    assert body["acceptance"]["automated_qa"]["fingerprint"] == server_qa["fingerprint"]
    assert body["acceptance"]["automated_qa"]["checks"] == server_qa["checks"]
    assert body["readiness"]["automated_qa_passed"] is True


def test_project_bound_video_download_requires_acceptance_and_current_artifacts(
    client: TestClient,
):
    _create_project(client)
    job_dir = main.JOBS_ROOT / JOB_ID
    job_dir.mkdir(parents=True)
    initialize_job_status(job_dir, JOB_ID, "gemini")
    update_job_status(job_dir, {"status": "completed", "video_url": f"/api/jobs/{JOB_ID}/video"})
    (job_dir / "video.mp4").write_bytes(b"video")
    (job_dir / "project_context.json").write_text(
        json.dumps(
            {
                "project_id": PROJECT_ID,
                "dispatched_version": 1,
                "idempotency_key": "render-v1",
            }
        ),
        encoding="utf-8",
    )

    response = client.get(f"/api/jobs/{JOB_ID}/video")
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["error"] == "download_not_ready"


def test_project_bound_export_download_rechecks_acceptance_gate(client: TestClient):
    _create_project(client)
    job_dir = main.JOBS_ROOT / JOB_ID
    job_dir.mkdir(parents=True)
    (job_dir / "project_context.json").write_text(
        json.dumps(
            {
                "project_id": PROJECT_ID,
                "dispatched_version": 1,
                "idempotency_key": "render-v1",
            }
        ),
        encoding="utf-8",
    )
    export_path = job_dir / "exports" / "transcript.md"
    export_path.parent.mkdir(parents=True)
    export_path.write_text("stale export", encoding="utf-8")

    response = client.get(f"/api/jobs/{JOB_ID}/exports/transcript.md")
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["error"] == "download_not_ready"


def test_telemetry_reconciliation_api_exposes_local_truth_without_cloud_claim(
    client: TestClient,
):
    response = client.get("/api/telemetry/reconciliation")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["source"] == "local_mirror"
    assert body["cloud"]["connected"] is False
    assert body["cloud"]["label"] != "ClickHouse Cloud"
    assert body["outbox"]["pending"] == 0
    assert body["completeness"]["status"] in {"ok", "unavailable"}


def test_project_render_enqueue_failure_terminally_rolls_back_exact_approval(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    _create_project(client)

    async def fail_enqueue(*args, **kwargs):
        raise RuntimeError("queue unavailable")

    monkeypatch.setattr(main, "generate_video", fail_enqueue)
    response = client.post(
        f"/api/projects/{PROJECT_ID}/render",
        headers={"X-FYF-Idempotency-Key": "render-v1"},
        json={"base_version": 1, "approved_spend_usd": 1.0},
    )
    assert response.status_code == 500, response.text

    ledger = json.loads((Path(main.JOBS_ROOT).parent / "budget.json").read_text())
    approval = ledger["approvals"]["project-render:render-v1"]
    assert approval["project_id"] == PROJECT_ID
    assert approval["target_ref"] == f"{PROJECT_ID}@v1"
    assert approval["status"] == "failed"
