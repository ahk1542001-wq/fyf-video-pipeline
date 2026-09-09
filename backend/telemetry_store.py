"""Privacy-safe local telemetry store and cost reporting.

Never stores full prompts, model responses, API keys, service accounts,
voice reference audio, client IP addresses, or raw exception strings.
"""

from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from backend.budget_store import record_cost
from backend.cost_catalog import estimate_job_cost
from backend.job_store import is_valid_job_id, read_job_status, write_json_atomically
from backend.telemetry_reconcile import audience_metrics, separate_quality_signals

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TELEMETRY_DIR = REPO_ROOT / "output" / "telemetry"


def _optional_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        numeric = float(value)
        if not math.isfinite(numeric):
            return None
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed >= 0 else None


def _optional_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed >= 0 and math.isfinite(parsed) else None


def _sanitize_non_finite(value: Any) -> Any:
    """Replace non-finite nested numeric values with unavailable ``None``."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {key: _sanitize_non_finite(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_sanitize_non_finite(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_sanitize_non_finite(item) for item in value)
    return value


def _sum_known(values: list[int | float | None]) -> int | float | None:
    known = [value for value in values if value is not None]
    return sum(known) if known else None


def _numeric_status(values: list[Any]) -> str:
    known = [value for value in values if value is not None]
    if not known:
        return "unavailable"
    return "complete" if len(known) == len(values) else "partial"


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

    raw_model_name = metrics.get("model_name")
    model_name = (
        raw_model_name.strip()
        if isinstance(raw_model_name, str) and raw_model_name.strip()
        else None
    )
    input_tokens = _optional_int(metrics.get("input_tokens"))
    output_tokens = _optional_int(metrics.get("output_tokens"))
    tts_characters = _optional_int(metrics.get("tts_characters"))
    has_usage = input_tokens is not None and output_tokens is not None
    cost_estimate = (
        estimate_job_cost(
            model_name=model_name or "",
            input_tokens=input_tokens or 0,
            output_tokens=output_tokens or 0,
            tts_characters=tts_characters or 0,
        )
        if has_usage
        else None
    )
    estimated_cost_usd = (
        _optional_float(cost_estimate.estimated_cost_usd)
        if cost_estimate is not None and cost_estimate.cost_status != "unknown"
        else None
    )
    cost_status = cost_estimate.cost_status if cost_estimate is not None else "unavailable"
    cost_availability = (
        "unavailable" if cost_status == "unknown" else cost_status
    )
    call_count = _optional_int(metrics.get("model_call_count"))
    if call_count is None:
        observed_calls = metrics.get("calls")
        if isinstance(observed_calls, list):
            # An explicitly supplied call list gives us a real count even when
            # the legacy aggregate field was omitted.
            call_count = len(observed_calls)
        elif input_tokens == 0 and output_tokens == 0:
            # Explicit zero usage is a known no-call aggregate in the legacy
            # adapter; do not apply this fallback to unavailable token fields.
            call_count = 0
    retry_count = _optional_int(metrics.get("retry_count"))
    tts_request_count = _optional_int(metrics.get("tts_request_count"))
    total_duration_ms = _optional_float(metrics.get("total_duration_ms"))
    render_duration_ms = _optional_float(metrics.get("render_duration_ms"))
    total_tokens = _sum_known([input_tokens, output_tokens])
    token_status = _numeric_status([input_tokens, output_tokens])
    qa_passed = metrics.get("qa_passed") if isinstance(metrics.get("qa_passed"), bool) else None

    sanitized: Dict[str, Any] = {
        "job_id": job_id,
        "project_id": _sanitize_non_finite(metrics.get("project_id")),
        "version_id": _sanitize_non_finite(metrics.get("version_id")),
        "created_at": metrics.get("created_at") or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "completed_at": metrics.get("completed_at"),
        "total_duration_ms": total_duration_ms,
        "stage_duration_ms": _sanitize_non_finite(metrics.get("stage_duration_ms", {})),
        "model_name": model_name,
        "model_call_count": call_count,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "retry_count": retry_count,
        "tts_request_count": tts_request_count,
        "tts_characters": tts_characters,
        "render_duration_ms": render_duration_ms,
        "estimated_cost_usd": estimated_cost_usd,
        "cost_status": cost_status,
        "cost_catalog_version": cost_estimate.catalog_version if cost_estimate is not None else None,
        "is_estimate": bool(cost_estimate is not None and cost_status != "unknown"),
        "cost_availability": cost_availability,
        "status": _sanitize_non_finite(metrics.get("status", "unknown")),
        "studio_name": str(_sanitize_non_finite(metrics.get("studio_name", "FYF Studio"))),
        "language": str(_sanitize_non_finite(metrics.get("language", "my-MM"))),
        "genre": str(_sanitize_non_finite(metrics.get("genre", "explainer"))),
        "summary": {
            "total_calls": call_count,
            "total_input_tokens": input_tokens,
            "total_output_tokens": output_tokens,
            "total_tokens": total_tokens,
            "token_status": token_status,
            "estimated_cost_usd": estimated_cost_usd,
            "cost_status": cost_status,
            "cost_availability": cost_availability,
            "job_status": _sanitize_non_finite(metrics.get("status", "unknown")),
            "retry_calls": retry_count,
            "failed_calls": _optional_int(metrics.get("failed_calls")),
        },
        "calls": _sanitize_non_finite(metrics.get("calls", [])),
        # Stage E7: AI quality, human approval and undo are stored as three
        # DISTINCT signals and never collapsed into one number (document 156).
        "quality_signals": separate_quality_signals(
            ai_quality_score=metrics.get("ai_quality_score"),
            human_approved=metrics.get("human_approved"),
            undo_count=metrics.get("undo_count"),
        ),
    }

    write_json_atomically(telemetry_file, sanitized)

    # Best-effort ClickHouse Cloud mirror (Agentic Cinema partner track).
    # Never blocks the local write; silently skipped when unconfigured.
    try:
        from backend.clickhouse_telemetry import record_pipeline_telemetry

        record_pipeline_telemetry(
            job_id=job_id,
            title=str(metrics.get("title", "")),
            duration_sec=(
                float(sanitized["total_duration_ms"]) / 1000.0
                if sanitized["total_duration_ms"] is not None
                else None
            ),
            voice_mode="gemini",
            status=str(sanitized["status"]),
            total_render_time_ms=(
                int(render_duration_ms) if render_duration_ms is not None else None
            ),
            total_tokens_used=(int(total_tokens) if total_tokens is not None else None),
            cost_usd=estimated_cost_usd,
            qa_passed=qa_passed,
            studio_name=str(metrics.get("studio_name", "FYF Studio")),
            language=str(metrics.get("language", "my-MM")),
            genre=str(metrics.get("genre", "explainer")),
            scenes=metrics.get("scenes") or metrics.get("scene_events") or (),
            qa_records=metrics.get("qa_records") or metrics.get("qa_events") or (),
            calls=metrics.get("calls") or (),
            base_dir=target_dir,
        )
    except Exception as exc:  # pragma: no cover - optional sink
        logger.debug("ClickHouse telemetry mirror skipped: %s", exc)

    if estimated_cost_usd is not None and estimated_cost_usd > 0:
        from backend.budget_store import reconcile_budget
        reconcile_budget(
            job_id,
            estimated_cost_usd,
            outcome=metrics.get("status", "completed"),
            root_dir=target_dir.parent,
            project_id=metrics.get("project_id"),
        )
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
                if not data.get("title"):
                    for meta_name in ("script.json", "render_input.json", "status.json"):
                        meta_file = job_dir / meta_name
                        if meta_file.is_file():
                            try:
                                mdata = json.loads(meta_file.read_text(encoding="utf-8"))
                                if mdata.get("title"):
                                    data["title"] = mdata["title"]
                                    break
                            except Exception:
                                pass
                target_dir = base_dir or DEFAULT_TELEMETRY_DIR
                for name in (f"job_{job_id}.json", f"{job_id}.json"):
                    mirror_file = target_dir / name
                    if mirror_file.is_file():
                        try:
                            mdata = json.loads(mirror_file.read_text(encoding="utf-8"))
                            if not data.get("title") and mdata.get("title"):
                                data["title"] = mdata["title"]
                            if data.get("duration_sec") is None and mdata.get("duration_sec") is not None:
                                data["duration_sec"] = mdata["duration_sec"]
                            if data.get("cost_usd") is None and mdata.get("cost_usd") is not None:
                                data["cost_usd"] = mdata["cost_usd"]
                            if not data.get("created_at") and mdata.get("created_at"):
                                data["created_at"] = mdata["created_at"]
                            if not data.get("total_render_time_ms") and mdata.get("total_render_time_ms"):
                                data["total_render_time_ms"] = mdata["total_render_time_ms"]
                        except Exception:
                            pass
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
                    "total_duration_ms": None,
                    "stage_duration_ms": status_data.get("stage_timings", {}),
                    "model_name": None,
                    "model_call_count": None,
                    "input_tokens": None,
                    "output_tokens": None,
                    "total_tokens": None,
                    "retry_count": _optional_int(status_data.get("resume_count")),
                    "tts_request_count": None,
                    "tts_characters": None,
                    "render_duration_ms": None,
                    "estimated_cost_usd": None,
                    "cost_status": "unavailable",
                    "cost_catalog_version": None,
                    "is_estimate": False,
                    "status": status_data.get("status", "unknown"),
                    "summary": {
                        "total_calls": None,
                        "total_input_tokens": None,
                        "total_output_tokens": None,
                        "total_tokens": None,
                        "token_status": "unavailable",
                        "estimated_cost_usd": None,
                        "cost_status": "unavailable",
                        "job_status": status_data.get("status", "unknown"),
                        "retry_calls": _optional_int(status_data.get("resume_count")),
                        "failed_calls": None,
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
        "total_duration_ms": None,
        "stage_duration_ms": {},
        "model_name": None,
        "model_call_count": None,
        "input_tokens": None,
        "output_tokens": None,
        "total_tokens": None,
        "retry_count": None,
        "tts_request_count": None,
        "tts_characters": None,
        "render_duration_ms": None,
        "estimated_cost_usd": None,
        "cost_status": "unavailable",
        "cost_catalog_version": None,
        "is_estimate": False,
        "status": "not_found",
        "summary": {
            "total_calls": None,
            "total_input_tokens": None,
            "total_output_tokens": None,
            "total_tokens": None,
            "token_status": "none",
            "estimated_cost_usd": None,
            "cost_status": "unavailable",
            "job_status": "not_found",
            "retry_calls": None,
            "failed_calls": None,
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
                        if not data.get("title"):
                            for meta_name in ("script.json", "render_input.json", "status.json"):
                                meta_file = job_dir / meta_name
                                if meta_file.is_file():
                                    try:
                                        mdata = json.loads(meta_file.read_text(encoding="utf-8"))
                                        if mdata.get("title"):
                                            data["title"] = mdata["title"]
                                            break
                                    except Exception:
                                        pass
                        for name in (f"job_{job_dir.name}.json", f"{job_dir.name}.json"):
                            mirror_file = target_dir / name
                            if mirror_file.is_file():
                                try:
                                    mdata = json.loads(mirror_file.read_text(encoding="utf-8"))
                                    if not data.get("title") and mdata.get("title"):
                                        data["title"] = mdata["title"]
                                    if data.get("duration_sec") is None and mdata.get("duration_sec") is not None:
                                        data["duration_sec"] = mdata["duration_sec"]
                                    if data.get("cost_usd") is None and mdata.get("cost_usd") is not None:
                                        data["cost_usd"] = mdata["cost_usd"]
                                    if not data.get("created_at") and mdata.get("created_at"):
                                        data["created_at"] = mdata["created_at"]
                                    if not data.get("total_render_time_ms") and mdata.get("total_render_time_ms"):
                                        data["total_render_time_ms"] = mdata["total_render_time_ms"]
                                except Exception:
                                    pass
                        if not data.get("status") and isinstance(data.get("summary"), dict):
                            data["status"] = data["summary"].get("job_status")
                        job_records.append(data)
                        seen_ids.add(job_dir.name)
                    except (OSError, json.JSONDecodeError):
                        continue

    # Collect from telemetry dir
    if target_dir.is_dir():
        # ``record_job_telemetry`` writes the canonical ``<job>.json`` mirror;
        # the optional ClickHouse adapter also writes a legacy ``job_<job>.json``
        # mirror.  Prefer the canonical record so richer unknown/availability
        # fields are not replaced by the compact sink shape merely because
        # filesystem glob order happened to return it first.
        files = sorted(
            target_dir.glob("*.json"),
            key=lambda path: (path.name.startswith("job_"), path.name),
        )
        for file in files:
            stem = file.stem.replace("job_", "")
            if not is_valid_job_id(stem):
                continue
            if stem in seen_ids:
                try:
                    mdata = json.loads(file.read_text(encoding="utf-8"))
                    for rec in job_records:
                        if rec.get("job_id") == stem:
                            if not rec.get("title") and mdata.get("title"):
                                rec["title"] = mdata["title"]
                            if rec.get("duration_sec") is None and mdata.get("duration_sec") is not None:
                                rec["duration_sec"] = mdata["duration_sec"]
                            if rec.get("cost_usd") is None and mdata.get("cost_usd") is not None:
                                rec["cost_usd"] = mdata["cost_usd"]
                            if not rec.get("created_at") and mdata.get("created_at"):
                                rec["created_at"] = mdata["created_at"]
                            if not rec.get("total_render_time_ms") and mdata.get("total_render_time_ms"):
                                rec["total_render_time_ms"] = mdata["total_render_time_ms"]
                            break
                except Exception:
                    pass
                continue
            try:
                data = json.loads(file.read_text(encoding="utf-8"))
                if not data.get("title"):
                    for root in roots:
                        for meta_name in ("script.json", "render_input.json", "status.json"):
                            meta_file = root / stem / meta_name
                            if meta_file.is_file():
                                try:
                                    mdata = json.loads(meta_file.read_text(encoding="utf-8"))
                                    if mdata.get("title"):
                                        data["title"] = mdata["title"]
                                        break
                                except Exception:
                                    pass
                job_records.append(data)
                seen_ids.add(stem)
            except (OSError, json.JSONDecodeError):
                continue

    total_jobs = len(job_records)
    input_values: list[int] = []
    output_values: list[int] = []
    token_values: list[int] = []
    cost_values: list[float] = []
    call_values: list[int] = []
    render_values: list[float] = []
    token_status_values: list[int | None] = []
    cost_status_values: list[float | None] = []

    for rec in job_records:
        summary = rec.get("summary") if isinstance(rec.get("summary"), dict) else {}
        in_tok = _optional_int(
            summary["total_input_tokens"]
            if "total_input_tokens" in summary
            else rec.get("input_tokens")
        )
        out_tok = _optional_int(
            summary["total_output_tokens"]
            if "total_output_tokens" in summary
            else rec.get("output_tokens")
        )
        total_for_record = _optional_int(
            summary["total_tokens"] if "total_tokens" in summary else rec.get("total_tokens")
        )
        cost = _optional_float(
            summary["estimated_cost_usd"]
            if "estimated_cost_usd" in summary
            else rec.get("estimated_cost_usd")
        )
        calls = _optional_int(
            summary["total_calls"] if "total_calls" in summary else rec.get("model_call_count")
        )
        render_ms = _optional_float(
            rec.get("render_duration_ms")
            if "render_duration_ms" in rec
            else rec.get("total_duration_ms")
        )
        if in_tok is not None:
            input_values.append(in_tok)
        if out_tok is not None:
            output_values.append(out_tok)
        if total_for_record is None and in_tok is not None and out_tok is not None:
            total_for_record = in_tok + out_tok
        token_status_values.append(total_for_record)
        cost_status_values.append(cost)
        if total_for_record is not None:
            token_values.append(total_for_record)
        if cost is not None:
            cost_values.append(cost)
        if calls is not None:
            call_values.append(calls)
        if render_ms is not None:
            render_values.append(render_ms)

    total_input_tokens = sum(input_values) if input_values else None
    total_output_tokens = sum(output_values) if output_values else None
    total_tokens = sum(token_values) if token_values else None
    total_cost_usd = round(sum(cost_values), 6) if cost_values else None
    total_calls = sum(call_values) if call_values else None
    avg_render_time_sec = (
        round((sum(render_values) / len(render_values)) / 1000, 1)
        if render_values
        else None
    )

    job_records.sort(key=lambda r: r.get("created_at") or r.get("started_at", ""), reverse=True)

    from backend.budget_store import get_budget_status
    budget_info = get_budget_status(root_dir=budget_root)
    if budget_info.get("corrupted") or budget_info.get("total_spend_usd") == float("inf"):
        computed_budget_status = "corrupted"
    elif budget_info.get("budget_exceeded"):
        computed_budget_status = "cap_exceeded"
    else:
        computed_budget_status = "healthy"

    # Best-effort outbox delivery visibility (document line 149/151).  Never
    # raises and never fabricates: unknown lag stays None, not 0.
    delivery: Dict[str, Any] = {
        "pending": None, "failed": None, "delivered": None,
        "schema_ready": None, "cloud_connected": False,
        "drain_blocked_reason": None, "ingestion_lag_seconds": None,
        "last_delivered_at": None,
    }
    try:
        from backend.clickhouse_telemetry import get_telemetry_delivery_status

        delivery = get_telemetry_delivery_status()
    except Exception:  # pragma: no cover - optional visibility
        pass

    return {
        "total_jobs": total_jobs,
        "total_tokens_used": total_tokens,
        "total_input_tokens": total_input_tokens,
        "total_output_tokens": total_output_tokens,
        "total_cost_usd": total_cost_usd,
        "total_estimated_cost_usd": total_cost_usd,
        "cost_status": _numeric_status(cost_status_values),
        "token_status": _numeric_status(token_status_values),
        "avg_render_time_sec": avg_render_time_sec,
        "total_vertex_calls": total_calls,
        "jobs": job_records[:50],
        "recent_jobs": job_records[:50],
        "budget_status": computed_budget_status,
        "delivery": delivery,
        "ingestion_lag_seconds": delivery.get("ingestion_lag_seconds"),
        # Audience retention/conversion is never captured by this pipeline and
        # is reported unavailable rather than invented (document line 158).
        "audience": audience_metrics(),
    }
