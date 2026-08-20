"""Privacy-safe per-job telemetry for Vertex AI and Gemini TTS calls.

The production pipeline keeps retry behavior in its existing callers.  This
module wraps the SDK client at the existing client factories so every actual
SDK attempt is recorded without storing prompts, response text, credentials,
or raw provider errors.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from backend.job_store import write_json_atomically

logger = logging.getLogger(__name__)

TELEMETRY_SCHEMA_VERSION = 1
PRICING_VERSION = "2026-08-20"
PRICING_SOURCE = {
    "text": "https://cloud.google.com/gemini-enterprise-agent-platform/generative-ai/pricing",
    "tts": "https://cloud.google.com/text-to-speech/pricing",
}

_BILLABLE_OPERATIONS = {"generate_content", "generate_videos"}
_current_collector: ContextVar["VertexTelemetryCollector | None"] = ContextVar(
    "fyf_vertex_telemetry_collector", default=None
)
_current_retry_attempt: ContextVar[tuple[str, int] | None] = ContextVar(
    "fyf_vertex_telemetry_retry_attempt", default=None
)
_current_job_attempt: ContextVar[int] = ContextVar(
    "fyf_vertex_telemetry_job_attempt", default=1
)

# Only routes with a verified price entry are listed.  Image/video routes and
# future preview routes remain unpriced instead of receiving invented costs.
_DEFAULT_PRICING: dict[str, dict[str, Any]] = {
    "gemini-3.1-pro-preview": {
        "input_usd_per_million": 2.0,
        "output_usd_per_million": 12.0,
        "cached_input_usd_per_million": 0.2,
        "source": PRICING_SOURCE["text"],
    },
    "gemini-2.5-flash-preview-tts": {
        "input_usd_per_million": 0.5,
        "output_usd_per_million": 10.0,
        "source": PRICING_SOURCE["tts"],
    },
    "gemini-2.5-flash-tts": {
        "input_usd_per_million": 0.5,
        "output_usd_per_million": 10.0,
        "source": PRICING_SOURCE["tts"],
    },
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        value = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return max(0, value)


def _field(value: Any, name: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)


def _usage_metadata(response: Any) -> dict[str, int | None]:
    usage = getattr(response, "usage_metadata", None)
    return {
        "input_tokens": _safe_int(_field(usage, "prompt_token_count")),
        "output_tokens": _safe_int(_field(usage, "candidates_token_count")),
        "total_tokens": _safe_int(_field(usage, "total_token_count")),
        "cached_input_tokens": _safe_int(_field(usage, "cached_content_token_count")),
        "thoughts_tokens": _safe_int(_field(usage, "thoughts_token_count")),
    }


def _text_length(value: Any) -> int:
    if isinstance(value, str):
        return len(value)
    if isinstance(value, (list, tuple)):
        return sum(_text_length(item) for item in value)
    return 0


def _request_character_count(args: tuple[Any, ...], kwargs: dict[str, Any]) -> int | None:
    if "contents" in kwargs:
        count = _text_length(kwargs["contents"])
    elif len(args) >= 2:
        count = _text_length(args[1])
    else:
        count = 0
    return count if count else None


def _audio_output_bytes(response: Any) -> int | None:
    total = 0
    found = False
    for candidate in getattr(response, "candidates", []) or []:
        content = getattr(candidate, "content", None)
        for part in getattr(content, "parts", []) or []:
            inline_data = getattr(part, "inline_data", None)
            data = getattr(inline_data, "data", None)
            if isinstance(data, (bytes, bytearray)):
                total += len(data)
                found = True
    return total if found else None


def _status_code(error: BaseException) -> int | str | None:
    value = getattr(error, "code", None) or getattr(error, "status_code", None)
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,40}", value):
        return value
    return None


def _pricing_catalog() -> dict[str, dict[str, Any]]:
    raw = os.getenv("FYF_VERTEX_PRICING_JSON", "").strip()
    if not raw:
        return dict(_DEFAULT_PRICING)
    try:
        override = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("Ignoring invalid FYF_VERTEX_PRICING_JSON override")
        return dict(_DEFAULT_PRICING)
    if not isinstance(override, dict):
        logger.warning("Ignoring non-object FYF_VERTEX_PRICING_JSON override")
        return dict(_DEFAULT_PRICING)
    merged = dict(_DEFAULT_PRICING)
    for model, entry in override.items():
        if isinstance(model, str) and isinstance(entry, dict):
            merged[model] = entry
    return merged


def _model_key(model: str | None) -> str | None:
    if not model:
        return None
    return str(model).rsplit("/", 1)[-1]


def _call_cost(call: dict[str, Any], catalog: dict[str, dict[str, Any]]) -> float | None:
    if not call.get("billable") or call.get("status") != "succeeded":
        return 0.0
    model = _model_key(call.get("model"))
    price = catalog.get(model or "")
    usage = call.get("usage") or {}
    if not price:
        return None
    input_tokens = usage.get("input_tokens")
    output_tokens = usage.get("output_tokens")
    if input_tokens is None or output_tokens is None:
        return None
    input_rate = price.get("input_usd_per_million")
    output_rate = price.get("output_usd_per_million")
    if not isinstance(input_rate, (int, float)) or not isinstance(output_rate, (int, float)):
        return None
    cached_tokens = usage.get("cached_input_tokens") or 0
    cached_rate = price.get("cached_input_usd_per_million")
    uncached_tokens = max(0, input_tokens - cached_tokens)
    input_cost = uncached_tokens * float(input_rate) / 1_000_000
    if isinstance(cached_rate, (int, float)):
        input_cost += cached_tokens * float(cached_rate) / 1_000_000
    return input_cost + output_tokens * float(output_rate) / 1_000_000


class VertexTelemetryCollector:
    """Collect and persist one script or video job's provider usage."""

    def __init__(self, job_id: str, job_kind: str, job_dir: Path):
        self.job_id = job_id
        self.job_kind = job_kind
        self.job_dir = Path(job_dir)
        self.started_at = _utc_now()
        self.calls: list[dict[str, Any]] = []

    def record_call(
        self,
        *,
        stage: str,
        operation: str,
        model: str | None,
        invoke: Callable[[], Any],
        args: tuple[Any, ...] = (),
        kwargs: dict[str, Any] | None = None,
        default_attempt: int = 1,
    ) -> Any:
        kwargs = kwargs or {}
        model_value = str(model) if model is not None else None
        retry_context = _current_retry_attempt.get()
        retry_group, retry_attempt = retry_context or (None, default_attempt)
        attempt = max(1, int(retry_attempt))
        started = time.perf_counter()
        record: dict[str, Any] = {
            "call_id": f"{self.job_id}-{len(self.calls) + 1:04d}",
            "stage": stage,
            "model": model_value,
            "operation": operation,
            "attempt": attempt,
            "job_attempt": _current_job_attempt.get(),
            "billable": operation in _BILLABLE_OPERATIONS,
            "status": "failed",
            "duration_ms": 0.0,
            "usage": {
                "input_tokens": None,
                "output_tokens": None,
                "total_tokens": None,
                "cached_input_tokens": None,
                "thoughts_tokens": None,
            },
        }
        if retry_group:
            record["retry_group"] = retry_group
        if stage == "tts":
            record["input_characters"] = _request_character_count(args, kwargs)

        try:
            response = invoke()
        except Exception as exc:
            record.update({
                "status": "failed",
                "error_type": type(exc).__name__,
                "http_status": _status_code(exc),
            })
            self.calls.append(record)
            record["duration_ms"] = round((time.perf_counter() - started) * 1000, 3)
            raise

        record.update({
            "status": "succeeded",
            "usage": _usage_metadata(response),
            "duration_ms": round((time.perf_counter() - started) * 1000, 3),
        })
        if stage == "tts":
            record["audio_output_bytes"] = _audio_output_bytes(response)
        self.calls.append(record)
        return response

    def _job_status(self) -> dict[str, Any]:
        candidates = [self.job_dir / "status.json", self.job_dir / "qa_report.json"]
        for path in candidates:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(data, dict):
                return data
        return {}

    def _fallback_count(self) -> int | None:
        for filename in ("status.json", "qa_report.json"):
            try:
                data = json.loads((self.job_dir / filename).read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            metrics = data.get("metrics") if isinstance(data, dict) else None
            value = metrics.get("visual_fallbacks") if isinstance(metrics, dict) else None
            parsed = _safe_int(value)
            if parsed is not None:
                return parsed
        return None

    def summary(self) -> dict[str, Any]:
        billable = [call for call in self.calls if call.get("billable")]
        succeeded_billable = [call for call in billable if call.get("status") == "succeeded"]
        catalog = _pricing_catalog()
        successful_cost_calls = [call for call in billable if call.get("status") == "succeeded"]
        costs = [_call_cost(call, catalog) for call in successful_cost_calls]
        priced_costs = [cost for cost in costs if cost is not None]

        input_tokens = [call["usage"].get("input_tokens") for call in billable]
        output_tokens = [call["usage"].get("output_tokens") for call in billable]
        total_tokens = [call["usage"].get("total_tokens") for call in billable]
        cached_tokens = [call["usage"].get("cached_input_tokens") for call in billable]
        thoughts_tokens = [call["usage"].get("thoughts_tokens") for call in billable]

        def sum_known(values: list[int | None]) -> int:
            return sum(value for value in values if value is not None)

        if not billable:
            token_status = "none"
        elif all(call["usage"].get("total_tokens") is not None for call in succeeded_billable) and len(
            succeeded_billable
        ) == len(billable):
            token_status = "complete"
        elif any(value is not None for value in input_tokens + output_tokens + total_tokens):
            token_status = "partial"
        else:
            token_status = "unavailable"

        if not billable or not successful_cost_calls:
            cost_status = "unavailable"
        elif len(priced_costs) == len(successful_cost_calls):
            cost_status = "exact"
        elif priced_costs:
            cost_status = "partial"
        else:
            cost_status = "unpriced"

        status = self._job_status()
        job_attempts = [
            _safe_int(call.get("job_attempt")) or 1 for call in self.calls
        ]
        job_retry_count = max(job_attempts, default=1) - 1
        return {
            "total_calls": len(self.calls),
            "billable_calls": len(billable),
            "operation_poll_calls": sum(call["operation"] == "operation_poll" for call in self.calls),
            "successful_calls": sum(call["status"] == "succeeded" for call in self.calls),
            "failed_calls": sum(call["status"] == "failed" for call in self.calls),
            "retry_calls": sum(
                call["attempt"] > 1 or (_safe_int(call.get("job_attempt")) or 1) > 1
                for call in self.calls
            ),
            "job_retry_count": job_retry_count,
            "total_input_tokens": sum_known(input_tokens),
            "total_output_tokens": sum_known(output_tokens),
            "total_tokens": sum_known(total_tokens),
            "total_cached_input_tokens": sum_known(cached_tokens),
            "total_thoughts_tokens": sum_known(thoughts_tokens),
            "token_status": token_status,
            "estimated_cost_usd": round(sum(priced_costs), 8) if priced_costs else None,
            "cost_status": cost_status,
            "pricing_version": PRICING_VERSION,
            "pricing_sources": sorted({
                str(catalog.get(_model_key(call.get("model")) or "", {}).get("source"))
                for call in billable
                if catalog.get(_model_key(call.get("model")) or "", {}).get("source")
            }),
            "visual_fallbacks": self._fallback_count(),
            "job_status": status.get("status"),
        }

    def snapshot(self) -> dict[str, Any]:
        return {
            "schema_version": TELEMETRY_SCHEMA_VERSION,
            "job_id": self.job_id,
            "job_kind": self.job_kind,
            "started_at": self.started_at,
            "finished_at": _utc_now(),
            "calls": self.calls,
            "summary": self.summary(),
            "privacy": {
                "prompts_recorded": False,
                "response_text_recorded": False,
                "credentials_recorded": False,
                "raw_provider_errors_recorded": False,
            },
        }

    def persist(self) -> dict[str, Any]:
        payload = self.snapshot()
        try:
            self.job_dir.mkdir(parents=True, exist_ok=True)
            write_json_atomically(self.job_dir / "telemetry.json", payload)
        except Exception:
            logger.exception("Could not persist telemetry for job %s", self.job_id)
        return payload


@contextmanager
def telemetry_scope(job_id: str, job_kind: str, job_dir: Path) -> Iterator[VertexTelemetryCollector]:
    """Reuse an outer collector for recursive retries and async child threads."""
    existing = _current_collector.get()
    if existing is not None:
        yield existing
        return
    collector = VertexTelemetryCollector(job_id, job_kind, Path(job_dir))
    token = _current_collector.set(collector)
    try:
        yield collector
    finally:
        try:
            collector.persist()
        finally:
            _current_collector.reset(token)


@contextmanager
def telemetry_retry_attempt(group: str, attempt: int) -> Iterator[None]:
    """Annotate one SDK invocation made by an existing retry loop."""
    token = _current_retry_attempt.set((str(group), max(1, int(attempt))))
    try:
        yield
    finally:
        _current_retry_attempt.reset(token)


@contextmanager
def telemetry_job_attempt(attempt: int) -> Iterator[None]:
    """Annotate a persisted job-level retry while reusing one collector."""
    token = _current_job_attempt.set(max(1, int(attempt)))
    try:
        yield
    finally:
        _current_job_attempt.reset(token)


class _TrackedModels:
    def __init__(
        self,
        delegate: Any,
        collector: VertexTelemetryCollector,
        stage: str,
        default_attempt: int,
    ):
        self._delegate = delegate
        self._collector = collector
        self._stage = stage
        self._default_attempt = default_attempt

    def _invoke(self, operation: str, *args: Any, **kwargs: Any) -> Any:
        model = kwargs.get("model")
        if model is None and args:
            model = args[0]
        target = getattr(self._delegate, operation)
        return self._collector.record_call(
            stage=self._stage,
            operation=operation,
            model=str(model) if model is not None else None,
            invoke=lambda: target(*args, **kwargs),
            args=args,
            kwargs=kwargs,
            default_attempt=self._default_attempt,
        )

    def generate_content(self, *args: Any, **kwargs: Any) -> Any:
        return self._invoke("generate_content", *args, **kwargs)

    def generate_videos(self, *args: Any, **kwargs: Any) -> Any:
        return self._invoke("generate_videos", *args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)


class _TrackedOperations:
    def __init__(self, delegate: Any, collector: VertexTelemetryCollector, stage: str):
        self._delegate = delegate
        self._collector = collector
        self._stage = stage

    def get(self, *args: Any, **kwargs: Any) -> Any:
        target = getattr(self._delegate, "get")
        return self._collector.record_call(
            stage=self._stage,
            operation="operation_poll",
            model=None,
            invoke=lambda: target(*args, **kwargs),
            args=args,
            kwargs=kwargs,
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)


class _TrackedClient:
    def __init__(
        self,
        delegate: Any,
        collector: VertexTelemetryCollector,
        stage: str,
        default_attempt: int,
    ):
        self._delegate = delegate
        self._collector = collector
        self._stage = stage
        self._models = _TrackedModels(delegate.models, collector, stage, default_attempt)
        self._operations = _TrackedOperations(delegate.operations, collector, stage)

    @property
    def models(self) -> _TrackedModels:
        return self._models

    @property
    def operations(self) -> _TrackedOperations:
        return self._operations

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)


def track_client(client: Any, *, stage: str, attempt: int = 1) -> Any:
    """Wrap a GenAI client only when a production job scope is active."""
    collector = _current_collector.get()
    if collector is None:
        return client
    return _TrackedClient(client, collector, stage, max(1, int(attempt)))


def current_collector() -> VertexTelemetryCollector | None:
    """Return the active collector for integration tests and job adapters."""
    return _current_collector.get()
