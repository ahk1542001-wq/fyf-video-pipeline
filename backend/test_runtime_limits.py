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
            with patch.dict("os.environ", {"FYF_DAILY_BUDGET_CAP_USD": "0.05", "FYF_TOTAL_BUDGET_CAP_USD": "50.0"}):
                record_cost(0.06, root)
                with self.assertRaises(HTTPException) as ctx:
                    enforce_generation_guardrails(client_ip="127.0.0.1", estimated_charge_usd=0.05, root_dir=root)
                self.assertEqual(ctx.exception.status_code, 429)
                self.assertIn("Budget guardrail", ctx.exception.detail)

    def test_guardrail_lease_transactional_rollback_on_slot_rejection(self):
        from backend.budget_store import get_budget_status
        from backend.runtime_limits import acquire_guardrail_lease, register_active_job

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with patch.dict("os.environ", {"FYF_MAX_CONCURRENT_JOBS": "1", "FYF_DAILY_BUDGET_CAP_USD": "10.0", "FYF_TOTAL_BUDGET_CAP_USD": "50.0"}):
                # Occupy the only slot
                register_active_job("job_occupying")

                # Try to acquire lease for job_blocked
                with self.assertRaises(HTTPException) as ctx:
                    acquire_guardrail_lease(
                        operation_id="job_blocked",
                        client_ip="127.0.0.1",
                        estimated_charge_usd=0.05,
                        root_dir=root,
                    )
                self.assertEqual(ctx.exception.status_code, 429)

                # Verify TRANSACTIONAL ROLLBACK: zero budget reserved for job_blocked!
                status_info = get_budget_status(root)
                self.assertEqual(status_info["active_reserved_usd"], 0.0, "Budget reservation must be rolled back on slot failure")
                self.assertEqual(get_active_job_count(), 1, "Only the original job must be active")


class RejectionTaxonomyTests(unittest.TestCase):
    """The 429/queued rejection-reason taxonomy + pinned guardrail ordering.

    Every paid-dispatch refusal must name the exact guardrail that fired in the
    ``X-FYF-Rejection-Reason`` header so a caller can distinguish disabled vs
    exceeded vs insufficient vs rate-limited vs queue-full. Ordering is
    rate -> budget -> slot; these tests pin THAT order instead of a bare 429.
    """

    def setUp(self):
        clear_limits_state()

    def tearDown(self):
        clear_limits_state()

    def _lease(self, root, *, op="op1", ip="127.0.0.1", est=0.05, job_roots=()):
        from backend.runtime_limits import acquire_guardrail_lease

        return acquire_guardrail_lease(
            operation_id=op,
            client_ip=ip,
            estimated_charge_usd=est,
            root_dir=root,
            job_roots=job_roots,
        )

    def test_reason_code_taxonomy_is_complete(self):
        from backend.runtime_limits import (
            REASON_BUDGET_EXCEEDED,
            REASON_BUDGET_INSUFFICIENT,
            REASON_CAPACITY_LIMIT,
            REASON_PAID_DISABLED,
            REASON_QUEUE_FULL,
            REASON_RATE_LIMITED,
            REJECTION_REASON_CODES,
        )

        self.assertEqual(
            REJECTION_REASON_CODES,
            frozenset({
                REASON_RATE_LIMITED,
                REASON_PAID_DISABLED,
                REASON_BUDGET_EXCEEDED,
                REASON_BUDGET_INSUFFICIENT,
                REASON_QUEUE_FULL,
                REASON_CAPACITY_LIMIT,
            }),
        )

    def test_paid_disabled_when_no_explicit_ceiling(self):
        from backend.runtime_limits import REASON_PAID_DISABLED, REJECTION_HEADER

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with patch.dict("os.environ", {}, clear=True):
                with self.assertRaises(HTTPException) as ctx:
                    self._lease(root)
        self.assertEqual(ctx.exception.status_code, 429)
        self.assertEqual(ctx.exception.headers[REJECTION_HEADER], REASON_PAID_DISABLED)

    def test_budget_exceeded_when_ceiling_reached(self):
        from backend.runtime_limits import REASON_BUDGET_EXCEEDED, REJECTION_HEADER

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with patch.dict("os.environ", {"FYF_DAILY_BUDGET_CAP_USD": "0.05", "FYF_TOTAL_BUDGET_CAP_USD": "50.0"}, clear=True):
                record_cost(0.06, root)
                with self.assertRaises(HTTPException) as ctx:
                    self._lease(root)
        self.assertEqual(ctx.exception.headers[REJECTION_HEADER], REASON_BUDGET_EXCEEDED)

    def test_budget_insufficient_when_remaining_below_estimate(self):
        from backend.runtime_limits import REASON_BUDGET_INSUFFICIENT, REJECTION_HEADER

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with patch.dict("os.environ", {"FYF_DAILY_BUDGET_CAP_USD": "0.10", "FYF_TOTAL_BUDGET_CAP_USD": "50.0"}, clear=True):
                with self.assertRaises(HTTPException) as ctx:
                    self._lease(root, est=0.50)
        self.assertEqual(ctx.exception.headers[REJECTION_HEADER], REASON_BUDGET_INSUFFICIENT)

    def test_rate_limit_fires_before_the_budget_gate(self):
        from backend.runtime_limits import REASON_RATE_LIMITED, REJECTION_HEADER

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            # No budget ceiling either: rate must STILL be the guardrail that fires.
            with patch.dict("os.environ", {"FYF_RATE_LIMIT_PER_MINUTE": "1"}, clear=True):
                check_rate_limit("9.9.9.9")  # consume the single allowance
                with self.assertRaises(HTTPException) as ctx:
                    self._lease(root, ip="9.9.9.9")
        self.assertEqual(ctx.exception.headers[REJECTION_HEADER], REASON_RATE_LIMITED)

    def test_budget_gate_fires_before_the_slot_gate(self):
        from backend.runtime_limits import REASON_BUDGET_EXCEEDED, REJECTION_HEADER

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with patch.dict("os.environ", {"FYF_MAX_CONCURRENT_JOBS": "1", "FYF_DAILY_BUDGET_CAP_USD": "0.05", "FYF_TOTAL_BUDGET_CAP_USD": "50.0"}, clear=True):
                register_active_job("occupier")  # slot is ALSO full
                record_cost(0.06, root)          # budget exceeded
                with self.assertRaises(HTTPException) as ctx:
                    self._lease(root, op="x")
        # budget is evaluated before slot => exceeded, NOT queue_full
        self.assertEqual(ctx.exception.headers[REJECTION_HEADER], REASON_BUDGET_EXCEEDED)

    def test_queue_full_is_an_honest_queued_state_with_depth_and_position(self):
        from backend.runtime_limits import (
            QUEUE_DEPTH_HEADER,
            QUEUE_POSITION_HEADER,
            REASON_QUEUE_FULL,
            REJECTION_HEADER,
        )
        from backend.budget_store import get_budget_status

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with patch.dict("os.environ", {"FYF_MAX_CONCURRENT_JOBS": "1", "FYF_DAILY_BUDGET_CAP_USD": "10.0", "FYF_TOTAL_BUDGET_CAP_USD": "50.0"}, clear=True):
                register_active_job("occupier")
                with self.assertRaises(HTTPException) as ctx:
                    self._lease(root, op="blocked")
                headers = ctx.exception.headers
                self.assertEqual(headers[REJECTION_HEADER], REASON_QUEUE_FULL)
                self.assertIn(QUEUE_DEPTH_HEADER, headers)
                self.assertIn(QUEUE_POSITION_HEADER, headers)
                self.assertGreaterEqual(int(headers[QUEUE_POSITION_HEADER]), 1)
                # transactional rollback: the slot rejection left no reservation
                self.assertEqual(get_budget_status(root)["active_reserved_usd"], 0.0)


class ProviderOperationRegistryTests(unittest.TestCase):
    """B8: a provider operation ID gates every paid retry."""

    def setUp(self):
        clear_limits_state()

    def tearDown(self):
        clear_limits_state()

    def test_provider_operation_key_is_stable_and_content_addressed(self):
        from backend.runtime_limits import provider_operation_key

        k1 = provider_operation_key("stage", "a", 1)
        k2 = provider_operation_key("stage", "a", 1)
        k3 = provider_operation_key("stage", "a", 2)
        self.assertEqual(k1, k2)
        self.assertNotEqual(k1, k3)
        self.assertTrue(k1.startswith("stage:"))

    def test_registry_is_passthrough_without_a_telemetry_scope(self):
        from backend.runtime_limits import begin_provider_operation, settle_provider_operation

        guard = begin_provider_operation("op:nokey", attempt=0)
        self.assertFalse(guard.blocked)
        settle_provider_operation("op:nokey", outcome="succeeded", billed=True)
        self.assertFalse(begin_provider_operation("op:nokey", attempt=1).blocked)

    def test_already_billed_operation_blocks_a_second_paid_dispatch(self):
        """EXIT GATE: provider-already-billed => exactly one paid dispatch."""
        from backend.runtime_limits import begin_provider_operation, settle_provider_operation
        from backend.vertex_telemetry import telemetry_scope

        with tempfile.TemporaryDirectory() as temp_dir:
            job_dir = Path(temp_dir) / "job"
            job_dir.mkdir()
            with telemetry_scope("jobid01", "video", job_dir):
                key = "op:billed"
                first = begin_provider_operation(key, attempt=0)
                self.assertFalse(first.blocked)
                dispatches = 1  # the one paid call this attempt issues
                settle_provider_operation(
                    key, outcome="succeeded", billed=True,
                    provider_operation_id=first.provider_operation_id,
                )
                # A redelivery / retry of the SAME operation reconciles the prior
                # provider operation ID and is refused -> no second paid call.
                second = begin_provider_operation(key, attempt=0)
                self.assertTrue(second.blocked)
                self.assertTrue(second.prior_billed)
                self.assertEqual(second.provider_operation_id, first.provider_operation_id)
                if not second.blocked:
                    dispatches += 1
                self.assertEqual(dispatches, 1, "exactly one paid dispatch")
                self.assertTrue((job_dir / "provider_operations.json").is_file())

    def test_non_billed_transient_failure_allows_a_legitimate_retry(self):
        from backend.runtime_limits import (
            begin_provider_operation,
            provider_operation_registry,
            settle_provider_operation,
        )
        from backend.vertex_telemetry import telemetry_scope

        with tempfile.TemporaryDirectory() as temp_dir:
            job_dir = Path(temp_dir) / "job2"
            job_dir.mkdir()
            with telemetry_scope("jobid02", "video", job_dir):
                key = "op:retry"
                first = begin_provider_operation(key, attempt=0)
                settle_provider_operation(
                    key, outcome="retryable_error", billed=False,
                    provider_operation_id=first.provider_operation_id,
                )
                second = begin_provider_operation(key, attempt=1)
                self.assertFalse(second.blocked, "a non-billed failure must not block a retry")
                self.assertFalse(provider_operation_registry().is_settled_paid(key))

    def test_blocked_redispatch_reuses_the_serialized_prior_result(self):
        from backend.runtime_limits import begin_provider_operation, settle_provider_operation
        from backend.vertex_telemetry import telemetry_scope

        with tempfile.TemporaryDirectory() as temp_dir:
            job_dir = Path(temp_dir) / "job3"
            job_dir.mkdir()
            with telemetry_scope("jobid03", "script", job_dir):
                key = "op:result"
                first = begin_provider_operation(key, attempt=0)
                settle_provider_operation(
                    key, outcome="succeeded", billed=True,
                    provider_operation_id=first.provider_operation_id,
                    result_json=json.dumps({"answer": 42}),
                )
                second = begin_provider_operation(key, attempt=0)
                self.assertTrue(second.blocked)
                self.assertEqual(json.loads(second.prior_result_json), {"answer": 42})

    def test_late_provider_cost_reconciles_into_ledger_after_cancellation(self):
        """EXIT GATE: cancel mid-job, then a late provider result that already
        succeeded and billed => the cost STILL lands in the ledger (reconcile on a
        cancelled outcome debits) and the settled-paid op blocks a second dispatch.
        """
        from backend import cancellation
        from backend.budget_store import get_budget_status
        from backend.runtime_limits import (
            acquire_guardrail_lease,
            begin_provider_operation,
            settle_provider_operation,
        )
        from backend.vertex_telemetry import telemetry_scope

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            job_dir = root / "jobc"
            job_dir.mkdir()
            with patch.dict(
                "os.environ",
                {"FYF_DAILY_BUDGET_CAP_USD": "10.0", "FYF_TOTAL_BUDGET_CAP_USD": "50.0"},
                clear=True,
            ):
                lease = acquire_guardrail_lease(
                    operation_id="jobc",
                    client_ip="127.0.0.1",
                    estimated_charge_usd=0.05,
                    root_dir=root,
                )
                key = "op:late"
                with telemetry_scope("jobc", "video", job_dir):
                    guard = begin_provider_operation(key, attempt=0)
                    self.assertFalse(guard.blocked)
                    lease.attach_provider_operation(guard.provider_operation_id)
                    # User cancels mid-render; the in-flight call still succeeds + bills.
                    cancellation.request_cancellation("jobc", job_dir=job_dir, reason="user_requested")
                    self.assertTrue(cancellation.is_cancellation_requested("jobc", job_dir=job_dir))
                    settle_provider_operation(
                        key, outcome="succeeded", billed=True, actual_usd=0.04,
                        provider_operation_id=guard.provider_operation_id,
                    )
                    # settled-paid => a redelivery of the same op is refused.
                    self.assertTrue(begin_provider_operation(key, attempt=0).blocked)
                # Reconcile on the cancelled outcome: incurred cost STILL lands.
                lease.reconcile(0.04, outcome="cancelled")
                status = get_budget_status(root)
                self.assertEqual(status["total_spend_usd"], 0.04)
                self.assertEqual(status["active_reserved_usd"], 0.0)
            cancellation.reset_cancellation_state()

if __name__ == "__main__":
    unittest.main()
