import copy
import unittest

from backend.creative_quality import audit_creative_quality
from backend.video_director import apply_director_pass, validate_director_contract


def _shot(index: int, media_type: str, mascot: str = "none") -> dict:
    motion = {"layout": "concept", "labels": [f"Beat {index}"], "values": []} if media_type == "motion_graphic" else None
    return {
        "shot_id": f"S{index}_SHOT",
        "proves_claim_ids": [f"S{index}_C1"],
        "prompt": f"Show beat {index}",
        "caption": f"Beat {index}",
        "hold_fraction": 1.0,
        "media_type": media_type,
        "motion_preset": "static",
        "transition": "cut",
        "composition": "focal_center",
        "mascot_presence": mascot,
        "motion_spec": motion,
        "asset_path": None,
        "fallback_asset_path": None,
        "fallback_used": False,
        "verification_status": "planned",
    }


def _script(count: int = 8) -> dict:
    media = ["generated_image", "motion_graphic"] * 4
    return {
        "title": "Director test",
        "language": "my-MM",
        "segments": [{
            "id": f"S{index}", "text": f"စာပိုဒ် {index}", "visual_action": f"Show {index}",
            "scene_type": "demo", "mascot_action": "explain", "emotion": "focused", "emphasis": [],
            "visual": {
                "kind": "generic", "phase": "in_progress", "camera": "wide", "screen_text": [f"Beat {index}"],
                "evidence_claims": [{"claim_id": f"S{index}_C1", "statement": f"Claim {index}", "evidence_type": "concept", "values": []}],
                "evidence_shots": [_shot(index, media[index - 1])],
            },
        } for index in range(1, count + 1)],
    }


class TestVideoDirector(unittest.TestCase):
    def test_valid_direction_preserves_narration(self):
        source = _script()
        result = validate_director_contract(copy.deepcopy(source))
        self.assertEqual(
            [(item["id"], item["text"]) for item in result["segments"]],
            [(item["id"], item["text"]) for item in source["segments"]],
        )

    def test_creative_audit_rejects_repeated_valid_treatments(self):
        source = copy.deepcopy(_script())
        treatment = {
            "treatment_type": "object_action", "focal_object": "lever", "action": "pull",
            "change": "state changes", "visual_world": "workshop", "motion_family": "object",
            "text_mode": "none", "attention_reset": False, "director_reason": "Shows change.",
        }
        for segment in source["segments"]:
            segment["visual"]["evidence_shots"][0]["treatment"] = copy.deepcopy(treatment)
        source["segments"][3]["visual"]["evidence_shots"][0]["treatment"] = dict(
            treatment, treatment_type="story_scene", focal_object="person", action="moves",
            change="person changes position", visual_world="street", motion_family="camera",
        )
        with self.assertRaisesRegex(ValueError, "TREATMENT_RUN_REPEATED"):
            validate_director_contract(source)

    def test_legacy_treatmentless_script_still_validates(self):
        source = copy.deepcopy(_script())
        result = validate_director_contract(source)
        self.assertEqual(
            [(item["id"], item["text"]) for item in result["segments"]],
            [(item["id"], item["text"]) for item in source["segments"]],
        )
        self.assertTrue(all(
            shot.get("treatment") is None
            for segment in result["segments"]
            for shot in segment["visual"]["evidence_shots"]
        ))

    def test_partial_treatment_metadata_is_rejected(self):
        source = copy.deepcopy(_script())
        source["segments"][0]["visual"]["evidence_shots"][0]["treatment"] = {
            "treatment_type": "object_action", "focal_object": "lever", "action": "pull",
            "change": "state changes", "visual_world": "workshop", "motion_family": "object",
            "text_mode": "none", "attention_reset": False, "director_reason": "Shows change.",
        }
        with self.assertRaisesRegex(ValueError, "incomplete"):
            validate_director_contract(source)

    def test_director_pass_drops_partial_optional_treatments(self):
        source = copy.deepcopy(_script())
        source["segments"][0]["visual"]["evidence_shots"][0]["treatment"] = {
            "treatment_type": "object_action", "focal_object": "lever", "action": "pull",
            "change": "state changes", "visual_world": "workshop", "motion_family": "object",
            "text_mode": "none", "attention_reset": False, "director_reason": "Shows change.",
        }

        directed = apply_director_pass(source)

        self.assertTrue(all(
            shot.get("treatment") is None
            for segment in directed["segments"]
            for shot in segment["visual"]["evidence_shots"]
        ))

    def test_director_pass_preserves_mixed_treatment_metadata_and_narration(self):
        source = copy.deepcopy(_script())
        treatments = [
            {"treatment_type": "object_action", "focal_object": "lever", "action": "pull", "change": "state changes", "visual_world": "workshop", "motion_family": "object", "text_mode": "none", "attention_reset": False, "director_reason": "Shows change."},
            {"treatment_type": "story_scene", "focal_object": "person", "action": "moves", "change": "position changes", "visual_world": "street", "motion_family": "camera", "text_mode": "caption", "attention_reset": True, "director_reason": "Shows context."},
        ]
        transitions = ["cut", "crossfade", "wipe", "cut"]
        for index, segment in enumerate(source["segments"]):
            shot = segment["visual"]["evidence_shots"][0]
            shot["treatment"] = copy.deepcopy(treatments[index % 2])
            shot["transition"] = transitions[index % len(transitions)]
        directed = apply_director_pass(source)
        self.assertEqual(
            [(item["id"], item["text"]) for item in directed["segments"]],
            [(item["id"], item["text"]) for item in source["segments"]],
        )
        self.assertEqual(
            [item["visual"]["evidence_shots"][0]["treatment"] for item in directed["segments"]],
            [item["visual"]["evidence_shots"][0]["treatment"] for item in source["segments"]],
        )

    def test_rejects_four_consecutive_single_treatment_segments(self):
        source = _script()
        for index in range(4):
            source["segments"][index]["visual"]["evidence_shots"] = [_shot(index + 1, "generated_image")]
        with self.assertRaisesRegex(ValueError, "four consecutive single-treatment"):
            validate_director_contract(source)

    def test_rejects_adjacent_duplicate_captions(self):
        source = _script()
        source["segments"][1]["visual"]["evidence_shots"][0]["caption"] = "  BEAT 1  "
        with self.assertRaisesRegex(ValueError, "adjacent duplicate caption"):
            validate_director_contract(source)

    def test_rejects_four_consecutive_mascot_segments(self):
        source = _script()
        for index in range(4):
            source["segments"][index]["visual"]["evidence_shots"][0]["mascot_presence"] = "explain"
        with self.assertRaisesRegex(ValueError, "four consecutive mascot"):
            validate_director_contract(source)

    def test_director_pass_limits_mascot_cadence_without_changing_narration(self):
        source = _script()
        for index in range(4):
            source["segments"][index]["visual"]["evidence_shots"][0]["mascot_presence"] = "explain"
        directed = apply_director_pass(source)
        self.assertEqual(
            [(item["id"], item["text"]) for item in directed["segments"]],
            [(item["id"], item["text"]) for item in source["segments"]],
        )
        self.assertEqual(
            directed["segments"][3]["visual"]["evidence_shots"][0]["mascot_presence"],
            "none",
        )

    def test_director_pass_rebalances_transition_rhythm(self):
        source = _script()
        directed = apply_director_pass(source)
        transitions = [
            shot["transition"] for segment in directed["segments"]
            for shot in segment["visual"]["evidence_shots"]
        ]
        self.assertNotEqual(transitions, ["cut"] * len(transitions))
        for index in range(len(transitions) - 3):
            self.assertFalse(
                transitions[index:index + 4]
                == [transitions[index]] * 4
            )


if __name__ == "__main__":
    unittest.main()
