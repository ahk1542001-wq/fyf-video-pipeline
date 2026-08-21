"""Tests for Runtime Limits, Rate Limiting, and Concurrency Guards."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from backend.budget_store import record_cost
from backend.runtime_limits import (
    check_concurrency,
    check_rate_limit,
    clear_limits_state,
    enforce_generation_guardrails,
    get_active_job_count,
    register_active_job,
    release_active_job,
)


class RuntimeLimitsTests(unittest.TestCase):
    def setUp(self):
        clear_limits_state()

    def tearDown(self):
        clear_limits_state()

    def test_concurrency_tracking_and_limit_enforcement(self):
        with patch.dict("os.environ", {"FYF_MAX_CONCURRENT_JOBS": "2"}):
            self.assertEqual(get_active_job_count(), 0)
            ok, _ = check_concurrency()
            self.assertTrue(ok)

            register_active_job("job1")
            register_active_job("job2")
            self.assertEqual(get_active_job_count(), 2)

            ok, reason = check_concurrency()
            self.assertFalse(ok)
            self.assertIn("busy", reason)

            release_active_job("job1")
            self.assertEqual(get_active_job_count(), 1)
            ok, _ = check_concurrency()
            self.assertTrue(ok)

    def test_rate_limiting_sliding_window(self):
        with patch.dict("os.environ", {"FYF_RATE_LIMIT_PER_MINUTE": "3"}):
            ip = "192.168.1.50"
            for _ in range(3):
                ok, _ = check_rate_limit(ip)
                self.assertTrue(ok)

            # 4th request in the same minute should be rejected
            ok, reason = check_rate_limit(ip)
            self.assertFalse(ok)
            self.assertIn("Rate limit exceeded", reason)

    def test_enforce_guardrails_raises_http_exception_on_budget_exceeded(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with patch.dict("os.environ", {"FYF_DAILY_BUDGET_CAP_USD": "0.05"}):
                record_cost(0.06, root)
                with self.assertRaises(HTTPException) as ctx:
                    enforce_generation_guardrails("127.0.0.1", 0.05, root_dir=root)
                self.assertEqual(ctx.exception.status_code, 429)
                self.assertIn("Budget guardrail", ctx.exception.detail)


if __name__ == "__main__":
    unittest.main()
