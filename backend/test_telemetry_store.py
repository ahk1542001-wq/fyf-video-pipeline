"""Tests for privacy-safe telemetry store."""

from pathlib import Path
from backend.telemetry_store import (
    get_all_telemetry_summary,
    get_job_telemetry,
    record_job_telemetry,
)


def test_record_and_get_job_telemetry(tmp_path: Path):
    job_id = "abcd1234"
    metrics = {
        "model_name": "gemini-3.7-flash",
        "model_call_count": 5,
        "input_tokens": 20_000,
        "output_tokens": 4_000,
        "tts_characters": 800,
        "status": "completed",
    }
    telemetry_file = record_job_telemetry(job_id, metrics, base_dir=tmp_path)
    assert telemetry_file.is_file()

    read_back = get_job_telemetry(job_id, base_dir=tmp_path)
    job_data = read_back["job"]
    assert job_data["job_id"] == job_id
    assert job_data["model_name"] == "gemini-3.7-flash"
    assert job_data["input_tokens"] == 20_000
    assert job_data["output_tokens"] == 4_000
    assert job_data["estimated_cost_usd"] > 0.0
    assert job_data["is_estimate"] is True
    # Ensure sensitive fields are not stored
    assert "prompt" not in job_data
    assert "response" not in job_data
    assert "ip" not in job_data


def test_telemetry_overview_summary(tmp_path: Path):
    record_job_telemetry(
        "11112222",
        {"model_name": "gemini-3.7-flash", "input_tokens": 1000, "output_tokens": 200},
        base_dir=tmp_path,
    )
    record_job_telemetry(
        "33334444",
        {"model_name": "gemini-3.7-flash", "input_tokens": 2000, "output_tokens": 400},
        base_dir=tmp_path,
    )

    summary = get_all_telemetry_summary(base_dir=tmp_path)
    assert summary["total_jobs"] == 2
    assert summary["total_input_tokens"] == 3000
    assert summary["total_output_tokens"] == 600
    assert summary["total_estimated_cost_usd"] > 0.0
