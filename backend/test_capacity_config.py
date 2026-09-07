"""B11 — capacity config: explicit, validated, env-configurable, exposed.

Covers: validated defaults, env override, fail-closed fallback on malformed /
out-of-range values (never silently widened), the typed ``to_runtime_dict``
shape returned by ``GET /api/runtime``, and ``validate_submission`` naming the
specific limit a submission exceeded.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from backend.capacity_config import (
    CapacityConfig,
    LimitViolation,
    load_capacity_config,
    max_concurrent_jobs,
    rate_limit_per_minute,
    validate_submission,
)

_RUNTIME_FIELDS = {
    "max_upload_bytes": int,
    "max_input_duration_seconds": float,
    "max_output_duration_seconds": float,
    "worker_cpu": float,
    "worker_memory_mb": int,
    "worker_wall_time_seconds": float,
    "queue_depth": int,
    "max_concurrent_jobs": int,
    "rate_limit_per_minute": int,
}


class CapacityConfigTests(unittest.TestCase):
    def test_defaults_are_used_when_environment_is_empty(self):
        with patch.dict("os.environ", {}, clear=True):
            cfg = load_capacity_config()
        self.assertEqual(cfg, CapacityConfig())
        self.assertEqual(cfg.config_warnings, ())

    def test_to_runtime_dict_exposes_every_typed_field(self):
        with patch.dict("os.environ", {}, clear=True):
            payload = load_capacity_config().to_runtime_dict()
        self.assertEqual(set(payload), set(_RUNTIME_FIELDS))
        for name, expected_type in _RUNTIME_FIELDS.items():
            with self.subTest(field=name):
                self.assertIsInstance(payload[name], expected_type)

    def test_config_warnings_are_not_exposed_publicly(self):
        with patch.dict("os.environ", {"FYF_QUEUE_DEPTH": "-5"}, clear=True):
            payload = load_capacity_config().to_runtime_dict()
        self.assertNotIn("config_warnings", payload)

    def test_environment_overrides_every_limit(self):
        env = {
            "FYF_MAX_UPLOAD_BYTES": "2048",
            "FYF_MAX_INPUT_DURATION_SECONDS": "120.5",
            "FYF_MAX_OUTPUT_DURATION_SECONDS": "60.25",
            "FYF_WORKER_CPU": "4",
            "FYF_WORKER_MEMORY_MB": "8192",
            "FYF_WORKER_WALL_TIME_SECONDS": "7200",
            "FYF_QUEUE_DEPTH": "16",
            "FYF_MAX_CONCURRENT_JOBS": "3",
            "FYF_RATE_LIMIT_PER_MINUTE": "42",
        }
        with patch.dict("os.environ", env, clear=True):
            cfg = load_capacity_config()
        self.assertEqual(cfg.max_upload_bytes, 2048)
        self.assertEqual(cfg.max_input_duration_seconds, 120.5)
        self.assertEqual(cfg.max_output_duration_seconds, 60.25)
        self.assertEqual(cfg.worker_cpu, 4.0)
        self.assertEqual(cfg.worker_memory_mb, 8192)
        self.assertEqual(cfg.worker_wall_time_seconds, 7200.0)
        self.assertEqual(cfg.queue_depth, 16)
        self.assertEqual(cfg.max_concurrent_jobs, 3)
        self.assertEqual(cfg.rate_limit_per_minute, 42)
        self.assertEqual(cfg.config_warnings, ())

    def test_out_of_range_values_fail_closed_to_default_with_warning(self):
        with patch.dict("os.environ", {"FYF_QUEUE_DEPTH": "-5", "FYF_MAX_CONCURRENT_JOBS": "-1"}, clear=True):
            cfg = load_capacity_config()
        default = CapacityConfig()
        self.assertEqual(cfg.queue_depth, default.queue_depth)
        self.assertEqual(cfg.max_concurrent_jobs, default.max_concurrent_jobs)
        self.assertEqual(len(cfg.config_warnings), 2)
        self.assertTrue(any("FYF_QUEUE_DEPTH" in w for w in cfg.config_warnings))

    def test_malformed_values_fail_closed_to_default_with_warning(self):
        with patch.dict("os.environ", {"FYF_WORKER_CPU": "not-a-number"}, clear=True):
            cfg = load_capacity_config()
        self.assertEqual(cfg.worker_cpu, CapacityConfig().worker_cpu)
        self.assertEqual(len(cfg.config_warnings), 1)
        self.assertIn("FYF_WORKER_CPU", cfg.config_warnings[0])

    def test_validate_submission_flags_only_exceeded_limits(self):
        cfg = CapacityConfig(max_upload_bytes=100, max_input_duration_seconds=10.0, max_output_duration_seconds=5.0)
        self.assertEqual(validate_submission(config=cfg, upload_bytes=50), [])
        violations = validate_submission(config=cfg, upload_bytes=200)
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0].limit, "max_upload_bytes")
        self.assertIn("max_upload_bytes", violations[0].message())

    def test_validate_submission_checks_each_declared_dimension(self):
        cfg = CapacityConfig(max_upload_bytes=100, max_input_duration_seconds=10.0, max_output_duration_seconds=5.0)
        violations = validate_submission(
            config=cfg, upload_bytes=999, input_duration_seconds=99.0, output_duration_seconds=99.0
        )
        self.assertEqual({v.limit for v in violations}, {"max_upload_bytes", "max_input_duration_seconds", "max_output_duration_seconds"})

    def test_validate_submission_skips_undeclared_dimensions(self):
        cfg = CapacityConfig(max_upload_bytes=100)
        self.assertEqual(validate_submission(config=cfg), [])
        self.assertEqual(validate_submission(config=cfg, upload_bytes=None), [])

    def test_limit_violation_to_dict_is_serializable_and_specific(self):
        violation = LimitViolation("max_upload_bytes", 200.0, 100.0, " bytes")
        payload = violation.to_dict()
        self.assertEqual(payload["limit"], "max_upload_bytes")
        self.assertEqual(payload["provided"], 200.0)
        self.assertEqual(payload["allowed"], 100.0)
        self.assertEqual(payload["message"], violation.message())

    def test_module_helpers_reflect_configured_concurrency_and_rate(self):
        with patch.dict("os.environ", {"FYF_MAX_CONCURRENT_JOBS": "7", "FYF_RATE_LIMIT_PER_MINUTE": "9"}, clear=True):
            self.assertEqual(max_concurrent_jobs(), 7)
            self.assertEqual(rate_limit_per_minute(), 9)


if __name__ == "__main__":
    unittest.main()
