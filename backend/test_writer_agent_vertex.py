import ast
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from writer_agent_vertex import (
    _reconcile_compact_plan,
    _reconcile_storyboard_segment_ids,
    _sleep_before_vertex_retry,
    _stage_client,
    _stage_location,
    generate_video_script,
)
from video_contract import (
    CompactVisualPlanResponse,
    CompactVisualPlanSegment,
    EvidenceClaim,
    EvidenceShot,
    StoryboardResponse,
    StoryboardSegment,
)


VALID_SCRIPT = {
    "title": "စမ်းသပ်မှု",
    "language": "my-MM",
    "segments": [
        {
            "id": "s1",
            "text": "စမ်းသပ် စာသား",
            "visual_action": "typed visual",
            "scene_type": "demo",
            "mascot_action": "present",
            "emotion": "focused",
            "emphasis": [],
        },
        {"id": "s2", "text": "ဒုတိယ စာသား", "visual_action": "show", "scene_type": "demo", "mascot_action": "explain", "emotion": "neutral", "emphasis": []},
        {"id": "s3", "text": "တတိယ စာသား", "visual_action": "show", "scene_type": "demo", "mascot_action": "think", "emotion": "focused", "emphasis": []},
        {"id": "s4", "text": "စတုတ္ထ စာသား", "visual_action": "show", "scene_type": "whiteboard", "mascot_action": "warn", "emotion": "concerned", "emphasis": []},
        {"id": "s5", "text": "နောက်ဆုံး စာသား", "visual_action": "show", "scene_type": "whiteboard", "mascot_action": "approve", "emotion": "confident", "emphasis": []},
    ],
}


class VertexWriterRetryTests(unittest.TestCase):
    def test_storyboard_id_drift_is_reconciled_by_claim_ownership(self):
        def claim(claim_id: str) -> EvidenceClaim:
            return EvidenceClaim(
                claim_id=claim_id,
                statement="သက်သေပြချက်",
                evidence_type="concept",
            )

        def shot(shot_id: str, claim_id: str) -> EvidenceShot:
            return EvidenceShot(
                shot_id=shot_id,
                proves_claim_ids=[claim_id],
                prompt="အကြောင်းအရာကို ပြပါ",
                caption="သက်သေ",
                hold_fraction=1.0,
                media_type="generated_image",
            )

        plan = CompactVisualPlanResponse(segments=[
            CompactVisualPlanSegment(
                id="s1", visual_action="show", scene_type="demo",
                mascot_action="present", emotion="focused", phase="setup",
                camera="wide", screen_text=["တစ်"], evidence_claims=[claim("c1")],
                evidence_shots=[shot("p1", "c1")],
            ),
            CompactVisualPlanSegment(
                id="s2", visual_action="show", scene_type="demo",
                mascot_action="explain", emotion="focused", phase="in_progress",
                camera="close_up", screen_text=["နှစ်"], evidence_claims=[claim("c2")],
                evidence_shots=[shot("p2", "c2")],
            ),
        ])
        storyboard = StoryboardResponse(segments=[
            StoryboardSegment(id="segment_2", evidence_shots=[shot("b2", "c2")]),
            StoryboardSegment(id="segment_1", evidence_shots=[shot("b1", "c1")]),
        ])

        reconciled = _reconcile_storyboard_segment_ids(storyboard, plan)

        self.assertEqual([segment.id for segment in reconciled.segments], ["s2", "s1"])

    def test_writer_defaults_to_global_vertex_endpoint(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(_stage_location("script"), "global")

    def test_writer_client_has_bounded_http_timeout(self):
        with (
            patch.dict("os.environ", {"FYF_VERTEX_CALL_TIMEOUT_SECONDS": "45"}),
            patch(
                "backend.vertex_client.vertex_client_kwargs",
                return_value={"vertexai": True, "project": "test", "location": "global"},
            ),
            patch("writer_agent_vertex.genai.Client") as client_constructor,
        ):
            _stage_client("script")

        http_options = client_constructor.call_args.kwargs["http_options"]
        self.assertEqual(http_options.timeout, 45_000)
        self.assertEqual(http_options.retry_options.attempts, 1)

    def test_transient_vertex_retry_uses_long_bounded_backoff(self):
        with (
            patch.dict("os.environ", {}, clear=True),
            patch("writer_agent_vertex.time.sleep") as sleep,
        ):
            _sleep_before_vertex_retry(0)
            _sleep_before_vertex_retry(1)
            _sleep_before_vertex_retry(3)

        self.assertEqual([call.args[0] for call in sleep.call_args_list], [10, 20, 60])

    def test_transient_vertex_retry_backoff_can_be_tuned_for_local_runs(self):
        with (
            patch.dict(
                "os.environ",
                {
                    "FYF_VERTEX_RETRY_BASE_SECONDS": "7",
                    "FYF_VERTEX_RETRY_MAX_SECONDS": "25",
                },
            ),
            patch("writer_agent_vertex.time.sleep") as sleep,
        ):
            _sleep_before_vertex_retry(0)
            _sleep_before_vertex_retry(2)

        self.assertEqual([call.args[0] for call in sleep.call_args_list], [7, 25])

    def test_all_writer_vertex_configs_omit_sampling_parameters(self):
        source_path = Path(__file__).resolve().parents[1] / "writer_agent_vertex.py"
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        config_calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and (
                (isinstance(node.func, ast.Attribute) and node.func.attr == "generation_config_for")
                or (isinstance(node.func, ast.Name) and node.func.id == "generation_config_for")
            )
        ]

        self.assertEqual(len(config_calls), 6)
        forbidden = {"temperature", "top_p", "top_k"}
        for call in config_calls:
            configured = {
                keyword.arg for keyword in call.keywords if keyword.arg is not None
            }
            self.assertFalse(
                forbidden & configured,
                f"sampling parameters found at line {call.lineno}: {forbidden & configured}",
            )

    def test_invalid_output_is_repaired_automatically(self):
        client = MagicMock()
        client.models.generate_content.side_effect = [
            SimpleNamespace(text="not-json"),
            SimpleNamespace(text=json.dumps(VALID_SCRIPT, ensure_ascii=False)),
        ]
        with (
            patch("writer_agent_vertex.genai.Client", return_value=client) as factory,
            patch.dict("os.environ", {"FYF_VERTEX_MAX_ATTEMPTS": "2"}),
            patch("writer_agent_vertex.generate_exact_lock", return_value={"title": "စမ်းသပ်မှု", "language": "my-MM", "segments": [{"id": "s1", "text": "စမ်းသပ် စာသား", "visual_action": "show", "scene_type": "demo", "mascot_action": "present", "emotion": "focused", "emphasis": [], "visual": {"kind": "generic", "phase": "setup", "camera": "wide", "screen_text": ["စမ်းသပ်မှု"]}}]}) as lock,
        ):
            result = generate_video_script("စမ်းသပ်ရန်")

        self.assertEqual(result["segments"][0]["visual"]["kind"], "generic")
        self.assertEqual(client.models.generate_content.call_count, 2)
        self.assertTrue(factory.call_args.kwargs["vertexai"])
        self.assertEqual(len(lock.call_args.args[0]["approved_segments"]), 5)

    def test_invalid_output_fails_closed_after_retry_limit(self):
        client = MagicMock()
        client.models.generate_content.return_value = SimpleNamespace(text="not-json")
        with (
            patch("writer_agent_vertex.genai.Client", return_value=client),
            patch.dict("os.environ", {"FYF_VERTEX_MAX_ATTEMPTS": "2"}),
        ):
            with self.assertRaisesRegex(ValueError, "after 2 attempts"):
                generate_video_script("စမ်းသပ်ရန်")

        self.assertEqual(client.models.generate_content.call_count, 2)
        self.assertEqual(
            [call.kwargs["model"] for call in client.models.generate_content.call_args_list],
            ["gemini-3.7-flash", "gemini-3.7-flash"],
        )
        self.assertNotIn(
            "gemini-3.6-flash",
            [call.kwargs["model"] for call in client.models.generate_content.call_args_list],
        )


class CompactPlanReconciliationTests(unittest.TestCase):
    """Regression: extra model-returned plan segments must be dropped, not fail the lock."""

    def _segment(self, segment_id: str) -> CompactVisualPlanSegment:
        return CompactVisualPlanSegment(
            id=segment_id, visual_action="show", scene_type="demo",
            mascot_action="present", emotion="focused", phase="setup",
            camera="wide", screen_text=["စာ"], evidence_claims=[
                EvidenceClaim(claim_id=f"c_{segment_id}", statement="သက်သေပြချက်", evidence_type="concept"),
            ],
            evidence_shots=[EvidenceShot(
                shot_id=f"p_{segment_id}", proves_claim_ids=[f"c_{segment_id}"],
                prompt="prompt", caption="caption", hold_fraction=0.5,
            )],
        )

    def test_extra_out_of_scope_segments_are_dropped_and_order_restored(self):
        from types import SimpleNamespace

        request = SimpleNamespace(approved_segments=[
            SimpleNamespace(id="s2"), SimpleNamespace(id="s1"),
        ])
        metadata = CompactVisualPlanResponse(segments=[
            self._segment("s1"), self._segment("extra_9"), self._segment("s2"),
        ])

        _reconcile_compact_plan(metadata, request)

        self.assertEqual([segment.id for segment in metadata.segments], ["s2", "s1"])

    def test_missing_approved_segment_still_raises_for_retry(self):
        from types import SimpleNamespace

        request = SimpleNamespace(approved_segments=[
            SimpleNamespace(id="s1"), SimpleNamespace(id="s_missing"),
        ])
        metadata = CompactVisualPlanResponse(segments=[self._segment("s1")])

        with self.assertRaises(ValueError) as ctx:
            _reconcile_compact_plan(metadata, request)
        self.assertIn("s_missing", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
