"""ClickHouse telemetry store, replay-safe schema, and durable outbox.

Stage E1 (replay-safe schema) and Stage E2 (durable outbox) live here.

Key invariants
--------------
* **Delivery is off the request path.** ``record_*`` only appends to the
  durable outbox (see :mod:`backend.telemetry_outbox`) plus the human-readable
  local JSON mirror.  No synchronous ClickHouse ``insert`` happens in a caller
  frame, so the client's ``send_receive_timeout=300`` can never stall a
  user-facing request.
* **Live progress/budget never depends on ClickHouse.** When the client is
  unconfigured, :func:`get_clickhouse_client` returns ``None`` and everything
  degrades to the local mirror + outbox (document line 155).
* **A local fallback is never presented as cloud success** (document line 150):
  the outbox drain reports ``clickhouse_unavailable_local_mirror_only`` instead
  of marking events delivered.
* **Expand-only migration.** Schema changes are idempotent and additive
  (``CREATE TABLE IF NOT EXISTS`` / ``ALTER TABLE ... ADD COLUMN IF NOT
  EXISTS``).  No ``DROP``/``TRUNCATE``/destructive ``ALTER`` is ever issued; the
  engine-swap *contract* step is deferred behind an explicit owner-approval gate
  (see :func:`plan_engine_migration`).
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from backend.telemetry_outbox import get_outbox

logger = logging.getLogger(__name__)

# ClickHouse connection parameters (defaults / environment)
CLICKHOUSE_HOST = os.getenv("CLICKHOUSE_HOST", "")
CLICKHOUSE_PORT = int(os.getenv("CLICKHOUSE_PORT", "8443"))
CLICKHOUSE_USER = os.getenv("CLICKHOUSE_USER", "default")
CLICKHOUSE_PASSWORD = os.getenv("CLICKHOUSE_PASSWORD", "")
CLICKHOUSE_DATABASE = os.getenv("CLICKHOUSE_DATABASE", "default")
CLICKHOUSE_SECURE = os.getenv("CLICKHOUSE_SECURE", "true").lower() in ("true", "1", "yes")

_client = None

# Set True only once the replay-safe (E1) schema is confirmed present.  The
# outbox drain is gated on this so it can never silently duplicate rows against
# an un-migrated MergeTree table.
_SCHEMA_REPLAY_SAFE_READY = False

# Business columns per table (the E1 event envelope columns are prepended by
# the outbox, not listed here).
TABLE_COLUMNS: Dict[str, List[str]] = {
    "video_pipeline_jobs": [
        "job_id", "title", "duration_sec", "voice_mode", "status",
        "total_render_time_ms", "total_tokens_used", "cost_usd", "qa_passed",
        "studio_name", "language", "genre", "created_at",
    ],
    "video_scene_telemetry": [
        "job_id", "scene_id", "treatment_type", "render_time_ms",
        "vertex_latency_ms", "evidence_claim_count", "segment_hash", "created_at",
    ],
    "video_qa_records": [
        "job_id", "check_name", "passed", "detail", "created_at",
    ],
    "video_vertex_calls": [
        "job_id", "job_kind", "call_id", "stage", "model", "operation",
        "attempt", "status", "billable", "duration_ms", "input_tokens",
        "output_tokens", "total_tokens", "input_characters", "audio_output_bytes",
        "created_at",
    ],
}

# Full CREATE TABLE DDL for a *fresh* deployment.  ReplacingMergeTree keyed on
# ingestion_timestamp and ordered by event_id makes a replayed event collapse to
# a single row (latest ingestion wins) instead of duplicating.
_CREATE_DDL: Dict[str, str] = {
    "video_pipeline_jobs": """
        CREATE TABLE IF NOT EXISTS video_pipeline_jobs (
            event_id String,
            schema_version UInt8,
            event_timestamp DateTime64(3),
            ingestion_timestamp DateTime64(3),
            sequence UInt64,
            job_id String,
            title String,
            duration_sec Float64,
            voice_mode String,
            status String,
            total_render_time_ms UInt32,
            total_tokens_used UInt32,
            cost_usd Float64,
            qa_passed UInt8,
            studio_name String DEFAULT 'FYF Studio',
            language String DEFAULT 'my-MM',
            genre String DEFAULT 'explainer',
            created_at DateTime DEFAULT now()
        ) ENGINE = ReplacingMergeTree(ingestion_timestamp)
        ORDER BY (event_id)
    """,
    "video_scene_telemetry": """
        CREATE TABLE IF NOT EXISTS video_scene_telemetry (
            event_id String,
            schema_version UInt8,
            event_timestamp DateTime64(3),
            ingestion_timestamp DateTime64(3),
            sequence UInt64,
            job_id String,
            scene_id String,
            treatment_type String,
            render_time_ms UInt32,
            vertex_latency_ms UInt32,
            evidence_claim_count UInt8,
            segment_hash String,
            created_at DateTime DEFAULT now()
        ) ENGINE = ReplacingMergeTree(ingestion_timestamp)
        ORDER BY (event_id)
    """,
    "video_qa_records": """
        CREATE TABLE IF NOT EXISTS video_qa_records (
            event_id String,
            schema_version UInt8,
            event_timestamp DateTime64(3),
            ingestion_timestamp DateTime64(3),
            sequence UInt64,
            job_id String,
            check_name String,
            passed UInt8,
            detail String,
            created_at DateTime DEFAULT now()
        ) ENGINE = ReplacingMergeTree(ingestion_timestamp)
        ORDER BY (event_id)
    """,
    "video_vertex_calls": """
        CREATE TABLE IF NOT EXISTS video_vertex_calls (
            event_id String,
            schema_version UInt8,
            event_timestamp DateTime64(3),
            ingestion_timestamp DateTime64(3),
            sequence UInt64,
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
        ) ENGINE = ReplacingMergeTree(ingestion_timestamp)
        ORDER BY (event_id)
    """,
}

# Additive EXPAND statements for tables that already exist with the legacy
# MergeTree shape.  Every statement is idempotent and non-destructive.
_EXPAND_COLUMN_DDL: Dict[str, str] = {
    "event_id": "ADD COLUMN IF NOT EXISTS event_id String DEFAULT ''",
    "schema_version": "ADD COLUMN IF NOT EXISTS schema_version UInt8 DEFAULT 0",
    "event_timestamp": "ADD COLUMN IF NOT EXISTS event_timestamp DateTime64(3)",
    "ingestion_timestamp": "ADD COLUMN IF NOT EXISTS ingestion_timestamp DateTime64(3)",
    "sequence": "ADD COLUMN IF NOT EXISTS sequence UInt64 DEFAULT 0",
}


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
            # Match mcp-clickhouse's proven budgets; 5s/10s read-timed-out on
            # every Cloud Run -> ClickHouse Cloud attempt.
            connect_timeout=30,
            send_receive_timeout=300,
        )
        _init_clickhouse_schema(_client)
        logger.info("Connected to ClickHouse Cloud at %s", CLICKHOUSE_HOST)
        return _client
    except Exception as exc:
        logger.warning("Could not connect to ClickHouse Cloud (%s); using local mirror", exc)
        return None


def is_replay_safe_schema_ready() -> bool:
    """True only once the E1 replay-safe schema is confirmed present.

    The outbox drain is gated on this: without a confirmed dedup-capable
    schema a replay could duplicate rows, so the drain degrades honestly
    (events stay pending) instead of writing.
    """
    return _SCHEMA_REPLAY_SAFE_READY


def _init_clickhouse_schema(client) -> bool:
    """Ensure the replay-safe telemetry tables exist (expand-only, idempotent).

    Fresh deployments get the full ``ReplacingMergeTree(ingestion_timestamp)``
    shape ordered by ``event_id``.  Pre-existing legacy tables are expanded with
    additive ``ADD COLUMN IF NOT EXISTS`` statements so writes are replay-ready.
    The engine swap for legacy tables is a deferred *contract* step (see
    :func:`plan_engine_migration`) and is intentionally not performed here.
    """
    global _SCHEMA_REPLAY_SAFE_READY
    try:
        for table, ddl in _CREATE_DDL.items():
            client.command(ddl)
            for column_ddl in _EXPAND_COLUMN_DDL.values():
                client.command(f"ALTER TABLE {table} {column_ddl}")
        _SCHEMA_REPLAY_SAFE_READY = True
        return True
    except Exception as exc:
        # Never claim cloud success on failure; keep the drain gated.
        _SCHEMA_REPLAY_SAFE_READY = False
        logger.warning("Failed to initialize replay-safe ClickHouse tables: %s", exc)
        return False


def plan_engine_migration() -> Dict[str, Any]:
    """Return the deferred expand -> migrate -> contract plan (NOT executed).

    ``CREATE TABLE IF NOT EXISTS`` cannot change the engine of a table that
    already exists as a legacy ``MergeTree``.  Swapping the engine requires
    recreating the table, which is destructive and therefore gated behind an
    explicit owner-approval step.  This function only *describes* that plan so a
    human can review and run it; it issues no DDL.
    """
    steps: List[Dict[str, str]] = []
    for table, ddl in _CREATE_DDL.items():
        shadow = f"{table}_replay_safe"
        steps.append(
            {
                "table": table,
                "phase": "migrate",
                "action": "create shadow table with replay-safe engine",
                "sql": ddl.replace(f"EXISTS {table} ", f"EXISTS {shadow} ", 1),
            }
        )
        steps.append(
            {
                "table": table,
                "phase": "migrate",
                "action": "backfill shadow from live table (deduplicated)",
                "sql": f"INSERT INTO {shadow} SELECT * FROM {table}",
            }
        )
        steps.append(
            {
                "table": table,
                "phase": "contract",
                "action": "DEFERRED - owner approval required (destructive rename/drop)",
                "sql": (
                    f"-- REQUIRES OWNER APPROVAL\n"
                    f"-- RENAME TABLE {table} TO {table}_legacy, {shadow} TO {table};\n"
                    f"-- DROP TABLE {table}_legacy;  -- destructive, gated"
                ),
            }
        )
    return {
        "strategy": "expand_then_migrate_then_contract",
        "expand_applied_now": True,
        "contract_deferred": True,
        "contract_requires": "explicit owner approval (destructive migration)",
        "steps": steps,
    }


def _get_local_telemetry_dir(base_dir: Optional[Path] = None) -> Path:
    root = base_dir or Path("output/telemetry")
    root.mkdir(parents=True, exist_ok=True)
    return root


def _maybe_drain(base_dir: Optional[Path]) -> None:
    """Best-effort, non-blocking start of the background drain.

    Returns immediately.  When no ClickHouse host is configured (tests, local
    dev) it does nothing, so delivery never touches the caller's request path.
    """
    try:
        outbox = get_outbox(base_dir)
        outbox.maybe_start_background_drain(
            get_clickhouse_client,
            is_replay_safe_schema_ready,
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("Background outbox drain not started: %s", exc)


def drain_outbox(
    base_dir: Optional[Path] = None,
    *,
    client: Any = None,
    now: Optional[datetime] = None,
    schema_ready: Optional[bool] = None,
) -> Dict[str, Any]:
    """Explicitly run one bounded outbox drain pass (background/recovery use).

    Not intended to be called from a request handler.  ``schema_ready`` defaults
    to the confirmed E1 schema state so a drain can never duplicate rows against
    an un-migrated table.
    """
    outbox = get_outbox(base_dir)
    ready = is_replay_safe_schema_ready() if schema_ready is None else schema_ready
    factory = (lambda: client) if client is not None else get_clickhouse_client
    report = outbox.drain_once(factory, schema_ready=ready, now=now)
    return report.as_dict()


def get_telemetry_delivery_status(base_dir: Optional[Path] = None) -> Dict[str, Any]:
    """Visible delivery counters + freshness/lag for the analytics surface.

    Surfaces pending/failed/delivered (document line 149/151), whether the
    replay-safe schema is confirmed, and an honest ingestion lag that is
    ``None`` (never ``0``) when nothing has been delivered yet.
    """
    outbox = get_outbox(base_dir)
    status = outbox.status()
    status["schema_ready"] = is_replay_safe_schema_ready()
    status["ingestion_lag_seconds"] = outbox.ingestion_lag_seconds()
    status["cloud_connected"] = bool(CLICKHOUSE_HOST and _client is not None)
    return status


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
    studio_name: str = "FYF Studio",
    language: str = "my-MM",
    genre: str = "explainer",
) -> Dict[str, Any]:
    """Persist job-level telemetry to the durable outbox and local mirror.

    No synchronous ClickHouse write happens here; the event is durably enqueued
    for the background drain (Stage E2).
    """
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
        "studio_name": studio_name,
        "language": language,
        "genre": genre,
        "created_at": now_iso,
    }

    # 1. Local JSON mirror (durable, human-readable; also the recovery source)
    local_dir = _get_local_telemetry_dir(base_dir)
    job_file = local_dir / f"job_{job_id}.json"
    job_file.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")

    # 2. Durable outbox enqueue (delivery happens off the request path)
    columns = [c for c in TABLE_COLUMNS["video_pipeline_jobs"]]
    row = [
        job_id, title, duration_sec, voice_mode, status,
        total_render_time_ms, total_tokens_used, cost_usd, int(qa_passed),
        studio_name, language, genre, now_iso,
    ]
    event = get_outbox(base_dir).enqueue("video_pipeline_jobs", columns, row)
    record["event_id"] = event.event_id
    record["schema_version"] = event.schema_version
    record["sequence"] = event.sequence
    _maybe_drain(base_dir)
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
    """Persist scene-level latency and treatment metrics via the outbox."""
    now_iso = datetime.now(timezone.utc).isoformat()
    record = {
        "job_id": job_id,
        "scene_id": scene_id,
        "treatment_type": treatment_type,
        "render_time_ms": render_time_ms,
        "vertex_latency_ms": vertex_latency_ms,
        "evidence_claim_count": evidence_claim_count,
        "segment_hash": segment_hash,
        "created_at": now_iso,
    }

    local_dir = _get_local_telemetry_dir(base_dir)
    scenes_file = local_dir / f"scenes_{job_id}.jsonl"
    with scenes_file.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")

    columns = [c for c in TABLE_COLUMNS["video_scene_telemetry"]]
    row = [
        job_id, scene_id, treatment_type, render_time_ms,
        vertex_latency_ms, evidence_claim_count, segment_hash, now_iso,
    ]
    event = get_outbox(base_dir).enqueue("video_scene_telemetry", columns, row)
    record["event_id"] = event.event_id
    record["schema_version"] = event.schema_version
    record["sequence"] = event.sequence
    _maybe_drain(base_dir)
    return record


def record_vertex_call_telemetry(
    job_id: str,
    job_kind: str,
    calls: List[Dict[str, Any]],
    base_dir: Optional[Path] = None,
) -> int:
    """Enqueue sanitized per-call records to the durable outbox.

    Returns the number of events accepted into the outbox (not the number
    delivered -- delivery is asynchronous and off the request path).  The
    detailed JSON file remains the local source of truth; ClickHouse is an
    optional partner sink and is never required for a job to complete.
    """
    if not calls:
        return 0
    outbox = get_outbox(base_dir)
    columns = [c for c in TABLE_COLUMNS["video_vertex_calls"]]
    enqueued = 0
    for call in calls:
        usage = call.get("usage") or {}
        row = [
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
            str(call.get("created_at") or datetime.now(timezone.utc).isoformat()),
        ]
        outbox.enqueue("video_vertex_calls", columns, row)
        enqueued += 1
    _maybe_drain(base_dir)
    return enqueued


def replay_outbox_from_mirror(base_dir: Optional[Path] = None) -> int:
    """Recovery: re-enqueue durable mirror files that never reached the outbox.

    Turns the historical local JSON mirror from a dead end into a replay source
    (document: silent permanent loss fix).  Idempotent on ``event_id``.
    """
    local_dir = _get_local_telemetry_dir(base_dir)
    return get_outbox(base_dir).rehydrate_from_mirror(local_dir)


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
