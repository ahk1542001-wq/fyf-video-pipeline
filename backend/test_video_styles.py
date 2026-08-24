"""Tests for dynamic video styles and presets."""

from __future__ import annotations

import unittest

from backend.video_styles import (
    DEFAULT_STYLE_ID,
    VIDEO_STYLES,
    apply_video_style,
    get_available_styles,
    get_style_config,
)


class VideoStylesTests(unittest.TestCase):
    def test_get_available_styles_returns_three_core_presets(self):
        styles = get_available_styles()
        self.assertEqual(len(styles), 3)
        style_ids = {s["id"] for s in styles}
        self.assertEqual(style_ids, {"fyf_explainer", "cinematic_continuity", "evidence_story"})

    def test_get_style_config_with_valid_and_fallback_ids(self):
        explainer = get_style_config("fyf_explainer")
        self.assertEqual(explainer["id"], "fyf_explainer")

        cinematic = get_style_config("cinematic_continuity")
        self.assertEqual(cinematic["id"], "cinematic_continuity")

        # Unknown style falls back to default
        fallback = get_style_config("unknown_style")
        self.assertEqual(fallback["id"], DEFAULT_STYLE_ID)

        # None falls back to default
        default_cfg = get_style_config(None)
        self.assertEqual(default_cfg["id"], DEFAULT_STYLE_ID)

    def test_apply_video_style_adjusts_cameras_and_motions_preserves_narration(self):
        script = {
            "title": "ဗီဒီယို စတိုင် စမ်းသပ်ချက်",
            "language": "my-MM",
            "segments": [
                {
                    "id": "s1",
                    "text": "စာသား ၁",
                    "visual": {
                        "camera": "wide",
                        "evidence_shots": [
                            {"shot_id": "s1_shot1", "motion_preset": "static"}
                        ],
                    },
                },
                {
                    "id": "s2",
                    "text": "စာသား ၂",
                    "visual": {
                        "camera": "wide",
                        "evidence_shots": [
                            {"shot_id": "s2_shot1", "motion_preset": "static"}
                        ],
                    },
                },
            ],
        }

        # Apply cinematic continuity
        cinematic_script = apply_video_style(script, "cinematic_continuity")
        self.assertEqual(cinematic_script["style_applied"], "cinematic_continuity")
        self.assertEqual(cinematic_script["segments"][0]["text"], "စာသား ၁")
        self.assertEqual(cinematic_script["segments"][1]["text"], "စာသား ၂")
        self.assertEqual(
            cinematic_script["segments"][0]["visual"]["camera"],
            "push_in",
        )
        self.assertEqual(
            cinematic_script["segments"][1]["visual"]["camera"],
            "close_up",
        )

        # Apply evidence story
        evidence_script = apply_video_style(script, "evidence_story")
        self.assertEqual(evidence_script["style_applied"], "evidence_story")
        self.assertEqual(evidence_script["segments"][0]["visual"]["camera"], "wide")

    def test_every_style_stays_within_video_contract_enums(self):
        """Regression: style presets once used out-of-contract values ('medium',
        'mascot_focus', 'zoom_in', 'steady', ...) that crashed the video pipeline
        at plan_visual_treatments validation time."""
        import sys
        from pathlib import Path

        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from backend.test_video_contract import SCRIPT
        from video_contract import VideoScript

        valid_cameras = {"wide", "push_in", "close_up", "over_shoulder"}
        valid_motions = {"slow_push", "pan_left", "pan_right", "drift", "static"}

        for style_id, config in VIDEO_STYLES.items():
            for camera in config["preferred_cameras"]:
                self.assertIn(
                    camera, valid_cameras,
                    f"style {style_id} uses out-of-contract camera {camera!r}",
                )
            for motion in config["preferred_motion_presets"]:
                self.assertIn(
                    motion, valid_motions,
                    f"style {style_id} uses out-of-contract motion {motion!r}",
                )

        for style_id in VIDEO_STYLES:
            styled = apply_video_style(
                {**SCRIPT, "segments": [dict(seg) for seg in SCRIPT["segments"]]},
                style_id,
            )
            # Must survive the strict pipeline contract (extra keys included).
            VideoScript.model_validate(styled)


if __name__ == "__main__":
    unittest.main()
