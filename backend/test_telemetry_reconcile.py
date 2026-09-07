"""Tests for telemetry reconciliation and honesty guards (Stage E4 / E7).

Covers document line 152 (three-way reconcile), line 153 (missing / duplicate /
reorder detection + "unknown is never 0"), line 154 (distinct cost tiers),
line 156 (separated quality signals) and line 158 (audience never invented).
"""

from __future__ import annotations

from datetime import datetime, timezone

from backend.telemetry_reconcile import (
    UNAVAILABLE,
    audience_metrics,
    build_reconciliation_report,
    cost_breakdown,
    cost_or_unavailable,
    detect_duplicate_events,
    detect_missing_events,
    detect_reordered_events,
    freshness_report,
    number_or_none,
    reconcile_sources,
    report_is_clean,
    separate_quality_signals,
)


# ---------------------------------------------------------------------------
# Honesty: unknown is never 0
# ---------------------------------------------------------------------------

def test_number_or_none_never_coerces_unknown_to_zero():
    assert number_or_none(3.5) == 3.5
    assert number_or_none(0) == 0.0
    assert number_or_none(None) is None
    assert number_or_none("12") is None  # a non-number stays unknown, not 0
    assert number_or_none(True) is None  # bool is not a real number


def test_cost_or_unavailable_reports_unavailable_not_zero():
    unknown = cost_or_unavailable(None)
    assert unknown == {"value": None, "status": UNAVAILABLE}
    assert unknown["value"] is None  # NEVER 0
    reported = cost_or_unavailable(0.0)
    assert reported == {"value": 0.0, "status": "reported"}  # a real 0 is real


# ---------------------------------------------------------------------------
# Integrity detection (document line 153)
# ---------------------------------------------------------------------------

def test_detect_missing_events_finds_sequence_gaps():
    events = [
        {"event_id": "a", "sequence": 0},
        {"event_id": "b", "sequence": 1},
        {"event_id": "d", "sequence": 3},  # sequence 2 is missing
    ]
    result = detect_missing_events(events)
    assert result["status"] == "gaps_detected"
    assert result["missing_count"] == 1
    assert result["missing"] == [{"stream": "__global__", "sequence": 2}]


def test_detect_missing_events_clean_when_contiguous():
    events = [{"event_id": str(n), "sequence": n} for n in range(5)]
    result = detect_missing_events(events)
    assert result["status"] == "ok"
    assert result["missing_count"] == 0


def test_detect_missing_events_is_per_stream():
    events = [
        {"event_id": "a", "sequence": 0, "stream": "x"},
        {"event_id": "b", "sequence": 2, "stream": "x"},  # gap in stream x
        {"event_id": "c", "sequence": 0, "stream": "y"},
        {"event_id": "d", "sequence": 1, "stream": "y"},  # stream y is clean
    ]
    result = detect_missing_events(events)
    assert result["streams_checked"] == 2
    assert result["missing"] == [{"stream": "x", "sequence": 1}]


def test_detect_duplicate_events_by_event_id():
    events = [
        {"event_id": "dup"},
        {"event_id": "dup"},
        {"event_id": "unique"},
    ]
    result = detect_duplicate_events(events)
    assert result["status"] == "duplicates_detected"
    assert result["duplicate_count"] == 1
    assert result["duplicates"] == [{"event_id": "dup", "count": 2}]


def test_detect_duplicate_events_clean():
    events = [{"event_id": "a"}, {"event_id": "b"}]
    assert detect_duplicate_events(events)["status"] == "ok"


def test_detect_reordered_events_via_timestamps():
    events = [
        {"event_id": "a", "event_timestamp": "2026-01-01T00:00:05+00:00",
         "ingestion_timestamp": "2026-01-01T00:00:01+00:00"},
        # b is ingested after a but was produced BEFORE a -> late arrival
        {"event_id": "b", "event_timestamp": "2026-01-01T00:00:02+00:00",
         "ingestion_timestamp": "2026-01-01T00:00:06+00:00"},
    ]
    result = detect_reordered_events(events)
    assert result["status"] == "reordered_detected"
    assert result["reordered_count"] == 1
    assert result["reordered"][0]["event_id"] == "b"


def test_detect_reordered_events_clean_when_in_order():
    events = [
        {"event_id": "a", "event_timestamp": "2026-01-01T00:00:01+00:00",
         "ingestion_timestamp": "2026-01-01T00:00:01+00:00"},
        {"event_id": "b", "event_timestamp": "2026-01-01T00:00:02+00:00",
         "ingestion_timestamp": "2026-01-01T00:00:02+00:00"},
    ]
    assert detect_reordered_events(events)["status"] == "ok"


# ---------------------------------------------------------------------------
# Freshness (document line 151)
# ---------------------------------------------------------------------------

def test_freshness_is_unavailable_and_lag_none_when_empty():
    result = freshness_report([])
    assert result["status"] == UNAVAILABLE
    assert result["ingestion_lag_seconds"] is None  # never a fabricated 0
    assert result["latest_ingestion_timestamp"] is None


def test_freshness_reports_lag_against_now():
    events = [{"ingestion_timestamp": "2026-01-01T00:00:00+00:00"}]
    now = datetime(2026, 1, 1, 0, 0, 10, tzinfo=timezone.utc)
    result = freshness_report(events, now=now)
    assert result["status"] == "fresh"
    assert result["ingestion_lag_seconds"] == 10.0


# ---------------------------------------------------------------------------
# Cost tiers stay distinct (document line 154)
# ---------------------------------------------------------------------------

def test_cost_breakdown_keeps_tiers_distinct_and_honest():
    result = cost_breakdown(estimated=0.02, provider_reported=0.018)
    assert result["estimated"] == {"value": 0.02, "status": "reported"}
    assert result["provider_reported"] == {"value": 0.018, "status": "reported"}
    # invoice not confirmed -> unavailable, never 0, never folded into estimate
    assert result["invoice_confirmed"] == {"value": None, "status": UNAVAILABLE}


# ---------------------------------------------------------------------------
# Audience never invented (document line 158)
# ---------------------------------------------------------------------------

def test_audience_metrics_default_to_unavailable_not_invented():
    result = audience_metrics()
    assert result["retention"]["status"] == UNAVAILABLE
    assert result["conversion"]["status"] == UNAVAILABLE
    assert result["retention"]["value"] is None
    assert result["source"] == "not_recorded"


# ---------------------------------------------------------------------------
# Separated quality signals (Stage E7 / document line 156)
# ---------------------------------------------------------------------------

def test_separate_quality_signals_are_three_distinct_fields():
    result = separate_quality_signals(
        ai_quality_score=0.91, human_approved=True, undo_count=2
    )
    assert set(result) == {"ai_quality_score", "human_approved", "undo_count"}
    assert result["ai_quality_score"] == {"value": 0.91, "status": "reported"}
    assert result["human_approved"] == {"value": True, "status": "reported"}
    assert result["undo_count"] == {"value": 2, "status": "reported"}


def test_separate_quality_signals_each_unavailable_independently():
    result = separate_quality_signals(ai_quality_score=0.5)
    assert result["ai_quality_score"]["status"] == "reported"
    assert result["human_approved"] == {"value": None, "status": UNAVAILABLE}
    assert result["undo_count"] == {"value": None, "status": UNAVAILABLE}


def test_quality_signals_are_never_collapsed_into_one_number():
    result = separate_quality_signals(
        ai_quality_score=0.1, human_approved=True, undo_count=9
    )
    # A high undo + low AI score + human approval must remain independently
    # visible; no single blended "quality" key is produced.
    assert "quality" not in result
    assert result["undo_count"]["value"] == 9
    assert result["ai_quality_score"]["value"] == 0.1
    assert result["human_approved"]["value"] is True


# ---------------------------------------------------------------------------
# Three-way reconciliation (document line 152)
# ---------------------------------------------------------------------------

def test_reconcile_sources_agrees_when_consistent():
    result = reconcile_sources(
        job_records=[{"total_tokens_used": 100, "cost_usd": 0.01}],
        provider_usage=[{"total_tokens": 100, "cost_usd": 0.01}],
        clickhouse_aggregates=[{"total_tokens_used": 100, "cost_usd": 0.01}],
    )
    assert result["status"] == "ok"
    assert result["discrepancy_count"] == 0


def test_reconcile_sources_flags_discrepancy():
    result = reconcile_sources(
        job_records=[{"total_tokens_used": 100, "cost_usd": 0.01}],
        provider_usage=[{"total_tokens": 120, "cost_usd": 0.01}],
        clickhouse_aggregates=[{"total_tokens_used": 100, "cost_usd": 0.01}],
    )
    assert result["status"] == "discrepancies_detected"
    assert result["discrepancy_count"] >= 1
    metrics = [d["metric"] for d in result["discrepancies"]]
    assert "total_tokens" in metrics


def test_reconcile_missing_source_is_not_a_false_disagreement():
    # No provider usage at all -> unknown, must not be treated as 0 and flagged.
    result = reconcile_sources(
        job_records=[{"total_tokens_used": 100, "cost_usd": 0.01}],
        provider_usage=[],
        clickhouse_aggregates=[],
    )
    assert result["status"] == "ok"
    assert result["discrepancy_count"] == 0
    assert result["sources"]["provider_usage"]["total_tokens"] is None


# ---------------------------------------------------------------------------
# Combined report
# ---------------------------------------------------------------------------

def test_build_reconciliation_report_is_clean_for_consistent_history():
    events = [
        {"event_id": "a", "sequence": 0,
         "event_timestamp": "2026-01-01T00:00:01+00:00",
         "ingestion_timestamp": "2026-01-01T00:00:01+00:00"},
        {"event_id": "b", "sequence": 1,
         "event_timestamp": "2026-01-01T00:00:02+00:00",
         "ingestion_timestamp": "2026-01-01T00:00:02+00:00"},
    ]
    report = build_reconciliation_report(
        events,
        job_records=[{"total_tokens_used": 10, "cost_usd": 0.1}],
        provider_usage=[{"total_tokens": 10, "cost_usd": 0.1}],
        clickhouse_aggregates=[{"total_tokens_used": 10, "cost_usd": 0.1}],
    )
    assert report["event_count"] == 2
    assert report_is_clean(report) is True
    assert report["audience"]["retention"]["status"] == UNAVAILABLE


def test_build_reconciliation_report_surfaces_injected_defects():
    events = [
        {"event_id": "a", "sequence": 0,
         "event_timestamp": "2026-01-01T00:00:01+00:00",
         "ingestion_timestamp": "2026-01-01T00:00:01+00:00"},
        {"event_id": "a", "sequence": 0,  # duplicate event_id
         "event_timestamp": "2026-01-01T00:00:01+00:00",
         "ingestion_timestamp": "2026-01-01T00:00:01+00:00"},
        {"event_id": "c", "sequence": 2,  # sequence 1 missing
         "event_timestamp": "2026-01-01T00:00:00+00:00",  # produced earlier
         "ingestion_timestamp": "2026-01-01T00:00:09+00:00"},
    ]
    report = build_reconciliation_report(events)
    assert report_is_clean(report) is False
    assert report["integrity"]["duplicates"]["duplicate_count"] == 1
    assert report["integrity"]["missing"]["missing_count"] == 1
    assert report["integrity"]["reordered"]["reordered_count"] >= 1
