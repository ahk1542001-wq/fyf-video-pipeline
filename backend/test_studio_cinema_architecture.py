"""Unit and integration tests for Agentic Cinema Studio multi-role architecture."""

from __future__ import annotations

import json
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from backend.agent.fyf_producer import (
    PRODUCER_INSTRUCTION,
    build_producer_instruction,
    create_fyf_producer_agent,
)
from backend.main import VideoRequest
from backend.video_director import apply_director_pass
from backend.video_styles import GENRE_STYLES, get_available_genres, get_style_config
from video_contract import ExactLockRequest, ScriptGenerationRequest, StoryDraftScript, VideoScript
from voice_service.gemini_tts import ENGLISH_STYLE_PROMPTS, STUDIO_VOICES
from writer_agent_vertex import (
    build_exact_lock_instruction,
    generate_story_modes,
    lock_narration_in_batches,
)


class StudioCinemaArchitectureTests(unittest.TestCase):
    def test_script_generation_request_defaults(self):
        req = ScriptGenerationRequest(topic="Artificial Intelligence in Film")
        self.assertEqual(req.studio_name, "FYF Studio")
        self.assertEqual(req.language, "my-MM")
        self.assertEqual(req.genre, "explainer")
        self.assertEqual(req.presenter_mode, "on_screen")
        self.assertEqual(req.voice_actor, "Sadaltager")
        self.assertEqual(req.duration_mode, "short")

    def test_script_generation_request_custom(self):
        req = ScriptGenerationRequest(
            topic="Next-Gen Agentic Workflows",
            duration_mode="standard",
            studio_name="Cinema AI Lab",
            language="en-US",
            genre="tech_explainer",
            presenter_mode="voiceover_only",
            voice_actor="Puck",
        )
        self.assertEqual(req.studio_name, "Cinema AI Lab")
        self.assertEqual(req.language, "en-US")
        self.assertEqual(req.genre, "tech_explainer")
        self.assertEqual(req.presenter_mode, "voiceover_only")
        self.assertEqual(req.voice_actor, "Puck")

    def test_video_script_studio_fields_roundtrip(self):
        script_dict = {
            "title": "Quantum Computing",
            "language": "en-US",
            "studio_name": "Quantum Studio",
            "genre": "cinematic_documentary",
            "presenter_mode": "voiceover_only",
            "voice_actor": "Fenrir",
            "segments": [
                {
                    "id": "s1",
                    "text": "Quantum bits exist in superposition.",
                    "visual_action": "explain",
                    "scene_type": "whiteboard",
                    "mascot_action": "explain",
                    "emotion": "focused",
                    "emphasis": [],
                }
            ],
        }
        script = VideoScript.model_validate(script_dict)
        dumped = script.model_dump(mode="json", exclude_none=True)
        self.assertEqual(dumped["studio_name"], "Quantum Studio")
        self.assertEqual(dumped["language"], "en-US")
        self.assertEqual(dumped["genre"], "cinematic_documentary")
        self.assertEqual(dumped["presenter_mode"], "voiceover_only")
        self.assertEqual(dumped["voice_actor"], "Fenrir")

    def test_producer_instruction_burmese_flagship(self):
        instruction = build_producer_instruction(
            language="my-MM",
            genre="explainer",
            presenter_mode="on_screen",
            studio_name="FYF Studio",
        )
        self.assertIn("Burmese", instruction)
        self.assertIn("Executive Producer Agent", instruction)
        self.assertIn("Ko Kyaw", instruction)

    def test_producer_instruction_english_cinema(self):
        instruction = build_producer_instruction(
            language="en-US",
            genre="cinematic_documentary",
            presenter_mode="voiceover_only",
            studio_name="Agentic Cinema Studio",
        )
        self.assertIn("English vertical videos", instruction)
        self.assertIn("Agentic Cinema Studio", instruction)
        self.assertIn("Voiceover-only mode", instruction)
        self.assertIn("130-150 words per minute", instruction)

    def test_producer_research_tool_is_bound_to_selected_studio_context(self):
        with patch(
            "backend.agent.fyf_producer.vertex_client_kwargs",
            return_value={"vertexai": True, "project": "test", "location": "global"},
        ):
            agent = create_fyf_producer_agent(
                language="en-US",
                genre="cinematic_documentary",
                presenter_mode="voiceover_only",
                studio_name="Cinema Lab",
            )

        research = agent.tools[0]("Quantum Computing", "standard")
        self.assertEqual(research["language"], "en-US")
        self.assertEqual(research["genre"], "cinematic_documentary")
        self.assertEqual(research["studio_name"], "Cinema Lab")

    def test_exact_lock_instruction_follows_selected_language(self):
        english_request = ExactLockRequest(
            title="Quantum Computing",
            approved_segments=[{"id": "s1", "text": "Quantum bits exist."}],
            studio_name="Cinema Lab",
            language="en-US",
            genre="cinematic_documentary",
            presenter_mode="voiceover_only",
        )
        english_instruction = build_exact_lock_instruction(english_request)

        self.assertIn("English", english_instruction)
        self.assertIn("Cinema Lab", english_instruction)
        self.assertIn("Voiceover-only mode", english_instruction)
        self.assertNotIn("Burmese", english_instruction)

        burmese_instruction = build_exact_lock_instruction(
            ExactLockRequest(
                title="စမ်းသပ်မှု",
                approved_segments=[{"id": "s1", "text": "စမ်းသပ်ချက်"}],
            )
        )
        self.assertIn("Burmese", burmese_instruction)

    def test_genre_styles_resolution(self):
        genres = get_available_genres()
        self.assertEqual(len(genres), 4)
        genre_ids = {g["id"] for g in genres}
        self.assertEqual(
            genre_ids,
            {"cinematic_documentary", "tech_explainer", "investigative", "narrative"},
        )

        doc_cfg = get_style_config("cinematic_documentary")
        self.assertEqual(doc_cfg["id"], "cinematic_documentary")
        self.assertEqual(doc_cfg["badge_accent"], "#854D0E")

        tech_cfg = get_style_config("tech_explainer")
        self.assertEqual(tech_cfg["id"], "tech_explainer")

    def test_voice_service_studio_voices(self):
        self.assertIn("Sadaltager", STUDIO_VOICES)
        self.assertIn("Puck", STUDIO_VOICES)
        self.assertIn("Aoede", STUDIO_VOICES)
        self.assertIn("Fenrir", STUDIO_VOICES)
        self.assertIn("cinematic", ENGLISH_STYLE_PROMPTS)
        self.assertIn("tech", ENGLISH_STYLE_PROMPTS)

    def test_video_request_validation(self):
        for style_or_genre in (
            "fyf_explainer",
            "cinematic_continuity",
            "evidence_story",
            "cinematic_documentary",
            "tech_explainer",
            "investigative",
            "narrative",
        ):
            req = VideoRequest(
                lock_id="12345678",
                style=style_or_genre,
                studio_name="Test Studio",
                language="en-US",
                genre=style_or_genre,
            )
            self.assertEqual(req.style, style_or_genre)

    def test_generate_story_modes_english_cinema_prompt_and_metadata(self):
        sample_variants = {
            "variants": [
                {
                    "name": f"Variant {i}",
                    "script": {
                        "title": "Quantum Tech",
                        "language": "en-US",
                        "segments": [
                            {"id": f"s{j}", "text": f"Narration segment {j}", "visual_action": "action", "scene_type": "whiteboard", "mascot_action": "present", "emotion": "focused", "emphasis": []}
                            for j in range(1, 6)
                        ],
                    },
                }
                for i in range(1, 4)
            ]
        }
        client = MagicMock()
        client.models.generate_content.return_value = SimpleNamespace(text=json.dumps(sample_variants))

        with patch("writer_agent_vertex.genai.Client", return_value=client):
            result = generate_story_modes(
                "Quantum Computing",
                language="en-US",
                genre="cinematic_documentary",
                presenter_mode="voiceover_only",
                studio_name="Agentic Cinema Studio",
                voice_actor="Fenrir",
            )

        self.assertEqual(len(result["variants"]), 3)
        for v in result["variants"]:
            s = v["script"]
            self.assertEqual(s["language"], "en-US")
            self.assertEqual(s["studio_name"], "Agentic Cinema Studio")
            self.assertEqual(s["genre"], "cinematic_documentary")
            self.assertEqual(s["presenter_mode"], "voiceover_only")
            self.assertEqual(s["voice_actor"], "Fenrir")

        system_instruction = client.models.generate_content.call_args.kwargs["config"].system_instruction
        self.assertIn("English (en-US)", system_instruction)
        self.assertIn("Agentic Cinema Studio", system_instruction)
        self.assertIn("Voiceover-only mode", system_instruction)
        self.assertIn("cinematic_documentary", system_instruction)

    def test_lock_narration_in_batches_preserves_studio_metadata(self):
        draft = {
            "title": "Autonomous Agents",
            "language": "en-US",
            "studio_name": "NextGen AI",
            "genre": "tech_explainer",
            "presenter_mode": "voiceover_only",
            "voice_actor": "Puck",
            "segments": [
                {"id": f"s{i}", "text": f"Agent narration {i}", "visual_action": "v", "scene_type": "demo", "mascot_action": "present", "emotion": "focused", "emphasis": []}
                for i in range(1, 6)
            ],
        }

        def mock_generate_exact_lock(request_dict):
            return {
                "title": request_dict["title"],
                "language": request_dict.get("language", "my-MM"),
                "studio_name": request_dict.get("studio_name"),
                "genre": request_dict.get("genre"),
                "presenter_mode": request_dict.get("presenter_mode"),
                "voice_actor": request_dict.get("voice_actor"),
                "segments": [
                    {
                        "id": s["id"],
                        "text": s["text"],
                        "visual_action": "action",
                        "scene_type": "demo",
                        "mascot_action": "present",
                        "emotion": "focused",
                        "emphasis": [],
                        "visual": {
                            "kind": "generic",
                            "phase": "setup",
                            "camera": "wide",
                            "screen_text": [s["text"]],
                            "evidence_claims": [{"claim_id": f"c-{s['id']}", "statement": "Fact", "evidence_type": "concept", "values": []}],
                            "evidence_shots": [
                                {
                                    "shot_id": f"shot-{s['id']}",
                                    "proves_claim_ids": [f"c-{s['id']}"],
                                    "prompt": "prompt",
                                    "caption": "caption",
                                    "hold_fraction": 1.0,
                                    "composition": "focal_center",
                                    "mascot_presence": "none",
                                    "media_type": "generated_image",
                                }
                            ],
                        },
                    }
                    for s in request_dict["approved_segments"]
                ],
            }

        with patch("writer_agent_vertex.generate_exact_lock", side_effect=mock_generate_exact_lock):
            locked = lock_narration_in_batches(draft, batch_size=3)

        self.assertEqual(locked["language"], "en-US")
        self.assertEqual(locked["studio_name"], "NextGen AI")
        self.assertEqual(locked["genre"], "tech_explainer")
        self.assertEqual(locked["presenter_mode"], "voiceover_only")
        self.assertEqual(locked["voice_actor"], "Puck")
        self.assertEqual(len(locked["segments"]), 5)

    def test_apply_director_pass_voiceover_only_suppresses_mascot(self):
        script_with_mascot = {
            "title": "Pure Cinema B-Roll",
            "language": "en-US",
            "presenter_mode": "voiceover_only",
            "segments": [
                {
                    "id": "s1",
                    "text": "Atmospheric city shot.",
                    "visual_action": "action",
                    "scene_type": "demo",
                    "mascot_action": "present",
                    "emotion": "focused",
                    "emphasis": [],
                    "visual": {
                        "kind": "generic",
                        "phase": "setup",
                        "camera": "wide",
                        "screen_text": ["City"],
                        "evidence_claims": [{"claim_id": "c1", "statement": "Fact", "evidence_type": "concept", "values": []}],
                        "evidence_shots": [
                            {
                                "shot_id": "shot-1",
                                "proves_claim_ids": ["c1"],
                                "prompt": "prompt 1",
                                "caption": "cap 1",
                                "hold_fraction": 0.5,
                                "composition": "focal_center",
                                "mascot_presence": "explain",
                                "media_type": "generated_video",
                            },
                            {
                                "shot_id": "shot-2",
                                "proves_claim_ids": ["c1"],
                                "prompt": "prompt 2",
                                "caption": "cap 2",
                                "hold_fraction": 0.5,
                                "composition": "split_stage",
                                "mascot_presence": "reaction",
                                "media_type": "generated_image",
                            },
                        ],
                    },
                }
            ],
        }
        directed = apply_director_pass(script_with_mascot)
        for seg in directed["segments"]:
            for shot in seg["visual"]["evidence_shots"]:
                self.assertEqual(shot["mascot_presence"], "none")


if __name__ == "__main__":
    unittest.main()
