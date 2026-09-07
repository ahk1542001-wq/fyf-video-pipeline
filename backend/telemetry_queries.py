"""Server-owned, read-only telemetry query contracts.

The public endpoint accepts a query identifier, never SQL.  Analytics are
expressed once in a **dialect-neutral** subset (``QUERY_SQL``) that is valid in
both ClickHouse and the hermetic SQLite mirror, so a local fallback has the same
column contract without inventing metrics that were not recorded.

Replay-safety / parity strategy (Stage E1 + E3)
----------------------------------------------
The E1 tables are ``ReplacingMergeTree(ingestion_timestamp)`` ordered by
``event_id``.  ``FINAL`` / ``argMax`` are ClickHouse-only and would break the
SQLite mirror, which must run the *same* projection.  We therefore use a thin
per-backend variant layer (option (c)):

* ``QUERY_SQL``           -- dialect-neutral SQL (unchanged projections).
* ``CLICKHOUSE_QUERY_SQL``-- generated from ``QUERY_SQL`` by inserting ``FINAL``
  after each telemetry ``FROM <table>``.  ``FINAL`` is the canonical,
  index-efficient way to read a ``ReplacingMergeTree`` deduplicated (latest
  ``ingestion_timestamp`` wins), so a replayed ``event_id`` collapses to one row.

Because the ClickHouse variant differs from the neutral SQL by *only* the
``FINAL`` keyword, projection parity is guaranteed **by construction** and is
asserted by tests (stripping ``FINAL`` must reproduce the neutral SQL and the
same rows on the mirror).  The SQLite mirror is duplicate-free by construction
(one row per job file), so it needs no dedup clause; ``FINAL`` is the remote
replay defence and write-time ``event_id`` idempotency in the outbox is the
first line of defence.

All existing guarantees are preserved unchanged: caller-SQL rejection,
``MAX_RESULT_ROWS = 200``, ``MAX_EXECUTION_SECONDS = 10`` and the honest
``source`` / ``availability`` reporting.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable

from backend.telemetry_outbox import SCHEMA_VERSION, stable_event_id

logger = logging.getLogger(__name__)

MAX_RESULT_ROWS = 200
MAX_EXECUTION_SECONDS = 10
CLICKHOUSE_SETTINGS = {
    "max_result_rows": MAX_RESULT_ROWS,
    "max_execution_time": MAX_EXECUTION_SECONDS,
    "result_overflow_mode": "break",
}

# ---------------------------------------------------------------------------
# Six analytics capabilities (document line 147), dialect-neutral SQL.
#   1. creation timeline        -> creation_timeline
#   2. cost intelligence        -> cost_summary
#   3. quality tracking         -> quality_tracking
#   4. editing friction         -> editing_friction
#   5. version comparison       -> version_comparison
#   6. grounded recommendations -> grounded_recommendations
# The original four ids (jobs_overview, model_calls, scene_latency,
# cost_summary) are preserved byte-for-byte so they keep working unchanged.
# ---------------------------------------------------------------------------
QUERY_SQL: dict[str, str] = {
    "jobs_overview": """
        SELECT job_id, title, duration_sec, voice_mode, status,
               total_render_time_ms, total_tokens_used, cost_usd, qa_passed,
               studio_name, language, genre, created_at
        FROM video_pipeline_jobs
        ORDER BY created_at DESC
        LIMIT 200
    """,
    "model_calls": """
        SELECT job_id, job_kind, call_id, stage, model, operation, attempt,
               status, billable, duration_ms, input_tokens, output_tokens,
               total_tokens, input_characters, audio_output_bytes, created_at
        FROM video_vertex_calls
        ORDER BY created_at DESC
        LIMIT 200
    """,
    "scene_latency": """
        SELECT job_id, scene_id, treatment_type, render_time_ms,
               vertex_latency_ms, evidence_claim_count, segment_hash, created_at
        FROM video_scene_telemetry
        ORDER BY created_at DESC
        LIMIT 200
    """,
    "cost_summary": """
        SELECT status, COUNT(*) AS job_count,
               SUM(total_tokens_used) AS total_tokens_used,
               SUM(cost_usd) AS total_cost_usd
        FROM video_pipeline_jobs
        GROUP BY status
        ORDER BY total_cost_usd DESC
        LIMIT 200
    """,
    # 1. Creation timeline: chronological order jobs were created.
    "creation_timeline": """
        SELECT job_id, title, status, created_at,
               total_render_time_ms, total_tokens_used, cost_usd
        FROM video_pipeline_jobs
        ORDER BY created_at ASC
        LIMIT 200
    """,
    # 3. Quality tracking: QA pass rate and outcomes grouped by job status.
    "quality_tracking": """
        SELECT status,
               COUNT(*) AS job_count,
               SUM(CASE WHEN COALESCE(qa_passed, 0) = 1 THEN 1 ELSE 0 END) AS qa_passed_count,
               SUM(CASE WHEN COALESCE(qa_passed, 0) = 0 THEN 1 ELSE 0 END) AS qa_failed_count
        FROM video_pipeline_jobs
        GROUP BY status
        ORDER BY job_count DESC
        LIMIT 200
    """,
    # 4. Editing friction: retries, failed calls and rework per stage.
    "editing_friction": """
        SELECT job_id, stage,
               COUNT(*) AS call_count,
               SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed_calls,
               MAX(attempt) AS max_attempt,
               SUM(duration_ms) AS total_duration_ms
        FROM video_vertex_calls
        GROUP BY job_id, stage
        ORDER BY failed_calls DESC, max_attempt DESC
        LIMIT 200
    """,
    # 5. Version comparison: metrics grouped by telemetry schema_version.
    "version_comparison": """
        SELECT schema_version,
               COUNT(*) AS event_count,
               SUM(total_tokens_used) AS total_tokens_used,
               SUM(cost_usd) AS total_cost_usd,
               AVG(duration_sec) AS avg_duration_sec
        FROM video_pipeline_jobs
        GROUP BY schema_version
        ORDER BY schema_version DESC
        LIMIT 200
    """,
    # 6. Grounded recommendations: factual attention flags only.  This returns
    #    recorded facts (never fabricated metrics) and is read-only; it must
    #    never auto-change permissions, budget or preferences (document line 157).
    "grounded_recommendations": """
        SELECT job_id, status, qa_passed, cost_usd,
               total_tokens_used, total_render_time_ms, created_at
        FROM video_pipeline_jobs
        WHERE COALESCE(qa_passed, 0) = 0 OR COALESCE(status, '') <> 'completed'
        ORDER BY created_at DESC
        LIMIT 200
    """,
}

# Tables whose ClickHouse engine is ReplacingMergeTree; reads append FINAL.
_FINALIZABLE_TABLES = (
    "video_pipeline_jobs",
    "video_scene_telemetry",
    "video_qa_records",
    "video_vertex_calls",
)
_FINAL_PATTERN = re.compile(
    r"\bFROM\s+(" + "|".join(_FINALIZABLE_TABLES) + r")\b",
    re.IGNORECASE,
)


def _to_clickhouse_variant(sql: str) -> str:
    """Insert ``FINAL`` after each telemetry ``FROM <table>`` (dedup read)."""
    return _FINAL_PATTERN.sub(lambda match: f"{match.group(0)} FINAL", sql)


# Per-backend variant layer: identical projection, ClickHouse adds FINAL.
CLICKHOUSE_QUERY_SQL: dict[str, str] = {
    query_id: _to_clickhouse_variant(sql) for query_id, sql in QUERY_SQL.items()
}


def query_sql(query_id: str) -> str:
    """Return the fixed dialect-neutral SQL for a supported identifier."""
    if query_id not in QUERY_SQL:
        raise KeyError(query_id)
    return QUERY_SQL[query_id]


def resolve_query_sql(query_id: str, dialect: str = "sqlite") -> str:
    """Return the SQL for ``query_id`` in the requested dialect.

    ``dialect="clickhouse"`` yields the ``FINAL``-deduplicated variant; any
    other dialect (the SQLite mirror) yields the neutral SQL.  Projections are
    identical, so the column contract and row semantics match across backends.
    """
    if query_id not in QUERY_SQL:
        raise KeyError(query_id)
    if dialect == "clickhouse":
        return CLICKHOUSE_QUERY_SQL.get(query_id, QUERY_SQL[query_id])
    return QUERY_SQL[query_id]


def validate_readonly_sql(sql: str) -> str:
    """Validate that SQL is strictly a single read-only SELECT or WITH statement."""
    if not isinstance(sql, str):
        raise ValueError("SQL query must be a string")

    # Strip comments
    cleaned = re.sub(r"--.*?\n", "\n", sql)
    cleaned = re.sub(r"/\*.*?\*/", "", cleaned, flags=re.DOTALL)
    cleaned = cleaned.strip()
    if not cleaned:
        raise ValueError("Query cannot be empty")

    # Strip string literals so keywords inside quotes don't trigger false positives
    without_strings = re.sub(r"'(''|[^'])*'", "''", cleaned)
    without_strings = re.sub(r'"(""|[^"])*"', '""', without_strings)

    # Check for multiple statements (semicolon separating non-empty statements)
    statements = [s.strip() for s in without_strings.split(";") if s.strip()]
    if len(statements) != 1:
        raise ValueError("Multiple SQL statements are not permitted")

    single_stmt = statements[0]
    first_word = single_stmt.split()[0].upper() if single_stmt.split() else ""
    if first_word not in ("SELECT", "WITH"):
        raise ValueError("Only read-only SELECT or WITH statements are allowed")

    # Check for forbidden mutation keywords (whole words)
    forbidden = r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE|CREATE|REPLACE|GRANT|REVOKE|EXEC|EXECUTE)\b"
    if re.search(forbidden, without_strings, flags=re.IGNORECASE):
        raise ValueError("Data modification statements are not permitted")

    return sql


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        if not path.is_file() or path.is_symlink():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _number(value: Any, *, integer: bool = False) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value) if integer else float(value)


def _job_dirs(root: Path) -> list[Path]:
    if not root.is_dir() or root.is_symlink():
        return []
    try:
        entries = root.iterdir()
    except OSError:
        return []
    return sorted(
        (
            entry
            for entry in entries
            if entry.is_dir() and not entry.is_symlink() and len(entry.name) == 8
        ),
        key=lambda entry: entry.name,
    )


def _summary_values(
    telemetry: dict[str, Any] | None,
) -> tuple[int | None, float | None]:
    if not telemetry:
        return None, None
    summary = telemetry.get("summary")
    if not isinstance(summary, dict):
        summary = {}
    total_tokens = _number(summary.get("total_tokens"), integer=True)
    cost = _number(summary.get("estimated_cost_usd"))
    if total_tokens is None:
        calls = telemetry.get("calls")
        if isinstance(calls, list):
            observed = 0
            has_observed = False
            for call in calls:
                if not isinstance(call, dict):
                    continue
                usage = call.get("usage")
                if not isinstance(usage, dict):
                    continue
                value = _number(usage.get("total_tokens"), integer=True)
                if value is not None:
                    observed += value
                    has_observed = True
            total_tokens = observed if has_observed else None
    return total_tokens, cost


# Business columns mirrored per table (event envelope columns are appended by
# the mirror builder so the SQLite schema matches the ClickHouse column set).
_JOB_COLUMNS = [
    "job_id", "title", "duration_sec", "voice_mode", "status",
    "total_render_time_ms", "total_tokens_used", "cost_usd", "qa_passed",
    "studio_name", "language", "genre", "created_at",
]
_CALL_COLUMNS = [
    "job_id", "job_kind", "call_id", "stage", "model", "operation",
    "attempt", "status", "billable", "duration_ms", "input_tokens",
    "output_tokens", "total_tokens", "input_characters", "audio_output_bytes",
    "created_at",
]
_SCENE_COLUMNS = [
    "job_id", "scene_id", "treatment_type", "render_time_ms",
    "vertex_latency_ms", "evidence_claim_count", "segment_hash", "created_at",
]
_EVENT_COLUMNS = ["event_id", "schema_version", "event_timestamp", "ingestion_timestamp", "sequence"]


def _job_row(job_dir: Path) -> tuple[Any, ...] | None:
    status = _read_json(job_dir / "status.json")
    script = _read_json(job_dir / "script.json") or {}
    if status is None:
        return None
    telemetry = _read_json(job_dir / "telemetry.json")
    total_tokens, cost = _summary_values(telemetry)
    qa = status.get("qa_report")
    qa_passed = int(qa["passed"]) if isinstance(qa, dict) and isinstance(qa.get("passed"), bool) else None
    metrics = qa.get("metrics") if isinstance(qa, dict) else None
    metrics = metrics if isinstance(metrics, dict) else {}
    duration_sec = _number(metrics.get("video_duration"))
    timings = status.get("stage_timings")
    render_seconds = timings.get("render") if isinstance(timings, dict) else None
    render_seconds = _number(render_seconds)
    render_ms = int(round(render_seconds * 1000)) if render_seconds is not None else None
    created_at = status.get("created_at") or (telemetry or {}).get("started_at")
    return (
        job_dir.name,
        script.get("title") if isinstance(script.get("title"), str) else None,
        duration_sec,
        status.get("voice_provider"),
        status.get("status"),
        render_ms,
        total_tokens,
        cost,
        qa_passed,
        script.get("studio_name"),
        script.get("language"),
        script.get("genre"),
        created_at,
    )


def _call_rows(root: Path) -> list[tuple[Any, ...]]:
    rows: list[tuple[Any, ...]] = []
    for job_dir in _job_dirs(root):
        telemetry = _read_json(job_dir / "telemetry.json")
        if not telemetry or not isinstance(telemetry.get("calls"), list):
            continue
        for call in telemetry["calls"]:
            if not isinstance(call, dict):
                continue
            usage = call.get("usage") if isinstance(call.get("usage"), dict) else {}
            rows.append(
                (
                    job_dir.name,
                    telemetry.get("job_kind"),
                    call.get("call_id"),
                    call.get("stage"),
                    call.get("model"),
                    call.get("operation"),
                    _number(call.get("attempt"), integer=True),
                    call.get("status"),
                    int(call["billable"]) if isinstance(call.get("billable"), bool) else None,
                    _number(call.get("duration_ms")),
                    _number(usage.get("input_tokens"), integer=True),
                    _number(usage.get("output_tokens"), integer=True),
                    _number(usage.get("total_tokens"), integer=True),
                    _number(call.get("input_characters"), integer=True),
                    _number(call.get("audio_output_bytes"), integer=True),
                    telemetry.get("started_at"),
                )
            )
    return rows


def _scene_rows(repo_root: Path) -> list[tuple[Any, ...]]:
    telemetry_dir = repo_root / "output" / "telemetry"
    if not telemetry_dir.is_dir() or telemetry_dir.is_symlink():
        return []
    rows: list[tuple[Any, ...]] = []
    for scenes_file in sorted(telemetry_dir.glob("scenes_*.jsonl")):
        if scenes_file.is_symlink() or not scenes_file.is_file():
            continue
        try:
            lines = scenes_file.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict):
                continue
            rows.append(
                (
                    row.get("job_id"),
                    row.get("scene_id"),
                    row.get("treatment_type"),
                    _number(row.get("render_time_ms"), integer=True),
                    _number(row.get("vertex_latency_ms"), integer=True),
                    _number(row.get("evidence_claim_count"), integer=True),
                    row.get("segment_hash"),
                    row.get("created_at"),
                )
            )
    return rows


def _event_envelope(table: str, columns: list[str], row: tuple[Any, ...], index: int) -> tuple[Any, ...]:
    """Build the (event_id, schema_version, event_ts, ingestion_ts, sequence) tail.

    ``event_id`` is a deterministic hash of the business payload plus a mirror
    index so distinct recorded rows never accidentally collapse, while an exact
    replayed row (same content, same index) hashes identically.  The mirror is
    duplicate-free by construction; this exists for column-contract parity with
    the ClickHouse tables and for reconciliation/integrity detection.
    """
    payload = dict(zip(columns, row))
    payload["_mirror_index"] = index
    event_id = stable_event_id(table, payload)
    created_at = row[-1] if row else None
    timestamp = created_at if isinstance(created_at, str) else None
    return (event_id, SCHEMA_VERSION, timestamp, timestamp, index)


def _build_local_database(
    jobs_root: Path,
    script_jobs_root: Path,
    repo_root: Path,
) -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    cursor = connection.cursor()
    # Business columns keep their original SQLite affinity; the E1 event
    # envelope columns are appended so the mirror schema matches ClickHouse.
    cursor.execute(
        """CREATE TABLE video_pipeline_jobs (
            job_id TEXT, title TEXT, duration_sec REAL, voice_mode TEXT, status TEXT,
            total_render_time_ms INTEGER, total_tokens_used INTEGER, cost_usd REAL,
            qa_passed INTEGER, studio_name TEXT, language TEXT, genre TEXT, created_at TEXT,
            event_id TEXT, schema_version INTEGER, event_timestamp TEXT,
            ingestion_timestamp TEXT, sequence INTEGER
        )"""
    )
    cursor.execute(
        """CREATE TABLE video_vertex_calls (
            job_id TEXT, job_kind TEXT, call_id TEXT, stage TEXT, model TEXT,
            operation TEXT, attempt INTEGER, status TEXT, billable INTEGER,
            duration_ms REAL, input_tokens INTEGER, output_tokens INTEGER,
            total_tokens INTEGER, input_characters INTEGER, audio_output_bytes INTEGER,
            created_at TEXT,
            event_id TEXT, schema_version INTEGER, event_timestamp TEXT,
            ingestion_timestamp TEXT, sequence INTEGER
        )"""
    )
    cursor.execute(
        """CREATE TABLE video_scene_telemetry (
            job_id TEXT, scene_id TEXT, treatment_type TEXT, render_time_ms INTEGER,
            vertex_latency_ms INTEGER, evidence_claim_count INTEGER, segment_hash TEXT,
            created_at TEXT,
            event_id TEXT, schema_version INTEGER, event_timestamp TEXT,
            ingestion_timestamp TEXT, sequence INTEGER
        )"""
    )
    job_table_columns = _JOB_COLUMNS + _EVENT_COLUMNS
    call_table_columns = _CALL_COLUMNS + _EVENT_COLUMNS
    scene_table_columns = _SCENE_COLUMNS + _EVENT_COLUMNS

    job_placeholders = ", ".join("?" * len(job_table_columns))
    for index, job_dir in enumerate(_job_dirs(jobs_root)):
        row = _job_row(job_dir)
        if row is not None:
            envelope = _event_envelope("video_pipeline_jobs", _JOB_COLUMNS, row, index)
            cursor.execute(
                f"INSERT INTO video_pipeline_jobs VALUES ({job_placeholders})",
                tuple(row) + envelope,
            )

    call_placeholders = ", ".join("?" * len(call_table_columns))
    call_index = 0
    for root in (jobs_root, script_jobs_root):
        for row in _call_rows(root):
            envelope = _event_envelope("video_vertex_calls", _CALL_COLUMNS, row, call_index)
            call_index += 1
            cursor.execute(
                f"INSERT INTO video_vertex_calls VALUES ({call_placeholders})",
                tuple(row) + envelope,
            )

    scene_placeholders = ", ".join("?" * len(scene_table_columns))
    for index, row in enumerate(_scene_rows(repo_root)):
        envelope = _event_envelope("video_scene_telemetry", _SCENE_COLUMNS, row, index)
        cursor.execute(
            f"INSERT INTO video_scene_telemetry VALUES ({scene_placeholders})",
            tuple(row) + envelope,
        )
    connection.commit()
    return connection


def _delivery_context() -> dict[str, Any]:
    """Best-effort outbox delivery counters + honest ingestion lag.

    Never raises and never fabricates: when no outbox exists the lag is
    ``None`` (unavailable), not ``0``.
    """
    try:
        from backend.clickhouse_telemetry import get_telemetry_delivery_status

        status = get_telemetry_delivery_status()
        return {
            "pending": status.get("pending"),
            "failed": status.get("failed"),
            "delivered": status.get("delivered"),
            "schema_ready": status.get("schema_ready"),
            "cloud_connected": status.get("cloud_connected"),
            "drain_blocked_reason": status.get("drain_blocked_reason"),
            "ingestion_lag_seconds": status.get("ingestion_lag_seconds"),
            "last_delivered_at": status.get("last_delivered_at"),
        }
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("Delivery context unavailable: %s", exc)
        return {
            "pending": None,
            "failed": None,
            "delivered": None,
            "schema_ready": None,
            "cloud_connected": False,
            "drain_blocked_reason": None,
            "ingestion_lag_seconds": None,
            "last_delivered_at": None,
        }


def _response(
    *,
    columns: list[str],
    rows: list[list[Any]],
    source: str,
    started_at: float,
    delivery: dict[str, Any] | None = None,
) -> dict[str, Any]:
    bounded_rows = rows[:MAX_RESULT_ROWS]
    payload: dict[str, Any] = {
        "success": True,
        "columns": columns,
        "rows": bounded_rows,
        "row_count": len(bounded_rows),
        "duration_ms": round((time.perf_counter() - started_at) * 1000, 2),
        "source": source,
        "availability": "available" if bounded_rows else "unavailable",
    }
    if delivery is not None:
        payload["delivery"] = delivery
        payload["ingestion_lag_seconds"] = delivery.get("ingestion_lag_seconds")
        payload["freshness"] = (
            "unavailable"
            if delivery.get("last_delivered_at") is None
            else delivery.get("last_delivered_at")
        )
    return payload


def execute_telemetry_query(
    query_id: str | None = None,
    *,
    sql: str | None = None,
    jobs_root: Path,
    script_jobs_root: Path,
    repo_root: Path,
    client_factory: Callable[[], Any] | None = None,
    include_delivery: bool = True,
) -> dict[str, Any]:
    """Run one allowlisted query (or validated read-only SQL) against Cloud, then the real local mirror."""
    if query_id:
        if query_id not in QUERY_SQL:
            raise ValueError(f"query_id must be one of: {', '.join(sorted(QUERY_SQL.keys()))}")
        clickhouse_sql = resolve_query_sql(query_id, "clickhouse")
        mirror_sql = resolve_query_sql(query_id, "sqlite")
    elif sql:
        validated = validate_readonly_sql(sql)
        clickhouse_sql = validated
        mirror_sql = validated
    else:
        raise ValueError("Either query_id or query is required")

    delivery = _delivery_context() if include_delivery else None
    started_at = time.perf_counter()
    if client_factory is None:
        from backend.clickhouse_telemetry import get_clickhouse_client

        client_factory = get_clickhouse_client
    try:
        client = client_factory()
        if client is not None:
            try:
                result = client.query(clickhouse_sql, settings=CLICKHOUSE_SETTINGS)
            except TypeError:
                # Older clickhouse-connect clients may not accept settings;
                # the SQL LIMIT still enforces the response bound.
                result = client.query(clickhouse_sql)
            columns = list(result.column_names)
            rows = [list(row) for row in result.result_rows]
            return _response(
                columns=columns,
                rows=rows,
                source="clickhouse_cloud",
                started_at=started_at,
                delivery=delivery,
            )
    except Exception as exc:
        logger.info("ClickHouse query failed; using local mirror: %s", exc)

    connection = _build_local_database(jobs_root, script_jobs_root, repo_root)
    try:
        cursor = connection.cursor()
        cursor.execute(mirror_sql)
        rows = [list(row) for row in cursor.fetchall()]
        columns = [description[0] for description in cursor.description or []]
        return _response(
            columns=columns,
            rows=rows,
            source="local_mirror",
            started_at=started_at,
            delivery=delivery,
        )
    finally:
        connection.close()
