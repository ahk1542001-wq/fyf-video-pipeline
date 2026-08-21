"""Tests for cost estimation catalog."""

from backend.cost_catalog import CATALOG_VERSION, estimate_job_cost


def test_estimate_job_cost_known_flash_model():
    est = estimate_job_cost("gemini-3.7-flash", input_tokens=10_000, output_tokens=2_000, tts_characters=500)
    assert est.catalog_version == CATALOG_VERSION
    assert est.cost_status == "estimated"
    assert est.is_estimate is True
    assert est.estimated_cost_usd > 0.0
    assert est.model_cost_usd > 0.0
    assert est.tts_cost_usd > 0.0


def test_estimate_job_cost_unknown_model():
    est = estimate_job_cost("some-obscure-model-v99", input_tokens=10_000, output_tokens=2_000)
    assert est.cost_status == "unknown"
    assert est.estimated_cost_usd == 0.0
