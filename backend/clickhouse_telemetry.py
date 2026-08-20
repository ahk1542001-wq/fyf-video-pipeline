"""ClickHouse telemetry store and local mirror for Agentic Cinema video pipeline."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ClickHouse connection parameters (defaults / environment)
CLICKHOUSE_HOST = os.getenv("CLICKHOUSE_HOST", "")
CLICKHOUSE_PORT = int(os.getenv("CLICKHOUSE_PORT", "8443"))
CLICKHOUSE_USER = os.getenv("CLICKHOUSE_USER", "default")
CLICKHOUSE_PASSWORD = os.getenv("CLICKHOUSE_PASSWORD", "")
CLICKHOUSE_DATABASE = os.getenv("CLICKHOUSE_DATABASE", "default")
CLICKHOUSE_SECURE = os.getenv("CLICKHOUSE_SECURE", "true").lower() in ("true", "1", "yes")

_client = None


def get_clickhouse_client():
    """Lazily initialize and return ClickHouse client if configured."""
    global _client
    if _client is not None:
        return _client

    if not CLICKHOUSE_HOST:
        return None

    try:
        import clickhouse_connect
        _client = clickhouse_connect.get_client(
            host=CLICKHOUSE_HOST,
            port=CLICKHOUSE_PORT,
            username=CLICKHOUSE_USER,
            password=CLICKHOUSE_PASSWORD,
            database=CLICKHOUSE_DATABASE,
            secure=CLICKHOUSE_SECURE,
            connect_timeout=5,
            send_receive_timeout=10,
        )
        _init_clickhouse_schema(_client)
        logger.info("Connected to ClickHouse Cloud at %s", CLICKHOUSE_HOST)
        return _client
    except Exception as exc:
        logger.warning("Could not connect to ClickHouse Cloud (%s); using local mirror", exc)
        return None


def _init_clickhouse_schema(client):
    """Ensure telemetry tables exist in ClickHouse."""
    try:
        client.command("""
        CREATE TABLE IF NOT EXISTS video_pipeline_jobs (
            job_id String,
            title String,
            duration_sec Float64,
            voice_mode String,
            status String,
            total_render_time_ms UInt32,
            total_tokens_used UInt32,
            cost_usd Float64,
            qa_passed UInt8,
            created_at DateTime DEFAULT now()
        ) ENGINE = MergeTree()
        ORDER BY (job_id, created_at)
        """)

        client.command("""
        CREATE TABLE IF NOT EXISTS video_scene_telemetry (
            job_id String,
            scene_id String,
            treatment_type String,
            render_time_ms UInt32,
            vertex_latency_ms UInt32,
            evidence_claim_count UInt8,
            segment_hash String,
            created_at DateTime DEFAULT now()
        ) ENGINE = MergeTree()
        ORDER BY (job_id, scene_id, created_at)
        """)

        client.command("""
        CREATE TABLE IF NOT EXISTS video_qa_records (
            job_id String,
            check_name String,
            passed UInt8,
            detail String,
            created_at DateTime DEFAULT now()
        ) ENGINE = MergeTree()
        ORDER BY (job_id, check_name, created_at)
        """)

        client.command("""
        CREATE TABLE IF NOT EXISTS video_vertex_calls (
            job_id String,
            job_kind String,
            call_id String,
            stage String,
            model String,
            operation String,
            attempt UInt16,
            status String,
            billable UInt8,
            duration_ms Float64,
            input_tokens UInt32,
            output_tokens UInt32,
            total_tokens UInt32,
            input_characters UInt32,
            audio_output_bytes UInt64,
            created_at DateTime DEFAULT now()
        ) ENGINE = MergeTree()
        ORDER BY (job_id, call_id, created_at)
        """)
    except Exception as exc:
        logger.warning("Failed to initialize ClickHouse tables: %s", exc)


def _get_local_telemetry_dir(base_dir: Optional[Path] = None) -> Path:
    root = base_dir or Path("output/telemetry")
    root.mkdir(parents=True, exist_ok=True)
    return root


def record_job_telemetry(
    job_id: str,
    title: str,
    duration_sec: float,
    voice_mode: str,
    status: str,
    total_render_time_ms: int,
    total_tokens_used: int,
    cost_usd: float,
    qa_passed: bool,
    base_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Persist job-level telemetry to ClickHouse and local mirror."""
    now_iso = datetime.now(timezone.utc).isoformat()
    record = {
        "job_id": job_id,
        "title": title,
        "duration_sec": duration_sec,
        "voice_mode": voice_mode,
        "status": status,
        "total_render_time_ms": total_render_time_ms,
        "total_tokens_used": total_tokens_used,
        "cost_usd": cost_usd,
        "qa_passed": int(qa_passed),
        "created_at": now_iso,
    }

    # 1. Local JSON mirror
    local_dir = _get_local_telemetry_dir(base_dir)
    job_file = local_dir / f"job_{job_id}.json"
    job_file.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")

    # 2. Remote ClickHouse (if connected)
    client = get_clickhouse_client()
    if client:
        try:
            client.insert(
                "video_pipeline_jobs",
                [[
                    job_id, title, duration_sec, voice_mode, status,
                    total_render_time_ms, total_tokens_used, cost_usd, int(qa_passed)
                ]],
                column_names=[
                    "job_id", "title", "duration_sec", "voice_mode", "status",
                    "total_render_time_ms", "total_tokens_used", "cost_usd", "qa_passed"
                ]
            )
        except Exception as exc:
            logger.warning("ClickHouse job insert failed: %s", exc)

    return record


def record_scene_telemetry(
    job_id: str,
    scene_id: str,
    treatment_type: str,
    render_time_ms: int,
    vertex_latency_ms: int = 0,
    evidence_claim_count: int = 1,
    segment_hash: str = "",
    base_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Persist scene-level latency and treatment metrics."""
    record = {
        "job_id": job_id,
        "scene_id": scene_id,
        "treatment_type": treatment_type,
        "render_time_ms": render_time_ms,
        "vertex_latency_ms": vertex_latency_ms,
        "evidence_claim_count": evidence_claim_count,
        "segment_hash": segment_hash,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    local_dir = _get_local_telemetry_dir(base_dir)
    scenes_file = local_dir / f"scenes_{job_id}.jsonl"
    with scenes_file.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")

    client = get_clickhouse_client()
    if client:
        try:
            client.insert(
                "video_scene_telemetry",
                [[
                    job_id, scene_id, treatment_type, render_time_ms,
                    vertex_latency_ms, evidence_claim_count, segment_hash
                ]],
                column_names=[
                    "job_id", "scene_id", "treatment_type", "render_time_ms",
                    "vertex_latency_ms", "evidence_claim_count", "segment_hash"
                ]
            )
        except Exception as exc:
            logger.warning("ClickHouse scene insert failed: %s", exc)

    return record


def record_vertex_call_telemetry(
    job_id: str,
    job_kind: str,
    calls: List[Dict[str, Any]],
) -> int:
    """Forward sanitized per-call records to ClickHouse when configured.

    The detailed JSON file remains the local source of truth.  ClickHouse is
    an optional partner sink and is never required for a job to complete.
    """
    client = get_clickhouse_client()
    if not client or not calls:
        return 0
    rows = []
    for call in calls:
        usage = call.get("usage") or {}
        rows.append([
            job_id,
            job_kind,
            str(call.get("call_id") or ""),
            str(call.get("stage") or ""),
            str(call.get("model") or ""),
            str(call.get("operation") or ""),
            int(call.get("attempt") or 0),
            str(call.get("status") or ""),
            int(bool(call.get("billable"))),
            float(call.get("duration_ms") or 0.0),
            int(usage.get("input_tokens") or 0),
            int(usage.get("output_tokens") or 0),
            int(usage.get("total_tokens") or 0),
            int(call.get("input_characters") or 0),
            int(call.get("audio_output_bytes") or 0),
        ])
    try:
        client.insert(
            "video_vertex_calls",
            rows,
            column_names=[
                "job_id", "job_kind", "call_id", "stage", "model", "operation",
                "attempt", "status", "billable", "duration_ms", "input_tokens",
                "output_tokens", "total_tokens", "input_characters", "audio_output_bytes",
            ],
        )
        return len(rows)
    except Exception as exc:
        logger.warning("ClickHouse Vertex-call insert failed: %s", exc)
        return 0


def _read_job_local_telemetry(
    job_id: str,
    job_roots: Optional[tuple[Path, ...]],
) -> Dict[str, Any]:
    """Read the detailed job-local record when the caller supplies job roots."""
    for root in job_roots or ():
        path = Path(root) / job_id / "telemetry.json"
        try:
            if path.is_file():
                payload = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(payload, dict):
                    return payload
        except (OSError, json.JSONDecodeError):
            logger.warning("Could not read job-local telemetry at %s", path)
    return {}


def get_job_telemetry(
    job_id: str,
    base_dir: Optional[Path] = None,
    job_roots: Optional[tuple[Path, ...]] = None,
) -> Dict[str, Any]:
    """Retrieve detailed job-local telemetry plus legacy scenes."""
    local_dir = _get_local_telemetry_dir(base_dir)
    job_file = local_dir / f"job_{job_id}.json"
    scenes_file = local_dir / f"scenes_{job_id}.jsonl"

    job_data: Dict[str, Any] = _read_job_local_telemetry(job_id, job_roots)
    if not job_data and job_file.exists():
        job_data = json.loads(job_file.read_text(encoding="utf-8"))

    scenes: List[Dict[str, Any]] = []
    if scenes_file.exists():
        for line in scenes_file.read_text(encoding="utf-8").strip().split("\n"):
            if line:
                scenes.append(json.loads(line))

    return {
        "job": job_data,
        "scenes": scenes,
        "scene_count": len(scenes),
        "connected_to_cloud": bool(CLICKHOUSE_HOST and _client is not None),
    }


def get_all_telemetry_summary(
    base_dir: Optional[Path] = None,
    job_roots: Optional[tuple[Path, ...]] = None,
) -> Dict[str, Any]:
    """Get aggregated metrics across legacy and detailed job records."""
    local_dir = _get_local_telemetry_dir(base_dir)
    job_files = list(local_dir.glob("job_*.json"))

    jobs = []
    total_tokens = 0
    total_cost = 0.0
    total_render_ms = 0
    total_vertex_calls = 0

    for jf in job_files:
        try:
            d = json.loads(jf.read_text(encoding="utf-8"))
            jobs.append(d)
            total_tokens += d.get("total_tokens_used", 0)
            total_cost += d.get("cost_usd", 0.0)
            total_render_ms += d.get("total_render_time_ms", 0)
        except Exception:
            pass

    detailed_job_ids = {str(job.get("job_id")) for job in jobs if job.get("job_id")}
    for root in job_roots or ():
        for telemetry_file in Path(root).glob("*/telemetry.json"):
            try:
                detailed = json.loads(telemetry_file.read_text(encoding="utf-8"))
                job_id = str(detailed.get("job_id") or telemetry_file.parent.name)
                if job_id in detailed_job_ids:
                    continue
                detail_summary = detailed.get("summary") or {}
                tokens = detail_summary.get("total_tokens")
                cost = detail_summary.get("estimated_cost_usd")
                compact = {
                    "job_id": job_id,
                    "job_kind": detailed.get("job_kind"),
                    "status": detail_summary.get("job_status"),
                    "total_tokens_used": int(tokens or 0),
                    "cost_usd": float(cost or 0.0),
                    "cost_status": detail_summary.get("cost_status"),
                    "total_calls": detail_summary.get("total_calls", 0),
                }
                jobs.append(compact)
                detailed_job_ids.add(job_id)
                total_tokens += compact["total_tokens_used"]
                total_cost += compact["cost_usd"]
                total_vertex_calls += int(compact["total_calls"] or 0)
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                pass

    return {
        "total_jobs": len(jobs),
        "total_tokens_used": total_tokens,
        "total_cost_usd": round(total_cost, 4),
        "total_vertex_calls": total_vertex_calls,
        "avg_render_time_sec": round((total_render_ms / max(1, len(jobs))) / 1000, 1),
        "jobs": jobs,
        "clickhouse_status": "connected" if (CLICKHOUSE_HOST and _client) else "local_mirror_active",
    }
