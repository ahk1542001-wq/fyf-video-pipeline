"""B7 — durable queue with idempotent redelivery.

Covers: client-supplied idempotency-key dedup (a redelivered key produces zero
duplicate jobs), at-least-once delivery, the delivery-ID dedup table, visibility
timeout + crash recovery (``recover_visible``), durable persistence, the
``FYF_QUEUE_ROOT`` override, Path argument round-tripping, and honest queued
state (position + depth). No broker dependency — stdlib/disk only.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.job_queue import JobQueue, default_queue_root

_CALLS: list[tuple] = []


def _handler_record(*args, **kwargs):
    _CALLS.append(("record", args, kwargs))
    return {"ok": True, "n": len(_CALLS)}


def _handler_boom(*args, **kwargs):
    _CALLS.append(("boom", args, kwargs))
    raise RuntimeError("handler failed")


def _handler_path_echo(*args, **kwargs):
    _CALLS.append(("path_echo", args, kwargs))
    return {"path_ok": isinstance(args[0], Path), "second_type": type(args[1]).__name__}


_TARGETS = {"record": _handler_record, "boom": _handler_boom, "path_echo": _handler_path_echo}


def _resolver(target: str):
    if target not in _TARGETS:
        raise KeyError(target)
    return _TARGETS[target]


class _Clock:
    def __init__(self, start: float = 1_000.0):
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, delta: float) -> None:
        self.t += delta


class JobQueueTests(unittest.TestCase):
    def setUp(self):
        _CALLS.clear()
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "queue"
        self.clock = _Clock()

    def tearDown(self):
        self._tmp.cleanup()
        _CALLS.clear()

    def _queue(self, **kwargs) -> JobQueue:
        kwargs.setdefault("visibility_timeout", 300.0)
        kwargs.setdefault("resolver", _resolver)
        kwargs.setdefault("now_fn", self.clock)
        return JobQueue(self.root, **kwargs)

    def test_default_queue_root_honors_env_override(self):
        with patch.dict("os.environ", {"FYF_QUEUE_ROOT": "/tmp/fyf-queue-xyz"}, clear=True):
            self.assertEqual(default_queue_root(), Path("/tmp/fyf-queue-xyz"))

    def test_default_queue_root_falls_back_to_output_tree(self):
        with patch.dict("os.environ", {}, clear=True):
            root = default_queue_root()
        self.assertEqual(root.name, "queue")
        self.assertEqual(root.parent.name, "output")

    def test_submit_creates_a_durable_message(self):
        q = self._queue()
        msg = q.submit(idempotency_key="k1", target="record", args=[1, 2], job_id="job1")
        self.assertTrue(msg.created)
        self.assertFalse(msg.is_duplicate)
        self.assertEqual(msg.job_id, "job1")
        self.assertEqual(q.depth(), 1)
        # Durable: a brand-new queue instance over the same root sees the message.
        self.assertIsNotNone(self._queue().resolve("k1"))

    def test_redelivered_idempotency_key_creates_no_duplicate_job(self):
        q = self._queue()
        first = q.submit(idempotency_key="dup", target="record", job_id="jobD")
        second = q.submit(idempotency_key="dup", target="record", job_id="jobD")
        self.assertEqual(first.message_id, second.message_id)
        self.assertTrue(second.is_duplicate)
        self.assertFalse(second.created)
        self.assertEqual(q.depth(), 1, "a duplicate submission must not enqueue a second job")

    def test_dispatch_runs_handler_exactly_once_then_reports_completed(self):
        q = self._queue()
        msg = q.submit(idempotency_key="once", target="record", args=["a"])
        result = q.dispatch(msg.message_id)
        self.assertEqual(result.status, "dispatched")
        self.assertEqual(len(_CALLS), 1)
        self.assertEqual(q.depth(), 0)
        # Redelivery of the SAME message must not run the handler a second time
        # (the foundation of "no duplicate budget reservation").
        again = q.dispatch(msg.message_id)
        self.assertEqual(again.status, "completed")
        self.assertEqual(len(_CALLS), 1)

    def test_duplicate_submission_then_dispatch_pays_once(self):
        q = self._queue()
        a = q.submit(idempotency_key="pay", target="record")
        b = q.submit(idempotency_key="pay", target="record")  # duplicate
        self.assertEqual(a.message_id, b.message_id)
        q.dispatch(a.message_id)
        q.dispatch(b.message_id)
        self.assertEqual(len(_CALLS), 1, "exactly one dispatch for one idempotency key")

    def test_delivery_id_dedup_table_blocks_a_replayed_delivery(self):
        q = self._queue()
        msg = q.submit(idempotency_key="deliv", target="record")
        did, state = q.claim(msg.message_id, delivery_id="d1")
        self.assertEqual((did, state), ("d1", "claimed"))
        q.ack(msg.message_id, "d1")
        # White-box: force the record back to queued (a broker replay) while the
        # delivery table still records d1 as acked -> the replay is a duplicate.
        record = q._load_record(msg.message_id)
        record["state"] = "queued"
        record["visible_at"] = self.clock()
        q._save_record(record)
        replay_did, replay_state = q.claim(msg.message_id, delivery_id="d1")
        self.assertIsNone(replay_did)
        self.assertEqual(replay_state, "duplicate")
        self.assertEqual(len(_CALLS), 0)

    def test_visibility_timeout_hides_then_recover_visible_redelivers(self):
        q = self._queue(visibility_timeout=300.0)
        msg = q.submit(idempotency_key="vis", target="record")
        did, state = q.claim(msg.message_id)
        self.assertEqual(state, "claimed")
        # Before the timeout the in-flight message is not visible to another worker.
        self.clock.advance(10.0)
        hidden_did, hidden_state = q.claim(msg.message_id)
        self.assertIsNone(hidden_did)
        self.assertEqual(hidden_state, "not_visible")
        # Worker crash + restart: past the timeout, recover_visible re-queues it.
        self.clock.advance(400.0)
        recovered = q.recover_visible()
        self.assertIn(msg.message_id, recovered)
        redid, rstate = q.claim(msg.message_id)
        self.assertEqual(rstate, "claimed")
        self.assertIsNotNone(redid)

    def test_nack_makes_the_message_visible_again_for_retry(self):
        q = self._queue()
        msg = q.submit(idempotency_key="nack", target="record")
        did, _ = q.claim(msg.message_id)
        q.nack(msg.message_id, did, error="transient")
        self.assertEqual(q.get(msg.message_id).state, "queued")
        q.dispatch(msg.message_id)
        self.assertEqual(len(_CALLS), 1)

    def test_dispatch_handler_failure_nacks_and_reraises(self):
        q = self._queue()
        msg = q.submit(idempotency_key="fail", target="boom")
        with self.assertRaises(RuntimeError):
            q.dispatch(msg.message_id)
        record = q._load_record(msg.message_id)
        self.assertEqual(record["state"], "queued")
        self.assertIn("handler failed", record["error"])

    def test_abort_removes_message_and_frees_the_idempotency_key(self):
        q = self._queue()
        msg = q.submit(idempotency_key="abort", target="record")
        q.abort(msg.message_id)
        self.assertIsNone(q.resolve("abort"))
        self.assertEqual(q.depth(), 0)
        fresh = q.submit(idempotency_key="abort", target="record")
        self.assertTrue(fresh.created)

    def test_path_arguments_round_trip_through_persistence(self):
        q = self._queue()
        payload = Path("/tmp/some/job/dir")
        msg = q.submit(idempotency_key="pathy", target="path_echo", args=[payload, "x"])
        self.assertIsInstance(msg.args[0], Path)
        result = q.dispatch(msg.message_id)
        self.assertTrue(result.result["path_ok"])
        self.assertEqual(result.result["second_type"], "str")

    def test_depth_and_position_report_honest_queued_state(self):
        q = self._queue()
        m1 = q.submit(idempotency_key="p1", target="record")
        self.clock.advance(1.0)
        m2 = q.submit(idempotency_key="p2", target="record")
        self.clock.advance(1.0)
        m3 = q.submit(idempotency_key="p3", target="record")
        self.assertEqual(q.depth(), 3)
        self.assertEqual(q.position(m1.message_id), 1)
        self.assertEqual(q.position(m2.message_id), 2)
        self.assertEqual(q.position(m3.message_id), 3)
        state = m3.queued_state()
        self.assertEqual(state["state"], "queued")
        self.assertEqual(state["queue_depth"], 3)
        self.assertIn("queue_position", state)

    def test_submit_requires_key_and_target(self):
        q = self._queue()
        with self.assertRaises(ValueError):
            q.submit(idempotency_key="  ", target="record")
        with self.assertRaises(ValueError):
            q.submit(idempotency_key="k", target="")

    def test_default_resolver_rejects_unknown_target(self):
        from backend.job_queue import _default_resolver

        with self.assertRaises(KeyError):
            _default_resolver("definitely_not_a_real_target")


if __name__ == "__main__":
    unittest.main()
