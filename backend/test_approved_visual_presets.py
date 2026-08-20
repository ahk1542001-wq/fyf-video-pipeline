import copy
import json
import unittest
from pathlib import Path

from backend.approved_visual_presets import approved_visual_preset, visual_content_signature


class TestApprovedVisualPresets(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[1]
        self.approved = json.loads((root / "output/jobs/0cd76fbe/render_input.json").read_text())

    def test_exact_approved_content_resolves_preset(self):
        self.assertIsNotNone(approved_visual_preset(self.approved))

    def test_timing_and_voice_do_not_change_semantic_signature(self):
        other_voice = json.loads(
            (Path(__file__).resolve().parents[1] / "output/jobs/c5deb1c9/render_input.json").read_text()
        )
        self.assertEqual(visual_content_signature(self.approved), visual_content_signature(other_voice))

    def test_unrelated_content_cannot_receive_inventory_preset(self):
        unrelated = copy.deepcopy(self.approved)
        unrelated["segments"][0]["text"] = "Different future topic"
        self.assertIsNone(approved_visual_preset(unrelated))


if __name__ == "__main__":
    unittest.main()
