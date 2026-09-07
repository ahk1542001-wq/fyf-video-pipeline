"""Server-owned, read-only telemetry query contracts.

The public endpoint accepts a query identifier, never SQL.  The same fixed SQL
is used for ClickHouse and the hermetic SQLite mirror so a local fallback has
the same column contract without inventing metrics that were not recorded.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

MAX_RESULT_ROWS = 200
MAX_EXECUTION_SECONDS = 10
CLICKHOUSE_SETTINGS = {
    "max_result_rows": MAX_RESULT_ROWS,
    "max_execution_time": MAX_EXECUTION_SECONDS,
    "result_overflow_mode": "break",
}

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
}


def query_sql(query_id: str) -> str:
    """Return the fixed SQL for a supported identifier."""
    if query_id not in QUERY_SQL:
        raise KeyError(query_id)
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


def _build_local_database(
    jobs_root: Path,
    script_jobs_root: Path,
    repo_root: Path,
) -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    cursor = connection.cursor()
    cursor.execute(
        """CREATE TABLE video_pipeline_jobs (
            job_id TEXT, title TEXT, duration_sec REAL, voice_mode TEXT, status TEXT,
            total_render_time_ms INTEGER, total_tokens_used INTEGER, cost_usd REAL,
            qa_passed INTEGER, studio_name TEXT, language TEXT, genre TEXT, created_at TEXT
        )"""
    )
    cursor.execute(
        """CREATE TABLE video_vertex_calls (
            job_id TEXT, job_kind TEXT, call_id TEXT, stage TEXT, model TEXT,
            operation TEXT, attempt INTEGER, status TEXT, billable INTEGER,
            duration_ms REAL, input_tokens INTEGER, output_tokens INTEGER,
            total_tokens INTEGER, input_characters INTEGER, audio_output_bytes INTEGER,
            created_at TEXT
        )"""
    )
    cursor.execute(
        """CREATE TABLE video_scene_telemetry (
            job_id TEXT, scene_id TEXT, treatment_type TEXT, render_time_ms INTEGER,
            vertex_latency_ms INTEGER, evidence_claim_count INTEGER, segment_hash TEXT,
            created_at TEXT
        )"""
    )
    for job_dir in _job_dirs(jobs_root):
        row = _job_row(job_dir)
        if row is not None:
            cursor.execute(
                "INSERT INTO video_pipeline_jobs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                row,
            )
    for root in (jobs_root, script_jobs_root):
        cursor.executemany(
            "INSERT INTO video_vertex_calls VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            _call_rows(root),
        )
    cursor.executemany(
        "INSERT INTO video_scene_telemetry VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        _scene_rows(repo_root),
    )
    connection.commit()
    return connection


def _response(
    *,
    columns: list[str],
    rows: list[list[Any]],
    source: str,
    started_at: float,
) -> dict[str, Any]:
    bounded_rows = rows[:MAX_RESULT_ROWS]
    return {
        "success": True,
        "columns": columns,
        "rows": bounded_rows,
        "row_count": len(bounded_rows),
        "duration_ms": round((time.perf_counter() - started_at) * 1000, 2),
        "source": source,
        "availability": "available" if bounded_rows else "unavailable",
    }


def execute_telemetry_query(
    query_id: str | None = None,
    *,
    sql: str | None = None,
    jobs_root: Path,
    script_jobs_root: Path,
    repo_root: Path,
    client_factory: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    """Run one allowlisted query or validated read-only SQL query against Cloud, then the real local mirror."""
    if query_id:
        if query_id not in QUERY_SQL:
            raise ValueError(f"query_id must be one of: {', '.join(sorted(QUERY_SQL.keys()))}")
        resolved_sql = query_sql(query_id)
    elif sql:
        resolved_sql = validate_readonly_sql(sql)
    else:
        raise ValueError("Either query_id or query is required")

    started_at = time.perf_counter()
    if client_factory is None:
        from backend.clickhouse_telemetry import get_clickhouse_client

        client_factory = get_clickhouse_client
    try:
        client = client_factory()
        if client is not None:
            try:
                result = client.query(resolved_sql, settings=CLICKHOUSE_SETTINGS)
            except TypeError:
                # Older clickhouse-connect clients may not accept settings;
                # the SQL LIMIT still enforces the response bound.
                result = client.query(resolved_sql)
            columns = list(result.column_names)
            rows = [list(row) for row in result.result_rows]
            return _response(
                columns=columns,
                rows=rows,
                source="clickhouse_cloud",
                started_at=started_at,
            )
    except Exception as exc:
        logger.info("ClickHouse query failed; using local mirror: %s", exc)

    connection = _build_local_database(jobs_root, script_jobs_root, repo_root)
    try:
        cursor = connection.cursor()
        cursor.execute(resolved_sql)
        rows = [list(row) for row in cursor.fetchall()]
        columns = [description[0] for description in cursor.description or []]
        return _response(
            columns=columns,
            rows=rows,
            source="local_mirror",
            started_at=started_at,
        )
    finally:
        connection.close()
