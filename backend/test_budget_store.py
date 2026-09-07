"""Tests for Budget Store and Cap Enforcement (fail-closed account ceiling).

B2 migration note: paid-path tests here now configure an EXPLICIT account spending
ceiling (FYF_DAILY_BUDGET_CAP_USD + FYF_TOTAL_BUDGET_CAP_USD). Previously they
relied on budget_store's silent $10/day / $50 total default, which was the fail-open
defect fixed in this task. Values match the old defaults so the numeric envelope is
identical; assertions are unchanged. The new fail-closed semantics are verified by
FailClosedCeilingTests, which keep the ceiling genuinely unset/malformed.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.budget_store import (
    PROJECT_DEFAULT_BUDGET_USD,
    REASON_CEILING_UNCONFIGURED,
    get_budget_status,
    get_project_default_budget_usd,
    is_budget_available,
    read_approvals,
    record_approval,
    record_cost,
    reconcile_budget,
    reserve_budget,
)

# Explicit account ceiling for tests that exercise the PAID path. Under the
# corrected fail-closed semantics paid production is disabled unless BOTH caps are
# configured, so every paid-path test must set them (values == old silent defaults).
_CEILING_ENV = {
    "FYF_DAILY_BUDGET_CAP_USD": "10.0",
    "FYF_TOTAL_BUDGET_CAP_USD": "50.0",
}


class BudgetStoreTests(unittest.TestCase):
    def test_budget_store_records_and_aggregates_spend(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            # B2 migration: explicit ceiling replaces the removed silent default.
            with patch.dict("os.environ", _CEILING_ENV):
                status = get_budget_status(root)
                self.assertEqual(status["total_spend_usd"], 0.0)
                self.assertFalse(status["budget_exceeded"])
                self.assertTrue(status["paid_production_enabled"])

                record_cost(0.045, root)
                record_cost(0.015, root)

                status = get_budget_status(root)
                self.assertEqual(status["total_spend_usd"], 0.06)
                self.assertEqual(status["daily_spend_usd"], 0.06)
                self.assertTrue(is_budget_available(0.05, root))

    def test_budget_store_blocks_when_daily_cap_exceeded(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            # B2 migration: added the explicit TOTAL cap; daily cap intent unchanged.
            with patch.dict("os.environ", {"FYF_DAILY_BUDGET_CAP_USD": "0.10", "FYF_TOTAL_BUDGET_CAP_USD": "50.0"}):
                record_cost(0.09, root)
                self.assertTrue(is_budget_available(0.005, root))
                self.assertFalse(is_budget_available(0.02, root))

                record_cost(0.02, root)
                status = get_budget_status(root)
                self.assertTrue(status["budget_exceeded"])
                self.assertIn("Daily budget", status["reason"])
                self.assertFalse(is_budget_available(0.01, root))

    def test_budget_reservation_and_concurrent_contention(self):
        import concurrent.futures

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            # B2 migration: added the explicit TOTAL cap; daily contention intent unchanged.
            with patch.dict("os.environ", {"FYF_DAILY_BUDGET_CAP_USD": "0.08", "FYF_TOTAL_BUDGET_CAP_USD": "50.0"}):
                def try_reserve(op_id):
                    ok, _ = reserve_budget(op_id, 0.05, root_dir=root)
                    return ok

                with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                    f1 = executor.submit(try_reserve, "job_alpha")
                    f2 = executor.submit(try_reserve, "job_beta")
                    results = [f1.result(), f2.result()]

                self.assertEqual(results.count(True), 1, "Exactly one job must acquire budget reservation")
                self.assertEqual(results.count(False), 1, "The second concurrent job must be rejected")

    def test_budget_reconciliation_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            # B2 migration: added the explicit TOTAL cap; reconciliation intent unchanged.
            with patch.dict("os.environ", {"FYF_DAILY_BUDGET_CAP_USD": "1.00", "FYF_TOTAL_BUDGET_CAP_USD": "50.0"}):
                ok, _ = reserve_budget("job_123", 0.05, root_dir=root)
                self.assertTrue(ok)

                reconcile_budget("job_123", 0.03, root_dir=root)
                status1 = get_budget_status(root)
                self.assertEqual(status1["total_spend_usd"], 0.03)

                # Second identical reconciliation must not double-debit
                reconcile_budget("job_123", 0.03, root_dir=root)
                status2 = get_budget_status(root)
                self.assertEqual(status2["total_spend_usd"], 0.03)


class CancellationAccountingTests(unittest.TestCase):
    """B1: cancellation must never erase an incurred provider charge (document line 61)."""

    def test_cancelled_with_incurred_charge_is_debited(self):
        """Requirement 1: cancelled + actual_usd > 0 => total_spend and daily_spend increase."""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with patch.dict("os.environ", _CEILING_ENV):
                ok, _ = reserve_budget("op_cancel_charged", 0.05, root_dir=root)
                self.assertTrue(ok)
                # Provider charged $0.04 even though the operation was cancelled.
                reconcile_budget("op_cancel_charged", 0.04, outcome="cancelled", root_dir=root)
                status = get_budget_status(root)
                self.assertEqual(status["total_spend_usd"], 0.04, "Incurred charge on a cancelled op must be debited")
                self.assertEqual(status["daily_spend_usd"], 0.04)
                self.assertEqual(status["active_reserved_usd"], 0.0, "Reservation must be released on cancellation")

    def test_cancelled_with_no_charge_only_releases_reservation(self):
        """Requirement 2: cancelled + actual_usd == 0 => no debit, reservation released only."""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with patch.dict("os.environ", _CEILING_ENV):
                ok, _ = reserve_budget("op_cancel_free", 0.05, root_dir=root)
                self.assertTrue(ok)
                self.assertEqual(get_budget_status(root)["active_reserved_usd"], 0.05)

                reconcile_budget("op_cancel_free", 0.0, outcome="cancelled", root_dir=root)
                status = get_budget_status(root)
                self.assertEqual(status["total_spend_usd"], 0.0, "No provider charge => no debit")
                self.assertEqual(status["daily_spend_usd"], 0.0)
                self.assertEqual(status["active_reserved_usd"], 0.0, "Reservation released")

    def test_duplicate_reconcile_debits_once(self):
        """Requirement 3: same operation_id reconciled twice => exactly one debit."""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with patch.dict("os.environ", _CEILING_ENV):
                reserve_budget("op_dup", 0.05, root_dir=root)
                reconcile_budget("op_dup", 0.07, outcome="cancelled", root_dir=root)
                reconcile_budget("op_dup", 0.07, outcome="cancelled", root_dir=root)
                self.assertEqual(
                    get_budget_status(root)["total_spend_usd"], 0.07,
                    "Idempotency guard must prevent a duplicate debit",
                )


class FailClosedCeilingTests(unittest.TestCase):
    """B2: the account spending ceiling is fail-closed (document line 62)."""

    def test_unset_ceiling_disables_paid_production_with_honest_reason(self):
        """Requirement 4: unset ceiling => paid disabled, honest reason, no silent numeric default."""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with patch.dict("os.environ", {}, clear=True):
                status = get_budget_status(root)
                self.assertFalse(status["paid_production_enabled"])
                self.assertTrue(status["budget_exceeded"], "No explicit ceiling => paid production must fail closed")
                self.assertEqual(status["reason"], REASON_CEILING_UNCONFIGURED)
                # No silent numeric default remains: caps/remaining are unknown (None), never a number.
                self.assertIsNone(status["daily_cap_usd"])
                self.assertIsNone(status["total_cap_usd"])
                self.assertIsNone(status["remaining_usd"])

                # Paid reservation is refused with the honest reason.
                ok, reason = reserve_budget("op_x", 0.05, root_dir=root)
                self.assertFalse(ok)
                self.assertEqual(reason, REASON_CEILING_UNCONFIGURED)
                self.assertFalse(is_budget_available(0.05, root))

                # Zero-cost / non-paid operations remain allowed (no ceiling required).
                ok0, reason0 = reserve_budget("op_zero", 0.0, root_dir=root)
                self.assertTrue(ok0)
                self.assertIsNone(reason0)
                self.assertTrue(is_budget_available(0.0, root))

    def test_partial_ceiling_is_still_disabled(self):
        """Only one of the two caps set => the account ceiling is not fully explicit => disabled."""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with patch.dict("os.environ", {"FYF_DAILY_BUDGET_CAP_USD": "10.0"}, clear=True):
                status = get_budget_status(root)
                self.assertFalse(status["paid_production_enabled"])
                self.assertTrue(status["budget_exceeded"])
                ok, _ = reserve_budget("op_partial", 0.05, root_dir=root)
                self.assertFalse(ok)

    def test_explicit_ceiling_enforces_normally(self):
        """Requirement 5: explicitly set ceiling => enforcement works normally."""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with patch.dict("os.environ", _CEILING_ENV):
                status = get_budget_status(root)
                self.assertTrue(status["paid_production_enabled"])
                self.assertFalse(status["budget_exceeded"])
                self.assertEqual(status["daily_cap_usd"], 10.0)
                self.assertEqual(status["total_cap_usd"], 50.0)
                ok, _ = reserve_budget("op_ok", 0.05, root_dir=root)
                self.assertTrue(ok)

    def test_malformed_ceiling_fails_closed(self):
        """Requirement 6: malformed/invalid ceiling value => fail-closed."""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with patch.dict("os.environ", {"FYF_DAILY_BUDGET_CAP_USD": "not_a_number", "FYF_TOTAL_BUDGET_CAP_USD": "50.0"}):
                status = get_budget_status(root)
                self.assertFalse(status["paid_production_enabled"])
                self.assertTrue(status["budget_exceeded"])
                self.assertEqual(status["total_spend_usd"], float("inf"))
                ok, _ = reserve_budget("op_bad", 0.05, root_dir=root)
                self.assertFalse(ok)
            with patch.dict("os.environ", {"FYF_DAILY_BUDGET_CAP_USD": "-5", "FYF_TOTAL_BUDGET_CAP_USD": "50.0"}):
                self.assertTrue(get_budget_status(root)["budget_exceeded"], "Negative cap must fail closed")


class BudgetStatusFieldsTests(unittest.TestCase):
    """B2: remaining_usd + separate cost-basis labels; unknown is never reported as 0."""

    def test_remaining_and_cost_basis_labels_are_separate_and_honest(self):
        """Requirement 7: remaining_usd correct; estimated/actual separate; unknown cost != 0."""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with patch.dict("os.environ", _CEILING_ENV):
                reserve_budget("op_r", 0.20, root_dir=root)                       # estimated (reservation)
                reconcile_budget("op_r", 0.10, outcome="completed", root_dir=root)  # actual debited
                reserve_budget("op_open", 0.05, root_dir=root)                    # still-active reservation

                status = get_budget_status(root)
                self.assertEqual(status["actual_usd"], 0.10)
                self.assertEqual(status["total_spend_usd"], 0.10)
                self.assertEqual(status["estimated_usd"], 0.05, "estimated_usd reflects active reservations only")
                self.assertEqual(status["daily_remaining_usd"], round(10.0 - (0.10 + 0.05), 4))
                self.assertEqual(status["total_remaining_usd"], round(50.0 - (0.10 + 0.05), 4))
                self.assertEqual(status["remaining_usd"], round(10.0 - 0.15, 4))
                # Unknown cost bases must be None ("unavailable"), NEVER 0.
                self.assertIsNone(status["provider_reported_usd"])
                self.assertIsNone(status["invoice_confirmed_usd"])


class ProjectDefaultBudgetTests(unittest.TestCase):
    """B2: per-project default budget ($3) is a SEPARATE concept from the account ceiling."""

    def test_project_default_is_three_dollars_and_configurable(self):
        """Requirement 8: per-project default budget == $3, overridable, separate env."""
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(PROJECT_DEFAULT_BUDGET_USD, 3.0)
            self.assertEqual(get_project_default_budget_usd(), 3.0)
        with patch.dict("os.environ", {"FYF_PROJECT_DEFAULT_BUDGET_USD": "7.5"}, clear=True):
            self.assertEqual(get_project_default_budget_usd(), 7.5)
        with patch.dict("os.environ", {"FYF_PROJECT_DEFAULT_BUDGET_USD": "bad"}, clear=True):
            self.assertEqual(get_project_default_budget_usd(), 3.0, "Malformed override falls back to $3 default")

    def test_project_default_is_not_the_account_ceiling(self):
        """The per-project default must never enable paid production or act as the ceiling."""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with patch.dict("os.environ", {"FYF_PROJECT_DEFAULT_BUDGET_USD": "3.0"}, clear=True):
                status = get_budget_status(root)
                self.assertEqual(status["project_default_budget_usd"], 3.0)
                self.assertFalse(status["paid_production_enabled"], "Project default is NOT an account ceiling")
                self.assertIsNone(status["daily_cap_usd"])


class ApprovalRecordTests(unittest.TestCase):
    """B10: persisted approval records (document line 87)."""

    def test_record_and_read_approval_round_trip(self):
        """Requirement 10: record_approval/read_approvals round-trip with all required fields."""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            rec = record_approval(
                "op_publish_v2",
                approved_spend_usd=1.25,
                decision="approved",
                actor="victor",
                target_ref="script@v2/change-set-42",
                approval_id="appr_1",
                root_dir=root,
            )
            self.assertEqual(rec["approval_id"], "appr_1")

            result = read_approvals(root)
            self.assertFalse(result["corrupted"])
            self.assertIn("appr_1", result["approvals"])
            stored = result["approvals"]["appr_1"]
            self.assertEqual(stored["operation"], "op_publish_v2")
            self.assertEqual(stored["target_ref"], "script@v2/change-set-42")
            self.assertEqual(stored["approved_spend_usd"], 1.25)
            self.assertEqual(stored["decision"], "approved")
            self.assertEqual(stored["actor"], "victor")
            self.assertTrue(stored["timestamp"])

    def test_approvals_share_canonical_ledger_and_coexist_with_spend(self):
        """Approvals are written into the SAME canonical ledger and do not corrupt spend."""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            record_cost(0.5, root)
            record_approval("op_x", approved_spend_usd=0.5, decision="approved", actor="victor", approval_id="a", root_dir=root)

            data = json.loads((root / ".budget_ledger.json").read_text(encoding="utf-8"))
            self.assertIn("approvals", data)
            self.assertEqual(data["total_spend_usd"], 0.5)
            self.assertEqual(get_budget_status(root)["total_spend_usd"], 0.5)

    def test_record_approval_rejects_invalid_input(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with self.assertRaises(ValueError):
                record_approval("op", approved_spend_usd=-1.0, decision="approved", actor="v", root_dir=root)
            with self.assertRaises(ValueError):
                record_approval("", approved_spend_usd=1.0, decision="approved", actor="v", root_dir=root)

    def test_approvals_fail_closed_on_corrupt_ledger(self):
        """Requirement 10: corrupt ledger => approvals read/report fail-closed, no write."""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / ".budget_ledger.json").write_text("this is not json", encoding="utf-8")

            result = read_approvals(root)
            self.assertTrue(result["corrupted"])
            self.assertEqual(result["approvals"], {})
            with self.assertRaises(RuntimeError):
                record_approval("op", approved_spend_usd=1.0, decision="approved", actor="v", root_dir=root)


if __name__ == "__main__":
    unittest.main()
