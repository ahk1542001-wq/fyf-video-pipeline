"""Explicit, validated deployment capacity limits (Stage B-III / B11).

This module is the SINGLE SOURCE OF TRUTH for every capacity limit the pipeline
enforces. Each limit is:

  (a) configurable via deployment / environment variables,
  (b) validated server-side on submit (``validate_submission``), and
  (c) returned by ``GET /api/runtime`` (``CapacityConfig.to_runtime_dict``).

Design rules
------------
* Fail-closed on malformed configuration: an unparseable or out-of-range value
  never silently widens a limit. It falls back to the documented safe default
  and is recorded in ``config_warnings`` so the operator can see the mistake.
* No limit is invented at request time. Overload produces an honest queued
  state (see ``backend.job_queue``) rather than a bare 429.
* ``max_concurrent_jobs`` here supersedes the ad-hoc read that previously lived
  in ``backend.runtime_limits``; that module now delegates to this one so the
  env var ``FYF_MAX_CONCURRENT_JOBS`` has exactly one owner.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Documented safe defaults. These are conservative on purpose: a deployment that
# cares about throughput raises them explicitly via env (see deploy_cloudrun.sh).
# ---------------------------------------------------------------------------
DEFAULT_MAX_UPLOAD_BYTES = 8 * 1024 * 1024          # 8 MiB request body
DEFAULT_MAX_INPUT_DURATION_SECONDS = 600.0          # 10 minutes of source audio
DEFAULT_MAX_OUTPUT_DURATION_SECONDS = 180.0         # 3 minutes of rendered video
DEFAULT_WORKER_CPU = 2.0                            # vCPU per worker
DEFAULT_WORKER_MEMORY_MB = 4096                     # MiB per worker
DEFAULT_WORKER_WALL_TIME_SECONDS = 3600.0           # 1 hour hard wall-clock cap
DEFAULT_QUEUE_DEPTH = 32                            # max queued (not-yet-running) jobs
DEFAULT_MAX_CONCURRENT_JOBS = 1                     # global concurrency
DEFAULT_RATE_LIMIT_PER_MINUTE = 10

# Hard sanity bounds so a typo (e.g. FYF_QUEUE_DEPTH=-5) cannot produce an
# unusable or dangerous configuration. Values outside these bounds fall back to
# the default and emit a warning.
_BOUNDS: dict[str, tuple[float, float]] = {
    "max_upload_bytes": (1024, 512 * 1024 * 1024),
    "max_input_duration_seconds": (1.0, 24 * 3600.0),
    "max_output_duration_seconds": (1.0, 6 * 3600.0),
    "worker_cpu": (0.5, 128.0),
    "worker_memory_mb": (256, 128 * 1024),
    "worker_wall_time_seconds": (1.0, 24 * 3600.0),
    "queue_depth": (1, 10_000),
    "max_concurrent_jobs": (0, 512),  # 0 = reject-all maintenance posture (honoured, not clamped)
    "rate_limit_per_minute": (1, 100_000),
}

_ENV_BY_FIELD: dict[str, str] = {
    "max_upload_bytes": "FYF_MAX_UPLOAD_BYTES",
    "max_input_duration_seconds": "FYF_MAX_INPUT_DURATION_SECONDS",
    "max_output_duration_seconds": "FYF_MAX_OUTPUT_DURATION_SECONDS",
    "worker_cpu": "FYF_WORKER_CPU",
    "worker_memory_mb": "FYF_WORKER_MEMORY_MB",
    "worker_wall_time_seconds": "FYF_WORKER_WALL_TIME_SECONDS",
    "queue_depth": "FYF_QUEUE_DEPTH",
    "max_concurrent_jobs": "FYF_MAX_CONCURRENT_JOBS",
    "rate_limit_per_minute": "FYF_RATE_LIMIT_PER_MINUTE",
}

_INT_FIELDS = {
    "max_upload_bytes",
    "worker_memory_mb",
    "queue_depth",
    "max_concurrent_jobs",
    "rate_limit_per_minute",
}

_DEFAULTS: dict[str, Any] = {
    "max_upload_bytes": DEFAULT_MAX_UPLOAD_BYTES,
    "max_input_duration_seconds": DEFAULT_MAX_INPUT_DURATION_SECONDS,
    "max_output_duration_seconds": DEFAULT_MAX_OUTPUT_DURATION_SECONDS,
    "worker_cpu": DEFAULT_WORKER_CPU,
    "worker_memory_mb": DEFAULT_WORKER_MEMORY_MB,
    "worker_wall_time_seconds": DEFAULT_WORKER_WALL_TIME_SECONDS,
    "queue_depth": DEFAULT_QUEUE_DEPTH,
    "max_concurrent_jobs": DEFAULT_MAX_CONCURRENT_JOBS,
    "rate_limit_per_minute": DEFAULT_RATE_LIMIT_PER_MINUTE,
}


@dataclass(frozen=True)
class CapacityConfig:
    """Validated capacity limits resolved from the deployment environment."""

    max_upload_bytes: int = DEFAULT_MAX_UPLOAD_BYTES
    max_input_duration_seconds: float = DEFAULT_MAX_INPUT_DURATION_SECONDS
    max_output_duration_seconds: float = DEFAULT_MAX_OUTPUT_DURATION_SECONDS
    worker_cpu: float = DEFAULT_WORKER_CPU
    worker_memory_mb: int = DEFAULT_WORKER_MEMORY_MB
    worker_wall_time_seconds: float = DEFAULT_WORKER_WALL_TIME_SECONDS
    queue_depth: int = DEFAULT_QUEUE_DEPTH
    max_concurrent_jobs: int = DEFAULT_MAX_CONCURRENT_JOBS
    rate_limit_per_minute: int = DEFAULT_RATE_LIMIT_PER_MINUTE
    config_warnings: tuple[str, ...] = field(default_factory=tuple)

    def to_runtime_dict(self) -> dict[str, Any]:
        """Typed shape returned by ``GET /api/runtime`` under ``limits``.

        Field names and types are the contract Task 13 renders. ``config_warnings``
        is intentionally NOT exposed publicly (operator-only signal).
        """
        return {
            "max_upload_bytes": int(self.max_upload_bytes),
            "max_input_duration_seconds": float(self.max_input_duration_seconds),
            "max_output_duration_seconds": float(self.max_output_duration_seconds),
            "worker_cpu": float(self.worker_cpu),
            "worker_memory_mb": int(self.worker_memory_mb),
            "worker_wall_time_seconds": float(self.worker_wall_time_seconds),
            "queue_depth": int(self.queue_depth),
            "max_concurrent_jobs": int(self.max_concurrent_jobs),
            "rate_limit_per_minute": int(self.rate_limit_per_minute),
        }


def _parse_number(name: str, raw: str | None, default: float, *, as_int: bool) -> tuple[Any, str | None]:
    lo, hi = _BOUNDS[name]
    env_var = _ENV_BY_FIELD[name]
    if raw is None or not str(raw).strip():
        return (int(default) if as_int else float(default)), None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return (int(default) if as_int else float(default)), (
            f"{env_var}={raw!r} is not a number; using default {default}"
        )
    if value != value or value in (float("inf"), float("-inf")):  # NaN / inf
        return (int(default) if as_int else float(default)), (
            f"{env_var}={raw!r} is not finite; using default {default}"
        )
    if value < lo or value > hi:
        return (int(default) if as_int else float(default)), (
            f"{env_var}={value} outside [{lo}, {hi}]; using default {default}"
        )
    return (int(value) if as_int else float(value)), None


def load_capacity_config() -> CapacityConfig:
    """Resolve and validate every capacity limit from the environment.

    Malformed / out-of-range values fall back to the safe default and are
    collected in ``config_warnings`` (fail-closed: never silently widened).
    """
    values: dict[str, Any] = {}
    warnings: list[str] = []
    for name, env_var in _ENV_BY_FIELD.items():
        parsed, warning = _parse_number(
            name, os.getenv(env_var), _DEFAULTS[name], as_int=name in _INT_FIELDS
        )
        values[name] = parsed
        if warning:
            warnings.append(warning)
    if warnings:
        logger.warning("Capacity config fell back to defaults: %s", "; ".join(warnings))
    return CapacityConfig(config_warnings=tuple(warnings), **values)


@dataclass(frozen=True)
class LimitViolation:
    """One specific capacity limit that a submission exceeded."""

    limit: str
    provided: float
    allowed: float
    unit: str

    def message(self) -> str:
        return (
            f"{self.limit} exceeded: provided {self.provided:g}{self.unit} "
            f"but the deployment allows at most {self.allowed:g}{self.unit}"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "limit": self.limit,
            "provided": self.provided,
            "allowed": self.allowed,
            "unit": self.unit,
            "message": self.message(),
        }


def validate_submission(
    *,
    config: CapacityConfig | None = None,
    upload_bytes: int | None = None,
    input_duration_seconds: float | None = None,
    output_duration_seconds: float | None = None,
) -> list[LimitViolation]:
    """Validate a submission against every applicable capacity limit.

    Only the values the caller actually supplies are checked (``None`` means the
    submission did not declare that dimension). Returns the list of specific
    violations, each naming the exact limit, so the caller can reject honestly
    instead of a generic error. An empty list means the submission fits.
    """
    cfg = config or load_capacity_config()
    violations: list[LimitViolation] = []

    def _as_float(value: Any) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return float("inf")

    if upload_bytes is not None and _as_float(upload_bytes) > cfg.max_upload_bytes:
        violations.append(
            LimitViolation("max_upload_bytes", _as_float(upload_bytes), float(cfg.max_upload_bytes), " bytes")
        )
    if input_duration_seconds is not None and _as_float(input_duration_seconds) > cfg.max_input_duration_seconds:
        violations.append(
            LimitViolation(
                "max_input_duration_seconds",
                _as_float(input_duration_seconds),
                cfg.max_input_duration_seconds,
                "s",
            )
        )
    if output_duration_seconds is not None and _as_float(output_duration_seconds) > cfg.max_output_duration_seconds:
        violations.append(
            LimitViolation(
                "max_output_duration_seconds",
                _as_float(output_duration_seconds),
                cfg.max_output_duration_seconds,
                "s",
            )
        )
    return violations


def max_concurrent_jobs() -> int:
    """Single accessor for global concurrency (delegates to the validated config)."""
    return load_capacity_config().max_concurrent_jobs


def rate_limit_per_minute() -> int:
    """Single accessor for the per-IP request rate limit."""
    return load_capacity_config().rate_limit_per_minute
