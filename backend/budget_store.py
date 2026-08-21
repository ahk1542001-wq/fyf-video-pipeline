"""Budget Ledger and Cost Cap Tracking with Atomic Reservation & Reconciliation (Fail-Closed)."""

from __future__ import annotations

import json
import logging
import math
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.job_store import write_json_atomically

logger = logging.getLogger(__name__)

DEFAULT_DAILY_BUDGET_CAP_USD = 10.0
DEFAULT_TOTAL_BUDGET_CAP_USD = 50.0

_BUDGET_LOCK = threading.Lock()


def get_canonical_budget_file(root_dir: Path | None = None) -> Path:
    """Return the single canonical budget ledger path."""
    if root_dir is not None:
        base = Path(root_dir)
        base.mkdir(parents=True, exist_ok=True)
        return base / ".budget_ledger.json"

    env_override = os.getenv("FYF_BUDGET_LEDGER_PATH")
    if env_override:
        path = Path(env_override)
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    output_dir = Path(__file__).resolve().parent.parent / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir / ".budget_ledger.json"


def _is_valid_number(val: Any) -> bool:
    if not isinstance(val, (int, float)) or isinstance(val, bool):
        return False
    return math.isfinite(val) and val >= 0.0


def _read_budget_ledger(root_dir: Path | None = None) -> dict[str, Any]:
    budget_file = get_canonical_budget_file(root_dir)
    if not budget_file.exists():
        return {
            "total_spend_usd": 0.0,
            "daily_spend": {},
            "active_reservations": {},
            "reconciled_operations": {},
            "last_updated": None,
            "corrupted": False,
        }
    try:
        data = json.loads(budget_file.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("Budget ledger must be a dictionary")

        total_spend = data.get("total_spend_usd", 0.0)
        if not _is_valid_number(total_spend):
            raise ValueError(f"Invalid total_spend_usd in ledger: {total_spend}")

        daily_spend = data.get("daily_spend", {})
        if not isinstance(daily_spend, dict) or not all(_is_valid_number(v) for v in daily_spend.values()):
            raise ValueError("Invalid daily_spend map in ledger")

        reservations = data.get("active_reservations", {})
        if not isinstance(reservations, dict):
            data["active_reservations"] = {}

        reconciled = data.get("reconciled_operations", {})
        if not isinstance(reconciled, dict):
            data["reconciled_operations"] = {}

        data["corrupted"] = False
        return data
    except Exception as exc:
        logger.error("Budget ledger corrupted at %s: %s (failing closed)", budget_file, exc)
        return {
            "total_spend_usd": float("inf"),
            "daily_spend": {},
            "active_reservations": {},
            "reconciled_operations": {},
            "last_updated": None,
            "corrupted": True,
            "error": str(exc),
        }


def get_budget_status(root_dir: Path | None = None) -> dict[str, Any]:
    """Return the current budget consumption, reservations, and configured caps."""
    with _BUDGET_LOCK:
        ledger = _read_budget_ledger(root_dir)

    if ledger.get("corrupted"):
        return {
            "daily_spend_usd": 0.0,
            "daily_cap_usd": 0.0,
            "total_spend_usd": float("inf"),
            "total_cap_usd": 0.0,
            "active_reserved_usd": 0.0,
            "budget_exceeded": True,
            "reason": "Budget ledger corrupted (fail-closed protection active)",
        }

    today_key = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    daily_cap = float(os.getenv("FYF_DAILY_BUDGET_CAP_USD", str(DEFAULT_DAILY_BUDGET_CAP_USD)))
    total_cap = float(os.getenv("FYF_TOTAL_BUDGET_CAP_USD", str(DEFAULT_TOTAL_BUDGET_CAP_USD)))

    today_spend = float(ledger.get("daily_spend", {}).get(today_key, 0.0))
    total_spend = float(ledger.get("total_spend_usd", 0.0))

    reservations = ledger.get("active_reservations", {})
    active_reserved_today = sum(
        float(r.get("amount_usd", 0.0))
        for r in reservations.values()
        if isinstance(r, dict) and r.get("date") == today_key
    )
    active_reserved_total = sum(
        float(r.get("amount_usd", 0.0))
        for r in reservations.values()
        if isinstance(r, dict)
    )

    daily_exceeded = (today_spend + active_reserved_today) >= daily_cap
    total_exceeded = (total_spend + active_reserved_total) >= total_cap
    exceeded = daily_exceeded or total_exceeded

    return {
        "daily_spend_usd": round(today_spend, 4),
        "daily_reserved_usd": round(active_reserved_today, 4),
        "daily_cap_usd": daily_cap,
        "total_spend_usd": round(total_spend, 4),
        "total_reserved_usd": round(active_reserved_total, 4),
        "total_cap_usd": total_cap,
        "active_reserved_usd": round(active_reserved_total, 4),
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
    if (status["daily_spend_usd"] + status.get("daily_reserved_usd", 0.0) + estimated_charge_usd) > status["daily_cap_usd"]:
        return False
    if (status["total_spend_usd"] + status.get("total_reserved_usd", 0.0) + estimated_charge_usd) > status["total_cap_usd"]:
        return False
    return True


def reserve_budget(
    operation_id: str,
    estimated_usd: float = 0.05,
    root_dir: Path | None = None,
) -> tuple[bool, str | None]:
    """Atomically reserve estimated cost before queuing or starting paid work."""
    if estimated_usd <= 0.0:
        return True, None

    with _BUDGET_LOCK:
        ledger = _read_budget_ledger(root_dir)
        if ledger.get("corrupted"):
            return False, "Budget ledger corrupted (fail-closed)"

        today_key = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        daily_cap = float(os.getenv("FYF_DAILY_BUDGET_CAP_USD", str(DEFAULT_DAILY_BUDGET_CAP_USD)))
        total_cap = float(os.getenv("FYF_TOTAL_BUDGET_CAP_USD", str(DEFAULT_TOTAL_BUDGET_CAP_USD)))

        today_spend = float(ledger.get("daily_spend", {}).get(today_key, 0.0))
        total_spend = float(ledger.get("total_spend_usd", 0.0))
        reservations = ledger.get("active_reservations", {})

        # If this operation already holds a reservation, don't double count
        if operation_id in reservations:
            return True, None

        reserved_today = sum(
            float(r.get("amount_usd", 0.0))
            for r in reservations.values()
            if isinstance(r, dict) and r.get("date") == today_key
        )
        reserved_total = sum(
            float(r.get("amount_usd", 0.0))
            for r in reservations.values()
            if isinstance(r, dict)
        )

        if (today_spend + reserved_today + estimated_usd) > daily_cap:
            return False, f"Daily budget cap exceeded ({today_spend + reserved_today:.4f} + {estimated_usd:.4f} > {daily_cap:.4f} USD)"

        if (total_spend + reserved_total + estimated_usd) > total_cap:
            return False, f"Total budget cap exceeded ({total_spend + reserved_total:.4f} + {estimated_usd:.4f} > {total_cap:.4f} USD)"

        reservations[operation_id] = {
            "amount_usd": round(estimated_usd, 6),
            "date": today_key,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        ledger["active_reservations"] = reservations
        ledger["last_updated"] = datetime.now(timezone.utc).isoformat()

        budget_file = get_canonical_budget_file(root_dir)
        write_json_atomically(budget_file, ledger)
        return True, None


def reconcile_budget(
    operation_id: str,
    actual_usd: float,
    outcome: str = "completed",
    root_dir: Path | None = None,
) -> dict[str, Any]:
    """Atomically reconcile a budget reservation with actual measured provider spend."""
    with _BUDGET_LOCK:
        ledger = _read_budget_ledger(root_dir)
        if ledger.get("corrupted"):
            return ledger

        reconciled = ledger.get("reconciled_operations", {})
        # Idempotency guard: prevent duplicate debiting
        if operation_id in reconciled:
            logger.info("Operation %s was already reconciled, skipping duplicate debit", operation_id)
            return ledger

        reservations = ledger.get("active_reservations", {})
        reservations.pop(operation_id, None)
        ledger["active_reservations"] = reservations

        if actual_usd > 0.0 and outcome != "cancelled":
            today_key = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            total_spend = float(ledger.get("total_spend_usd", 0.0)) + actual_usd
            daily_spend = ledger.get("daily_spend", {})
            daily_spend[today_key] = float(daily_spend.get(today_key, 0.0)) + actual_usd

            ledger["total_spend_usd"] = round(total_spend, 6)
            ledger["daily_spend"] = daily_spend

        reconciled[operation_id] = {
            "actual_usd": round(actual_usd, 6),
            "outcome": outcome,
            "reconciled_at": datetime.now(timezone.utc).isoformat(),
        }
        ledger["reconciled_operations"] = reconciled
        ledger["last_updated"] = datetime.now(timezone.utc).isoformat()

        budget_file = get_canonical_budget_file(root_dir)
        write_json_atomically(budget_file, ledger)
        return ledger


def release_reservation(
    operation_id: str,
    root_dir: Path | None = None,
) -> dict[str, Any]:
    """Release a reservation without debiting spend."""
    with _BUDGET_LOCK:
        ledger = _read_budget_ledger(root_dir)
        if ledger.get("corrupted"):
            return ledger

        reservations = ledger.get("active_reservations", {})
        if operation_id in reservations:
            del reservations[operation_id]
            ledger["active_reservations"] = reservations
            ledger["last_updated"] = datetime.now(timezone.utc).isoformat()
            budget_file = get_canonical_budget_file(root_dir)
            write_json_atomically(budget_file, ledger)
        return ledger


def record_cost(cost_usd: float, root_dir: Path | None = None) -> dict[str, Any]:
    """Direct record helper for backward-compatible call points."""
    if cost_usd <= 0:
        return _read_budget_ledger(root_dir)

    with _BUDGET_LOCK:
        ledger = _read_budget_ledger(root_dir)
        if ledger.get("corrupted"):
            return ledger

        today_key = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        current_total = float(ledger.get("total_spend_usd", 0.0)) + cost_usd
        daily_map = ledger.get("daily_spend", {})
        daily_map[today_key] = float(daily_map.get(today_key, 0.0)) + cost_usd

        ledger["total_spend_usd"] = round(current_total, 4)
        ledger["daily_spend"] = daily_map
        ledger["last_updated"] = datetime.now(timezone.utc).isoformat()

        budget_file = get_canonical_budget_file(root_dir)
        write_json_atomically(budget_file, ledger)
        return ledger
