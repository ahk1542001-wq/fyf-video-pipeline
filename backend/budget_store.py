"""Budget Ledger and Cost Cap Tracking for Public Pipeline Protection."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.job_store import write_json_atomically

logger = logging.getLogger(__name__)

DEFAULT_DAILY_BUDGET_CAP_USD = 10.0
DEFAULT_TOTAL_BUDGET_CAP_USD = 50.0


def _get_budget_file(root_dir: Path | None = None) -> Path:
    base = root_dir or Path(os.getenv("FYF_JOBS_ROOT", "jobs"))
    base.mkdir(parents=True, exist_ok=True)
    return base / ".budget_ledger.json"


def _read_budget_ledger(root_dir: Path | None = None) -> dict[str, Any]:
    budget_file = _get_budget_file(root_dir)
    if not budget_file.exists():
        return {"total_spend_usd": 0.0, "daily_spend": {}, "last_updated": None}
    try:
        return json.loads(budget_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"total_spend_usd": 0.0, "daily_spend": {}, "last_updated": None}


def record_cost(cost_usd: float, root_dir: Path | None = None) -> dict[str, Any]:
    """Record an estimated charge in the budget ledger."""
    if cost_usd <= 0:
        return _read_budget_ledger(root_dir)

    ledger = _read_budget_ledger(root_dir)
    today_key = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    current_total = float(ledger.get("total_spend_usd", 0.0)) + cost_usd
    daily_map = ledger.get("daily_spend", {})
    daily_map[today_key] = float(daily_map.get(today_key, 0.0)) + cost_usd

    updated = {
        "total_spend_usd": round(current_total, 4),
        "daily_spend": daily_map,
        "last_updated": datetime.now(timezone.utc).isoformat(),
    }
    budget_file = _get_budget_file(root_dir)
    write_json_atomically(budget_file, updated)
    return updated


def get_budget_status(root_dir: Path | None = None) -> dict[str, Any]:
    """Return the current budget consumption and configured caps."""
    ledger = _read_budget_ledger(root_dir)
    today_key = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    daily_cap = float(os.getenv("FYF_DAILY_BUDGET_CAP_USD", str(DEFAULT_DAILY_BUDGET_CAP_USD)))
    total_cap = float(os.getenv("FYF_TOTAL_BUDGET_CAP_USD", str(DEFAULT_TOTAL_BUDGET_CAP_USD)))

    today_spend = float(ledger.get("daily_spend", {}).get(today_key, 0.0))
    total_spend = float(ledger.get("total_spend_usd", 0.0))

    daily_exceeded = today_spend >= daily_cap
    total_exceeded = total_spend >= total_cap
    exceeded = daily_exceeded or total_exceeded

    return {
        "daily_spend_usd": round(today_spend, 4),
        "daily_cap_usd": daily_cap,
        "total_spend_usd": round(total_spend, 4),
        "total_cap_usd": total_cap,
        "budget_exceeded": exceeded,
        "reason": (
            "Daily budget cap exceeded" if daily_exceeded
            else "Total budget cap exceeded" if total_exceeded
            else None
        ),
    }


def is_budget_available(estimated_charge_usd: float = 0.05, root_dir: Path | None = None) -> bool:
    """Check if budget is available for an upcoming operation."""
    status = get_budget_status(root_dir)
    if status["budget_exceeded"]:
        return False
    # Check if this charge would breach caps
    if (status["daily_spend_usd"] + estimated_charge_usd) > status["daily_cap_usd"]:
        return False
    if (status["total_spend_usd"] + estimated_charge_usd) > status["total_cap_usd"]:
        return False
    return True
