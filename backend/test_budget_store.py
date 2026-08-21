"""Tests for Budget Store and Cap Enforcement."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.budget_store import (
    get_budget_status,
    is_budget_available,
    record_cost,
)


class BudgetStoreTests(unittest.TestCase):
    def test_budget_store_records_and_aggregates_spend(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)

            # Initial state
            status = get_budget_status(root)
            self.assertEqual(status["total_spend_usd"], 0.0)
            self.assertFalse(status["budget_exceeded"])

            # Record costs
            record_cost(0.045, root)
            record_cost(0.015, root)

            status = get_budget_status(root)
            self.assertEqual(status["total_spend_usd"], 0.06)
            self.assertEqual(status["daily_spend_usd"], 0.06)
            self.assertTrue(is_budget_available(0.05, root))

    def test_budget_store_blocks_when_daily_cap_exceeded(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with patch.dict("os.environ", {"FYF_DAILY_BUDGET_CAP_USD": "0.10"}):
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
        from backend.budget_store import reconcile_budget, reserve_budget

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with patch.dict("os.environ", {"FYF_DAILY_BUDGET_CAP_USD": "0.08"}):
                # We have $0.08 cap. Each job wants $0.05.
                # Only 1 job must succeed in reservation when run concurrently!
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
        from backend.budget_store import get_budget_status, reconcile_budget, reserve_budget

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with patch.dict("os.environ", {"FYF_DAILY_BUDGET_CAP_USD": "1.00"}):
                ok, _ = reserve_budget("job_123", 0.05, root_dir=root)
                self.assertTrue(ok)

                # Reconcile with actual spend $0.03
                reconcile_budget("job_123", 0.03, root_dir=root)
                status1 = get_budget_status(root)
                self.assertEqual(status1["total_spend_usd"], 0.03)

                # Second identical reconciliation must not double-debit
                reconcile_budget("job_123", 0.03, root_dir=root)
                status2 = get_budget_status(root)
                self.assertEqual(status2["total_spend_usd"], 0.03)


if __name__ == "__main__":
    unittest.main()
