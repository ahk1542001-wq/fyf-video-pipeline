"""Telemetry reconciliation and honesty guards (Stage E4 / E7).

Pure, dependency-free functions that answer three questions the analytics
surface must never guess about:

1. **Integrity** -- are events missing, duplicated, or reordered?  Detection is
   over ``sequence`` (gaps), ``event_id`` (duplicates) and ``event_timestamp``
   vs ``ingestion_timestamp`` (reorders), per document line 153.
2. **Reconciliation** -- do the canonical job records, the provider-reported
   usage and the ClickHouse aggregates agree?  Per document line 152 the three
   sources are reported side by side with explicit discrepancies rather than
   silently trusting one.
3. **Honesty** -- unknown cost/usage renders as ``unavailable`` / ``None`` and
   is *never* coerced to ``0`` (document line 153).  Estimated,
   provider-reported and invoice-confirmed costs stay **distinct** fields
   (document line 154).  Audience retention/conversion that was never recorded
   is reported ``unavailable`` and never invented from production telemetry
   (document line 158).

Stage E7: AI quality score, human approval and undo are kept as **separate**
signals (document line 156) -- never collapsed into one "quality" number.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

UNAVAILABLE = "unavailable"
_OK = "ok"


# ---------------------------------------------------------------------------
# Honesty helpers
# ---------------------------------------------------------------------------

def _is_real_number(value: Any) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError, OverflowError):
        return False


def number_or_none(value: Any) -> Optional[float]:
    """Return a real number or ``None``.  Unknown is ``None``, never ``0``."""
    if _is_real_number(value):
        return float(value)
    return None


def cost_or_unavailable(value: Any) -> Dict[str, Any]:
    """Wrap a cost value so an unknown cost is ``unavailable``, never ``0``."""
    number = number_or_none(value)
    if number is None:
        return {"value": None, "status": UNAVAILABLE}
    return {"value": number, "status": "reported"}


def _parse_ts(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _as_records(events: Iterable[Any]) -> List[Dict[str, Any]]:
    return [e for e in events if isinstance(e, dict)]


# ---------------------------------------------------------------------------
# Integrity detection (document line 153)
# ---------------------------------------------------------------------------

def detect_missing_events(
    events: Iterable[Any],
    *,
    stream_key: str = "stream",
) -> Dict[str, Any]:
    """Detect gaps in the monotonic ``sequence`` per stream.

    A stream defaults to the value of ``stream_key`` (falling back to a single
    global stream) so interleaved producers are checked independently.
    """
    records = _as_records(events)
    streams: Dict[Any, List[int]] = {}
    for record in records:
        seq = record.get("sequence")
        if not _is_real_number(seq):
            continue
        stream = record.get(stream_key, "__global__")
        streams.setdefault(stream, []).append(int(seq))

    missing: List[Dict[str, Any]] = []
    for stream, seqs in streams.items():
        unique = sorted(set(seqs))
        if not unique:
            continue
        low, high = unique[0], unique[-1]
        gaps = [n for n in range(low, high + 1) if n not in set(unique)]
        for gap in gaps:
            missing.append({"stream": stream, "sequence": gap})

    return {
        "status": "gaps_detected" if missing else _OK,
        "missing": missing,
        "missing_count": len(missing),
        "streams_checked": len(streams),
    }


def detect_duplicate_events(events: Iterable[Any]) -> Dict[str, Any]:
    """Detect more than one record sharing the same ``event_id``."""
    records = _as_records(events)
    counts: Dict[str, int] = {}
    for record in records:
        event_id = record.get("event_id")
        if not isinstance(event_id, str) or not event_id:
            continue
        counts[event_id] = counts.get(event_id, 0) + 1

    duplicates = [
        {"event_id": event_id, "count": count}
        for event_id, count in sorted(counts.items())
        if count > 1
    ]
    return {
        "status": "duplicates_detected" if duplicates else _OK,
        "duplicates": duplicates,
        "duplicate_count": len(duplicates),
    }


def detect_reordered_events(events: Iterable[Any]) -> Dict[str, Any]:
    """Detect ingestion order disagreeing with event order.

    Sorted by ``ingestion_timestamp``, an event is *reordered* when its
    ``event_timestamp`` is earlier than the newest ``event_timestamp`` already
    ingested before it (i.e. late-arriving / out-of-order production).
    """
    records = _as_records(events)
    ordered: List[Dict[str, Any]] = []
    for record in records:
        ingested = _parse_ts(record.get("ingestion_timestamp"))
        produced = _parse_ts(record.get("event_timestamp"))
        if ingested is None or produced is None:
            continue
        ordered.append(
            {
                "event_id": record.get("event_id"),
                "ingestion_timestamp": ingested,
                "event_timestamp": produced,
            }
        )
    ordered.sort(key=lambda item: item["ingestion_timestamp"])

    reordered: List[Dict[str, Any]] = []
    newest_produced: Optional[datetime] = None
    for item in ordered:
        if newest_produced is not None and item["event_timestamp"] < newest_produced:
            reordered.append(
                {
                    "event_id": item["event_id"],
                    "event_timestamp": item["event_timestamp"].isoformat(),
                    "ingestion_timestamp": item["ingestion_timestamp"].isoformat(),
                }
            )
        if newest_produced is None or item["event_timestamp"] > newest_produced:
            newest_produced = item["event_timestamp"]

    return {
        "status": "reordered_detected" if reordered else _OK,
        "reordered": reordered,
        "reordered_count": len(reordered),
    }


# ---------------------------------------------------------------------------
# Freshness / ingestion lag (document line 151)
# ---------------------------------------------------------------------------

def freshness_report(
    events: Iterable[Any],
    *,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Report the newest ingestion timestamp and the lag behind ``now``.

    An empty history yields ``unavailable`` freshness and a ``None`` lag --
    never a fabricated ``0``-second lag.
    """
    moment = now or datetime.now(timezone.utc)
    records = _as_records(events)
    ingestions = [
        ts for ts in (_parse_ts(r.get("ingestion_timestamp")) for r in records) if ts
    ]
    if not ingestions:
        return {
            "status": UNAVAILABLE,
            "latest_ingestion_timestamp": None,
            "ingestion_lag_seconds": None,
        }
    latest = max(ingestions)
    return {
        "status": "fresh",
        "latest_ingestion_timestamp": latest.isoformat(),
        "ingestion_lag_seconds": round((moment - latest).total_seconds(), 3),
    }


# ---------------------------------------------------------------------------
# Cost honesty (document line 154)
# ---------------------------------------------------------------------------

def cost_breakdown(
    *,
    estimated: Any = None,
    provider_reported: Any = None,
    invoice_confirmed: Any = None,
) -> Dict[str, Any]:
    """Keep estimated / provider-reported / invoice-confirmed costs distinct.

    Each tier is reported independently; an absent tier is ``unavailable``
    (``None``), never folded into another tier and never zero-filled.
    """
    return {
        "estimated": cost_or_unavailable(estimated),
        "provider_reported": cost_or_unavailable(provider_reported),
        "invoice_confirmed": cost_or_unavailable(invoice_confirmed),
    }


# ---------------------------------------------------------------------------
# Audience honesty (document line 158)
# ---------------------------------------------------------------------------

def audience_metrics(
    *,
    retention: Any = None,
    conversion: Any = None,
) -> Dict[str, Any]:
    """Report audience retention/conversion only when genuinely recorded.

    The video pipeline records no audience telemetry, so the honest default is
    ``unavailable``.  This function never derives retention/conversion from
    production telemetry.
    """
    return {
        "retention": cost_or_unavailable(retention) if _is_real_number(retention)
        else {"value": None, "status": UNAVAILABLE},
        "conversion": cost_or_unavailable(conversion) if _is_real_number(conversion)
        else {"value": None, "status": UNAVAILABLE},
        "source": "not_recorded",
        "note": "Audience retention/conversion is not captured by this pipeline.",
    }


# ---------------------------------------------------------------------------
# Separated quality signals (Stage E7 / document line 156)
# ---------------------------------------------------------------------------

def separate_quality_signals(
    *,
    ai_quality_score: Any = None,
    human_approved: Any = None,
    undo_count: Any = None,
) -> Dict[str, Any]:
    """Keep AI quality, human approval and undo as three distinct signals.

    They must never be merged into a single score: an AI score is a model
    opinion, human approval is an explicit decision, and undo is a behavioural
    correction.  Each is independently ``unavailable`` when not recorded.
    """
    ai = number_or_none(ai_quality_score)
    approved = human_approved if isinstance(human_approved, bool) else None
    undo = int(undo_count) if _is_real_number(undo_count) else None
    return {
        "ai_quality_score": {"value": ai, "status": "reported" if ai is not None else UNAVAILABLE},
        "human_approved": {"value": approved, "status": "reported" if approved is not None else UNAVAILABLE},
        "undo_count": {"value": undo, "status": "reported" if undo is not None else UNAVAILABLE},
    }


# ---------------------------------------------------------------------------
# Three-way reconciliation (document line 152)
# ---------------------------------------------------------------------------

def _totals(records: Iterable[Any], token_field: str, cost_field: str) -> Dict[str, Any]:
    tokens: Optional[float] = None
    cost: Optional[float] = None
    count = 0
    for record in _as_records(records):
        count += 1
        token_value = number_or_none(record.get(token_field))
        if token_value is not None:
            tokens = (tokens or 0.0) + token_value
        cost_value = number_or_none(record.get(cost_field))
        if cost_value is not None:
            cost = (cost or 0.0) + cost_value
    return {"records": count, "total_tokens": tokens, "total_cost_usd": cost}


def reconcile_sources(
    *,
    job_records: Sequence[Any] = (),
    provider_usage: Sequence[Any] = (),
    clickhouse_aggregates: Sequence[Any] = (),
    tolerance: float = 1e-6,
) -> Dict[str, Any]:
    """Compare canonical jobs vs provider usage vs ClickHouse aggregates.

    Missing sources are reported as ``unavailable`` rather than assumed zero,
    and a source that is entirely absent does not create a false discrepancy.
    """
    jobs = _totals(job_records, "total_tokens_used", "cost_usd")
    provider = _totals(provider_usage, "total_tokens", "cost_usd")
    cloud = _totals(clickhouse_aggregates, "total_tokens_used", "cost_usd")

    discrepancies: List[Dict[str, Any]] = []

    def _compare(label: str, left: Optional[float], right: Optional[float], sources: tuple) -> None:
        if left is None or right is None:
            return  # unknown is not a disagreement
        if abs(left - right) > tolerance:
            discrepancies.append(
                {
                    "metric": label,
                    "sources": list(sources),
                    "values": [left, right],
                    "delta": round(left - right, 6),
                }
            )

    _compare("total_tokens", jobs["total_tokens"], provider["total_tokens"], ("jobs", "provider"))
    _compare("total_tokens", jobs["total_tokens"], cloud["total_tokens"], ("jobs", "clickhouse"))
    _compare("total_cost_usd", jobs["total_cost_usd"], cloud["total_cost_usd"], ("jobs", "clickhouse"))

    return {
        "status": "discrepancies_detected" if discrepancies else _OK,
        "sources": {
            "canonical_jobs": jobs,
            "provider_usage": provider,
            "clickhouse_aggregates": cloud,
        },
        "discrepancies": discrepancies,
        "discrepancy_count": len(discrepancies),
    }


def completeness_report(
    source_events: Mapping[str, Iterable[Any]],
    *,
    canonical_name: str = "canonical",
) -> Dict[str, Any]:
    """Compare event identity sets across canonical, outbox and cloud sources.

    Event counts alone cannot reveal a replay gap: a duplicate can make a
    count look healthy while a different event is missing.  This report uses
    the stable ``event_id`` as the cross-source key, records duplicates
    separately, and returns ``unavailable`` when a source cannot expose event
    identities rather than treating it as an empty source.
    """
    materialised: Dict[str, List[Dict[str, Any]]] = {
        str(name): _as_records(events)
        for name, events in source_events.items()
    }

    def _ids(records: Sequence[Dict[str, Any]]) -> tuple[set[str], List[str]]:
        ids = [
            str(record.get("event_id"))
            for record in records
            if isinstance(record.get("event_id"), str) and record.get("event_id")
        ]
        unique = set(ids)
        duplicates = sorted({event_id for event_id in ids if ids.count(event_id) > 1})
        return unique, duplicates

    per_source: Dict[str, Dict[str, Any]] = {}
    for name, records in materialised.items():
        ids, duplicates = _ids(records)
        per_source[name] = {
            "records": len(records),
            "identified_records": len(ids),
            "missing_identity_count": len(records) - len(ids),
            "event_ids": sorted(ids),
            "duplicates": duplicates,
            "status": UNAVAILABLE if records and len(ids) != len(records) else _OK,
        }

    canonical_records = materialised.get(canonical_name)
    if canonical_records is None:
        return {
            "status": UNAVAILABLE,
            "canonical_source": canonical_name,
            "sources": per_source,
            "missing": {},
            "extra": {},
            "duplicates": {
                name: details["duplicates"]
                for name, details in per_source.items()
                if details["duplicates"]
            },
        }

    canonical_ids, canonical_duplicates = _ids(canonical_records)
    missing: Dict[str, List[str]] = {}
    extra: Dict[str, List[str]] = {}
    duplicates: Dict[str, List[str]] = {}
    for name, details in per_source.items():
        if name == canonical_name:
            continue
        source_ids = set(details["event_ids"])
        missing_ids = sorted(canonical_ids - source_ids)
        extra_ids = sorted(source_ids - canonical_ids)
        if missing_ids:
            missing[name] = missing_ids
        if extra_ids:
            extra[name] = extra_ids
        if details["duplicates"]:
            duplicates[name] = list(details["duplicates"])

    if canonical_duplicates:
        duplicates[canonical_name] = canonical_duplicates
    status = "incomplete" if missing or extra or duplicates else _OK
    return {
        "status": status,
        "canonical_source": canonical_name,
        "sources": per_source,
        "missing": missing,
        "extra": extra,
        "duplicates": duplicates,
    }


# ---------------------------------------------------------------------------
# Combined report
# ---------------------------------------------------------------------------

def build_reconciliation_report(
    events: Iterable[Any],
    *,
    job_records: Sequence[Any] = (),
    provider_usage: Sequence[Any] = (),
    clickhouse_aggregates: Sequence[Any] = (),
    now: Optional[datetime] = None,
    stream_key: str = "stream",
    source_events: Mapping[str, Iterable[Any]] | None = None,
) -> Dict[str, Any]:
    """One honest reconciliation bundle for the analytics surface."""
    materialised = _as_records(events)
    completeness_sources = source_events or {
        "canonical": materialised,
    }
    return {
        "integrity": {
            "missing": detect_missing_events(materialised, stream_key=stream_key),
            "duplicates": detect_duplicate_events(materialised),
            "reordered": detect_reordered_events(materialised),
        },
        "freshness": freshness_report(materialised, now=now),
        "reconciliation": reconcile_sources(
            job_records=job_records,
            provider_usage=provider_usage,
            clickhouse_aggregates=clickhouse_aggregates,
        ),
        "audience": audience_metrics(),
        "completeness": completeness_report(completeness_sources),
        "event_count": len(materialised),
    }


def report_is_clean(report: Dict[str, Any]) -> bool:
    """True when a reconciliation report shows no integrity/reconcile issues."""
    integrity = report.get("integrity", {})
    reconciliation = report.get("reconciliation", {})
    return (
        integrity.get("missing", {}).get("status") == _OK
        and integrity.get("duplicates", {}).get("status") == _OK
        and integrity.get("reordered", {}).get("status") == _OK
        and reconciliation.get("status") == _OK
        and report.get("completeness", {}).get("status", _OK) == _OK
    )


__all__ = [
    "UNAVAILABLE",
    "number_or_none",
    "cost_or_unavailable",
    "detect_missing_events",
    "detect_duplicate_events",
    "detect_reordered_events",
    "freshness_report",
    "cost_breakdown",
    "audience_metrics",
    "separate_quality_signals",
    "reconcile_sources",
    "completeness_report",
    "build_reconciliation_report",
    "report_is_clean",
]
