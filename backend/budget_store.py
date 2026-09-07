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

# NOTE: There is deliberately NO silent numeric default for the account-level
# spending ceiling. A previous version fell back to $10/day and $50 total when the
# deployment did not configure FYF_DAILY_BUDGET_CAP_USD / FYF_TOTAL_BUDGET_CAP_USD,
# which silently permitted paid spend up to an invented cap (a fail-open ceiling).
# Per the cost-safety contract (document line 62) the ceiling must be configured
# explicitly; when it is absent, paid production is DISABLED (fail-closed).

# Per-project default budget (document line 56). This is a SEPARATE concept from
# the account-level spending ceiling and must never be conflated with it: the
# ceiling is the hard account guardrail, this is the default budget allocated to a
# single project when the caller does not specify one.
PROJECT_DEFAULT_BUDGET_USD = 3.0

# Honest, human-readable refusal / fail-closed reason strings surfaced to callers
# and the UI. Task 5 wires these into runtime_limits.acquire_guardrail_lease and
# the /api/runtime surface.
REASON_CEILING_UNCONFIGURED = (
    "paid production disabled: no explicit account spending ceiling configured "
    "(set both FYF_DAILY_BUDGET_CAP_USD and FYF_TOTAL_BUDGET_CAP_USD)"
)
REASON_CEILING_INVALID = "Invalid configured budget cap (fail-closed protection active)"
REASON_LEDGER_CORRUPTED = "Budget ledger corrupted (fail-closed protection active)"

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


def _account_ceiling() -> dict[str, Any]:
    """Resolve the account-level spending ceiling from deployment config.

    Fail-closed by design (document line 62): the ceiling must be configured
    explicitly via BOTH FYF_DAILY_BUDGET_CAP_USD and FYF_TOTAL_BUDGET_CAP_USD.
    There is no silent numeric default. Returns a dict:

      daily_cap_usd / total_cap_usd : float | None  (None => not configured)
      configured : bool   both env vars explicitly present
      valid      : bool   False => a configured value was malformed (fail-closed)
      paid_production_enabled : bool   True only when configured AND valid
      reason     : str | None   honest reason when disabled/invalid
    """
    daily_raw = os.getenv("FYF_DAILY_BUDGET_CAP_USD")
    total_raw = os.getenv("FYF_TOTAL_BUDGET_CAP_USD")
    daily_configured = daily_raw is not None and str(daily_raw).strip() != ""
    total_configured = total_raw is not None and str(total_raw).strip() != ""

    if not (daily_configured and total_configured):
        return {
            "daily_cap_usd": None,
            "total_cap_usd": None,
            "configured": False,
            "valid": True,
            "paid_production_enabled": False,
            "reason": REASON_CEILING_UNCONFIGURED,
        }

    caps: list[float] = []
    for raw in (daily_raw, total_raw):
        try:
            parsed = float(raw)
        except (ValueError, TypeError, OverflowError):
            parsed = None
        if parsed is None or not _is_valid_number(parsed):
            return {
                "daily_cap_usd": None,
                "total_cap_usd": None,
                "configured": True,
                "valid": False,
                "paid_production_enabled": False,
                "reason": REASON_CEILING_INVALID,
            }
        caps.append(parsed)

    daily_cap, total_cap = caps
    return {
        "daily_cap_usd": daily_cap,
        "total_cap_usd": total_cap,
        "configured": True,
        "valid": True,
        "paid_production_enabled": True,
        "reason": None,
    }


def get_project_default_budget_usd() -> float:
    """Per-project default budget (document line 56), SEPARATE from the account ceiling.

    Falls back to PROJECT_DEFAULT_BUDGET_USD ($3) when the optional
    FYF_PROJECT_DEFAULT_BUDGET_USD override is unset or malformed. This is a
    convenience default for a single project's budget, never the account guardrail.
    """
    raw = os.getenv("FYF_PROJECT_DEFAULT_BUDGET_USD")
    if raw is None or not str(raw).strip():
        return PROJECT_DEFAULT_BUDGET_USD
    try:
        val = float(raw)
    except (ValueError, TypeError, OverflowError):
        return PROJECT_DEFAULT_BUDGET_USD
    if not _is_valid_number(val):
        return PROJECT_DEFAULT_BUDGET_USD
    return val


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
            raise ValueError("active_reservations must be a dictionary")
        for res_id, res in reservations.items():
            if not isinstance(res, dict):
                raise ValueError(f"Reservation {res_id} must be a dictionary")
            amount = res.get("amount_usd")
            if not _is_valid_number(amount):
                raise ValueError(f"Invalid amount_usd in reservation {res_id}: {amount}")

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
    """Return budget consumption, reservations, configured caps and cost-basis labels.

    Cost-basis labels are reported separately and never conflated (document line 154):
      estimated_usd          active reservations (pre-spend estimate)
      actual_usd             reconciled spend actually debited to the ledger
      provider_reported_usd  provider-reported cost not separately persisted here
      invoice_confirmed_usd  cost confirmed by invoice
    Unknown/unavailable values are reported as None, NEVER as 0 (document line 154).
    """
    with _BUDGET_LOCK:
        ledger = _read_budget_ledger(root_dir)

    ceiling = _account_ceiling()
    corrupted = bool(ledger.get("corrupted"))
    project_default = get_project_default_budget_usd()

    # Fail-closed branch: corrupt ledger OR malformed ceiling.
    if corrupted or not ceiling["valid"]:
        daily_cap_report = ceiling["daily_cap_usd"] if ceiling["daily_cap_usd"] is not None else 0.0
        total_cap_report = ceiling["total_cap_usd"] if ceiling["total_cap_usd"] is not None else 0.0
        return {
            "daily_spend_usd": 0.0,
            "daily_reserved_usd": 0.0,
            "daily_cap_usd": daily_cap_report,
            "total_spend_usd": float("inf"),
            "total_reserved_usd": 0.0,
            "total_cap_usd": total_cap_report,
            "active_reserved_usd": 0.0,
            "budget_exceeded": True,
            "paid_production_enabled": False,
            "reason": REASON_LEDGER_CORRUPTED if corrupted else REASON_CEILING_INVALID,
            "remaining_usd": None,
            "daily_remaining_usd": None,
            "total_remaining_usd": None,
            "estimated_usd": None,
            "actual_usd": None,
            "provider_reported_usd": None,
            "invoice_confirmed_usd": None,
            "project_default_budget_usd": project_default,
        }

    today_key = datetime.now(timezone.utc).strftime("%Y-%m-%d")
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

    # Paid production disabled: the account ceiling was never explicitly configured.
    # Report the real (known) ledger spend, but expose caps/remaining as unknown
    # (None) and refuse paid work via budget_exceeded=True.
    if not ceiling["paid_production_enabled"]:
        return {
            "daily_spend_usd": round(today_spend, 4),
            "daily_reserved_usd": round(active_reserved_today, 4),
            "daily_cap_usd": None,
            "total_spend_usd": round(total_spend, 4),
            "total_reserved_usd": round(active_reserved_total, 4),
            "total_cap_usd": None,
            "active_reserved_usd": round(active_reserved_total, 4),
            "budget_exceeded": True,
            "paid_production_enabled": False,
            "reason": ceiling["reason"],
            "remaining_usd": None,
            "daily_remaining_usd": None,
            "total_remaining_usd": None,
            "estimated_usd": round(active_reserved_total, 4),
            "actual_usd": round(total_spend, 4),
            "provider_reported_usd": None,
            "invoice_confirmed_usd": None,
            "project_default_budget_usd": project_default,
        }

    daily_cap = float(ceiling["daily_cap_usd"])
    total_cap = float(ceiling["total_cap_usd"])

    daily_exceeded = (today_spend + active_reserved_today) >= daily_cap
    total_exceeded = (total_spend + active_reserved_total) >= total_cap
    exceeded = daily_exceeded or total_exceeded

    daily_remaining = max(0.0, daily_cap - (today_spend + active_reserved_today))
    total_remaining = max(0.0, total_cap - (total_spend + active_reserved_total))

    return {
        "daily_spend_usd": round(today_spend, 4),
        "daily_reserved_usd": round(active_reserved_today, 4),
        "daily_cap_usd": daily_cap,
        "total_spend_usd": round(total_spend, 4),
        "total_reserved_usd": round(active_reserved_total, 4),
        "total_cap_usd": total_cap,
        "active_reserved_usd": round(active_reserved_total, 4),
        "budget_exceeded": exceeded,
        "paid_production_enabled": True,
        "reason": (
            "Daily budget cap exceeded" if daily_exceeded
            else "Total budget cap exceeded" if total_exceeded
            else None
        ),
        "remaining_usd": round(min(daily_remaining, total_remaining), 4),
        "daily_remaining_usd": round(daily_remaining, 4),
        "total_remaining_usd": round(total_remaining, 4),
        "estimated_usd": round(active_reserved_total, 4),
        "actual_usd": round(total_spend, 4),
        "provider_reported_usd": None,
        "invoice_confirmed_usd": None,
        "project_default_budget_usd": project_default,
    }


def is_budget_available(estimated_charge_usd: float = 0.05, root_dir: Path | None = None) -> bool:
    """Check whether budget is available for an upcoming PAID operation.

    Zero-cost (non-paid) operations are always available and never require a
    configured ceiling. Paid operations require an explicitly configured account
    ceiling (paid_production_enabled) plus enough remaining budget; otherwise this
    fails closed to False.
    """
    if _is_valid_number(estimated_charge_usd) and estimated_charge_usd == 0.0:
        return True
    if not _is_valid_number(estimated_charge_usd):
        return False
    status = get_budget_status(root_dir)
    if not status.get("paid_production_enabled", False):
        return False
    if status["budget_exceeded"]:
        return False
    daily_remaining = status.get("daily_remaining_usd")
    total_remaining = status.get("total_remaining_usd")
    if daily_remaining is None or total_remaining is None:
        return False
    if estimated_charge_usd > daily_remaining or estimated_charge_usd > total_remaining:
        return False
    return True


def reserve_budget(
    operation_id: str,
    estimated_usd: float = 0.05,
    root_dir: Path | None = None,
) -> tuple[bool, str | None]:
    """Atomically reserve estimated cost before queuing or starting paid work."""
    if not _is_valid_number(estimated_usd):
        return False, f"Invalid estimated budget amount: {estimated_usd}"
    if estimated_usd == 0.0:
        # Zero-cost / non-paid: allowed even when paid production is disabled.
        return True, None

    with _BUDGET_LOCK:
        ledger = _read_budget_ledger(root_dir)
        if ledger.get("corrupted"):
            return False, REASON_LEDGER_CORRUPTED

        today_key = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        ceiling = _account_ceiling()
        if not ceiling["paid_production_enabled"]:
            # Fail-closed: refuse a PAID reservation when the account ceiling is not
            # explicitly configured (disabled) or is malformed (invalid). The honest
            # reason lets callers/UI distinguish this from a cap-exceeded refusal.
            return False, ceiling["reason"]
        daily_cap = float(ceiling["daily_cap_usd"])
        total_cap = float(ceiling["total_cap_usd"])

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
    if not _is_valid_number(actual_usd):
        logger.error("Invalid actual_usd in reconcile_budget: %s", actual_usd)
        with _BUDGET_LOCK:
            return _read_budget_ledger(root_dir)

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

        # B1 fix (document line 61): an incurred provider charge is ALWAYS debited,
        # including when the operation was cancelled. Cancellation only releases the
        # reservation (popped above) so the estimated hold is not double-counted; it
        # must never erase money the provider actually charged -- dropping it would
        # let real spend silently bypass the account ceiling. A cancelled operation
        # with NO provider charge (actual_usd == 0) is a pure reservation release and
        # correctly results in no debit. The idempotency guard above still prevents a
        # duplicate debit for the same operation_id.
        if actual_usd > 0.0:
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


def is_reservation_active(operation_id: str, root_dir: Path | None = None) -> bool:
    """Check whether an active budget reservation exists for an operation."""
    with _BUDGET_LOCK:
        ledger = _read_budget_ledger(root_dir)
        return operation_id in ledger.get("active_reservations", {})


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
    if not _is_valid_number(cost_usd) or cost_usd <= 0.0:
        with _BUDGET_LOCK:
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


def record_approval(
    operation: str,
    *,
    approved_spend_usd: float,
    decision: str,
    actor: str,
    target_ref: str | None = None,
    approval_id: str | None = None,
    recorded_at: str | None = None,
    root_dir: Path | None = None,
) -> dict[str, Any]:
    """Persist a spend-approval decision into the canonical ledger (document line 87).

    The approval record captures: the operation, the target version / change-set
    reference, the approved spend, the decision, the acting approver and a UTC
    timestamp. It is written atomically into the SAME canonical budget ledger file
    (get_canonical_budget_file) via write_json_atomically.

    Fail-closed: refuses to persist (raises) when the ledger is corrupted or when
    the approved spend / required fields are invalid, so an approval is never
    silently dropped or recorded against an unreadable ledger.
    """
    if not isinstance(operation, str) or not operation.strip():
        raise ValueError("operation is required to record an approval")
    if not isinstance(decision, str) or not decision.strip():
        raise ValueError("decision is required to record an approval")
    if not isinstance(actor, str) or not actor.strip():
        raise ValueError("actor is required to record an approval")
    if not _is_valid_number(approved_spend_usd):
        raise ValueError(f"Invalid approved_spend_usd: {approved_spend_usd}")

    now_iso = datetime.now(timezone.utc).isoformat()
    key = approval_id or f"{operation}@{now_iso}"

    with _BUDGET_LOCK:
        ledger = _read_budget_ledger(root_dir)
        if ledger.get("corrupted"):
            # Never write an approval into (or on top of) a corrupted ledger.
            raise RuntimeError(REASON_LEDGER_CORRUPTED)

        approvals = ledger.get("approvals", {})
        if not isinstance(approvals, dict):
            raise RuntimeError("Malformed approvals map in ledger (fail-closed)")

        record = {
            "operation": operation,
            "target_ref": target_ref,
            "approved_spend_usd": round(approved_spend_usd, 6),
            "decision": decision,
            "actor": actor,
            "timestamp": recorded_at or now_iso,
        }
        approvals[key] = record
        ledger["approvals"] = approvals
        ledger["last_updated"] = datetime.now(timezone.utc).isoformat()

        budget_file = get_canonical_budget_file(root_dir)
        write_json_atomically(budget_file, ledger)
        return {"approval_id": key, **record}


def read_approvals(root_dir: Path | None = None) -> dict[str, Any]:
    """Read persisted approval records from the canonical ledger (fail-closed).

    Returns {"corrupted": bool, "approvals": dict, "reason": str | None}. On a
    corrupted or malformed ledger it reports corrupted=True with an empty approvals
    map rather than fabricating records.
    """
    with _BUDGET_LOCK:
        ledger = _read_budget_ledger(root_dir)

    if ledger.get("corrupted"):
        return {"corrupted": True, "approvals": {}, "reason": REASON_LEDGER_CORRUPTED}

    approvals = ledger.get("approvals", {})
    if not isinstance(approvals, dict):
        return {"corrupted": True, "approvals": {}, "reason": "Malformed approvals map in ledger (fail-closed)"}
    return {"corrupted": False, "approvals": approvals, "reason": None}
