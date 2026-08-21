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


if __name__ == "__main__":
    unittest.main()
