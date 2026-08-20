import json
import inspect
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import writer_agent_vertex
from writer_agent_vertex import generate_story_modes, generate_exact_lock


VALID_STORY_MODES = {
    "variants": [
        {
            "name": "Variant 1",
            "script": {
                "title": "Title 1",
                "language": "my-MM",
                "segments": [
                    {"id": "s1", "text": "scene", "visual_action": "v", "scene_type": "demo", "mascot_action": "present", "emotion": "neutral", "emphasis": [], "visual": {"kind": "generic", "phase": "setup", "camera": "wide", "screen_text": ["t"]}},
                    {"id": "s2", "text": "wrong action", "visual_action": "v", "scene_type": "demo", "mascot_action": "present", "emotion": "neutral", "emphasis": [], "visual": {"kind": "generic", "phase": "setup", "camera": "wide", "screen_text": ["t"]}},
                    {"id": "s3", "text": "root cause", "visual_action": "v", "scene_type": "demo", "mascot_action": "present", "emotion": "neutral", "emphasis": [], "visual": {"kind": "generic", "phase": "setup", "camera": "wide", "screen_text": ["t"]}},
                    {"id": "s4", "text": "human boundary", "visual_action": "v", "scene_type": "demo", "mascot_action": "approve", "emotion": "focused", "emphasis": []},
                    {"id": "s5", "text": "practical ending", "visual_action": "v", "scene_type": "whiteboard", "mascot_action": "present", "emotion": "confident", "emphasis": []},
                ]
            }
        },
        {
            "name": "Variant 2",
            "script": {
                "title": "Title 2",
                "language": "my-MM",
                "segments": [
                    {"id": "s1", "text": "scene", "visual_action": "v", "scene_type": "demo", "mascot_action": "present", "emotion": "neutral", "emphasis": [], "visual": {"kind": "generic", "phase": "setup", "camera": "wide", "screen_text": ["t"]}},
                    {"id": "s2", "text": "wrong action", "visual_action": "v", "scene_type": "demo", "mascot_action": "present", "emotion": "neutral", "emphasis": [], "visual": {"kind": "generic", "phase": "setup", "camera": "wide", "screen_text": ["t"]}},
                    {"id": "s3", "text": "root cause", "visual_action": "v", "scene_type": "demo", "mascot_action": "present", "emotion": "neutral", "emphasis": [], "visual": {"kind": "generic", "phase": "setup", "camera": "wide", "screen_text": ["t"]}},
                    {"id": "s4", "text": "human boundary", "visual_action": "v", "scene_type": "demo", "mascot_action": "approve", "emotion": "focused", "emphasis": []},
                    {"id": "s5", "text": "practical ending", "visual_action": "v", "scene_type": "whiteboard", "mascot_action": "present", "emotion": "confident", "emphasis": []},
                ]
            }
        },
        {
            "name": "Variant 3",
            "script": {
                "title": "Title 3",
                "language": "my-MM",
                "segments": [
                    {"id": "s1", "text": "scene", "visual_action": "v", "scene_type": "demo", "mascot_action": "present", "emotion": "neutral", "emphasis": [], "visual": {"kind": "generic", "phase": "setup", "camera": "wide", "screen_text": ["t"]}},
                    {"id": "s2", "text": "wrong action", "visual_action": "v", "scene_type": "demo", "mascot_action": "present", "emotion": "neutral", "emphasis": [], "visual": {"kind": "generic", "phase": "setup", "camera": "wide", "screen_text": ["t"]}},
                    {"id": "s3", "text": "root cause", "visual_action": "v", "scene_type": "demo", "mascot_action": "present", "emotion": "neutral", "emphasis": [], "visual": {"kind": "generic", "phase": "setup", "camera": "wide", "screen_text": ["t"]}},
                    {"id": "s4", "text": "human boundary", "visual_action": "v", "scene_type": "demo", "mascot_action": "approve", "emotion": "focused", "emphasis": []},
                    {"id": "s5", "text": "practical ending", "visual_action": "v", "scene_type": "whiteboard", "mascot_action": "present", "emotion": "confident", "emphasis": []},
                ]
            }
        }
    ]
}

VALID_EXACT_LOCK_RESPONSE = {
    "segments": [
        {
            "id": "s1",
            "visual_action": "some action",
            "scene_type": "demo",
            "mascot_action": "present",
            "emotion": "focused",
            "emphasis": [],
            "visual": {
                "kind": "generic",
                "phase": "setup",
                "camera": "wide",
                "screen_text": ["test"],
                "evidence_claims": [{
                    "claim_id": "c1", "statement": "Approved fact",
                    "evidence_type": "concept", "values": []
                }],
                "evidence_shots": [{
                    "shot_id": "shot-1", "proves_claim_ids": ["c1"],
                    "prompt": "Show the approved fact as a clear object action",
                    "caption": "test", "hold_fraction": 1,
                    "media_type": "motion_graphic", "motion_preset": "slow_push",
                    "motion_spec": {"layout": "concept", "labels": ["test"], "values": []},
                    "asset_path": None, "fallback_asset_path": None,
                    "verification_status": "planned"
                }]
            }
        }
    ]
}

INVALID_EXACT_LOCK_RESPONSE_MODIFIED_TEXT = {
    "segments": [
        {
            "id": "s1",
            "unexpected_text": "Modified text 1",
            "visual_action": "some action",
            "scene_type": "demo",
            "mascot_action": "present",
            "emotion": "focused",
            "emphasis": [],
            "visual": {
                "kind": "generic",
                "phase": "setup",
                "camera": "wide",
                "screen_text": ["test"],
                "evidence_claims": [{
                    "claim_id": "c1", "statement": "Approved fact",
                    "evidence_type": "concept", "values": []
                }],
                "evidence_shots": [{
                    "shot_id": "shot-1", "proves_claim_ids": ["c1"],
                    "prompt": "Show the approved fact as a clear object action",
                    "caption": "test", "hold_fraction": 1,
                    "media_type": "generated_image", "motion_preset": "slow_push",
                    "motion_spec": None,
                    "asset_path": None, "fallback_asset_path": None,
                    "verification_status": "planned"
                }]
            }
        }
    ]
}


def _compact_visual_response(payload):
    result = json.loads(json.dumps(payload))
    for segment in result["segments"]:
        visual = segment.pop("visual")
        visual.pop("kind")
        segment.update(visual)
    return result


VALID_EXACT_LOCK_RESPONSE = _compact_visual_response(VALID_EXACT_LOCK_RESPONSE)
INVALID_EXACT_LOCK_RESPONSE_MODIFIED_TEXT = _compact_visual_response(INVALID_EXACT_LOCK_RESPONSE_MODIFIED_TEXT)
VALID_STORYBOARD_RESPONSE = {
    "segments": [{
        "id": "s1",
        "evidence_shots": VALID_EXACT_LOCK_RESPONSE["segments"][0]["evidence_shots"],
    }]
}


class StoryModesTests(unittest.TestCase):
    def test_storyboard_requires_mixed_media_for_long_form_story(self):
        source = inspect.getsource(writer_agent_vertex._direct_storyboard)
        self.assertIn("at least two generated story-scene shots", source)

    def test_storyboard_repairs_motion_shot_missing_spec_to_generated_image(self):
        source = inspect.getsource(writer_agent_vertex._direct_storyboard)
        self.assertIn('raw_shot["media_type"] = "generated_image"', source)


    FACT_RESPONSE = {
        "segments": [{
            "id": "s1",
            "claims": [{
                "claim_id": "c1", "statement": "Approved fact",
                "evidence_type": "concept", "values": []
            }],
        }]
    }
    COVERAGE_RESPONSE = {
        "segments": [{"id": "s1", "passed": True, "missing_claims": [], "issues": []}]
    }

    def test_fyf_polish_success(self):
        client = MagicMock()
        client.models.generate_content.return_value = SimpleNamespace(text=json.dumps(VALID_STORY_MODES))

        with patch("writer_agent_vertex.genai.Client", return_value=client):
            result = generate_story_modes("Test Topic")

        self.assertEqual(len(result["variants"]), 3)
        self.assertEqual(result["variants"][0]["name"], "Variant 1")
        self.assertEqual(client.models.generate_content.call_count, 1)
        config = client.models.generate_content.call_args.kwargs["config"]
        self.assertIsNone(config.response_schema)
        self.assertNotIn('"visual"', json.dumps(config.response_json_schema))
        self.assertNotIn("discriminator", json.dumps(config.response_json_schema))
        self.assertEqual(
            client.models.generate_content.call_args.kwargs["model"],
            "gemini-3.7-flash",
        )

    def test_fyf_polish_repair(self):
        client = MagicMock()
        client.models.generate_content.side_effect = [
            SimpleNamespace(text="not-json"),
            SimpleNamespace(text=json.dumps(VALID_STORY_MODES)),
        ]

        with patch("writer_agent_vertex.genai.Client", return_value=client), patch.dict("os.environ", {"FYF_VERTEX_MAX_ATTEMPTS": "2"}):
            result = generate_story_modes("Test Topic")

        self.assertEqual(len(result["variants"]), 3)
        self.assertEqual(client.models.generate_content.call_count, 2)
        lock_config = client.models.generate_content.call_args_list[1].kwargs["config"]
        self.assertIsNotNone(lock_config.response_json_schema)
        self.assertNotIn("discriminator", json.dumps(lock_config.response_json_schema))
        self.assertNotIn('"kind"', json.dumps(lock_config.response_json_schema))
        self.assertEqual(
            client.models.generate_content.call_args_list[1].kwargs["model"],
            "gemini-3.7-flash",
        )
        self.assertEqual(
            [call.kwargs["model"] for call in client.models.generate_content.call_args_list],
            ["gemini-3.7-flash", "gemini-3.7-flash"],
        )


    def test_exact_lock_preserves_narration(self):
        client = MagicMock()
        client.models.generate_content.side_effect = [
            SimpleNamespace(text=json.dumps(self.FACT_RESPONSE)),
            SimpleNamespace(text=json.dumps(self.COVERAGE_RESPONSE)),
            SimpleNamespace(text=json.dumps(VALID_EXACT_LOCK_RESPONSE)),
            SimpleNamespace(text=json.dumps(VALID_STORYBOARD_RESPONSE)),
        ]

        req = {
            "title": "Approved Title",
            "approved_segments": [
                {"id": "s1", "text": "Approved text 1"}
            ]
        }

        with patch("writer_agent_vertex.genai.Client", return_value=client):
            result = generate_exact_lock(req)

        self.assertEqual(result["segments"][0]["text"], "Approved text 1")
        self.assertEqual(client.models.generate_content.call_count, 4)

    def test_exact_lock_keeps_fact_agent_claims_canonical(self):
        lock_response = json.loads(json.dumps(VALID_EXACT_LOCK_RESPONSE))
        lock_response["segments"][0]["evidence_claims"][0]["statement"] = "Director rewrite"
        client = MagicMock()
        client.models.generate_content.side_effect = [
            SimpleNamespace(text=json.dumps(self.FACT_RESPONSE)),
            SimpleNamespace(text=json.dumps(self.COVERAGE_RESPONSE)),
            SimpleNamespace(text=json.dumps(lock_response)),
            SimpleNamespace(text=json.dumps(VALID_STORYBOARD_RESPONSE)),
        ]
        req = {"title": "Approved Title", "approved_segments": [{"id": "s1", "text": "Approved text 1"}]}

        with patch("writer_agent_vertex.genai.Client", return_value=client):
            result = generate_exact_lock(req)

        self.assertEqual(
            result["segments"][0]["visual"]["evidence_claims"][0]["statement"],
            "Approved fact",
        )

    def test_exact_lock_retries_storyboard_on_the_configured_flash_route(self):
        class TransientVertexError(RuntimeError):
            code = 504

        client = MagicMock()
        client.models.generate_content.side_effect = [
            SimpleNamespace(text=json.dumps(self.FACT_RESPONSE)),
            SimpleNamespace(text=json.dumps(self.COVERAGE_RESPONSE)),
            SimpleNamespace(text=json.dumps(VALID_EXACT_LOCK_RESPONSE)),
            TransientVertexError("transient storyboard deadline"),
            SimpleNamespace(text=json.dumps(VALID_STORYBOARD_RESPONSE)),
        ]
        req = {"title": "Approved Title", "approved_segments": [{"id": "s1", "text": "Approved text 1"}]}

        with patch("writer_agent_vertex.genai.Client", return_value=client), patch(
            "writer_agent_vertex.time.sleep"
        ) as sleep, patch.dict(
            "os.environ", {"FYF_VERTEX_MAX_ATTEMPTS": "2", "FYF_VERTEX_RETRY_BASE_SECONDS": "1"}
        ):
            result = generate_exact_lock(req)

        self.assertEqual(result["segments"][0]["text"], "Approved text 1")
        storyboard_calls = [
            call for call in client.models.generate_content.call_args_list
            if "Create the final ordered evidence-shot storyboard" in call.kwargs["contents"]
        ]
        self.assertEqual(
            [call.kwargs["model"] for call in storyboard_calls],
            ["gemini-3.7-flash", "gemini-3.7-flash"],
        )
        sleep.assert_called_once_with(1.0)

    def test_exact_lock_retries_transient_fact_call_before_job_retry(self):
        class TransientVertexError(RuntimeError):
            code = 429

        client = MagicMock()
        client.models.generate_content.side_effect = [
            TransientVertexError("temporary fact quota"),
            SimpleNamespace(text=json.dumps(self.FACT_RESPONSE)),
            SimpleNamespace(text=json.dumps(self.COVERAGE_RESPONSE)),
            SimpleNamespace(text=json.dumps(VALID_EXACT_LOCK_RESPONSE)),
            SimpleNamespace(text=json.dumps(VALID_STORYBOARD_RESPONSE)),
        ]
        req = {"title": "Approved Title", "approved_segments": [{"id": "s1", "text": "Approved text 1"}]}

        with patch("writer_agent_vertex.genai.Client", return_value=client), patch(
            "writer_agent_vertex.time.sleep"
        ) as sleep, patch.dict(
            "os.environ", {"FYF_VERTEX_MAX_ATTEMPTS": "2", "FYF_VERTEX_RETRY_BASE_SECONDS": "1"}
        ):
            result = generate_exact_lock(req)

        self.assertEqual(result["segments"][0]["text"], "Approved text 1")
        self.assertEqual(client.models.generate_content.call_count, 5)
        sleep.assert_called_once_with(1.0)

    def test_exact_lock_prompt_limits_screen_text_to_two_labels(self):
        client = MagicMock()
        client.models.generate_content.side_effect = [
            SimpleNamespace(text=json.dumps(self.FACT_RESPONSE)),
            SimpleNamespace(text=json.dumps(self.COVERAGE_RESPONSE)),
            SimpleNamespace(text=json.dumps(VALID_EXACT_LOCK_RESPONSE)),
            SimpleNamespace(text=json.dumps(VALID_STORYBOARD_RESPONSE)),
        ]
        req = {"title": "Approved Title", "approved_segments": [{"id": "s1", "text": "Approved text 1"}]}

        with patch("writer_agent_vertex.genai.Client", return_value=client):
            generate_exact_lock(req)

        exact_lock_config = client.models.generate_content.call_args_list[2].kwargs["config"]
        self.assertIn("screen_text field MUST contain 1 or 2 strings only", exact_lock_config.system_instruction)

    def test_visual_prompts_state_non_schema_contract_constraints(self):
        client = MagicMock()
        client.models.generate_content.side_effect = [
            SimpleNamespace(text=json.dumps(self.FACT_RESPONSE)),
            SimpleNamespace(text=json.dumps(self.COVERAGE_RESPONSE)),
            SimpleNamespace(text=json.dumps(VALID_EXACT_LOCK_RESPONSE)),
            SimpleNamespace(text=json.dumps(VALID_STORYBOARD_RESPONSE)),
        ]
        req = {"title": "Approved Title", "approved_segments": [{"id": "s1", "text": "Approved text 1"}]}

        with patch("writer_agent_vertex.genai.Client", return_value=client):
            generate_exact_lock(req)

        exact_lock_config = client.models.generate_content.call_args_list[2].kwargs["config"]
        storyboard_contents = client.models.generate_content.call_args_list[3].kwargs["contents"]
        self.assertIn("motion_spec.relation_mode field may be present only when the layout is", exact_lock_config.system_instruction)
        self.assertIn("relationship; omit relation_mode for every other layout", exact_lock_config.system_instruction)
        self.assertIn("Every non-kinetic treatment MUST include focal_object, action, and change", exact_lock_config.system_instruction)
        self.assertIn("Every non-kinetic treatment MUST include focal_object, action, and change", storyboard_contents)

    def test_invalid_optional_treatments_are_dropped_before_lock_validation(self):
        invalid_treatment = {
            "treatment_type": "ui_proof",
            "visual_world": "checkout screen",
            "motion_family": "interface",
            "text_mode": "label",
            "attention_reset": True,
            "director_reason": "Shows the approval state.",
        }
        lock_response = json.loads(json.dumps(VALID_EXACT_LOCK_RESPONSE))
        storyboard_response = json.loads(json.dumps(VALID_STORYBOARD_RESPONSE))
        lock_response["segments"][0]["evidence_shots"][0]["treatment"] = invalid_treatment
        storyboard_response["segments"][0]["evidence_shots"][0]["treatment"] = invalid_treatment
        client = MagicMock()
        client.models.generate_content.side_effect = [
            SimpleNamespace(text=json.dumps(self.FACT_RESPONSE)),
            SimpleNamespace(text=json.dumps(self.COVERAGE_RESPONSE)),
            SimpleNamespace(text=json.dumps(lock_response)),
            SimpleNamespace(text=json.dumps(storyboard_response)),
        ]
        req = {"title": "Approved Title", "approved_segments": [{"id": "s1", "text": "Approved text 1"}]}

        with patch("writer_agent_vertex.genai.Client", return_value=client), patch.dict(
            "os.environ", {"FYF_VERTEX_MAX_ATTEMPTS": "1"}
        ):
            result = generate_exact_lock(req)

        self.assertIsNone(result["segments"][0]["visual"]["evidence_shots"][0]["treatment"])

    def test_exact_lock_fact_and_visual_models_use_global_endpoint(self):
        client = MagicMock()
        client.models.generate_content.side_effect = [
            SimpleNamespace(text=json.dumps(self.FACT_RESPONSE)),
            SimpleNamespace(text=json.dumps(self.COVERAGE_RESPONSE)),
            SimpleNamespace(text=json.dumps(VALID_EXACT_LOCK_RESPONSE)),
            SimpleNamespace(text=json.dumps(VALID_STORYBOARD_RESPONSE)),
        ]
        req = {"title": "Approved Title", "approved_segments": [{"id": "s1", "text": "Approved text 1"}]}
        with patch("writer_agent_vertex.genai.Client", return_value=client) as client_factory:
            generate_exact_lock(req)
        self.assertEqual(client_factory.call_args_list[0].kwargs["location"], "global")
        self.assertEqual(client_factory.call_args_list[1].kwargs["location"], "global")
        self.assertEqual(client_factory.call_args_list[2].kwargs["location"], "global")
        self.assertEqual(client_factory.call_args_list[3].kwargs["location"], "global")


    def test_exact_lock_rejects_unexpected_narration_field(self):
        client = MagicMock()
        client.models.generate_content.side_effect = [
            SimpleNamespace(text=json.dumps(self.FACT_RESPONSE)),
            SimpleNamespace(text=json.dumps(self.COVERAGE_RESPONSE)),
            SimpleNamespace(text=json.dumps(INVALID_EXACT_LOCK_RESPONSE_MODIFIED_TEXT)), # First tries modified text
            SimpleNamespace(text=json.dumps(VALID_EXACT_LOCK_RESPONSE)), # Second attempt gives correct text
            SimpleNamespace(text=json.dumps(VALID_STORYBOARD_RESPONSE)),
        ]

        req = {
            "title": "Approved Title",
            "approved_segments": [
                {"id": "s1", "text": "Approved text 1"}
            ]
        }

        with patch("writer_agent_vertex.genai.Client", return_value=client), patch.dict("os.environ", {"FYF_VERTEX_MAX_ATTEMPTS": "2"}):
            result = generate_exact_lock(req)

        self.assertEqual(result["segments"][0]["text"], "Approved text 1")
        self.assertEqual(client.models.generate_content.call_count, 5)

    def test_exact_lock_fails_when_fact_agent_omits_narration_claim(self):
        client = MagicMock()
        client.models.generate_content.side_effect = [
            SimpleNamespace(text=json.dumps(self.FACT_RESPONSE)),
            SimpleNamespace(text=json.dumps({"segments": [{"id": "s1", "passed": False, "missing_claims": ["missing consequence"], "issues": []}]})),
            SimpleNamespace(text=json.dumps(self.FACT_RESPONSE)),
            SimpleNamespace(text=json.dumps({"segments": [{"id": "s1", "passed": False, "missing_claims": ["still missing"], "issues": []}]})),
        ]
        req = {"title": "Approved Title", "approved_segments": [{"id": "s1", "text": "Approved fact and consequence"}]}
        with patch("writer_agent_vertex.genai.Client", return_value=client):
            with self.assertRaisesRegex(ValueError, "claim coverage incomplete"):
                generate_exact_lock(req)
        self.assertEqual(client.models.generate_content.call_count, 4)

    def test_exact_lock_repairs_fact_omission_then_continues(self):
        client = MagicMock()
        incomplete = {"segments": [{"id": "s1", "claims": [{"claim_id": "c1", "statement": "Approved", "evidence_type": "concept", "values": []}]}]}
        client.models.generate_content.side_effect = [
            SimpleNamespace(text=json.dumps(incomplete)),
            SimpleNamespace(text=json.dumps({"segments": [{"id": "s1", "passed": False, "missing_claims": ["fact"], "issues": []}]})),
            SimpleNamespace(text=json.dumps(self.FACT_RESPONSE)),
            SimpleNamespace(text=json.dumps(self.COVERAGE_RESPONSE)),
            SimpleNamespace(text=json.dumps(VALID_EXACT_LOCK_RESPONSE)),
            SimpleNamespace(text=json.dumps(VALID_STORYBOARD_RESPONSE)),
        ]
        req = {"title": "Approved Title", "approved_segments": [{"id": "s1", "text": "Approved text 1"}]}
        with patch("writer_agent_vertex.genai.Client", return_value=client):
            result = generate_exact_lock(req)
        self.assertEqual(result["segments"][0]["text"], "Approved text 1")
        self.assertIn("coverage auditor found omissions", client.models.generate_content.call_args_list[2].kwargs["contents"])

if __name__ == "__main__":
    unittest.main()
