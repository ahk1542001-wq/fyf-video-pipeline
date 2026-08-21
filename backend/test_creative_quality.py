import copy
import unittest

from backend.creative_quality import (
    DirectorPolicy,
    audit_creative_quality,
    failed_scene_ids,
    rebalance_creative_rhythm,
)


TREATMENTS = {
    "treatment_type": "object_action",
    "focal_object": "lever",
    "action": "pull",
    "change": "state changes from off to on",
    "visual_world": "workshop",
    "motion_family": "object",
    "text_mode": "none",
    "attention_reset": False,
    "director_reason": "Shows the claimed state change.",
}


def _scene(index, treatment=None, *, start=None, end=None, mascot="none",
           transition="cut", media_type="generated_image", motion_spec=None,
           composition="split_stage"):
    shot = {
        "shot_id": f"S{index}_SHOT", "media_type": media_type,
        "motion_preset": f"preset-{index}", "motion_spec": motion_spec,
        "composition": composition, "mascot_presence": mascot,
        "transition": transition, "treatment": copy.deepcopy(treatment or TREATMENTS),
    }
    scene = {"id": f"S{index}", "visual": {"evidence_shots": [shot]}}
    if start is not None:
        scene["startFrame"], scene["endFrame"] = start, end
    return scene


def _render_input(scenes, fps=30):
    return {"fps": fps, "segments": scenes}


def _codes(report):
    return set(report["failure_codes"])


def _legacy_render_input():
    """Build the old all-diagram shape without relying on private job output."""
    return {
        "fps": 30,
        "segments": [
            {
                "id": f"S{index}",
                "visual": {
                    "evidence_shots": [{
                        "media_type": "motion_graphic",
                        "composition": "focal_center",
                        "mascot_presence": "none",
                        "transition": "cut",
                        "motion_spec": {"layout": "relationship"},
                    }],
                },
            }
            for index in range(1, 13)
        ],
    }


class TestCreativeQuality(unittest.TestCase):
    def test_three_repeated_treatments_fail(self):
        report = audit_creative_quality(_render_input([_scene(i) for i in range(1, 4)]))
        self.assertIn("TREATMENT_RUN_REPEATED", _codes(report))

    def test_centered_motion_diagrams_fail_even_with_different_presets(self):
        scenes = [_scene(i, dict(TREATMENTS, treatment_type="motion_diagram",
                                 focal_object="nodes", action="connect", change="links move",
                                 motion_family="diagram"), composition="focal_center",
                           motion_spec={"preset": f"different-{i}"}, media_type="motion_graphic")
                  for i in range(1, 4)]
        report = audit_creative_quality(_render_input(scenes))
        self.assertIn("CENTER_CARD_SATURATION", _codes(report))

    def test_non_diagram_motion_graphics_are_not_counted_as_center_cards(self):
        scenes = [_scene(i, dict(TREATMENTS, treatment_type="ui_proof"),
                         composition="focal_center", media_type="motion_graphic",
                         motion_spec={"layout": "relationship", "labels": ["က", "ခ"]})
                  for i in range(1, 4)]
        report = audit_creative_quality(_render_input(scenes))
        self.assertNotIn("CENTER_CARD_SATURATION", _codes(report))

    def test_diagram_ratio_over_25_percent_for_at_least_12_scenes(self):
        scenes = [_scene(i, dict(TREATMENTS, treatment_type="motion_diagram",
                                 focal_object="nodes", action="connect", change="links move",
                                 motion_family="diagram"), media_type="motion_graphic",
                            composition="focal_center") if i < 5
                  else _scene(i) for i in range(1, 13)]
        codes = _codes(audit_creative_quality(_render_input(scenes)))
        self.assertIn("CENTER_CARD_SATURATION", codes)
        self.assertIn("MOTION_DIAGRAM_SATURATION", codes)

    def test_attention_reset_is_required_over_rolling_30_seconds(self):
        scenes = [_scene(i, dict(TREATMENTS, attention_reset=False), start=(i - 1) * 300,
                          end=i * 300) for i in range(1, 6)]
        self.assertIn("VISUAL_WORLD_NOT_RESET", _codes(audit_creative_quality(_render_input(scenes))))

    def test_non_kinetic_missing_action_or_change_is_reported_without_crash(self):
        bad = dict(TREATMENTS, action=None, change=None, treatment_type="object_action")
        report = audit_creative_quality(_render_input([_scene(1, bad)]))
        self.assertIn("STATIC_ACTION_MISSING", _codes(report))

    def test_legacy_scene_without_treatment_is_not_static_action_missing(self):
        scene = _scene(1)
        del scene["visual"]["evidence_shots"][0]["treatment"]
        report = audit_creative_quality(_render_input([scene]))
        self.assertNotIn("STATIC_ACTION_MISSING", _codes(report))

    def test_mascot_and_transition_runs(self):
        scenes = [_scene(i, mascot="present", transition="wipe") for i in range(1, 5)]
        codes = _codes(audit_creative_quality(_render_input(scenes)))
        self.assertIn("MASCOT_CADENCE_REPEATED", codes)
        self.assertIn("TRANSITION_RUN_REPEATED", codes)

    def test_policy_threshold_override_changes_result(self):
        scenes = [_scene(i) for i in range(1, 4)]
        default = audit_creative_quality(_render_input(scenes))
        relaxed = audit_creative_quality(_render_input(scenes), DirectorPolicy(max_treatment_run=3))
        self.assertNotEqual(default["passed"], relaxed["passed"])

    def test_rebalance_creative_rhythm_enforces_policy_without_changing_content(self):
        scenes = [_scene(
            i,
            dict(TREATMENTS, treatment_type="motion_diagram"),
            mascot="explain",
            transition="crossfade",
            composition="focal_center",
            media_type="motion_graphic",
            motion_spec={
                "layout": ["relationship", "sequence", "comparison", "count"][i % 4],
                "labels": [f"အချက် {i}", f"ရလဒ် {i}"],
                "values": [str(i), str(i + 1)],
            },
        ) for i in range(1, 13)]
        script = _render_input(scenes)
        locked = [(scene["id"], copy.deepcopy(scene["visual"]["evidence_shots"][0]["motion_spec"]))
                  for scene in scenes]

        result = rebalance_creative_rhythm(script)
        report = audit_creative_quality(result)

        self.assertTrue(report["passed"], report)
        self.assertEqual(
            [(scene["id"], scene["visual"]["evidence_shots"][0]["motion_spec"])
             for scene in result["segments"]],
            locked,
        )

    def test_failed_clusters_are_contiguous_and_ids_are_deduplicated_in_timeline_order(self):
        scenes = [_scene(i) for i in range(1, 7)]
        report = audit_creative_quality(_render_input(scenes))
        clusters = report["failed_clusters"]
        self.assertEqual([c["cluster_id"] for c in clusters], list(range(len(clusters))))
        self.assertEqual(failed_scene_ids({"failed_clusters": [{"scene_ids": ["S3", "S2", "S3"]},
                                                                 {"scene_ids": ["S1", "S2"]}]}), ["S1", "S2", "S3"])

    def test_historical_render_input_fails_with_inferred_legacy_signatures(self):
        report = audit_creative_quality(_legacy_render_input())
        self.assertFalse(report["passed"])
        self.assertTrue(any(set(c["scene_ids"]) & {f"S{i}" for i in range(2, 8)}
                            for c in report["failed_clusters"]))
        self.assertTrue({"CENTER_CARD_SATURATION", "MOTION_DIAGRAM_SATURATION"}.issubset(_codes(report)))


if __name__ == "__main__":
    unittest.main()
