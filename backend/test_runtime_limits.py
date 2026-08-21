"""Tests for Runtime Limits, Rate Limiting, and Concurrency Guards."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi import HTTPException

from backend.budget_store import record_cost
from backend.runtime_limits import (
    check_concurrency,
    check_rate_limit,
    clear_limits_state,
    count_active_disk_jobs,
    enforce_generation_guardrails,
    get_active_job_count,
    get_client_ip,
    register_active_job,
    release_active_job,
    try_acquire_job_slot,
)


class RuntimeLimitsTests(unittest.TestCase):
    def setUp(self):
        clear_limits_state()

    def tearDown(self):
        clear_limits_state()

    def test_client_ip_ignores_untrusted_forwarded_headers_by_default(self):
        # By default, untrusted proxies cannot spoof client IP
        req = MagicMock()
        req.headers = {"x-forwarded-for": "10.0.0.1", "x-real-ip": "10.0.0.2"}
        req.client.host = "192.168.1.100"

        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(get_client_ip(req), "192.168.1.100")

    def test_client_ip_trusts_forwarded_headers_when_explicitly_configured(self):
        req = MagicMock()
        req.headers = {"x-forwarded-for": "203.0.113.195, 70.41.3.18"}
        req.client.host = "127.0.0.1"

        with patch.dict("os.environ", {"FYF_TRUST_PROXY_HEADERS": "true"}):
            self.assertEqual(get_client_ip(req), "203.0.113.195")

    def test_try_acquire_job_slot_enforces_default_concurrency_one(self):
        with patch.dict("os.environ", {}, clear=True):
            # Default concurrency must be 1
            ok1, _ = try_acquire_job_slot("job_1")
            self.assertTrue(ok1)
            self.assertEqual(get_active_job_count(), 1)

            # Second job must be blocked
            ok2, reason = try_acquire_job_slot("job_2")
            self.assertFalse(ok2)
            self.assertIn("busy", reason)

            release_active_job("job_1")
            self.assertEqual(get_active_job_count(), 0)

            ok3, _ = try_acquire_job_slot("job_2")
            self.assertTrue(ok3)

    def test_count_active_disk_jobs_includes_script_and_video_jobs(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            jobs_root = temp_path / "jobs"
            script_root = temp_path / "script-jobs"
            jobs_root.mkdir()
            script_root.mkdir()

            # Active video job
            v1 = jobs_root / "v001"
            v1.mkdir()
            (v1 / "status.json").write_text(json.dumps({"status": "rendering"}))

            # Completed video job (not active)
            v2 = jobs_root / "v002"
            v2.mkdir()
            (v2 / "status.json").write_text(json.dumps({"status": "completed"}))

            # Active script job
            s1 = script_root / "s001"
            s1.mkdir()
            (s1 / "status.json").write_text(json.dumps({"status": "writing"}))

            count = count_active_disk_jobs((jobs_root, script_root))
            self.assertEqual(count, 2)

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
                    enforce_generation_guardrails(client_ip="127.0.0.1", estimated_charge_usd=0.05, root_dir=root)
                self.assertEqual(ctx.exception.status_code, 429)
                self.assertIn("Budget guardrail", ctx.exception.detail)


if __name__ == "__main__":
    unittest.main()
