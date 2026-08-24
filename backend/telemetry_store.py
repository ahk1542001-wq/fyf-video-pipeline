"""Privacy-safe local telemetry store and cost reporting.

Never stores full prompts, model responses, API keys, service accounts,
voice reference audio, client IP addresses, or raw exception strings.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from backend.budget_store import record_cost
from backend.cost_catalog import estimate_job_cost
from backend.job_store import is_valid_job_id, read_job_status, write_json_atomically

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TELEMETRY_DIR = REPO_ROOT / "output" / "telemetry"


def record_job_telemetry(
    job_id: str,
    metrics: Dict[str, Any],
    base_dir: Path | None = None,
) -> Path:
    """Atomically record sanitized per-job telemetry metrics and debit budget ledger."""
    if not is_valid_job_id(job_id):
        raise ValueError(f"Invalid job ID for telemetry: {job_id}")

    target_dir = base_dir or DEFAULT_TELEMETRY_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    telemetry_file = target_dir / f"{job_id}.json"

    model_name = str(metrics.get("model_name", "gemini-3.7-flash"))
    input_tokens = int(metrics.get("input_tokens", 0))
    output_tokens = int(metrics.get("output_tokens", 0))
    tts_characters = int(metrics.get("tts_characters", 0))

    cost_estimate = estimate_job_cost(
        model_name=model_name,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        tts_characters=tts_characters,
    )

    sanitized: Dict[str, Any] = {
        "job_id": job_id,
        "created_at": metrics.get("created_at") or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "completed_at": metrics.get("completed_at"),
        "total_duration_ms": metrics.get("total_duration_ms", 0),
        "stage_duration_ms": metrics.get("stage_duration_ms", {}),
        "model_name": model_name,
        "model_call_count": int(metrics.get("model_call_count", 0)),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "retry_count": int(metrics.get("retry_count", 0)),
        "tts_request_count": int(metrics.get("tts_request_count", 0)),
        "tts_characters": tts_characters,
        "render_duration_ms": metrics.get("render_duration_ms", 0),
        "estimated_cost_usd": cost_estimate.estimated_cost_usd,
        "cost_status": cost_estimate.cost_status,
        "cost_catalog_version": cost_estimate.catalog_version,
        "is_estimate": True,
        "status": metrics.get("status", "completed"),
        "summary": {
            "total_calls": int(metrics.get("model_call_count", 0)),
            "total_input_tokens": input_tokens,
            "total_output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
            "token_status": "complete" if (input_tokens or output_tokens) else "none",
            "estimated_cost_usd": cost_estimate.estimated_cost_usd,
            "cost_status": cost_estimate.cost_status,
            "job_status": metrics.get("status", "completed"),
            "retry_calls": int(metrics.get("retry_count", 0)),
            "failed_calls": 0,
        },
        "calls": metrics.get("calls", []),
    }

    write_json_atomically(telemetry_file, sanitized)

    # Best-effort ClickHouse Cloud mirror (Agentic Cinema partner track).
    # Never blocks the local write; silently skipped when unconfigured.
    try:
        from backend.clickhouse_telemetry import (
            record_job_telemetry as _ch_record_job_telemetry,
        )

        _ch_record_job_telemetry(
            job_id=job_id,
            title=str(metrics.get("title", "")),
            duration_sec=float(sanitized["total_duration_ms"]) / 1000.0,
            voice_mode="gemini",
            status=str(sanitized["status"]),
            total_render_time_ms=int(metrics.get("render_duration_ms", 0)),
            total_tokens_used=input_tokens + output_tokens,
            cost_usd=float(cost_estimate.estimated_cost_usd),
            qa_passed=bool(metrics.get("qa_passed", False)),
        )
    except Exception as exc:  # pragma: no cover - optional sink
        logger.debug("ClickHouse telemetry mirror skipped: %s", exc)

    if cost_estimate.estimated_cost_usd > 0:
        from backend.budget_store import reconcile_budget
        reconcile_budget(job_id, cost_estimate.estimated_cost_usd, outcome=metrics.get("status", "completed"), root_dir=target_dir.parent)
    return telemetry_file


def get_job_telemetry(
    job_id: str,
    base_dir: Path | None = None,
    job_roots: Tuple[Path, ...] | None = None,
) -> Dict[str, Any]:
    """Retrieve detailed telemetry for a single job wrapped in {job, scenes}."""
    if not is_valid_job_id(job_id):
        raise ValueError(f"Invalid job ID: {job_id}")

    roots = job_roots or (REPO_ROOT / "output" / "jobs", REPO_ROOT / "output" / "script-jobs")

    # 1. Prefer job-local telemetry.json
    for root in roots:
        job_dir = root / job_id
        telemetry_file = job_dir / "telemetry.json"
        if telemetry_file.is_file():
            try:
                data = json.loads(telemetry_file.read_text(encoding="utf-8"))
                return {"job": data, "scenes": []}
            except (OSError, json.JSONDecodeError):
                pass

    # 2. Check base_dir
    target_dir = base_dir or DEFAULT_TELEMETRY_DIR
    for name in (f"{job_id}.json", f"job_{job_id}.json"):
        file = target_dir / name
        if file.is_file():
            try:
                data = json.loads(file.read_text(encoding="utf-8"))
                return {"job": data, "scenes": []}
            except (OSError, json.JSONDecodeError):
                pass

    # 3. Fallback from job status with zero fabricated numbers
    for root in roots:
        job_dir = root / job_id
        if job_dir.is_dir():
            try:
                status_data = read_job_status(job_dir)
                job_payload = {
                    "job_id": job_id,
                    "created_at": status_data.get("created_at"),
                    "completed_at": status_data.get("updated_at"),
                    "total_duration_ms": 0,
                    "stage_duration_ms": status_data.get("stage_timings", {}),
                    "model_name": "gemini-3.7-flash",
                    "model_call_count": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "retry_count": int(status_data.get("resume_count", 0)),
                    "tts_request_count": 0,
                    "tts_characters": 0,
                    "render_duration_ms": 0,
                    "estimated_cost_usd": 0.0,
                    "cost_status": "unavailable",
                    "cost_catalog_version": "2026-08-20",
                    "is_estimate": False,
                    "status": status_data.get("status", "unknown"),
                    "summary": {
                        "total_calls": 0,
                        "total_input_tokens": 0,
                        "total_output_tokens": 0,
                        "total_tokens": 0,
                        "token_status": "unavailable",
                        "estimated_cost_usd": 0.0,
                        "cost_status": "unavailable",
                        "job_status": status_data.get("status", "unknown"),
                        "retry_calls": int(status_data.get("resume_count", 0)),
                        "failed_calls": 0,
                    },
                    "calls": [],
                }
                return {"job": job_payload, "scenes": []}
            except Exception:
                pass

    empty_job = {
        "job_id": job_id,
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "completed_at": None,
        "total_duration_ms": 0,
        "stage_duration_ms": {},
        "model_name": "gemini-3.7-flash",
        "model_call_count": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "retry_count": 0,
        "tts_request_count": 0,
        "tts_characters": 0,
        "render_duration_ms": 0,
        "estimated_cost_usd": 0.0,
        "cost_status": "unavailable",
        "cost_catalog_version": "2026-08-20",
        "is_estimate": False,
        "status": "not_found",
        "summary": {
            "total_calls": 0,
            "total_input_tokens": 0,
            "total_output_tokens": 0,
            "total_tokens": 0,
            "token_status": "none",
            "estimated_cost_usd": 0.0,
            "cost_status": "unavailable",
            "job_status": "not_found",
            "retry_calls": 0,
            "failed_calls": 0,
        },
        "calls": [],
    }
    return {"job": empty_job, "scenes": []}


def get_all_telemetry_summary(
    base_dir: Path | None = None,
    job_roots: Tuple[Path, ...] | None = None,
    budget_root: Path | None = None,
) -> Dict[str, Any]:
    """Provide aggregated telemetry overview for dashboard visualization."""
    target_dir = base_dir or DEFAULT_TELEMETRY_DIR
    roots = job_roots if job_roots is not None else (() if base_dir is not None else (REPO_ROOT / "output" / "jobs", REPO_ROOT / "output" / "script-jobs"))
    job_records: List[Dict[str, Any]] = []

    seen_ids: set[str] = set()

    # Collect from job roots IF explicit roots or base_dir is None
    if base_dir is None or job_roots is not None:
        for root in roots:
            if not root.is_dir():
                continue
            for job_dir in root.iterdir():
                if not job_dir.is_dir() or not is_valid_job_id(job_dir.name) or job_dir.name in seen_ids:
                    continue
                telemetry_file = job_dir / "telemetry.json"
                if telemetry_file.is_file():
                    try:
                        data = json.loads(telemetry_file.read_text(encoding="utf-8"))
                        job_records.append(data)
                        seen_ids.add(job_dir.name)
                    except (OSError, json.JSONDecodeError):
                        continue

    # Collect from telemetry dir
    if target_dir.is_dir():
        for file in target_dir.glob("*.json"):
            stem = file.stem.replace("job_", "")
            if not is_valid_job_id(stem) or stem in seen_ids:
                continue
            try:
                data = json.loads(file.read_text(encoding="utf-8"))
                job_records.append(data)
                seen_ids.add(stem)
            except (OSError, json.JSONDecodeError):
                continue

    total_jobs = len(job_records)
    total_tokens = 0
    total_input_tokens = 0
    total_output_tokens = 0
    total_cost_usd = 0.0
    total_calls = 0

    for rec in job_records:
        summary = rec.get("summary") or {}
        in_tok = summary.get("total_input_tokens") or rec.get("input_tokens", 0)
        out_tok = summary.get("total_output_tokens") or rec.get("output_tokens", 0)
        total_input_tokens += in_tok
        total_output_tokens += out_tok
        total_tokens += summary.get("total_tokens") or (in_tok + out_tok)
        cost = summary.get("estimated_cost_usd") or rec.get("estimated_cost_usd")
        if isinstance(cost, (int, float)):
            total_cost_usd += float(cost)
        total_calls += summary.get("total_calls") or rec.get("model_call_count", 0)

    job_records.sort(key=lambda r: r.get("created_at") or r.get("started_at", ""), reverse=True)

    from backend.budget_store import get_budget_status
    budget_info = get_budget_status(root_dir=budget_root)
    if budget_info.get("corrupted") or budget_info.get("total_spend_usd") == float("inf"):
        computed_budget_status = "corrupted"
    elif budget_info.get("budget_exceeded"):
        computed_budget_status = "cap_exceeded"
    else:
        computed_budget_status = "healthy"

    return {
        "total_jobs": total_jobs,
        "total_tokens_used": total_tokens,
        "total_input_tokens": total_input_tokens,
        "total_output_tokens": total_output_tokens,
        "total_cost_usd": round(total_cost_usd, 6),
        "total_estimated_cost_usd": round(total_cost_usd, 6),
        "avg_render_time_sec": 0,
        "total_vertex_calls": total_calls,
        "jobs": job_records[:10],
        "recent_jobs": job_records[:10],
        "budget_status": computed_budget_status,
    }
