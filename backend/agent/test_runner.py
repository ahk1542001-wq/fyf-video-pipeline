"""Tests for Google ADK Agent tools, producer definition, and runner pipeline."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    from agent import tools as tools_module
    from agent.fyf_producer import create_fyf_producer_agent
    from agent.runner import run_adk_pipeline
    from agent.tools import (
        audit_story_quality,
        draft_story_segments,
        plan_visual_shots,
        research_topic,
    )
except ImportError:
    from backend.agent import tools as tools_module
    from backend.agent.fyf_producer import create_fyf_producer_agent
    from backend.agent.runner import run_adk_pipeline
    from backend.agent.tools import (
        audit_story_quality,
        draft_story_segments,
        plan_visual_shots,
        research_topic,
    )


def _mock_draft():
    return {
        "title": "လယ်ယာကဏ္ဍ အခွင့်အလမ်းများ",
        "language": "my-MM",
        "segments": [
            {
                "id": "s1",
                "text": "ပထမအချက်ကတော့ စိုက်ပျိုးရေး ဖြစ်ပါတယ်။",
                "visual_action": "explain",
                "scene_type": "whiteboard",
                "mascot_action": "explain",
                "emotion": "focused",
                "emphasis": [],
            },
            {
                "id": "s2",
                "text": "ဒုတိယအချက်ကတော့ ရေပေးသွင်းမှု စနစ်ပါ။",
                "visual_action": "explain",
                "scene_type": "demo",
                "mascot_action": "present",
                "emotion": "warm",
                "emphasis": [],
            },
            {
                "id": "s3",
                "text": "တတိယအချက်ကတော့ မြေဆီလွှာ ထိန်းသိမ်းခြင်းပါ။",
                "visual_action": "explain",
                "scene_type": "whiteboard",
                "mascot_action": "think",
                "emotion": "neutral",
                "emphasis": [],
            },
            {
                "id": "s4",
                "text": "စတုတ္ထအချက်ကတော့ ဈေးကွက်ရှာဖွေခြင်း ဖြစ်ပါတယ်။",
                "visual_action": "explain",
                "scene_type": "demo",
                "mascot_action": "warn",
                "emotion": "concerned",
                "emphasis": [],
            },
            {
                "id": "s5",
                "text": "နောက်ဆုံးအနေနဲ့ အောင်မြင်မှု ရယူနိုင်ပါပြီ။",
                "visual_action": "present",
                "scene_type": "whiteboard",
                "mascot_action": "approve",
                "emotion": "confident",
                "emphasis": [],
            },
        ],
    }


def _mock_exact_lock():
    draft = _mock_draft()
    media_types = ["generated_image", "motion_graphic", "generated_image", "motion_graphic", "generated_image"]
    return {
        "title": draft["title"],
        "segments": [
            {
                **seg,
                "visual": {
                    "kind": "generic",
                    "phase": "setup",
                    "camera": "wide",
                    "screen_text": ["လယ်ယာကဏ္ဍ"],
                    "evidence_claims": [
                        {
                            "claim_id": f"{seg['id']}_c1",
                            "statement": "အချက်အလက်",
                            "evidence_type": "concept",
                            "values": [],
                        }
                    ],
                    "evidence_shots": [
                        {
                            "shot_id": f"{seg['id']}_shot1",
                            "proves_claim_ids": [f"{seg['id']}_c1"],
                            "prompt": "ပြကွက်",
                            "caption": f"အဓိက အချက် {i+1}",
                            "hold_fraction": 1.0,
                            "media_type": media_types[i],
                            "motion_preset": "static",
                            "composition": "focal_center",
                            "mascot_presence": "none",
                            "motion_spec": {"layout": "concept", "labels": ["စိုက်ပျိုးရေး"], "values": []} if media_types[i] == "motion_graphic" else None,
                            "asset_path": None,
                            "fallback_asset_path": None,
                            "fallback_used": False,
                            "verification_status": "planned",
                        }
                    ],
                },
            }
            for i, seg in enumerate(draft["segments"])
        ],
    }


class ADKAgentTests(unittest.TestCase):
    def test_research_topic_returns_structured_dossier(self):
        result = research_topic("ဆန်စပါး စိုက်ပျိုးရေး", duration_mode="short")
        self.assertEqual(result["topic"], "ဆန်စပါး စိုက်ပျိုးရေး")
        self.assertEqual(result["duration_mode"], "short")
        self.assertIn("target_audience", result)
        self.assertEqual(result["suggested_segments"], 4)

    def test_draft_story_segments_validates_schema(self):
        with patch.object(
            tools_module,
            "generate_narration_script",
            return_value=_mock_draft(),
        ):
            draft = draft_story_segments("စိုက်ပျိုးရေး", "short")
            self.assertEqual(draft["title"], "လယ်ယာကဏ္ဍ အခွင့်အလမ်းများ")
            self.assertEqual(len(draft["segments"]), 5)

    def test_audit_story_quality_passes_valid_draft(self):
        draft = _mock_draft()
        report = audit_story_quality(draft)
        self.assertTrue(report["passed"])
        self.assertEqual(report["segment_count"], 5)
        self.assertEqual(len(report["issues"]), 0)

    def test_audit_story_quality_fails_empty_segments(self):
        draft = {"title": "ခေါင်းစဉ်", "segments": []}
        report = audit_story_quality(draft)
        self.assertFalse(report["passed"])
        self.assertIn("No segments in draft", report["issues"])

    def test_plan_visual_shots_returns_directed_script(self):
        with patch.object(
            tools_module,
            "generate_exact_lock",
            return_value=_mock_exact_lock(),
        ):
            script = plan_visual_shots("လယ်ယာကဏ္ဍ အခွင့်အလမ်းများ", _mock_draft()["segments"])
            self.assertEqual(script["title"], "လယ်ယာကဏ္ဍ အခွင့်အလမ်းများ")
            self.assertEqual(len(script["segments"]), 5)
            self.assertIn("scene_type", script["segments"][0])

    def test_create_fyf_producer_agent_creates_adk_instance(self):
        agent = create_fyf_producer_agent()
        self.assertEqual(agent.name, "fyf_producer")
        self.assertEqual(len(agent.tools), 4)

    def test_run_adk_pipeline_end_to_end_with_checkpoints(self):
        with patch.object(
            tools_module,
            "generate_narration_script",
            return_value=_mock_draft(),
        ), patch.object(
            tools_module,
            "generate_exact_lock",
            return_value=_mock_exact_lock(),
        ):
            with tempfile.TemporaryDirectory() as temp_dir:
                job_dir = Path(temp_dir)
                result = run_adk_pipeline("စမ်းသပ်ချက်", "short", job_dir=job_dir)

                self.assertEqual(result["script"]["title"], "လယ်ယာကဏ္ဍ အခွင့်အလမ်းများ")
                self.assertEqual(len(result["script"]["segments"]), 5)
                self.assertTrue(result["audit"]["passed"])

                self.assertTrue((job_dir / "research.json").exists())
                self.assertTrue((job_dir / "narration.json").exists())
                self.assertTrue((job_dir / "story_audit.json").exists())
                self.assertTrue((job_dir / "result.json").exists())


if __name__ == "__main__":
    unittest.main()
