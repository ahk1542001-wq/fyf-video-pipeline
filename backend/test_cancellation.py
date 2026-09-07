"""B9 — cooperative cancellation semantics.

Covers: request/status/checkpoint, the terminal ``cancelled`` state (and that a
late request never downgrades it), durable markers that survive a process
restart, best-effort provider cancel, and the shared ``_apply_cancellation``
route semantics — queued work halts before dispatch, in-flight work seeks the
next safe boundary, and a newer terminal state is never overwritten by a stale
result.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from backend import cancellation
from backend.cancellation import CancellationRegistry, JobCancelledError


class CancellationRegistryTests(unittest.TestCase):
    def setUp(self):
        cancellation.reset_cancellation_state()

    def tearDown(self):
        cancellation.reset_cancellation_state()

    def test_request_sets_cancelling_and_is_requested(self):
        status = cancellation.request_cancellation("job1", reason="user_requested")
        self.assertTrue(status.requested)
        self.assertEqual(status.to_dict()["state"], "cancelling")
        self.assertEqual(status.to_dict()["reason"], "user_requested")
        self.assertTrue(cancellation.is_cancellation_requested("job1"))

    def test_checkpoint_is_a_cheap_noop_when_not_requested(self):
        cancellation.checkpoint("jobX", boundary="pre_dispatch")  # must not raise

    def test_checkpoint_raises_at_safe_boundary_when_requested(self):
        cancellation.request_cancellation("job2", reason="stop")
        with self.assertRaises(JobCancelledError) as ctx:
            cancellation.checkpoint("job2", boundary="between_segments")
        self.assertEqual(ctx.exception.job_id, "job2")
        self.assertEqual(ctx.exception.boundary, "between_segments")

    def test_mark_cancelled_is_terminal_and_never_downgraded(self):
        cancellation.request_cancellation("job3")
        status = cancellation.mark_cancelled("job3", boundary="pre_dispatch")
        self.assertEqual(status.to_dict()["state"], "cancelled")
        # A late-arriving request must NOT downgrade cancelled -> cancelling.
        later = cancellation.request_cancellation("job3")
        self.assertEqual(later.to_dict()["state"], "cancelled")

    def test_durable_marker_survives_a_fresh_registry(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            job_dir = Path(temp_dir) / "job4"
            job_dir.mkdir()
            cancellation.request_cancellation("job4", job_dir=job_dir, reason="crash")
            # Simulate a worker restart: a brand-new registry with no in-memory
            # state must still observe the cancellation from the durable marker.
            fresh = CancellationRegistry()
            self.assertTrue(fresh.is_requested("job4", job_dir=job_dir))
            self.assertEqual(fresh.status("job4", job_dir=job_dir).to_dict()["reason"], "crash")

    def test_clear_removes_in_process_state_and_durable_marker(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            job_dir = Path(temp_dir) / "job5"
            job_dir.mkdir()
            cancellation.request_cancellation("job5", job_dir=job_dir)
            cancellation.clear_cancellation("job5", job_dir=job_dir)
            self.assertFalse(cancellation.is_cancellation_requested("job5", job_dir=job_dir))
            self.assertFalse(CancellationRegistry().is_requested("job5", job_dir=job_dir))

    def test_best_effort_provider_cancel_is_honest_and_never_raises(self):
        self.assertFalse(cancellation.best_effort_provider_cancel(None))
        self.assertTrue(cancellation.best_effort_provider_cancel("op-123", job_id="job6"))


class ApplyCancellationRouteTests(unittest.TestCase):
    """The shared cancel semantics used by both cancel routes in main.py."""

    def setUp(self):
        cancellation.reset_cancellation_state()

    def tearDown(self):
        cancellation.reset_cancellation_state()

    def test_queued_work_halts_before_dispatch_and_is_terminal(self):
        from backend.main import _apply_cancellation

        with tempfile.TemporaryDirectory() as temp_dir:
            job_dir = Path(temp_dir) / "jobq"
            job_dir.mkdir()
            result = _apply_cancellation("jobq", job_dir, {"status": "queued"}, is_script=False)
        self.assertTrue(result["success"])
        self.assertFalse(result["already_terminal"])
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(
            cancellation.cancellation_status("jobq", job_dir=job_dir).to_dict()["state"],
            "cancelled",
        )

    def test_inflight_work_seeks_next_safe_boundary(self):
        from backend.main import _apply_cancellation

        with tempfile.TemporaryDirectory() as temp_dir:
            job_dir = Path(temp_dir) / "jobi"
            job_dir.mkdir()
            result = _apply_cancellation("jobi", job_dir, {"status": "rendering"}, is_script=False)
        self.assertEqual(result["status"], "cancelling")
        self.assertFalse(result["already_terminal"])
        self.assertTrue(cancellation.is_cancellation_requested("jobi", job_dir=job_dir))

    def test_stale_result_cannot_overwrite_a_newer_terminal_state(self):
        from backend.main import _apply_cancellation

        with tempfile.TemporaryDirectory() as temp_dir:
            job_dir = Path(temp_dir) / "jobt"
            job_dir.mkdir()
            for terminal in ("completed", "cancelled", "archived"):
                with self.subTest(terminal=terminal):
                    result = _apply_cancellation("jobt", job_dir, {"status": terminal}, is_script=False)
                    self.assertTrue(result["already_terminal"])
                    self.assertEqual(result["status"], terminal)
            # No cancellation was ever requested for terminal work.
            self.assertFalse(cancellation.is_cancellation_requested("jobt", job_dir=job_dir))


if __name__ == "__main__":
    unittest.main()
