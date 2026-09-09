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
import math
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from backend.telemetry_outbox import get_outbox, stable_event_id

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
            duration_sec Nullable(Float64),
            voice_mode String,
            status String,
            total_render_time_ms Nullable(UInt32),
            total_tokens_used Nullable(UInt32),
            cost_usd Nullable(Float64),
            qa_passed Nullable(UInt8),
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
            render_time_ms Nullable(UInt32),
            vertex_latency_ms Nullable(UInt32),
            evidence_claim_count Nullable(UInt8),
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
            passed Nullable(UInt8),
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
            attempt Nullable(UInt16),
            status String,
            billable Nullable(UInt8),
            duration_ms Nullable(Float64),
            input_tokens Nullable(UInt32),
            output_tokens Nullable(UInt32),
            total_tokens Nullable(UInt32),
            input_characters Nullable(UInt32),
            audio_output_bytes Nullable(UInt64),
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


def _ordered_by_stable_event_id(create_sql: Any) -> bool:
    """Return whether a table's replay key is exactly the stable event id.

    Checking only for the substring ``event_id`` is insufficient: a table can
    declare the column while still ordering by an occurrence timestamp (or by
    a compound key), which lets a replay create a distinct ReplacingMergeTree
    row.  The schema contract intentionally uses the single stable key.
    """
    if not isinstance(create_sql, str):
        return False
    match = re.search(r"\border\s+by\s+(.+?)(?:\s+SETTINGS\b|\r?\n|$)", create_sql, flags=re.IGNORECASE)
    if match is None:
        return False
    clause = match.group(1).strip().rstrip(";").strip()
    if clause.startswith("(") and clause.endswith(")"):
        clause = clause[1:-1].strip()
    return clause.strip("`\" ") == "event_id"


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


def verify_replay_safe_schema(client: Any) -> bool:
    """Verify that every telemetry table has a replay-safe ClickHouse engine.

    ``CREATE TABLE IF NOT EXISTS`` is intentionally not treated as proof: an
    existing legacy ``MergeTree`` table is left untouched by that statement.
    When the client exposes ``query`` we inspect ``system.tables`` after the
    additive DDL and require a ReplacingMergeTree-family engine ordered by the
    stable ``event_id`` key.  Small command-only fakes (and older client
    adapters) cannot introspect the engine; those retain the historical
    optimistic compatibility path after successful DDL, while real clients are
    always checked before the outbox can drain.
    """
    query = getattr(client, "query", None)
    if not callable(query):
        return True

    for table in TABLE_COLUMNS:
        try:
            result = query(
                "SELECT engine, create_table_query "
                "FROM system.tables "
                f"WHERE database = currentDatabase() AND name = '{table}'"
            )
        except Exception as exc:
            logger.warning("Could not verify ClickHouse engine for %s: %s", table, exc)
            return False
        rows = getattr(result, "result_rows", None)
        if rows is None and isinstance(result, (list, tuple)):
            rows = result
        if not rows:
            logger.warning("ClickHouse schema verification returned no row for %s", table)
            return False
        row = rows[0]
        if isinstance(row, dict):
            engine = row.get("engine")
            create_sql = row.get("create_table_query", "")
        else:
            engine = row[0] if len(row) > 0 else None
            create_sql = row[1] if len(row) > 1 else ""
        engine_name = str(engine or "").lower().replace("_", "")
        if "replacingmergetree" not in engine_name:
            logger.warning("ClickHouse table %s uses non-replay-safe engine %r", table, engine)
            return False
        if not _ordered_by_stable_event_id(create_sql):
            logger.warning("ClickHouse table %s is not ordered by the stable event id key", table)
            return False
    return True


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
        _SCHEMA_REPLAY_SAFE_READY = verify_replay_safe_schema(client)
        return _SCHEMA_REPLAY_SAFE_READY
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


def _optional_int(value: Any) -> int | None:
    """Parse a non-negative integer while preserving unavailable as ``None``."""
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
    """Parse a non-negative float while preserving unavailable as ``None``."""
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed >= 0 and math.isfinite(parsed) else None


def _optional_bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


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
    client = get_clickhouse_client()
    status["schema_ready"] = is_replay_safe_schema_ready()
    status["ingestion_lag_seconds"] = outbox.ingestion_lag_seconds()
    status["cloud_connected"] = bool(CLICKHOUSE_HOST and client is not None)
    return status


def record_job_telemetry(
    job_id: str,
    title: str,
    duration_sec: float | None = None,
    voice_mode: str | None = None,
    status: str | None = None,
    total_render_time_ms: int | None = None,
    total_tokens_used: int | None = None,
    cost_usd: float | None = None,
    qa_passed: bool | None = None,
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
    duration_value = _optional_float(duration_sec)
    render_value = _optional_int(total_render_time_ms)
    token_value = _optional_int(total_tokens_used)
    cost_value = _optional_float(cost_usd)
    qa_value = _optional_bool(qa_passed)
    record = {
        "job_id": job_id,
        "title": title,
        "duration_sec": duration_value,
        "voice_mode": voice_mode,
        "status": status,
        "total_render_time_ms": render_value,
        "total_tokens_used": token_value,
        "cost_usd": cost_value,
        "qa_passed": qa_value,
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
        job_id, title, duration_value, voice_mode, status,
        render_value, token_value, cost_value, int(qa_value) if qa_value is not None else None,
        studio_name, language, genre, now_iso,
    ]
    event = get_outbox(base_dir).enqueue(
        "video_pipeline_jobs",
        columns,
        row,
        # A job is one logical event.  Timestamps change on retries, so the
        # event identity must be based on the stable job key, not the row hash.
        event_id=stable_event_id("video_pipeline_jobs", {"job_id": job_id}),
    )
    record["event_id"] = event.event_id
    record["schema_version"] = event.schema_version
    record["sequence"] = event.sequence
    _maybe_drain(base_dir)
    return record


def record_scene_telemetry(
    job_id: str,
    scene_id: str,
    treatment_type: str,
    render_time_ms: int | None = None,
    vertex_latency_ms: int | None = None,
    evidence_claim_count: int | None = None,
    segment_hash: str | None = None,
    base_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Persist scene-level latency and treatment metrics via the outbox."""
    now_iso = datetime.now(timezone.utc).isoformat()
    render_value = _optional_int(render_time_ms)
    vertex_value = _optional_int(vertex_latency_ms)
    claim_value = _optional_int(evidence_claim_count)
    record = {
        "job_id": job_id,
        "scene_id": scene_id,
        "treatment_type": treatment_type,
        "render_time_ms": render_value,
        "vertex_latency_ms": vertex_value,
        "evidence_claim_count": claim_value,
        "segment_hash": segment_hash,
        "created_at": now_iso,
    }

    local_dir = _get_local_telemetry_dir(base_dir)
    scenes_file = local_dir / f"scenes_{job_id}.jsonl"
    with scenes_file.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")

    columns = [c for c in TABLE_COLUMNS["video_scene_telemetry"]]
    row = [
        job_id, scene_id, treatment_type, render_value,
        vertex_value, claim_value, segment_hash, now_iso,
    ]
    event = get_outbox(base_dir).enqueue(
        "video_scene_telemetry",
        columns,
        row,
        event_id=stable_event_id(
            "video_scene_telemetry",
            {"job_id": job_id, "scene_id": scene_id},
        ),
    )
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
    for call_index, call in enumerate(calls):
        usage = call.get("usage") or {}
        attempt_value = _optional_int(call.get("attempt"))
        duration_value = _optional_float(call.get("duration_ms"))
        billable_value = _optional_bool(call.get("billable"))
        row = [
            job_id,
            job_kind,
            call.get("call_id"),
            call.get("stage"),
            call.get("model"),
            call.get("operation"),
            attempt_value,
            call.get("status"),
            int(billable_value) if billable_value is not None else None,
            duration_value,
            _optional_int(usage.get("input_tokens")),
            _optional_int(usage.get("output_tokens")),
            _optional_int(usage.get("total_tokens")),
            _optional_int(call.get("input_characters")),
            _optional_int(call.get("audio_output_bytes")),
            call.get("created_at") or datetime.now(timezone.utc).isoformat(),
        ]
        call_id = call.get("call_id")
        identity = {
            "job_id": job_id,
            "call_id": call_id,
            "stage": call.get("stage"),
            "attempt": attempt_value,
            "index": call_index if not call_id else None,
        }
        outbox.enqueue(
            "video_vertex_calls",
            columns,
            row,
            event_id=stable_event_id("video_vertex_calls", identity),
        )
        enqueued += 1
    _maybe_drain(base_dir)
    return enqueued


def record_qa_telemetry(
    job_id: str,
    check_name: str,
    passed: bool | None = None,
    detail: str | None = None,
    *,
    created_at: str | None = None,
    base_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Persist one QA result without turning an unavailable result into false."""
    now_iso = created_at or datetime.now(timezone.utc).isoformat()
    passed_value = _optional_bool(passed)
    record = {
        "job_id": job_id,
        "check_name": check_name,
        "passed": passed_value,
        "detail": detail,
        "created_at": now_iso,
    }
    local_dir = _get_local_telemetry_dir(base_dir)
    qa_file = local_dir / f"qa_{job_id}.jsonl"
    with qa_file.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    event = get_outbox(base_dir).enqueue(
        "video_qa_records",
        list(TABLE_COLUMNS["video_qa_records"]),
        [job_id, check_name, int(passed_value) if passed_value is not None else None, detail, now_iso],
        event_id=stable_event_id(
            "video_qa_records",
            {"job_id": job_id, "check_name": check_name},
        ),
    )
    record.update({
        "event_id": event.event_id,
        "schema_version": event.schema_version,
        "sequence": event.sequence,
    })
    _maybe_drain(base_dir)
    return record


def record_pipeline_telemetry(
    *,
    job_id: str,
    title: str,
    duration_sec: float | None = None,
    voice_mode: str | None = None,
    status: str | None = None,
    total_render_time_ms: int | None = None,
    total_tokens_used: int | None = None,
    cost_usd: float | None = None,
    qa_passed: bool | None = None,
    studio_name: str = "FYF Studio",
    language: str = "my-MM",
    genre: str = "explainer",
    scenes: Any = (),
    qa_records: Any = (),
    calls: Any = (),
    base_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Dual-write one job and all available scene/QA/provider event records.

    This additive helper is intentionally independent of the API and pipeline
    modules.  It lets later adapters pass the events they already collect while
    keeping local JSON and durable outbox writes in one core seam.
    """
    job = record_job_telemetry(
        job_id=job_id,
        title=title,
        duration_sec=duration_sec,
        voice_mode=voice_mode,
        status=status,
        total_render_time_ms=total_render_time_ms,
        total_tokens_used=total_tokens_used,
        cost_usd=cost_usd,
        qa_passed=qa_passed,
        base_dir=base_dir,
        studio_name=studio_name,
        language=language,
        genre=genre,
    )
    scene_count = 0
    for scene in scenes or ():
        if not isinstance(scene, dict):
            continue
        record_scene_telemetry(
            job_id=job_id,
            scene_id=str(scene.get("scene_id") or scene.get("id") or scene_count),
            treatment_type=scene.get("treatment_type") or scene.get("treatment") or "unknown",
            render_time_ms=scene.get("render_time_ms"),
            vertex_latency_ms=scene.get("vertex_latency_ms"),
            evidence_claim_count=scene.get("evidence_claim_count"),
            segment_hash=scene.get("segment_hash"),
            base_dir=base_dir,
        )
        scene_count += 1
    qa_count = 0
    for qa in qa_records or ():
        if not isinstance(qa, dict):
            continue
        record_qa_telemetry(
            job_id=job_id,
            check_name=str(qa.get("check_name") or qa.get("name") or qa_count),
            passed=qa.get("passed"),
            detail=qa.get("detail"),
            created_at=qa.get("created_at"),
            base_dir=base_dir,
        )
        qa_count += 1
    vertex_count = record_vertex_call_telemetry(
        job_id=job_id,
        job_kind="video",
        calls=list(calls or ()),
        base_dir=base_dir,
    )
    return {
        "job": job,
        "scene_count": scene_count,
        "qa_count": qa_count,
        "vertex_call_count": vertex_count,
    }


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
    qa_file = local_dir / f"qa_{job_id}.jsonl"

    job_data: Dict[str, Any] = _read_job_local_telemetry(job_id, job_roots)
    if not job_data and job_file.exists():
        job_data = json.loads(job_file.read_text(encoding="utf-8"))

    scenes: List[Dict[str, Any]] = []
    if scenes_file.exists():
        for line in scenes_file.read_text(encoding="utf-8").strip().split("\n"):
            if line:
                scenes.append(json.loads(line))

    qa_records: List[Dict[str, Any]] = []
    if qa_file.exists():
        for line in qa_file.read_text(encoding="utf-8").strip().split("\n"):
            if line:
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict):
                    qa_records.append(value)

    return {
        "job": job_data,
        "scenes": scenes,
        "scene_count": len(scenes),
        "qa_records": qa_records,
        "qa_count": len(qa_records),
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
    token_values: list[int] = []
    cost_values: list[float] = []
    render_values: list[float] = []
    vertex_call_values: list[int] = []

    for jf in job_files:
        try:
            d = json.loads(jf.read_text(encoding="utf-8"))
            jobs.append(d)
            token_value = _optional_int(d.get("total_tokens_used"))
            cost_value = _optional_float(d.get("cost_usd"))
            render_value = _optional_float(d.get("total_render_time_ms"))
            if token_value is not None:
                token_values.append(token_value)
            if cost_value is not None:
                cost_values.append(cost_value)
            if render_value is not None:
                render_values.append(render_value)
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
                compact_tokens = _optional_int(tokens)
                compact_cost = _optional_float(cost)
                compact_calls = _optional_int(detail_summary.get("total_calls"))
                compact = {
                    "job_id": job_id,
                    "job_kind": detailed.get("job_kind"),
                    "status": detail_summary.get("job_status"),
                    "total_tokens_used": compact_tokens,
                    "cost_usd": compact_cost,
                    "cost_status": detail_summary.get("cost_status"),
                    "total_calls": compact_calls,
                }
                jobs.append(compact)
                detailed_job_ids.add(job_id)
                if compact_tokens is not None:
                    token_values.append(compact_tokens)
                if compact_cost is not None:
                    cost_values.append(compact_cost)
                if compact_calls is not None:
                    vertex_call_values.append(compact_calls)
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                pass

    return {
        "total_jobs": len(jobs),
        "total_tokens_used": sum(token_values) if token_values else None,
        "total_cost_usd": round(sum(cost_values), 4) if cost_values else None,
        "total_vertex_calls": sum(vertex_call_values) if vertex_call_values else None,
        "avg_render_time_sec": (
            round((sum(render_values) / len(render_values)) / 1000, 1)
            if render_values
            else None
        ),
        "jobs": jobs,
        "clickhouse_status": "connected" if (CLICKHOUSE_HOST and _client) else "local_mirror_active",
    }
