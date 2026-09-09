"""Budget Ledger and Cost Cap Tracking with Atomic Reservation & Reconciliation (Fail-Closed)."""

from __future__ import annotations

import json
import logging
import math
import os
from copy import deepcopy
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

try:  # pragma: no cover - the production/runtime target is POSIX
    import fcntl
except ImportError:  # pragma: no cover - retained for importability on Windows
    fcntl = None  # type: ignore[assignment]

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
REASON_PROJECT_CAP_EXCEEDED = "Project budget cap exceeded"
REASON_APPROVAL_INVALID = "Approval is invalid, expired, revoked, or already consumed"
REASON_RESERVATION_IDENTITY_MISMATCH = "Reservation identity does not match the requested project or approval"

_BUDGET_LOCK = threading.Lock()


class ApprovalError(RuntimeError):
    """Raised when an approval cannot authorize the exact requested target."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat()


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _required_project_id(project_id: Any) -> str:
    if not isinstance(project_id, str) or not project_id.strip():
        raise ValueError("project_id is required")
    return project_id.strip()


def _optional_project_id(project_id: Any) -> str | None:
    if project_id is None:
        return None
    return _required_project_id(project_id)


@contextmanager
def _budget_process_lock(root_dir: Path | None = None):
    """Serialize budget ledger read/modify/write transactions across processes.

    ``threading.Lock`` protects only callers in one interpreter.  The budget
    ledger is shared by workers, so reservations and reconciliations also take
    an advisory OS file lock while they read and atomically replace the ledger.
    The sidecar keeps the canonical JSON path replaceable without invalidating
    the lock held by another process.
    """
    budget_file = get_canonical_budget_file(root_dir)
    lock_file = budget_file.with_name(f"{budget_file.name}.lock")
    if lock_file.is_symlink():
        raise RuntimeError("Budget ledger lock file must not be a symlink")
    with lock_file.open("a+") as handle:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


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
    # Keep the documented name first, while accepting the two descriptive
    # aliases used by older local operators.  The value is configuration only;
    # it is never used as an account-level spend ceiling.
    raw = next(
        (
            os.getenv(name)
            for name in (
                "FYF_PROJECT_DEFAULT_BUDGET_USD",
                "FYF_DEFAULT_PROJECT_BUDGET_USD",
                "FYF_PROJECT_BUDGET_DEFAULT_USD",
            )
            if os.getenv(name) is not None
        ),
        None,
    )
    if raw is None or not str(raw).strip():
        return PROJECT_DEFAULT_BUDGET_USD
    try:
        val = float(raw)
    except (ValueError, TypeError, OverflowError):
        return PROJECT_DEFAULT_BUDGET_USD
    if not _is_valid_number(val):
        return PROJECT_DEFAULT_BUDGET_USD
    return val


def _empty_budget_ledger() -> dict[str, Any]:
    """Return the current ledger shape with additive maps initialized."""
    return {
        "total_spend_usd": 0.0,
        "daily_spend": {},
        "active_reservations": {},
        "reconciled_operations": {},
        "project_spend_usd": {},
        "project_daily_spend": {},
        "project_budgets": {},
        "approvals": {},
        "unknown_reconciliations": {},
        "last_updated": None,
        "corrupted": False,
    }


def _read_budget_ledger(root_dir: Path | None = None) -> dict[str, Any]:
    budget_file = get_canonical_budget_file(root_dir)
    if not budget_file.exists():
        return _empty_budget_ledger()
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
            raise ValueError("reconciled_operations must be a dictionary")

        for field in (
            "project_spend_usd",
            "project_daily_spend",
            "project_budgets",
            "approvals",
            "unknown_reconciliations",
        ):
            value = data.get(field, {})
            if not isinstance(value, dict):
                raise ValueError(f"{field} must be a dictionary")

        project_spend = data.get("project_spend_usd", {})
        if not all(_is_valid_number(value) for value in project_spend.values()):
            raise ValueError("Invalid project_spend_usd map in ledger")
        project_daily = data.get("project_daily_spend", {})
        if not all(
            isinstance(value, dict)
            and all(_is_valid_number(amount) for amount in value.values())
            for value in project_daily.values()
        ):
            raise ValueError("Invalid project_daily_spend map in ledger")
        for project_id, budget in data.get("project_budgets", {}).items():
            if not isinstance(project_id, str) or not isinstance(budget, dict):
                raise ValueError("Invalid project_budgets record")
            if not _is_valid_number(budget.get("budget_usd")):
                raise ValueError(f"Invalid project budget for {project_id}")
        for approval_id, approval in data.get("approvals", {}).items():
            if not isinstance(approval_id, str) or not isinstance(approval, dict):
                raise ValueError("Invalid approvals record")
            amount = approval.get("approved_spend_usd")
            if amount is not None and not _is_valid_number(amount):
                raise ValueError(f"Invalid approved spend for {approval_id}")

        # Additive schema migration for ledgers written by earlier stages.
        for field in (
            "project_spend_usd",
            "project_daily_spend",
            "project_budgets",
            "approvals",
            "unknown_reconciliations",
        ):
            data.setdefault(field, {})
        data["corrupted"] = False
        return data
    except Exception as exc:
        logger.error("Budget ledger corrupted at %s: %s (failing closed)", budget_file, exc)
        ledger = _empty_budget_ledger()
        ledger.update({
            "total_spend_usd": float("inf"),
            "corrupted": True,
            "error": str(exc),
        })
        return ledger


def _project_budget_record(
    project_id: str,
    ledger: dict[str, Any],
) -> tuple[float, bool, str, str | None]:
    """Resolve one project's persisted/custom or configured/default budget."""
    budgets = ledger.get("project_budgets", {})
    custom = budgets.get(project_id) if isinstance(budgets, dict) else None
    if isinstance(custom, dict) and _is_valid_number(custom.get("budget_usd")):
        return (
            float(custom["budget_usd"]),
            True,
            "custom",
            str(custom.get("updated_at")) if custom.get("updated_at") else None,
        )
    return get_project_default_budget_usd(), False, "default", None


def get_project_budget(
    project_id: str,
    root_dir: Path | None = None,
) -> dict[str, Any]:
    """Read a project's persisted budget and its configuration source.

    The requested project budget is kept distinct from the effective account
    guardrail.  Callers can therefore show the owner-configured value while
    enforcing the lower account cap at dispatch time.
    """
    project = _required_project_id(project_id)
    with _BUDGET_LOCK:
        ledger = _read_budget_ledger(root_dir)
    if ledger.get("corrupted"):
        return {
            "project_id": project,
            "budget_usd": None,
            "is_custom": False,
            "source": "unavailable",
            "updated_at": None,
            "availability": "unavailable",
            "reason": REASON_LEDGER_CORRUPTED,
        }
    amount, is_custom, source, updated_at = _project_budget_record(project, ledger)
    return {
        "project_id": project,
        "budget_usd": amount,
        "is_custom": is_custom,
        "source": source,
        "updated_at": updated_at,
        "availability": "available",
        "reason": None,
    }


def get_project_budget_usd(
    project_id: str,
    root_dir: Path | None = None,
) -> float | None:
    """Return the effective configured/default budget amount for one project."""
    return get_project_budget(project_id, root_dir=root_dir).get("budget_usd")


def set_project_budget(
    project_id: str,
    budget_usd: float,
    *,
    actor: str = "operator",
    root_dir: Path | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Persist a custom per-project budget atomically.

    The stored value is intentionally not silently clamped.  The effective
    ceiling returned by :func:`get_budget_status` is bounded by account caps,
    preserving both the owner's requested allocation and the hard guardrail.
    """
    project = _required_project_id(project_id)
    if not _is_valid_number(budget_usd):
        raise ValueError(f"Invalid project budget: {budget_usd}")
    if not isinstance(actor, str) or not actor.strip():
        raise ValueError("actor is required to set a project budget")
    if idempotency_key is not None and (
        not isinstance(idempotency_key, str) or not idempotency_key.strip()
    ):
        raise ValueError("idempotency_key must be non-blank when supplied")

    now_iso = _iso(_utcnow())
    with _BUDGET_LOCK, _budget_process_lock(root_dir):
        ledger = _read_budget_ledger(root_dir)
        if ledger.get("corrupted"):
            raise RuntimeError(REASON_LEDGER_CORRUPTED)
        budgets = ledger.setdefault("project_budgets", {})
        existing = budgets.get(project)
        if idempotency_key and isinstance(existing, dict) and existing.get("idempotency_key") == idempotency_key:
            if float(existing.get("budget_usd")) != float(budget_usd):
                raise ValueError("idempotency_key was reused with a different project budget")
            return dict(existing)
        record = {
            "project_id": project,
            "budget_usd": round(float(budget_usd), 6),
            "actor": actor.strip(),
            "source": "custom",
            "updated_at": now_iso,
        }
        if idempotency_key:
            record["idempotency_key"] = idempotency_key.strip()
        budgets[project] = record
        ledger["project_budgets"] = budgets
        ledger["last_updated"] = now_iso
        write_json_atomically(get_canonical_budget_file(root_dir), ledger)
        return dict(record)


def configure_project_budget(
    project_id: str,
    budget_usd: float,
    *,
    actor: str = "operator",
    root_dir: Path | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Descriptive alias for :func:`set_project_budget` used by API adapters."""
    return set_project_budget(
        project_id,
        budget_usd,
        actor=actor,
        root_dir=root_dir,
        idempotency_key=idempotency_key,
    )


def clear_project_budget(project_id: str, *, root_dir: Path | None = None) -> dict[str, Any]:
    """Remove a custom allocation so the project falls back to the configured default."""
    project = _required_project_id(project_id)
    with _BUDGET_LOCK, _budget_process_lock(root_dir):
        ledger = _read_budget_ledger(root_dir)
        if ledger.get("corrupted"):
            raise RuntimeError(REASON_LEDGER_CORRUPTED)
        budgets = ledger.setdefault("project_budgets", {})
        budgets.pop(project, None)
        ledger["project_budgets"] = budgets
        ledger["last_updated"] = _iso(_utcnow())
        write_json_atomically(get_canonical_budget_file(root_dir), ledger)
    return get_project_budget(project, root_dir=root_dir)


def get_budget_status(
    root_dir: Path | None = None,
    project_id: str | None = None,
) -> dict[str, Any]:
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
    project = _optional_project_id(project_id)

    project_budget: float | None = None
    project_is_custom = False
    project_source = "unavailable"
    project_updated_at: str | None = None
    if project is not None and not corrupted:
        (
            project_budget,
            project_is_custom,
            project_source,
            project_updated_at,
        ) = _project_budget_record(project, ledger)

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
            "project_id": project,
            "project_budget_usd": project_budget,
            "project_budget_source": project_source,
            "project_budget_is_custom": project_is_custom,
            "project_budget_updated_at": project_updated_at,
            "effective_project_cap_usd": None,
            "project_spend_usd": None,
            "project_reserved_usd": None,
            "project_remaining_usd": None,
            "project_budget_exceeded": True if project is not None else None,
            "project_budget_available": False if project is not None else None,
            "corrupted": corrupted,
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

    project_spend = (
        float(ledger.get("project_spend_usd", {}).get(project, 0.0))
        if project is not None
        else None
    )
    project_reserved = (
        sum(
            float(r.get("amount_usd", 0.0))
            for r in reservations.values()
            if isinstance(r, dict) and r.get("project_id") == project
        )
        if project is not None
        else None
    )
    effective_project_cap = (
        min(project_budget, float(ceiling["total_cap_usd"]))
        if project_budget is not None and ceiling["total_cap_usd"] is not None
        else project_budget
    )
    project_exceeded = (
        bool(
            effective_project_cap is not None
            and project_spend is not None
            and project_reserved is not None
            and project_spend + project_reserved >= effective_project_cap
        )
        if project is not None
        else None
    )
    project_remaining = (
        round(max(0.0, effective_project_cap - (project_spend + project_reserved)), 4)
        if effective_project_cap is not None and project_spend is not None and project_reserved is not None
        else None
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
            "project_id": project,
            "project_budget_usd": project_budget,
            "project_budget_source": project_source,
            "project_budget_is_custom": project_is_custom,
            "project_budget_updated_at": project_updated_at,
            "effective_project_cap_usd": effective_project_cap,
            "project_spend_usd": round(project_spend, 4) if project_spend is not None else None,
            "project_reserved_usd": round(project_reserved, 4) if project_reserved is not None else None,
            "project_remaining_usd": None,
            "project_budget_exceeded": True if project is not None else None,
            "project_budget_available": False if project is not None else None,
            "corrupted": False,
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
        "project_id": project,
        "project_budget_usd": project_budget,
        "project_budget_source": project_source,
        "project_budget_is_custom": project_is_custom,
        "project_budget_updated_at": project_updated_at,
        "effective_project_cap_usd": effective_project_cap,
        "project_spend_usd": round(project_spend, 4) if project_spend is not None else None,
        "project_reserved_usd": round(project_reserved, 4) if project_reserved is not None else None,
        "project_remaining_usd": project_remaining,
        "project_budget_exceeded": project_exceeded,
        "project_budget_available": (
            bool(
                project is not None
                and not project_exceeded
                and not daily_exceeded
                and not total_exceeded
                and project_remaining is not None
                and ceiling["paid_production_enabled"]
            )
            if project is not None
            else None
        ),
        "corrupted": False,
    }


def is_budget_available(
    estimated_charge_usd: float = 0.05,
    root_dir: Path | None = None,
    *,
    project_id: str | None = None,
) -> bool:
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
    status = get_budget_status(root_dir, project_id=project_id)
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
    if project_id is not None:
        project_remaining = status.get("project_remaining_usd")
        if project_remaining is None or estimated_charge_usd > project_remaining:
            return False
        if status.get("project_budget_exceeded"):
            return False
    return True


def _validate_approval_record(
    record: Any,
    *,
    operation: str | None,
    project_id: str | None,
    target_ref: str | None,
    amount_usd: float | None,
    now: datetime,
) -> dict[str, Any]:
    """Validate an approval without acquiring the ledger lock.

    ``None`` operation/target values are intentionally accepted only for the
    budget reservation seam, where the caller has already selected an approval
    id and the exact command target is checked by the command layer.  Public
    consumption always supplies all three identity fields.
    """
    if not isinstance(record, dict):
        raise ApprovalError(REASON_APPROVAL_INVALID)
    if str(record.get("decision", "")).lower() != "approved":
        raise ApprovalError(REASON_APPROVAL_INVALID)
    status = str(record.get("status", "approved")).lower()
    if status not in {"approved", "active"}:
        raise ApprovalError(REASON_APPROVAL_INVALID)
    if record.get("revoked_at") or record.get("consumed_at") or record.get("failed_at"):
        raise ApprovalError(REASON_APPROVAL_INVALID)
    expires_at = record.get("expires_at")
    if expires_at is not None:
        expiry = _parse_datetime(expires_at)
        if expiry is None or expiry <= now:
            raise ApprovalError(REASON_APPROVAL_INVALID)
    if operation is not None and record.get("operation") != operation:
        raise ApprovalError(REASON_APPROVAL_INVALID)
    if target_ref is not None and record.get("target_ref") != target_ref:
        raise ApprovalError(REASON_APPROVAL_INVALID)
    record_project = record.get("project_id")
    if project_id is not None and record_project != project_id:
        raise ApprovalError(REASON_APPROVAL_INVALID)
    approved = record.get("approved_spend_usd")
    if approved is None or not _is_valid_number(approved):
        raise ApprovalError(REASON_APPROVAL_INVALID)
    if amount_usd is not None:
        if not _is_valid_number(amount_usd) or float(amount_usd) > float(approved):
            raise ApprovalError(REASON_APPROVAL_INVALID)
    return record


def reserve_budget(
    operation_id: str,
    estimated_usd: float = 0.05,
    root_dir: Path | None = None,
    *,
    project_id: str | None = None,
    approval_id: str | None = None,
) -> tuple[bool, str | None]:
    """Atomically reserve estimated cost before queuing or starting paid work."""
    project = _optional_project_id(project_id)
    if not _is_valid_number(estimated_usd):
        return False, f"Invalid estimated budget amount: {estimated_usd}"
    if estimated_usd == 0.0:
        # Zero-cost / non-paid: allowed even when paid production is disabled.
        return True, None

    with _BUDGET_LOCK, _budget_process_lock(root_dir):
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

        if approval_id is not None:
            try:
                _validate_approval_record(
                    ledger.get("approvals", {}).get(approval_id),
                    operation=None,
                    project_id=project,
                    target_ref=None,
                    amount_usd=estimated_usd,
                    now=_utcnow(),
                )
            except ApprovalError as exc:
                return False, str(exc)
        daily_cap = float(ceiling["daily_cap_usd"])
        total_cap = float(ceiling["total_cap_usd"])

        today_spend = float(ledger.get("daily_spend", {}).get(today_key, 0.0))
        total_spend = float(ledger.get("total_spend_usd", 0.0))
        reservations = ledger.get("active_reservations", {})

        # If this operation already holds a reservation, don't double count
        if operation_id in reservations:
            existing = reservations.get(operation_id)
            if not isinstance(existing, dict):
                return False, REASON_LEDGER_CORRUPTED
            existing_project = existing.get("project_id")
            if project is not None and existing_project not in (None, project):
                return False, REASON_RESERVATION_IDENTITY_MISMATCH
            if approval_id is not None and existing.get("approval_id") not in (None, approval_id):
                return False, REASON_RESERVATION_IDENTITY_MISMATCH
            existing_amount = existing.get("amount_usd")
            if not _is_valid_number(existing_amount) or float(existing_amount) != float(estimated_usd):
                return False, REASON_RESERVATION_IDENTITY_MISMATCH
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

        if project is not None:
            project_budget, _, _, _ = _project_budget_record(project, ledger)
            project_spend = float(ledger.get("project_spend_usd", {}).get(project, 0.0))
            project_reserved = sum(
                float(r.get("amount_usd", 0.0))
                for r in reservations.values()
                if isinstance(r, dict) and r.get("project_id") == project
            )
            effective_project_cap = min(project_budget, total_cap)
            if project_spend + project_reserved + estimated_usd > effective_project_cap:
                return False, (
                    f"{REASON_PROJECT_CAP_EXCEEDED} "
                    f"({project_spend + project_reserved:.4f} + {estimated_usd:.4f} > "
                    f"{effective_project_cap:.4f} USD)"
                )

        reservations[operation_id] = {
            "amount_usd": round(estimated_usd, 6),
            "date": today_key,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        if project is not None:
            reservations[operation_id]["project_id"] = project
        if approval_id is not None:
            reservations[operation_id]["approval_id"] = approval_id
        ledger["active_reservations"] = reservations
        ledger["last_updated"] = datetime.now(timezone.utc).isoformat()

        budget_file = get_canonical_budget_file(root_dir)
        write_json_atomically(budget_file, ledger)
        return True, None


def reconcile_budget(
    operation_id: str,
    actual_usd: float | None,
    outcome: str = "completed",
    root_dir: Path | None = None,
    *,
    project_id: str | None = None,
) -> dict[str, Any]:
    """Atomically reconcile a budget reservation with actual measured provider spend."""
    project = _optional_project_id(project_id)
    if actual_usd is None:
        # Unknown provider billing is not a zero-cost outcome.  Keep any
        # reservation in place until a measured amount is supplied, and record
        # the unavailable reconciliation for an operator/a later repair job.
        with _BUDGET_LOCK, _budget_process_lock(root_dir):
            ledger = _read_budget_ledger(root_dir)
            if ledger.get("corrupted"):
                return ledger
            reservation = ledger.get("active_reservations", {}).get(operation_id)
            if (
                project is not None
                and isinstance(reservation, dict)
                and reservation.get("project_id") not in (None, project)
            ):
                raise ValueError(REASON_RESERVATION_IDENTITY_MISMATCH)
            if project is None and isinstance(reservation, dict):
                project = reservation.get("project_id")
            unknowns = ledger.setdefault("unknown_reconciliations", {})
            unknowns[operation_id] = {
                "project_id": project,
                "outcome": outcome,
                "status": "unavailable",
                "recorded_at": _iso(_utcnow()),
            }
            ledger["unknown_reconciliations"] = unknowns
            ledger["last_updated"] = _iso(_utcnow())
            write_json_atomically(get_canonical_budget_file(root_dir), ledger)
            return ledger
    if not _is_valid_number(actual_usd):
        logger.error("Invalid actual_usd in reconcile_budget: %s", actual_usd)
        with _BUDGET_LOCK, _budget_process_lock(root_dir):
            return _read_budget_ledger(root_dir)

    with _BUDGET_LOCK, _budget_process_lock(root_dir):
        ledger = _read_budget_ledger(root_dir)
        if ledger.get("corrupted"):
            return ledger

        reconciled = ledger.get("reconciled_operations", {})
        # Idempotency guard: prevent duplicate debiting
        if operation_id in reconciled:
            previous = reconciled.get(operation_id)
            if (
                project is not None
                and isinstance(previous, dict)
                and previous.get("project_id") not in (None, project)
            ):
                raise ValueError(REASON_RESERVATION_IDENTITY_MISMATCH)
            logger.info("Operation %s was already reconciled, skipping duplicate debit", operation_id)
            return ledger

        reservations = ledger.get("active_reservations", {})
        reservation = reservations.get(operation_id)
        if (
            project is not None
            and isinstance(reservation, dict)
            and reservation.get("project_id") not in (None, project)
        ):
            raise ValueError(REASON_RESERVATION_IDENTITY_MISMATCH)
        reservation = reservations.pop(operation_id, None)
        ledger["active_reservations"] = reservations
        if project is None and isinstance(reservation, dict):
            project = reservation.get("project_id")

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
            if project is not None:
                project_spend = ledger.setdefault("project_spend_usd", {})
                project_spend[project] = round(
                    float(project_spend.get(project, 0.0)) + actual_usd,
                    6,
                )
                ledger["project_spend_usd"] = project_spend
                project_daily = ledger.setdefault("project_daily_spend", {})
                project_daily_for_day = project_daily.setdefault(today_key, {})
                project_daily_for_day[project] = round(
                    float(project_daily_for_day.get(project, 0.0)) + actual_usd,
                    6,
                )
                ledger["project_daily_spend"] = project_daily

        reconciled[operation_id] = {
            "actual_usd": round(actual_usd, 6),
            "outcome": outcome,
            "project_id": project,
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
    with _BUDGET_LOCK, _budget_process_lock(root_dir):
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


def record_cost(
    cost_usd: float | None,
    root_dir: Path | None = None,
    *,
    project_id: str | None = None,
) -> dict[str, Any]:
    """Direct record helper for backward-compatible call points."""
    project = _optional_project_id(project_id)
    if not _is_valid_number(cost_usd) or cost_usd <= 0.0:
        if cost_usd is None:
            with _BUDGET_LOCK, _budget_process_lock(root_dir):
                ledger = _read_budget_ledger(root_dir)
                if not ledger.get("corrupted"):
                    unknowns = ledger.setdefault("unknown_reconciliations", {})
                    unknowns[f"direct:{len(unknowns)}"] = {
                        "project_id": project,
                        "status": "unavailable",
                        "recorded_at": _iso(_utcnow()),
                    }
                    ledger["unknown_reconciliations"] = unknowns
                    ledger["last_updated"] = _iso(_utcnow())
                    write_json_atomically(get_canonical_budget_file(root_dir), ledger)
                    return ledger
        with _BUDGET_LOCK:
            return _read_budget_ledger(root_dir)

    with _BUDGET_LOCK, _budget_process_lock(root_dir):
        ledger = _read_budget_ledger(root_dir)
        if ledger.get("corrupted"):
            return ledger

        today_key = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        current_total = float(ledger.get("total_spend_usd", 0.0)) + cost_usd
        daily_map = ledger.get("daily_spend", {})
        daily_map[today_key] = float(daily_map.get(today_key, 0.0)) + cost_usd

        ledger["total_spend_usd"] = round(current_total, 4)
        ledger["daily_spend"] = daily_map
        if project is not None:
            project_spend = ledger.setdefault("project_spend_usd", {})
            project_spend[project] = round(float(project_spend.get(project, 0.0)) + cost_usd, 4)
            ledger["project_spend_usd"] = project_spend
            project_daily = ledger.setdefault("project_daily_spend", {})
            project_day = project_daily.setdefault(today_key, {})
            project_day[project] = round(float(project_day.get(project, 0.0)) + cost_usd, 4)
            ledger["project_daily_spend"] = project_daily
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
    project_id: str | None = None,
    approval_id: str | None = None,
    recorded_at: str | None = None,
    expires_at: str | datetime | None = None,
    ttl_seconds: float | None = None,
    one_shot: bool = True,
    idempotency_key: str | None = None,
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
    project = _optional_project_id(project_id)
    if not isinstance(one_shot, bool):
        raise ValueError("one_shot must be a boolean")
    if ttl_seconds is not None:
        if not _is_valid_number(ttl_seconds) or float(ttl_seconds) <= 0:
            raise ValueError("ttl_seconds must be a positive finite number")
    expiry_value: str | None
    if expires_at is not None:
        expiry = _parse_datetime(expires_at)
        if expiry is None:
            raise ValueError("expires_at must be an ISO-8601 timestamp")
        expiry_value = _iso(expiry)
    elif ttl_seconds is not None:
        expiry_value = _iso(_utcnow() + timedelta(seconds=float(ttl_seconds)))
    else:
        expiry_value = None
    if idempotency_key is not None and (
        not isinstance(idempotency_key, str) or not idempotency_key.strip()
    ):
        raise ValueError("idempotency_key must be non-blank when supplied")

    now_iso = recorded_at or _iso(_utcnow())
    # Validate a caller-provided recorded_at before persisting it.
    if _parse_datetime(now_iso) is None:
        raise ValueError("recorded_at must be an ISO-8601 timestamp")
    idem = idempotency_key.strip() if idempotency_key else None
    key = approval_id or (f"approval:{idem}" if idem else f"{operation}@{now_iso}")

    with _BUDGET_LOCK, _budget_process_lock(root_dir):
        ledger = _read_budget_ledger(root_dir)
        if ledger.get("corrupted"):
            # Never write an approval into (or on top of) a corrupted ledger.
            raise RuntimeError(REASON_LEDGER_CORRUPTED)

        approvals = ledger.get("approvals", {})
        if not isinstance(approvals, dict):
            raise RuntimeError("Malformed approvals map in ledger (fail-closed)")

        if idem is not None and approval_id is None:
            # Recover idempotency across callers that did not supply an
            # explicit approval id (including records from the pre-keyed
            # implementation).
            for existing_key, candidate in approvals.items():
                if isinstance(candidate, dict) and candidate.get("idempotency_key") == idem:
                    key = existing_key
                    break
        if idem is None:
            idem = key

        existing = approvals.get(key)
        if isinstance(existing, dict):
            # Approval ids are idempotency keys.  Never overwrite an existing
            # decision with a different target, amount, or actor.
            identity_fields = (
                ("operation", operation),
                ("target_ref", target_ref),
                ("project_id", project),
                ("approved_spend_usd", round(float(approved_spend_usd), 6)),
                ("decision", decision),
                ("one_shot", one_shot),
                ("idempotency_key", idem),
            )
            if all(existing.get(field) == value for field, value in identity_fields):
                return dict(existing)
            raise ValueError("approval_id cannot be reused for a different approval")

        record = {
            "approval_id": key,
            "operation": operation,
            "target_ref": target_ref,
            "project_id": project,
            "approved_spend_usd": round(approved_spend_usd, 6),
            "decision": decision,
            "status": decision.lower(),
            "actor": actor,
            "timestamp": now_iso,
            "created_at": now_iso,
            "expires_at": expiry_value,
            "one_shot": one_shot,
            "idempotency_key": idem,
            "consumed_at": None,
            "revoked_at": None,
            "revoked_by": None,
        }
        approvals[key] = record
        ledger["approvals"] = approvals
        ledger["last_updated"] = datetime.now(timezone.utc).isoformat()

        budget_file = get_canonical_budget_file(root_dir)
        write_json_atomically(budget_file, ledger)
        return record


def read_approvals(
    root_dir: Path | None = None,
    *,
    now: datetime | None = None,
    include_inactive: bool = True,
) -> dict[str, Any]:
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
    moment = now or _utcnow()
    visible: dict[str, Any] = {}
    for approval_id, raw in approvals.items():
        if not isinstance(raw, dict):
            return {"corrupted": True, "approvals": {}, "reason": "Malformed approval record (fail-closed)"}
        record = deepcopy(raw)
        expiry = _parse_datetime(record.get("expires_at"))
        if (
            expiry is not None
            and expiry <= moment
            and str(record.get("status", record.get("decision", ""))).lower() in {"approved", "active"}
            and not record.get("consumed_at")
            and not record.get("revoked_at")
        ):
            record["status"] = "expired"
            record["decision"] = "expired"
            record["expired_at"] = _iso(moment)
        if include_inactive or str(record.get("status", "")).lower() in {"approved", "active"}:
            visible[approval_id] = record
    return {"corrupted": False, "approvals": visible, "reason": None}


def get_approval(
    approval_id: str,
    *,
    root_dir: Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Return one approval record with expiry reflected in its status."""
    result = read_approvals(root_dir=root_dir, now=now)
    if result.get("corrupted"):
        return None
    record = result.get("approvals", {}).get(approval_id)
    return dict(record) if isinstance(record, dict) else None


def validate_approval(
    approval_id: str,
    *,
    operation: str,
    project_id: str,
    target_ref: str,
    amount_usd: float | None = None,
    root_dir: Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Validate exact operation/project/target and return the active approval."""
    project = _required_project_id(project_id)
    if not isinstance(operation, str) or not operation.strip():
        raise ValueError("operation is required")
    if not isinstance(target_ref, str) or not target_ref.strip():
        raise ValueError("target_ref is required")
    if not isinstance(approval_id, str) or not approval_id.strip():
        raise ValueError("approval_id is required")
    with _BUDGET_LOCK:
        ledger = _read_budget_ledger(root_dir)
        if ledger.get("corrupted"):
            raise ApprovalError(REASON_LEDGER_CORRUPTED)
        record = ledger.get("approvals", {}).get(approval_id)
        return dict(
            _validate_approval_record(
                record,
                operation=operation,
                project_id=project,
                target_ref=target_ref,
                amount_usd=amount_usd,
                now=now or _utcnow(),
            )
        )


def is_approval_valid(
    approval_id: str,
    *,
    operation: str,
    project_id: str,
    target_ref: str,
    amount_usd: float | None = None,
    root_dir: Path | None = None,
    now: datetime | None = None,
) -> bool:
    """Boolean adapter around :func:`validate_approval` for guardrail callers."""
    try:
        validate_approval(
            approval_id,
            operation=operation,
            project_id=project_id,
            target_ref=target_ref,
            amount_usd=amount_usd,
            root_dir=root_dir,
            now=now,
        )
    except (ApprovalError, ValueError):
        return False
    return True


def consume_approval(
    approval_id: str,
    *,
    operation: str,
    project_id: str,
    target_ref: str,
    amount_usd: float | None = None,
    now: datetime | None = None,
    root_dir: Path | None = None,
) -> dict[str, Any]:
    """Atomically consume one exact-target approval.

    A consumed approval is terminal and cannot be reused.  A caller that still
    has to enqueue work should use :func:`begin_approval`; if enqueue fails,
    :func:`rollback_approval` burns the claim as failed rather than restoring
    reusable authority.
    """
    project = _required_project_id(project_id)
    moment = now or _utcnow()
    with _BUDGET_LOCK, _budget_process_lock(root_dir):
        ledger = _read_budget_ledger(root_dir)
        if ledger.get("corrupted"):
            raise ApprovalError(REASON_LEDGER_CORRUPTED)
        approvals = ledger.get("approvals", {})
        record = _validate_approval_record(
            approvals.get(approval_id),
            operation=operation,
            project_id=project,
            target_ref=target_ref,
            amount_usd=amount_usd,
            now=moment,
        )
        updated = dict(record)
        updated.update({
            "status": "consumed",
            "consumed_at": _iso(moment),
            "consumed_by": project,
        })
        approvals[approval_id] = updated
        ledger["approvals"] = approvals
        ledger["last_updated"] = _iso(moment)
        write_json_atomically(get_canonical_budget_file(root_dir), ledger)
        return dict(updated)


def begin_approval(
    approval_id: str,
    *,
    operation: str,
    project_id: str,
    target_ref: str,
    amount_usd: float | None = None,
    now: datetime | None = None,
    root_dir: Path | None = None,
) -> dict[str, Any]:
    """Claim an approval before an external enqueue/dispatch operation."""
    project = _required_project_id(project_id)
    moment = now or _utcnow()
    with _BUDGET_LOCK, _budget_process_lock(root_dir):
        ledger = _read_budget_ledger(root_dir)
        if ledger.get("corrupted"):
            raise ApprovalError(REASON_LEDGER_CORRUPTED)
        approvals = ledger.get("approvals", {})
        record = _validate_approval_record(
            approvals.get(approval_id),
            operation=operation,
            project_id=project,
            target_ref=target_ref,
            amount_usd=amount_usd,
            now=moment,
        )
        updated = dict(record)
        updated.update({"status": "consuming", "claimed_at": _iso(moment), "claimed_by": project})
        approvals[approval_id] = updated
        ledger["approvals"] = approvals
        ledger["last_updated"] = _iso(moment)
        write_json_atomically(get_canonical_budget_file(root_dir), ledger)
        return dict(updated)


def finalize_approval(
    approval_id: str,
    *,
    now: datetime | None = None,
    root_dir: Path | None = None,
) -> dict[str, Any]:
    """Finalize a previously claimed approval after successful enqueue."""
    moment = now or _utcnow()
    with _BUDGET_LOCK, _budget_process_lock(root_dir):
        ledger = _read_budget_ledger(root_dir)
        if ledger.get("corrupted"):
            raise ApprovalError(REASON_LEDGER_CORRUPTED)
        approvals = ledger.get("approvals", {})
        record = approvals.get(approval_id)
        if not isinstance(record, dict) or str(record.get("status")) != "consuming":
            raise ApprovalError(REASON_APPROVAL_INVALID)
        updated = dict(record)
        updated.update({"status": "consumed", "consumed_at": _iso(moment)})
        approvals[approval_id] = updated
        ledger["approvals"] = approvals
        ledger["last_updated"] = _iso(moment)
        write_json_atomically(get_canonical_budget_file(root_dir), ledger)
        return dict(updated)


def rollback_approval(
    approval_id: str,
    *,
    reason: str = "enqueue_failed",
    now: datetime | None = None,
    root_dir: Path | None = None,
) -> dict[str, Any]:
    """Close a claimed approval after failed validation/enqueue.

    The failed claim is retained for audit and is deliberately not returned to
    ``approved``.  This prevents a stale one-shot approval from authorizing a
    different retry or target after a partial external side effect.
    """
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("reason is required")
    moment = now or _utcnow()
    with _BUDGET_LOCK, _budget_process_lock(root_dir):
        ledger = _read_budget_ledger(root_dir)
        if ledger.get("corrupted"):
            raise ApprovalError(REASON_LEDGER_CORRUPTED)
        approvals = ledger.get("approvals", {})
        record = approvals.get(approval_id)
        if not isinstance(record, dict):
            raise ApprovalError(REASON_APPROVAL_INVALID)
        if str(record.get("status")) not in {"consuming", "consumed"}:
            raise ApprovalError(REASON_APPROVAL_INVALID)
        updated = dict(record)
        updated.update({"status": "failed", "failed_at": _iso(moment), "rollback_reason": reason.strip()})
        approvals[approval_id] = updated
        ledger["approvals"] = approvals
        ledger["last_updated"] = _iso(moment)
        write_json_atomically(get_canonical_budget_file(root_dir), ledger)
        return dict(updated)


def revoke_approval(
    approval_id: str,
    *,
    actor: str = "operator",
    reason: str = "revoked",
    now: datetime | None = None,
    root_dir: Path | None = None,
) -> dict[str, Any]:
    """Revoke an unconsumed approval atomically and retain the audit record."""
    if not isinstance(actor, str) or not actor.strip():
        raise ValueError("actor is required to revoke an approval")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("reason is required to revoke an approval")
    moment = now or _utcnow()
    with _BUDGET_LOCK, _budget_process_lock(root_dir):
        ledger = _read_budget_ledger(root_dir)
        if ledger.get("corrupted"):
            raise ApprovalError(REASON_LEDGER_CORRUPTED)
        approvals = ledger.get("approvals", {})
        record = approvals.get(approval_id)
        if not isinstance(record, dict) or record.get("status") not in {"approved", "active"}:
            raise ApprovalError(REASON_APPROVAL_INVALID)
        expiry = _parse_datetime(record.get("expires_at"))
        if expiry is not None and expiry <= moment:
            raise ApprovalError(REASON_APPROVAL_INVALID)
        updated = dict(record)
        updated.update({
            "status": "revoked",
            "decision": "revoked",
            "revoked_at": _iso(moment),
            "revoked_by": actor.strip(),
            "revoke_reason": reason.strip(),
        })
        approvals[approval_id] = updated
        ledger["approvals"] = approvals
        ledger["last_updated"] = _iso(moment)
        write_json_atomically(get_canonical_budget_file(root_dir), ledger)
        return dict(updated)


def expire_approvals(
    *,
    now: datetime | None = None,
    root_dir: Path | None = None,
) -> int:
    """Persist expiry transitions for audit and return the number changed."""
    moment = now or _utcnow()
    changed = 0
    with _BUDGET_LOCK, _budget_process_lock(root_dir):
        ledger = _read_budget_ledger(root_dir)
        if ledger.get("corrupted"):
            return 0
        approvals = ledger.get("approvals", {})
        for approval_id, raw in approvals.items():
            if not isinstance(raw, dict):
                continue
            expiry = _parse_datetime(raw.get("expires_at"))
            if expiry is not None and expiry <= moment and raw.get("status") in {"approved", "active"}:
                raw["status"] = "expired"
                raw["decision"] = "expired"
                raw["expired_at"] = _iso(moment)
                changed += 1
        if changed:
            ledger["approvals"] = approvals
            ledger["last_updated"] = _iso(moment)
            write_json_atomically(get_canonical_budget_file(root_dir), ledger)
    return changed
