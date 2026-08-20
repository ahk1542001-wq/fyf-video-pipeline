import unittest
from pydantic import ValidationError

from backend.director_context import DirectorPolicy, build_director_context


class DirectorContextTests(unittest.TestCase):
    @staticmethod
    def _segments(count=6):
        def treatment(index):
            return {
                "treatment_type": "story_scene" if index < 3 else "kinetic_type",
                "focal_object": f"object-{index}",
                "action": f"action-{index}",
                "change": f"change-{index}",
                "visual_world": f"world-{index}",
                "motion_family": "camera",
                "text_mode": "caption" if index % 2 == 0 else "label",
                "attention_reset": False,
                "director_reason": "reason",
            }

        return [
            {
                "id": f"s{i}",
                "text": f"text {i}",
                "visual": {
                    "screen_text": [f"line {i} second", f"line {i} third"] if i % 2 else [f"line {i}"],
                    "evidence_shots": [
                        {
                            "treatment": treatment(i),
                            "composition": f"composition-{i}",
                            "mascot_presence": "reaction" if i % 2 else "none",
                        }
                    ],
                },
            }
            for i in range(count)
        ]

    def test_builds_five_treatment_history_from_accepted_nested_shots(self):
        context = build_director_context(self._segments(), 5, DirectorPolicy())
        self.assertEqual([item.focal_object for item in context.previous_treatments], [f"object-{i}" for i in range(5)])
        self.assertEqual(context.recent_compositions, [f"composition-{i}" for i in range(5)])
        self.assertEqual(context.recent_mascot_presence, ["none", "reaction", "none", "reaction", "none"])
        self.assertEqual(context.recent_text_modes, ["caption", "label", "caption", "label", "caption"])
        self.assertEqual(context.recent_text_density, [1, 2, 1, 2, 1])

    def test_prohibits_treatment_after_configured_repeated_run(self):
        segments = self._segments(4)
        for segment in segments:
            segment["visual"]["evidence_shots"][0]["treatment"]["treatment_type"] = "story_scene"
        context = build_director_context(segments, 3, DirectorPolicy(prohibited_run_length=3))
        self.assertEqual(context.prohibited_treatments, ["story_scene"])

    def test_accepts_supported_top_level_treatment_history(self):
        treatment = {"treatment_type": "story_scene", "focal_object": "o", "action": "a", "change": "c", "visual_world": "w", "motion_family": "camera", "text_mode": "none", "attention_reset": False, "director_reason": "r"}
        segments = [{"id": "s0", "text": "text", "treatment": treatment}, {"id": "s1", "text": "text"}]
        context = build_director_context(segments, 1, DirectorPolicy())
        self.assertEqual([item.treatment_type for item in context.previous_treatments], ["story_scene"])
        self.assertEqual(context.previous_treatments[0].change, "c")


if __name__ == "__main__":
    unittest.main()
