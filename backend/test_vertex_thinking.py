import os
import unittest
from unittest.mock import patch

from google.genai import types

from backend.vertex_thinking import (
    DEFAULT_THINKING_LEVELS,
    generation_config_for,
    thinking_level_for,
)


class VertexThinkingPolicyTests(unittest.TestCase):
    def test_defaults_are_stage_specific(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(thinking_level_for("script"), types.ThinkingLevel.MEDIUM)
            self.assertEqual(thinking_level_for("fact"), types.ThinkingLevel.HIGH)
            self.assertEqual(thinking_level_for("visual_verification"), types.ThinkingLevel.HIGH)
            self.assertEqual(DEFAULT_THINKING_LEVELS["lock"], "MEDIUM")

    def test_stage_environment_override_is_applied_to_generation_config(self):
        with patch.dict(os.environ, {"FYF_VERTEX_THINKING_LOCK": "HIGH"}, clear=True):
            config = generation_config_for("lock", response_mime_type="application/json")

        self.assertEqual(config.thinking_config.thinking_level, types.ThinkingLevel.HIGH)
        self.assertEqual(config.response_mime_type, "application/json")

    def test_unsupported_level_falls_back_to_stage_default(self):
        with patch.dict(os.environ, {"FYF_VERTEX_THINKING_SCRIPT": "MINIMAL"}, clear=True):
            self.assertEqual(thinking_level_for("script"), types.ThinkingLevel.MEDIUM)


if __name__ == "__main__":
    unittest.main()
