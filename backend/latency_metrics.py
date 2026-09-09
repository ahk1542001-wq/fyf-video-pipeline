"""Measured latency instrumentation (D8) and the render concurrency sweep (D9).

HONESTY CONTRACT
----------------
Nothing in this module ever publishes an unmeasured number as a guarantee.
A metric is reported in exactly one of three states:

* ``measured-and-met``    — real samples exist and the percentile is within target
* ``measured-and-missed`` — real samples exist and the percentile is outside target
* ``unmeasured``          — no samples exist, with the reason stated

Every sample must carry the conditions it was measured under (cold vs warm cache,
scene count, aspect ratio, language, observed provider queue depth).  A sample
missing its cache state is rejected, because "chat round-trip p95 = 4s" means
nothing without knowing whether it was a warm replay.

This module is hermetic: it performs no provider call and incurs no cost.  Every
real-provider latency figure for FYF is therefore **unmeasured** until Task 16
runs the pipeline against live providers with this instrumentation attached.
"""

from __future__ import annotations

import json
import math
import os
import resource
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence


REPO_ROOT = Path(__file__).resolve().parent.parent
METRICS_FILENAME = "latency_metrics.jsonl"
SWEEP_FILENAME = "render_concurrency_sweep.json"

#: The three latency targets, in seconds.  Targets are ambitions to be measured
#: against — they are never asserted as achieved without samples.
LATENCY_TARGETS: dict[str, float] = {
    "chat_round_trip": 10.0,
    "draft_to_animatic": 60.0,
    "final_render": 600.0,
}

METRIC_NAMES: tuple[str, ...] = tuple(LATENCY_TARGETS)
CACHE_STATES: tuple[str, ...] = ("cold", "warm")

#: Deployment envelope read from scripts/deploy_cloudrun.sh (--cpu 2
#: --memory 4Gi --timeout 3600).  Recorded here so the concurrency sweep can be
#: judged against the real limit instead of an assumed one.
DEPLOY_CPU = 2
DEPLOY_MEMORY_GIB = 4
DEPLOY_TIMEOUT_SECONDS = 3600

CONCURRENCY_CLAMP_MIN = 1
CONCURRENCY_CLAMP_MAX = 4
CONCURRENCY_SWEEP_VALUES: tuple[int, ...] = (1, 2, 3, 4)


def metrics_root() -> Path:
    """Where latency samples live.  Defaults to the gitignored telemetry root."""

    override = os.environ.get("FYF_LATENCY_METRICS_ROOT")
    if override:
        return Path(override)
    return REPO_ROOT / "telemetry" / "latency"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# D8 — recording
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class LatencySample:
    """One measured latency observation with the conditions it was taken under."""

    metric: str
    seconds: float
    cache_state: str
    scene_count: int
    aspect_ratio: str | None
    language: str | None
    provider_queue_depth: int | None
    recorded_at: str
    job_id: str | None = None
    stage: str | None = None
    measured: bool = True
    extra: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        payload = {
            "metric": self.metric,
            "seconds": self.seconds,
            "cache_state": self.cache_state,
            "scene_count": self.scene_count,
            "aspect_ratio": self.aspect_ratio,
            "language": self.language,
            "provider_queue_depth": self.provider_queue_depth,
            "recorded_at": self.recorded_at,
            "measured": self.measured,
        }
        if self.job_id is not None:
            payload["job_id"] = self.job_id
        if self.stage is not None:
            payload["stage"] = self.stage
        if self.extra:
            payload["extra"] = dict(self.extra)
        return payload


def validate_sample_fields(
    metric: str,
    seconds: float,
    cache_state: str,
    scene_count: int,
) -> None:
    """Fail closed on anything that would make a later percentile meaningless."""

    if metric not in LATENCY_TARGETS:
        raise ValueError(f"unknown latency metric {metric!r}; known: {', '.join(METRIC_NAMES)}")
    if (
        not isinstance(seconds, (int, float))
        or isinstance(seconds, bool)
        or not math.isfinite(float(seconds))
        or seconds < 0
    ):
        raise ValueError("latency seconds must be a finite non-negative number")
    if cache_state not in CACHE_STATES:
        raise ValueError(f"cache_state must be one of {CACHE_STATES}, got {cache_state!r}")
    if not isinstance(scene_count, int) or isinstance(scene_count, bool) or scene_count < 0:
        raise ValueError("scene_count must be a non-negative integer")


def record_latency_sample(
    metric: str,
    seconds: float,
    *,
    cache_state: str,
    scene_count: int,
    aspect_ratio: str | None = None,
    language: str | None = None,
    provider_queue_depth: int | None = None,
    job_id: str | None = None,
    stage: str | None = None,
    root: str | Path | None = None,
    extra: Mapping[str, Any] | None = None,
    now_fn: Callable[[], str] = utc_now_iso,
) -> dict[str, Any]:
    """Append one measured sample to the JSONL store."""

    validate_sample_fields(metric, seconds, cache_state, scene_count)
    if provider_queue_depth is not None and (
        not isinstance(provider_queue_depth, int) or isinstance(provider_queue_depth, bool) or provider_queue_depth < 0
    ):
        raise ValueError("provider_queue_depth must be a non-negative integer or None")

    sample = LatencySample(
        metric=metric,
        seconds=round(float(seconds), 3),
        cache_state=cache_state,
        scene_count=int(scene_count),
        aspect_ratio=aspect_ratio if isinstance(aspect_ratio, str) else None,
        language=language if isinstance(language, str) else None,
        provider_queue_depth=provider_queue_depth,
        recorded_at=now_fn(),
        job_id=job_id,
        stage=stage,
        extra=dict(extra or {}),
    )
    payload = sample.as_dict()
    destination = Path(root) if root is not None else metrics_root()
    destination.mkdir(parents=True, exist_ok=True)
    with (destination / METRICS_FILENAME).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
    return payload


def read_samples(root: str | Path | None = None) -> list[dict[str, Any]]:
    """Read every recorded sample.  Corrupt lines are reported, not skipped."""

    destination = Path(root) if root is not None else metrics_root()
    path = destination / METRICS_FILENAME
    if not path.is_file():
        return []
    samples: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{METRICS_FILENAME}:{line_number} is corrupt") from exc
        if not isinstance(payload, dict):
            raise ValueError(f"{METRICS_FILENAME}:{line_number} must contain an object")
        samples.append(payload)
    return samples


def percentile(values: Sequence[float], quantile: float) -> float | None:
    """Nearest-rank percentile.  Returns ``None`` for an empty sample."""

    if not values:
        return None
    if not 0.0 < quantile <= 1.0:
        raise ValueError("quantile must be in (0, 1]")
    ordered = sorted(float(value) for value in values)
    rank = max(1, -(-int(len(ordered) * quantile * 100) // 100))
    return round(ordered[min(rank, len(ordered)) - 1], 3)


def summarize(
    samples: Iterable[Mapping[str, Any]],
    *,
    metric: str | None = None,
) -> dict[str, Any]:
    """p50/p95 plus the conditions, per metric, with an honest status."""

    collected = [sample for sample in samples if isinstance(sample, Mapping)]
    if metric is not None:
        collected = [sample for sample in collected if sample.get("metric") == metric]

    report: dict[str, Any] = {"metrics": {}, "sample_size": len(collected)}
    for name in METRIC_NAMES:
        rows = [sample for sample in collected if sample.get("metric") == name]
        values = [float(row["seconds"]) for row in rows if isinstance(row.get("seconds"), (int, float))]
        target = LATENCY_TARGETS[name]
        if not values:
            report["metrics"][name] = {
                "status": "unmeasured",
                "reason": "no samples recorded; a guarantee is never published without measurement",
                "target_seconds": target,
                "sample_size": 0,
                "p50": None,
                "p95": None,
            }
            continue
        p95 = percentile(values, 0.95)
        report["metrics"][name] = {
            "status": "measured-and-met" if p95 is not None and p95 <= target else "measured-and-missed",
            "target_seconds": target,
            "sample_size": len(values),
            "p50": percentile(values, 0.50),
            "p95": p95,
            "min": round(min(values), 3),
            "max": round(max(values), 3),
            "conditions": _conditions(rows),
        }
    return report


def _conditions(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Break a metric's samples down by the conditions they were measured under."""

    def tally(key: str) -> dict[str, int]:
        counts: dict[str, int] = {}
        for row in rows:
            value = row.get(key)
            label = str(value) if value is not None else "unspecified"
            counts[label] = counts.get(label, 0) + 1
        return dict(sorted(counts.items()))

    depths = [row.get("provider_queue_depth") for row in rows if isinstance(row.get("provider_queue_depth"), int)]
    return {
        "cache_state": tally("cache_state"),
        "scene_count": tally("scene_count"),
        "aspect_ratio": tally("aspect_ratio"),
        "language": tally("language"),
        "provider_queue_depth_observed": bool(depths),
        "provider_queue_depth_max": max(depths) if depths else None,
    }


def report_latency(root: str | Path | None = None) -> dict[str, Any]:
    """The publishable latency report.  Unmeasured stays unmeasured."""

    samples = read_samples(root)
    summary = summarize(samples)
    summary["recorded_at"] = utc_now_iso()
    summary["statement"] = (
        "Figures are measurements, not guarantees. Every metric with zero samples "
        "is reported as unmeasured. Real-provider latency for this deployment is "
        "unmeasured until a run against live providers is instrumented."
    )
    return summary


# --------------------------------------------------------------------------- #
# D9 — render concurrency sweep (LOCAL ONLY, cached assets only)
# --------------------------------------------------------------------------- #


def concurrency_clamp_justification() -> dict[str, Any]:
    """Why FYF_SEGMENT_RENDER_CONCURRENCY is clamped to 1..4 with a default of 2.

    This is DESIGN RATIONALE derived from the deployment envelope, and it is
    labelled as such.  It is not a measurement: :func:`sweep_segment_render_concurrency`
    produces the measurement, and reports ``unmeasured`` when it cannot.
    """

    return {
        "clamp_min": CONCURRENCY_CLAMP_MIN,
        "clamp_max": CONCURRENCY_CLAMP_MAX,
        "default": 2,
        "evidence_type": "design-rationale",
        "measured": False,
        "deployment_envelope": {
            "cpu": DEPLOY_CPU,
            "memory_gib": DEPLOY_MEMORY_GIB,
            "timeout_seconds": DEPLOY_TIMEOUT_SECONDS,
            "source": "scripts/deploy_cloudrun.sh (--cpu 2 --memory 4Gi --timeout 3600)",
        },
        "rationale": [
            "Each concurrent slot runs one headless Chromium plus one ffmpeg, so "
            "the worker count is CPU-bound on a 2 vCPU instance; 2 slots saturate "
            "the allocation without contending for it.",
            "The clamp ceiling of 4 leaves headroom for larger local machines and "
            "for a future CPU increase, while the hard floor of 1 keeps the "
            "serial fallback available for memory-constrained runs.",
            "Going above 4 would oversubscribe 2 vCPU by more than 2x and risk the "
            "3600s request timeout, which converts a slow render into a failed one.",
        ],
    }


@dataclass(frozen=True)
class SweepObservation:
    concurrency: int
    wall_clock_seconds: float
    peak_rss_mib: float
    cpu_seconds: float
    units: int
    measured: bool
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "concurrency": self.concurrency,
            "wall_clock_seconds": round(self.wall_clock_seconds, 3),
            "peak_rss_mib": round(self.peak_rss_mib, 2),
            "cpu_seconds": round(self.cpu_seconds, 3),
            "units": self.units,
            "measured": self.measured,
            "note": self.note,
        }


def sweep_segment_render_concurrency(
    *,
    scene_count: int,
    values: Sequence[int] = CONCURRENCY_SWEEP_VALUES,
    runner: Callable[[int, int], float] | None = None,
    root: str | Path | None = None,
    record: bool = True,
) -> dict[str, Any]:
    """Sweep the concurrency setting at a fixed scene count.

    LOCAL ONLY.  ``runner(concurrency, scene_count)`` must render from
    STORED/CACHED assets — no paid provider dispatch, no large-scale load test.
    When no runner is supplied the sweep is reported as ``unmeasured`` with the
    reason, because inventing wall-clock, RSS and CPU numbers for renders that
    never happened would be fabricated telemetry.

    Peak RSS and CPU seconds come from ``resource.getrusage(RUSAGE_CHILDREN)``,
    which measures the real child processes ffmpeg/Chromium spawn — stdlib only,
    no new dependency.
    """

    if not isinstance(scene_count, int) or isinstance(scene_count, bool) or scene_count < 1:
        raise ValueError("scene_count must be a positive integer")
    resolved_values = sorted({int(value) for value in values})
    for value in resolved_values:
        if not CONCURRENCY_CLAMP_MIN <= value <= CONCURRENCY_CLAMP_MAX:
            raise ValueError(
                f"sweep value {value} is outside the supported clamp "
                f"{CONCURRENCY_CLAMP_MIN}..{CONCURRENCY_CLAMP_MAX}"
            )

    envelope_budget_mib = DEPLOY_MEMORY_GIB * 1024.0

    if runner is None:
        report = {
            "status": "unmeasured",
            "reason": (
                "no cached-asset render runner was supplied; a meaningful sweep needs "
                "real segment renders from stored assets, and fabricating wall-clock, "
                "RSS or CPU numbers for renders that never happened is not permitted"
            ),
            "scene_count": scene_count,
            "values": resolved_values,
            "observations": [],
            "recommendation": None,
            "clamp": concurrency_clamp_justification(),
            "deployment_envelope": {
                "cpu": DEPLOY_CPU,
                "memory_gib": DEPLOY_MEMORY_GIB,
                "memory_budget_mib": envelope_budget_mib,
                "timeout_seconds": DEPLOY_TIMEOUT_SECONDS,
            },
            "recorded_at": utc_now_iso(),
        }
    else:
        observations: list[SweepObservation] = []
        for value in resolved_values:
            before_children = resource.getrusage(resource.RUSAGE_CHILDREN)
            started = time.monotonic()
            elapsed = runner(value, scene_count)
            wall = time.monotonic() - started
            after_children = resource.getrusage(resource.RUSAGE_CHILDREN)
            cpu = (after_children.ru_utime + after_children.ru_stime) - (
                before_children.ru_utime + before_children.ru_stime
            )
            # ru_maxrss is bytes on macOS and KiB on Linux; normalise to MiB.
            peak = float(after_children.ru_maxrss)
            peak_mib = peak / (1024.0 * 1024.0) if os.uname().sysname == "Darwin" else peak / 1024.0
            if not isinstance(elapsed, (int, float)) or elapsed < 0:
                raise ValueError("sweep runner must return a non-negative elapsed seconds value")
            observations.append(
                SweepObservation(
                    concurrency=value,
                    wall_clock_seconds=float(elapsed),
                    peak_rss_mib=peak_mib,
                    cpu_seconds=float(cpu),
                    units=scene_count,
                    measured=True,
                )
            )

        fastest = min(observations, key=lambda item: item.wall_clock_seconds)
        within_memory = [item for item in observations if item.peak_rss_mib <= envelope_budget_mib]
        recommendation = None
        if within_memory:
            best = min(within_memory, key=lambda item: item.wall_clock_seconds)
            recommendation = {
                "concurrency": best.concurrency,
                "reason": (
                    f"fastest wall clock ({best.wall_clock_seconds:.2f}s) among settings that "
                    f"stayed inside the {DEPLOY_MEMORY_GIB}GiB envelope"
                ),
            }
        report = {
            "status": "measured",
            "reason": None,
            "scene_count": scene_count,
            "values": resolved_values,
            "observations": [item.as_dict() for item in observations],
            "fastest": fastest.as_dict(),
            "recommendation": recommendation,
            "clamp": concurrency_clamp_justification(),
            "deployment_envelope": {
                "cpu": DEPLOY_CPU,
                "memory_gib": DEPLOY_MEMORY_GIB,
                "memory_budget_mib": envelope_budget_mib,
                "timeout_seconds": DEPLOY_TIMEOUT_SECONDS,
            },
            "recorded_at": utc_now_iso(),
        }

    if record:
        destination = Path(root) if root is not None else metrics_root()
        destination.mkdir(parents=True, exist_ok=True)
        (destination / SWEEP_FILENAME).write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    return report


def read_sweep(root: str | Path | None = None) -> dict[str, Any] | None:
    destination = Path(root) if root is not None else metrics_root()
    path = destination / SWEEP_FILENAME
    if not path.is_file():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{SWEEP_FILENAME} must contain an object")
    return payload
