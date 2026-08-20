import os
import unittest
from unittest.mock import patch

from vertex_model_routing import model_for


class VertexModelRoutingTests(unittest.TestCase):
    def test_text_route_defaults_keep_gemini_3_7_for_retries(self):
        expected = {
            "script": "gemini-3.7-flash",
            "story_polish": "gemini-3.7-flash",
            "story_fallback": "gemini-3.7-flash",
            "fact_extraction": "gemini-3.7-flash",
            "visual_direction": "gemini-3.7-flash",
            "storyboard_direction": "gemini-3.7-flash",
            "visual_verification": "gemini-3.7-flash",
            "visual_verification_fallback": "gemini-3.7-flash",
            "repair": "gemini-3.7-flash",
            "lock": "gemini-3.7-flash",
            "high_volume": "gemini-3.7-flash",
        }
        with patch.dict(os.environ, {}, clear=True):
            for route, model in expected.items():
                with self.subTest(route=route):
                    self.assertEqual(model_for(route), model)

        with patch.dict(os.environ, {"FYF_VERTEX_SCRIPT_MODEL": "custom-model"}):
            self.assertEqual(model_for("script"), "custom-model")

    def test_image_and_video_routes_remain_unchanged(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(model_for("visual_generation"), "gemini-3.1-flash-image")
            self.assertEqual(model_for("visual_generation_quality"), "gemini-3-pro-image")
            self.assertEqual(model_for("video_generation"), "veo-3.1-generate-001")


if __name__ == "__main__":
    unittest.main()
