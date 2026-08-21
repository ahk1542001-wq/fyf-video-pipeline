"""Estimated pricing catalog for Gemini models and TTS in Google Cloud."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Literal, Optional

CATALOG_VERSION = "2026-08-21"

# Prices per 1,000,000 tokens (USD)
# Vertex AI / Gemini estimated pricing rates
MODEL_PRICING: Dict[str, Dict[str, float]] = {
    "gemini-3.7-flash": {
        "input_per_million": 0.075,
        "output_per_million": 0.30,
    },
    "gemini-3.6-flash": {
        "input_per_million": 0.075,
        "output_per_million": 0.30,
    },
    "gemini-2.5-flash": {
        "input_per_million": 0.075,
        "output_per_million": 0.30,
    },
    "gemini-2.5-flash-preview-tts": {
        "input_per_million": 0.075,
        "output_per_million": 0.30,
    },
    "gemini-3.1-pro-preview": {
        "input_per_million": 1.25,
        "output_per_million": 5.00,
    },
    "gemini-3.1-pro": {
        "input_per_million": 1.25,
        "output_per_million": 5.00,
    },
}

# Gemini TTS cost per 1,000,000 characters (USD estimate)
TTS_CHARACTER_PER_MILLION = 16.0


@dataclass(frozen=True)
class CostEstimate:
    catalog_version: str
    cost_status: Literal["estimated", "unknown"]
    estimated_cost_usd: float
    model_cost_usd: float
    tts_cost_usd: float
    currency: str = "USD"
    is_estimate: bool = True


def estimate_job_cost(
    model_name: str,
    input_tokens: int,
    output_tokens: int,
    tts_characters: int = 0,
) -> CostEstimate:
    """Calculate estimated cost in USD based on token counts and TTS characters."""
    clean_model = (model_name or "").lower().strip()
    pricing = None

    for key, price_map in MODEL_PRICING.items():
        if key in clean_model or clean_model in key:
            pricing = price_map
            break

    if pricing is None and not clean_model:
        # Default to Flash pricing if standard
        pricing = MODEL_PRICING["gemini-3.7-flash"]

    if pricing is None:
        return CostEstimate(
            catalog_version=CATALOG_VERSION,
            cost_status="unknown",
            estimated_cost_usd=0.0,
            model_cost_usd=0.0,
            tts_cost_usd=0.0,
        )

    model_cost = (
        (input_tokens / 1_000_000.0) * pricing["input_per_million"]
        + (output_tokens / 1_000_000.0) * pricing["output_per_million"]
    )
    tts_cost = (tts_characters / 1_000_000.0) * TTS_CHARACTER_PER_MILLION
    total_cost = round(model_cost + tts_cost, 6)

    return CostEstimate(
        catalog_version=CATALOG_VERSION,
        cost_status="estimated",
        estimated_cost_usd=total_cost,
        model_cost_usd=round(model_cost, 6),
        tts_cost_usd=round(tts_cost, 6),
    )
