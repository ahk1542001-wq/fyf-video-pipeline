import copy
import tempfile
import unittest
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from google.genai.errors import ClientError
from video_contract import VideoScript
from visual_evidence_vertex import (
    _client, _input_fingerprint, _plan_final_visual_repair, _quota_retry, generate_and_verify_visual_evidence,
    repair_creative_failures, repair_final_visual_failures, ensure_relationship_modes,
    plan_visual_treatments,
)


def script_fixture():
    return {
        "title": "Evidence",
        "language": "my-MM",
        "segments": [{
            "id": "s1", "text": "တကယ် ၅ ခုရှိတယ်။", "visual_action": "count",
            "scene_type": "demo", "mascot_action": "explain", "emotion": "focused", "emphasis": [],
            "visual": {
                "kind": "generic", "phase": "setup", "camera": "close_up", "screen_text": ["၅ ခု"],
                "evidence_claims": [{"claim_id": "c1", "statement": "တကယ် ၅ ခု", "evidence_type": "count", "values": ["5"]}],
                "evidence_shots": [{"shot_id": "count", "proves_claim_ids": ["c1"], "prompt": "Show exactly five boxes", "caption": "၅ ခု", "hold_fraction": 1, "asset_path": None, "verification_status": "planned"}],
            },
        }],
    }


def many_shot_fixture(count: int):
    script = {"title": "Batch Evidence", "language": "my-MM", "segments": []}
    for index in range(count):
        number = index + 1
        segment = copy.deepcopy(script_fixture()["segments"][0])
        segment["id"] = f"s{number}"
        segment["text"] = f"အချက် {number} ကို စစ်ဆေးပါ။"
        segment["visual"]["evidence_claims"][0].update({
            "claim_id": f"c{number}",
            "statement": f"အချက် {number}",
            "values": [str(number)],
        })
        segment["visual"]["evidence_shots"][0].update({
            "shot_id": f"shot-{number}",
            "proves_claim_ids": [f"c{number}"],
            "prompt": f"Show evidence item {number}",
            "caption": f"အချက် {number}",
        })
        script["segments"].append(segment)
    return script


def object_action_treatment(number: int):
    return {
        "treatment_type": "object_action",
        "focal_object": f"evidence-{number}",
        "action": "reveal",
        "change": "becomes visible",
        "visual_world": "studio",
        "motion_family": "object",
        "text_mode": "caption",
        "director_reason": "proves the locked evidence",
        "attention_reset": False,
    }


def treatment_batch_response(numbers: list[int]):
    return SimpleNamespace(text=json.dumps({"items": [
        {
            "segment_id": f"s{number}",
            "shot_id": f"shot-{number}",
            "treatment": object_action_treatment(number),
        }
        for number in numbers
    ]}))


def add_treatments(script: dict, count: int):
    treated = copy.deepcopy(script)
    for index, segment in enumerate(treated["segments"][:count], start=1):
        segment["visual"]["evidence_shots"][0]["treatment"] = object_action_treatment(index)
    return treated


def director_fingerprint(script: dict):
    canonical = VideoScript.model_validate(script).model_dump(mode="json")
    return _input_fingerprint(canonical) + "-fyf-director-v1"


class VisualEvidenceVertexTests(unittest.TestCase):
    def image_response(self):
        return SimpleNamespace(candidates=[SimpleNamespace(content=SimpleNamespace(parts=[SimpleNamespace(inline_data=SimpleNamespace(data=b"png"))]))])

    def test_plan_visual_treatments_batches_twenty_seven_shots_into_six_calls(self):
        script = many_shot_fixture(27)
        call_index = 0

        def batch_response(*, model, contents, config):
            nonlocal call_index
            start = call_index * 5
            stop = min(start + 5, 27)
            call_index += 1
            return SimpleNamespace(text=json.dumps({"items": [
                {
                    "segment_id": f"s{number}",
                    "shot_id": f"shot-{number}",
                    "treatment": object_action_treatment(number),
                }
                for number in range(start + 1, stop + 1)
            ]}))

        client = MagicMock()
        client.models.generate_content.side_effect = batch_response
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            "os.environ", {"FYF_DIRECTOR_BATCH_SIZE": "5"}
        ), patch("visual_evidence_vertex._client", return_value=client):
            result = plan_visual_treatments(script, temp_dir)

        self.assertEqual(client.models.generate_content.call_count, 6)
        treated = [
            shot["shot_id"]
            for segment in result["segments"]
            for shot in segment["visual"]["evidence_shots"]
            if shot.get("treatment")
        ]
        self.assertEqual(treated, [f"shot-{number}" for number in range(1, 28)])

    def test_plan_visual_treatments_rejects_mismatched_batch_identity(self):
        script = many_shot_fixture(2)
        response = {"items": [
            {
                "segment_id": "s1",
                "shot_id": "shot-1",
                "treatment": object_action_treatment(1),
            },
            {
                "segment_id": "s2",
                "shot_id": "wrong-shot",
                "treatment": object_action_treatment(2),
            },
        ]}
        client = MagicMock()
        client.models.generate_content.return_value = SimpleNamespace(text=json.dumps(response))
        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "visual_evidence_vertex._client", return_value=client
        ):
            with self.assertRaisesRegex(RuntimeError, "unexpected treatment identities"):
                plan_visual_treatments(script, temp_dir)

    def test_partial_director_checkpoint_resumes_only_remaining_shots(self):
        script = many_shot_fixture(7)
        partial = add_treatments(script, 2)
        fingerprint = director_fingerprint(script)
        client = MagicMock()
        client.models.generate_content.return_value = treatment_batch_response([3, 4, 5, 6, 7])

        with tempfile.TemporaryDirectory() as temp_dir:
            Path(temp_dir, "director_treatment_checkpoint.json").write_text(json.dumps({
                "input_fingerprint": fingerprint,
                "policy_version": "fyf-director-v1",
                "model_route": "gemini-3.7-flash",
                "batch_size": 5,
                "total_shot_count": 7,
                "completed_shot_ids": ["s1/shot-1", "s2/shot-2"],
                "completed_batch_count": 1,
                "complete": False,
                "script": partial,
            }))
            with patch("visual_evidence_vertex._client", return_value=client):
                result = plan_visual_treatments(script, temp_dir)

        self.assertEqual(client.models.generate_content.call_count, 1)
        config = client.models.generate_content.call_args.kwargs["config"]
        self.assertIsNone(getattr(config, "temperature", None))
        self.assertEqual(sum(
            bool(shot.get("treatment"))
            for segment in result["segments"]
            for shot in segment["visual"]["evidence_shots"]
        ), 7)

    def test_complete_director_checkpoint_makes_zero_vertex_calls(self):
        script = many_shot_fixture(2)
        complete = add_treatments(script, 2)
        fingerprint = director_fingerprint(script)
        with tempfile.TemporaryDirectory() as temp_dir:
            Path(temp_dir, "director_treatment_checkpoint.json").write_text(json.dumps({
                "input_fingerprint": fingerprint,
                "policy_version": "fyf-director-v1",
                "model_route": "gemini-3.7-flash",
                "batch_size": 5,
                "total_shot_count": 2,
                "completed_shot_ids": ["s1/shot-1", "s2/shot-2"],
                "completed_batch_count": 1,
                "complete": True,
                "script": complete,
            }))
            with patch("visual_evidence_vertex._client", side_effect=AssertionError("Vertex called")):
                result = plan_visual_treatments(script, temp_dir)
        self.assertEqual(result, VideoScript.model_validate(complete).model_dump(mode="json"))

    def test_checkpoint_model_route_change_invalidates_reuse(self):
        script = many_shot_fixture(2)
        complete = add_treatments(script, 2)
        fingerprint = director_fingerprint(script)
        client = MagicMock()
        client.models.generate_content.return_value = treatment_batch_response([1, 2])
        with tempfile.TemporaryDirectory() as temp_dir:
            Path(temp_dir, "director_treatment_checkpoint.json").write_text(json.dumps({
                "input_fingerprint": fingerprint,
                "policy_version": "fyf-director-v1",
                "model_route": "old-director-model",
                "batch_size": 5,
                "total_shot_count": 2,
                "completed_shot_ids": ["s1/shot-1", "s2/shot-2"],
                "completed_batch_count": 1,
                "complete": True,
                "script": complete,
            }))
            with patch("visual_evidence_vertex._client", return_value=client), patch(
                "visual_evidence_vertex.model_for", return_value="new-director-model"
            ):
                result = plan_visual_treatments(script, temp_dir)
        self.assertEqual(client.models.generate_content.call_count, 1)
        self.assertEqual(len(result["segments"]), 2)

    def test_invalid_batch_item_retries_only_failed_identity_with_feedback(self):
        script = many_shot_fixture(2)
        invalid = object_action_treatment(2)
        invalid["action"] = None
        invalid["change"] = None
        first = SimpleNamespace(text=json.dumps({"items": [
            {"segment_id": "s1", "shot_id": "shot-1", "treatment": object_action_treatment(1)},
            {"segment_id": "s2", "shot_id": "shot-2", "treatment": invalid},
        ]}))
        second = treatment_batch_response([2])
        client = MagicMock()
        client.models.generate_content.side_effect = [first, second]

        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "visual_evidence_vertex._client", return_value=client
        ):
            result = plan_visual_treatments(script, temp_dir)

        self.assertEqual(client.models.generate_content.call_count, 2)
        first_prompt = client.models.generate_content.call_args_list[0].kwargs["contents"]
        repair_prompt = client.models.generate_content.call_args_list[1].kwargs["contents"]
        self.assertIn("shot-1", first_prompt)
        self.assertNotIn("shot-1", repair_prompt)
        self.assertIn("shot-2", repair_prompt)
        self.assertIn("Validation errors", repair_prompt)
        self.assertTrue(all(
            shot.get("treatment")
            for segment in result["segments"]
            for shot in segment["visual"]["evidence_shots"]
        ))

    def test_batch_repair_exhaustion_names_exact_failed_ids(self):
        script = many_shot_fixture(2)
        invalid = object_action_treatment(2)
        invalid["action"] = None
        invalid["change"] = None
        first = SimpleNamespace(text=json.dumps({"items": [
            {"segment_id": "s1", "shot_id": "shot-1", "treatment": object_action_treatment(1)},
            {"segment_id": "s2", "shot_id": "shot-2", "treatment": invalid},
        ]}))
        second = SimpleNamespace(text=json.dumps({"items": [
            {"segment_id": "s2", "shot_id": "shot-2", "treatment": invalid},
        ]}))
        client = MagicMock()
        client.models.generate_content.side_effect = [first, second]

        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "visual_evidence_vertex._client", return_value=client
        ):
            with self.assertRaisesRegex(RuntimeError, "s2/shot-2"):
                plan_visual_treatments(script, temp_dir)
            checkpoint = json.loads(
                Path(temp_dir, "director_treatment_checkpoint.json").read_text()
            )

        shots = [
            shot
            for segment in checkpoint["script"]["segments"]
            for shot in segment["visual"]["evidence_shots"]
        ]
        self.assertTrue(shots[0].get("treatment"))
        self.assertFalse(shots[1].get("treatment"))
        self.assertFalse(checkpoint["complete"])

    def test_transient_director_quota_uses_deterministic_treatment_fallback(self):
        script = many_shot_fixture(2)
        client = MagicMock()
        client.models.generate_content.side_effect = ClientError(
            429, {"error": {"message": "quota"}}
        )
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            "os.environ",
            {
                "FYF_VERTEX_RETRY_BASE_SECONDS": "0",
                "FYF_VERTEX_RETRY_MAX_SECONDS": "0",
            },
        ), patch("visual_evidence_vertex._client", return_value=client):
            result = plan_visual_treatments(script, temp_dir)

        shots = [
            shot
            for segment in result["segments"]
            for shot in segment["visual"]["evidence_shots"]
        ]
        self.assertEqual(len(shots), 2)
        self.assertTrue(all(shot["treatment"] for shot in shots))
        self.assertEqual(
            {shot["treatment"]["treatment_type"] for shot in shots},
            {"story_scene", "object_action"},
        )

    def test_transient_director_quota_then_partial_response_falls_back(self):
        script = many_shot_fixture(2)
        partial = SimpleNamespace(text=json.dumps({"items": [{
            "segment_id": "s1",
            "shot_id": "shot-1",
            "treatment": object_action_treatment(1),
        }]}))
        client = MagicMock()
        client.models.generate_content.side_effect = [
            ClientError(429, {"error": {"message": "quota"}}),
            partial,
            partial,
        ]
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            "os.environ",
            {
                "FYF_VERTEX_RETRY_BASE_SECONDS": "0",
                "FYF_VERTEX_RETRY_MAX_SECONDS": "0",
            },
        ), patch("visual_evidence_vertex._client", return_value=client):
            result = plan_visual_treatments(script, temp_dir)

        shots = [
            shot
            for segment in result["segments"]
            for shot in segment["visual"]["evidence_shots"]
        ]
        self.assertTrue(all(shot["treatment"] for shot in shots))

    def test_plan_visual_treatments_includes_director_context(self):
        script = copy.deepcopy(script_fixture())
        script["segments"] = []
        for index in range(4):
            segment = copy.deepcopy(script_fixture())["segments"][0]
            segment["id"] = f"s{index + 1}"
            segment["visual"]["evidence_claims"][0]["claim_id"] = f"c{index + 1}"
            shot = segment["visual"]["evidence_shots"][0]
            shot["shot_id"] = f"count-{index + 1}"
            shot["proves_claim_ids"] = [f"c{index + 1}"]
            script["segments"].append(segment)
        treatment = {"treatment_type": "object_action", "focal_object": "box", "action": "open", "change": "color", "visual_world": "studio", "motion_family": "object", "text_mode": "caption", "director_reason": "shows count", "attention_reset": False}
        for segment in script["segments"][:3]:
            segment["visual"]["evidence_shots"][0]["treatment"] = copy.deepcopy(treatment)
        client = MagicMock()
        client.models.generate_content.return_value = SimpleNamespace(text=json.dumps(treatment))
        with tempfile.TemporaryDirectory() as temp_dir, patch("visual_evidence_vertex._client", return_value=client):
            plan_visual_treatments(script, temp_dir)
        contents = client.models.generate_content.call_args.kwargs["contents"]
        self.assertIn("previous_treatments", contents)
        self.assertIn("prohibited_treatments", contents)
        self.assertIn("object_action", contents)

    def test_plan_visual_treatments_retries_invalid_json(self):
        script = copy.deepcopy(script_fixture())
        locked = [
            (segment["id"], segment["text"], copy.deepcopy(segment["visual"]["evidence_claims"]))
            for segment in script["segments"]
        ]
        treatment = {"treatment_type": "object_action", "focal_object": "box", "action": "open", "change": "color", "visual_world": "studio", "motion_family": "object", "text_mode": "caption", "director_reason": "shows count", "attention_reset": False}
        client = MagicMock()
        client.models.generate_content.side_effect = [SimpleNamespace(text="invalid-json"), SimpleNamespace(text=json.dumps(treatment))]
        with tempfile.TemporaryDirectory() as temp_dir, patch("visual_evidence_vertex._client", return_value=client):
            result = plan_visual_treatments(script, temp_dir)
        self.assertEqual(client.models.generate_content.call_count, 2)
        self.assertEqual(result["segments"][0]["visual"]["evidence_shots"][0]["treatment"]["treatment_type"], "object_action")
        self.assertEqual([
            (segment["id"], segment["text"], segment["visual"]["evidence_claims"])
            for segment in result["segments"]
        ], locked)

    def test_plan_visual_treatments_resumes_checkpoint(self):
        treatment = {"treatment_type": "object_action", "focal_object": "box", "action": "open", "change": "color", "visual_world": "studio", "motion_family": "object", "text_mode": "caption", "director_reason": "shows count", "attention_reset": False}
        client = MagicMock()
        client.models.generate_content.return_value = SimpleNamespace(text=json.dumps(treatment))
        with tempfile.TemporaryDirectory() as temp_dir, patch("visual_evidence_vertex._client", return_value=client):
            first = plan_visual_treatments(script_fixture(), temp_dir)
            with patch("visual_evidence_vertex._client", side_effect=AssertionError("Vertex called on resume")):
                second = plan_visual_treatments(script_fixture(), temp_dir)
        self.assertEqual(second, first)

    def test_passed_verification_writes_job_asset_and_updates_contract(self):
        client = MagicMock()
        client.models.generate_content.side_effect = [
            self.image_response(),
            SimpleNamespace(text='{"passed":true,"proved_claim_ids":["c1"],"observed_values":["5"],"issues":[]}'),
        ]
        with tempfile.TemporaryDirectory() as temp_dir, patch("visual_evidence_vertex._client", return_value=client):
            result = generate_and_verify_visual_evidence(script_fixture(), temp_dir)
            shot = result["segments"][0]["visual"]["evidence_shots"][0]
            self.assertEqual(shot["verification_status"], "passed")
            self.assertEqual(shot["asset_path"], "job-visuals/s1-count.png")
            self.assertEqual((Path(temp_dir) / "visuals" / "s1-count.png").read_bytes(), b"png")

    def test_transient_image_generation_uses_deterministic_motion_fallback(self):
        client = MagicMock()
        client.models.generate_content.side_effect = ClientError(
            429, {"error": {"message": "quota"}}
        )
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            "os.environ",
            {
                "FYF_VERTEX_RETRY_BASE_SECONDS": "0",
                "FYF_VERTEX_RETRY_MAX_SECONDS": "0",
            },
        ), patch("visual_evidence_vertex._client", return_value=client):
            result = generate_and_verify_visual_evidence(script_fixture(), temp_dir)

        shot = result["segments"][0]["visual"]["evidence_shots"][0]
        self.assertEqual(shot["media_type"], "motion_graphic")
        self.assertEqual(shot["verification_status"], "passed")
        self.assertTrue(shot["fallback_used"])
        self.assertEqual(shot["motion_spec"]["layout"], "count")

    def test_final_qa_repair_is_dynamic_and_changes_only_failed_segments(self):
        script = script_fixture()
        script["segments"][0]["visual"]["evidence_shots"][0].update({
            "asset_path": "job-visuals/s1-count.png", "verification_status": "passed",
        })
        second = json.loads(json.dumps(script["segments"][0]))
        second["id"] = "s2"
        second["text"] = "အခြားအကြောင်းအရာ။"
        second["visual"]["evidence_claims"][0]["claim_id"] = "c2"
        second["visual"]["evidence_shots"][0]["shot_id"] = "other"
        second["visual"]["evidence_shots"][0]["proves_claim_ids"] = ["c2"]
        script["segments"].append(second)
        report = {"passed": False, "segments": [
            {"segment_id": "s1", "passed": False, "issues": ["relation is unclear"]},
            {"segment_id": "s2", "passed": True, "issues": []},
        ]}
        original_second = json.loads(json.dumps(second))

        plan = {
            "media_type": "motion_graphic",
            "screen_text": ["စနစ်က ဆုံးဖြတ်ပုံကို အဆင့်လိုက်ရှင်းပြသည်"],
            "caption": "စနစ်က ဆုံးဖြတ်ပုံကို အဆင့်လိုက်ရှင်းပြသည်",
            "prompt": "Show an ordered explanation of a decision",
            "motion_preset": "static", "transition": "crossfade",
            "composition": "focal_center", "mascot_presence": "none",
            "motion_spec": {"layout": "sequence", "labels": ["အချက်အလက် ၅ ခု", "ဆုံးဖြတ်ချက်", "ရှင်းပြချက်"], "values": ["5"], "object_count": None, "accent_index": 2},
        }

        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "visual_evidence_vertex._client", return_value=MagicMock()
        ), patch("visual_evidence_vertex._plan_final_visual_repair", return_value=plan), patch(
            "visual_evidence_vertex._verify_motion_spec_semantics"
        ):
            visuals = Path(temp_dir) / "visuals"
            visuals.mkdir()
            (visuals / "s1-count.png").write_bytes(b"s1")
            (visuals / "s2-other.png").write_bytes(b"s2")
            script["segments"][1]["visual"]["evidence_shots"][0].update({
                "asset_path": "job-visuals/s2-other.png", "verification_status": "passed",
            })
            original_second = VideoScript.model_validate(script).model_dump(mode="json")["segments"][1]
            repaired = repair_final_visual_failures(script, report, temp_dir)

        repaired_first = repaired["segments"][0]
        self.assertEqual(repaired_first["visual"]["screen_text"], ["စနစ်က ဆုံးဖြတ်ပုံကို အဆင့်လိုက်ရှင်းပြသည်"])
        self.assertEqual(repaired_first["visual"]["evidence_shots"][0]["media_type"], "motion_graphic")
        self.assertEqual(repaired["segments"][1], original_second)

    def test_final_qa_repair_replans_after_semantic_rejection(self):
        script = script_fixture()
        report = {"passed": False, "segments": [
            {"segment_id": "s1", "passed": False, "issues": ["action is unclear"]},
        ]}
        base = {
            "media_type": "motion_graphic", "prompt": "Explain the action",
            "motion_preset": "static", "transition": "crossfade",
            "composition": "focal_center", "mascot_presence": "none",
        }
        first = {**base, "screen_text": ["မရှင်းသေး"], "caption": "မရှင်းသေး", "motion_spec": {
            "layout": "concept", "labels": ["၅"], "values": ["5"], "object_count": None, "accent_index": None,
        }}
        second = {**base, "screen_text": ["အဆင့်လိုက် ရှင်းပြချက်"], "caption": "အဆင့်လိုက် ရှင်းပြချက်", "motion_spec": {
            "layout": "sequence", "labels": ["အချက် ၅ ခု", "လုပ်ဆောင်မှု", "ရလဒ်"], "values": ["5"], "object_count": None, "accent_index": 1,
        }}
        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "visual_evidence_vertex._client", return_value=MagicMock()
        ), patch("visual_evidence_vertex._plan_final_visual_repair", side_effect=[first, second]), patch(
            "visual_evidence_vertex._verify_motion_spec_semantics",
            side_effect=[RuntimeError("labels do not show the action"), None],
        ):
            repaired = repair_final_visual_failures(script, report, temp_dir)
        self.assertEqual(repaired["segments"][0]["visual"]["screen_text"], ["အဆင့်လိုက် ရှင်းပြချက်"])

    def test_final_qa_repair_replans_every_shot_in_failed_multi_shot_segment(self):
        script = script_fixture()
        visual = script["segments"][0]["visual"]
        visual["evidence_claims"].append({
            "claim_id": "c2", "statement": "နောက်ဆုံး ရလဒ်", "evidence_type": "concept", "values": [],
        })
        visual["evidence_shots"][0]["hold_fraction"] = 0.5
        visual["evidence_shots"].append({
            "shot_id": "result", "proves_claim_ids": ["c2"], "prompt": "old second shot",
            "caption": "ဟောင်း", "hold_fraction": 0.5, "asset_path": None,
            "verification_status": "passed",
        })
        report = {"passed": False, "segments": [
            {"segment_id": "s1", "passed": False, "issues": ["second shot is unclear"]},
        ]}
        plans = [
            {
                "media_type": "generated_image", "screen_text": ["ပထမ"], "caption": "ပထမ",
                "prompt": "new first shot", "motion_preset": "static", "transition": "cut",
                "composition": "focal_center", "mascot_presence": "none", "motion_spec": None,
            },
            {
                "media_type": "generated_image", "screen_text": ["ဒုတိယ"], "caption": "ဒုတိယ",
                "prompt": "new second shot", "motion_preset": "static", "transition": "cut",
                "composition": "focal_center", "mascot_presence": "none", "motion_spec": None,
            },
        ]
        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "visual_evidence_vertex._client", return_value=MagicMock()
        ), patch(
            "visual_evidence_vertex._plan_final_visual_repair", side_effect=plans
        ) as planner, patch(
            "visual_evidence_vertex.generate_and_verify_visual_evidence", side_effect=lambda data, _root: data
        ):
            repaired = repair_final_visual_failures(script, report, temp_dir)

        shots = repaired["segments"][0]["visual"]["evidence_shots"]
        self.assertEqual(planner.call_count, 2)
        self.assertEqual([shot["prompt"] for shot in shots], ["new first shot", "new second shot"])

    def test_final_qa_repair_replans_after_invalid_structured_plan(self):
        script = script_fixture()
        report = {"passed": False, "segments": [
            {"segment_id": "s1", "passed": False, "issues": ["layout is unclear"]},
        ]}
        valid = {
            "media_type": "motion_graphic", "screen_text": ["အဆင့်လိုက်"],
            "caption": "အဆင့်လိုက်", "prompt": "ordered", "motion_preset": "static",
            "transition": "cut", "composition": "focal_center", "mascot_presence": "none",
            "motion_spec": {"layout": "count", "labels": ["ပစ္စည်း"], "values": ["၅"], "object_count": 5, "accent_index": 0},
        }
        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "visual_evidence_vertex._client", return_value=MagicMock()
        ), patch(
            "visual_evidence_vertex._plan_final_visual_repair",
            side_effect=[ValueError("invalid mascot enum"), valid],
        ) as planner, patch("visual_evidence_vertex._verify_motion_spec_semantics"):
            repaired = repair_final_visual_failures(script, report, temp_dir)
        self.assertEqual(planner.call_count, 2)
        self.assertEqual(repaired["segments"][0]["visual"]["screen_text"], ["အဆင့်လိုက်"])

    def test_final_qa_repair_can_use_four_bounded_plans_for_hard_relationship(self):
        script = script_fixture()
        report = {"passed": False, "segments": [{"segment_id": "s1", "passed": False, "issues": ["relationship missing"]}]}
        weak = {
            "media_type": "motion_graphic", "screen_text": ["မရှင်း"], "caption": "မရှင်း",
            "prompt": "relation", "motion_preset": "static", "transition": "cut",
            "composition": "focal_center", "mascot_presence": "none",
            "motion_spec": {"layout": "relationship", "labels": ["အချက် ၅"], "values": ["5"], "object_count": None, "accent_index": None},
        }
        strong = json.loads(json.dumps(weak))
        strong["screen_text"] = ["ဆက်နွယ်မှုကို ရှင်းလင်းပြသသည်"]
        strong["caption"] = strong["screen_text"][0]
        with tempfile.TemporaryDirectory() as temp_dir, patch("visual_evidence_vertex._client", return_value=MagicMock()), patch(
            "visual_evidence_vertex._plan_final_visual_repair", side_effect=[weak, weak, weak, strong]
        ), patch("visual_evidence_vertex._verify_motion_spec_semantics", side_effect=[RuntimeError("1"), RuntimeError("2"), RuntimeError("3"), None]):
            repaired = repair_final_visual_failures(script, report, temp_dir)
        self.assertEqual(repaired["segments"][0]["visual"]["screen_text"], strong["screen_text"])

    def test_final_repair_plan_can_escalate_to_pro_route(self):
        client = MagicMock()
        client.models.generate_content.return_value = SimpleNamespace(text=json.dumps({
            "media_type": "motion_graphic", "screen_text": ["AI စနစ်"],
            "caption": "AI စနစ်", "prompt": "ordered", "motion_preset": "static",
            "transition": "cut", "composition": "focal_center", "mascot_presence": "none",
            "motion_spec": {"layout": "concept", "labels": ["AI စနစ်"], "values": [], "object_count": None, "accent_index": None},
        }, ensure_ascii=False))
        with patch("visual_evidence_vertex.model_for", side_effect=lambda stage: {"storyboard_direction": "pro-model"}[stage]):
            _plan_final_visual_repair(client, script_fixture()["segments"][0], ["missing actor"], [], model_stage="storyboard_direction")
        self.assertEqual(client.models.generate_content.call_args.kwargs["model"], "pro-model")

    def test_final_repair_does_not_restore_stale_pre_creative_treatments(self):
        current = VideoScript.model_validate(script_fixture()).model_dump(mode="json")
        current_treatment = {
            "treatment_type": "object_action", "focal_object": "current", "action": "moves",
            "change": "position", "visual_world": "current-world", "motion_family": "object",
            "text_mode": "label", "director_reason": "current creative rhythm", "attention_reset": True,
        }
        stale = copy.deepcopy(current)
        stale_treatment = copy.deepcopy(current_treatment)
        stale_treatment["focal_object"] = "stale"
        current["segments"][0]["visual"]["evidence_shots"][0]["treatment"] = current_treatment
        stale["segments"][0]["visual"]["evidence_shots"][0]["treatment"] = stale_treatment
        report = {"passed": False, "segments": [
            {"segment_id": "s1", "passed": False, "issues": ["outcome missing"]},
        ]}
        plan = {
            "media_type": "motion_graphic", "screen_text": ["ရလဒ်"], "caption": "ရလဒ်",
            "prompt": "show outcome", "motion_preset": "static", "transition": "cut",
            "composition": "split_stage", "mascot_presence": "none",
            "motion_spec": {"layout": "sequence", "labels": ["ပစ္စည်း ၅ ခု", "ရလဒ်"], "values": ["5"], "object_count": None, "accent_index": 1},
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            Path(temp_dir, "visual_evidence_checkpoint.json").write_text(json.dumps({
                "input_fingerprint": _input_fingerprint(stale), "script": stale,
            }, ensure_ascii=False))
            with patch("visual_evidence_vertex._client", return_value=MagicMock()), patch(
                "visual_evidence_vertex._plan_final_visual_repair", return_value=plan
            ), patch("visual_evidence_vertex._verify_motion_spec_semantics"):
                repaired = repair_final_visual_failures(current, report, temp_dir)

        self.assertEqual(
            repaired["segments"][0]["visual"]["evidence_shots"][0]["treatment"],
            current_treatment,
        )

    def test_final_repair_resume_skips_checkpointed_verified_scene(self):
        original = script_fixture()
        original_shot = original["segments"][0]["visual"]["evidence_shots"][0]
        original_shot.update({
            "media_type": "generated_image",
            "asset_path": "job-visuals/old.png",
            "verification_status": "passed",
        })
        normalized_original = VideoScript.model_validate(copy.deepcopy(original)).model_dump(mode="json")
        repaired = copy.deepcopy(normalized_original)
        repaired_shot = repaired["segments"][0]["visual"]["evidence_shots"][0]
        repaired_shot.update({
            "media_type": "motion_graphic",
            "asset_path": None,
            "motion_spec": {"layout": "count", "labels": ["ပစ္စည်း"], "values": ["၅"], "object_count": 5},
            "verification_status": "passed",
        })
        report = {"passed": False, "segments": [{"segment_id": "s1", "passed": False, "issues": ["old English text"]}]}

        with tempfile.TemporaryDirectory() as temp_dir:
            Path(temp_dir, "final_visual_repair_checkpoint.json").write_text(json.dumps({
                "source_fingerprint": _input_fingerprint(normalized_original),
                "script": repaired,
            }, ensure_ascii=False))
            with patch("visual_evidence_vertex._client", return_value=MagicMock()), patch(
                "visual_evidence_vertex._plan_final_visual_repair"
            ) as planner, patch(
                "visual_evidence_vertex.generate_and_verify_visual_evidence", side_effect=lambda value, _root: value
            ):
                result = repair_final_visual_failures(original, report, temp_dir)

        planner.assert_not_called()
        self.assertEqual(result["segments"][0]["visual"]["evidence_shots"][0]["media_type"], "motion_graphic")

    def test_legacy_relationship_mode_is_vertex_classified_without_losing_passed_asset(self):
        script = script_fixture()
        shot = script["segments"][0]["visual"]["evidence_shots"][0]
        shot.update({
            "media_type": "motion_graphic", "asset_path": None,
            "verification_status": "passed", "motion_spec": {
                "layout": "relationship", "labels": ["AI", "လူသား"],
                "values": ["အစားထိုး၍မရပါ", "5"], "object_count": None,
                "accent_index": 0,
            },
        })
        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "visual_evidence_vertex._client", return_value=MagicMock()
        ), patch("visual_evidence_vertex._classify_relation_mode", return_value="non_replacement"):
            upgraded = ensure_relationship_modes(script, temp_dir)
        upgraded_shot = upgraded["segments"][0]["visual"]["evidence_shots"][0]
        self.assertEqual(upgraded_shot["motion_spec"]["relation_mode"], "non_replacement")
        self.assertEqual(upgraded_shot["verification_status"], "passed")

    def test_transient_relationship_classification_uses_directional_fallback(self):
        script = script_fixture()
        shot = script["segments"][0]["visual"]["evidence_shots"][0]
        shot.update({
            "media_type": "motion_graphic", "asset_path": None,
            "verification_status": "passed", "motion_spec": {
                "layout": "relationship", "labels": ["အကြောင်း", "ရလဒ်"],
                "values": ["5"], "object_count": None, "accent_index": 0,
            },
        })
        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "visual_evidence_vertex._client", return_value=MagicMock()
        ), patch(
            "visual_evidence_vertex._classify_relation_mode",
            side_effect=ClientError(429, {"error": {"message": "quota"}}),
        ):
            upgraded = ensure_relationship_modes(script, temp_dir)
        upgraded_shot = upgraded["segments"][0]["visual"]["evidence_shots"][0]
        self.assertEqual(upgraded_shot["motion_spec"]["relation_mode"], "directional")

    def test_mismatched_checkpoint_is_reset_before_first_remote_call(self):
        client = MagicMock()
        with tempfile.TemporaryDirectory() as temp_dir:
            checkpoint = Path(temp_dir) / "visual_evidence_checkpoint.json"
            stale = script_fixture()
            stale["segments"][0]["visual"]["evidence_shots"][0]["verification_status"] = "passed"
            checkpoint.write_text(json.dumps({
                "input_fingerprint": "different-lock",
                "script": stale,
            }))

            responses = iter([
                self.image_response(),
                SimpleNamespace(text='{"passed":true,"proved_claim_ids":["c1"],"observed_values":["5"],"issues":[]}'),
            ])
            def respond(*_args, **_kwargs):
                if client.models.generate_content.call_count == 1:
                    current = json.loads(checkpoint.read_text())
                    shot = current["script"]["segments"][0]["visual"]["evidence_shots"][0]
                    self.assertEqual(shot["verification_status"], "planned")
                return next(responses)
            client.models.generate_content.side_effect = respond
            with patch("visual_evidence_vertex._client", return_value=client):
                generate_and_verify_visual_evidence(script_fixture(), temp_dir)

    def test_failed_verification_regenerates_then_fails_closed(self):
        client = MagicMock()
        failure = SimpleNamespace(text='{"passed":false,"proved_claim_ids":[],"observed_values":["4"],"issues":["only four boxes"]}')
        client.models.generate_content.side_effect = [self.image_response(), failure, self.image_response(), failure]
        with tempfile.TemporaryDirectory() as temp_dir, patch("visual_evidence_vertex._client", return_value=client):
            with self.assertRaisesRegex(RuntimeError, "failed after 2 attempts"):
                generate_and_verify_visual_evidence(script_fixture(), temp_dir)

    def test_missing_evidence_plan_fails_before_vertex_call(self):
        script = script_fixture()
        script["segments"][0]["visual"]["evidence_claims"] = []
        script["segments"][0]["visual"]["evidence_shots"] = []
        client = MagicMock()
        with tempfile.TemporaryDirectory() as temp_dir, patch("visual_evidence_vertex._client", return_value=client):
            with self.assertRaisesRegex(ValueError, "no visual evidence plan"):
                generate_and_verify_visual_evidence(script, temp_dir)
        client.models.generate_content.assert_not_called()

    def test_generated_video_keeps_verified_still_fallback(self):
        script = script_fixture()
        script["segments"][0]["visual"]["evidence_shots"][0]["media_type"] = "generated_video"
        client = MagicMock()
        client.models.generate_content.side_effect = [
            self.image_response(),
            SimpleNamespace(text='{"passed":true,"proved_claim_ids":["c1"],"observed_values":["5"],"issues":[]}'),
        ]
        with tempfile.TemporaryDirectory() as temp_dir, patch("visual_evidence_vertex._client", return_value=client), patch.dict("os.environ", {"FYF_ENABLE_VERTEX_VIDEO": "0"}):
            shot = generate_and_verify_visual_evidence(script, temp_dir)["segments"][0]["visual"]["evidence_shots"][0]
        self.assertEqual(shot["fallback_asset_path"], "job-visuals/s1-count.png")
        self.assertEqual(shot["media_type"], "generated_image")
        self.assertTrue(shot["fallback_used"])

    def test_generated_video_pass_uses_verified_mp4(self):
        script = script_fixture()
        script["segments"][0]["visual"]["evidence_shots"][0]["media_type"] = "generated_video"
        client = MagicMock()
        client.models.generate_content.side_effect = [
            self.image_response(),
            SimpleNamespace(text='{"passed":true,"proved_claim_ids":["c1"],"observed_values":["5"],"issues":[]}'),
        ]

        def fake_video(_client, _still, destination, _required, _shot):
            destination.write_bytes(b"mp4")

        with tempfile.TemporaryDirectory() as temp_dir, patch("visual_evidence_vertex._client", return_value=client), patch(
            "visual_evidence_vertex._generate_verified_video", side_effect=fake_video
        ):
            shot = generate_and_verify_visual_evidence(script, temp_dir)["segments"][0]["visual"]["evidence_shots"][0]
            self.assertEqual((Path(temp_dir) / "visuals" / "s1-count.mp4").read_bytes(), b"mp4")
        self.assertEqual(shot["media_type"], "generated_video")
        self.assertEqual(shot["asset_path"], "job-visuals/s1-count.mp4")
        self.assertEqual(shot["fallback_asset_path"], "job-visuals/s1-count.png")
        self.assertFalse(shot["fallback_used"])

    def test_generated_video_qa_failure_falls_back_without_broken_mp4(self):
        script = script_fixture()
        script["segments"][0]["visual"]["evidence_shots"][0]["media_type"] = "generated_video"
        client = MagicMock()
        client.models.generate_content.side_effect = [
            self.image_response(),
            SimpleNamespace(text='{"passed":true,"proved_claim_ids":["c1"],"observed_values":["5"],"issues":[]}'),
        ]

        def failed_video(_client, _still, destination, _required, _shot):
            destination.write_bytes(b"broken")
            raise RuntimeError("video changed count")

        with tempfile.TemporaryDirectory() as temp_dir, patch("visual_evidence_vertex._client", return_value=client), patch(
            "visual_evidence_vertex._generate_verified_video", side_effect=failed_video
        ):
            shot = generate_and_verify_visual_evidence(script, temp_dir)["segments"][0]["visual"]["evidence_shots"][0]
            self.assertFalse((Path(temp_dir) / "visuals" / "s1-count.mp4").exists())
        self.assertEqual(shot["media_type"], "generated_image")
        self.assertTrue(shot["fallback_used"])

    def test_motion_graphic_missing_locked_value_fails_closed(self):
        script = script_fixture()
        shot = script["segments"][0]["visual"]["evidence_shots"][0]
        shot["media_type"] = "motion_graphic"
        shot["motion_spec"] = {"layout": "count", "labels": ["boxes"], "values": ["4"], "object_count": 4}
        client = MagicMock()
        with tempfile.TemporaryDirectory() as temp_dir, patch("visual_evidence_vertex._client", return_value=client):
            with self.assertRaisesRegex(ValueError, "does not visibly encode claim values"):
                generate_and_verify_visual_evidence(script, temp_dir)
        client.models.generate_content.assert_not_called()

    def test_motion_graphic_accepts_equivalent_myanmar_digit_label(self):
        script = script_fixture()
        shot = script["segments"][0]["visual"]["evidence_shots"][0]
        shot["media_type"] = "motion_graphic"
        shot["motion_spec"] = {"layout": "count", "labels": ["ပစ္စည်း"], "values": ["၅ ခု"], "object_count": 5}
        client = MagicMock()
        client.models.generate_content.return_value = SimpleNamespace(text='{"passed":true,"proved_claim_ids":["c1"],"observed_values":["5"],"issues":[]}')
        with tempfile.TemporaryDirectory() as temp_dir, patch("visual_evidence_vertex._client", return_value=client):
            result = generate_and_verify_visual_evidence(script, temp_dir)
        self.assertEqual(result["segments"][0]["visual"]["evidence_shots"][0]["verification_status"], "passed")
        client.models.generate_content.assert_called_once()

    def test_motion_graphic_allows_burmese_labels_for_english_concept_values(self):
        script = script_fixture()
        visual = script["segments"][0]["visual"]
        visual["evidence_claims"] = [{
            "claim_id": "c1",
            "statement": "Ko Kyaw uses AI to create software",
            "evidence_type": "relationship",
            "values": ["Ko Kyaw", "AI", "software"],
        }]
        shot = visual["evidence_shots"][0]
        shot.update({
            "media_type": "motion_graphic",
            "motion_spec": {
                "layout": "relationship",
                "labels": ["ကိုကျော်", "AI", "ဆော့ဖ်ဝဲ"],
                "values": ["အသုံးပြုသူ", "ကူညီသူ", "ရလဒ်"],
                "object_count": None,
            },
        })
        client = MagicMock()
        client.models.generate_content.return_value = SimpleNamespace(
            text='{"passed":true,"proved_claim_ids":["c1"],"observed_values":["Ko Kyaw","AI","software"],"issues":[]}'
        )
        with tempfile.TemporaryDirectory() as temp_dir, patch("visual_evidence_vertex._client", return_value=client):
            result = generate_and_verify_visual_evidence(script, temp_dir)
        self.assertEqual(
            result["segments"][0]["visual"]["evidence_shots"][0]["verification_status"],
            "passed",
        )

    def test_motion_graphic_rejects_value_label_when_count_objects_disagree(self):
        script = script_fixture()
        shot = script["segments"][0]["visual"]["evidence_shots"][0]
        shot["media_type"] = "motion_graphic"
        shot["motion_spec"] = {"layout": "count", "labels": ["ပစ္စည်း"], "values": ["၅"], "object_count": 4}
        with tempfile.TemporaryDirectory() as temp_dir, patch("visual_evidence_vertex._client") as client:
            with self.assertRaisesRegex(ValueError, "object_count must equal"):
                generate_and_verify_visual_evidence(script, temp_dir)
        client.assert_called_once()

    def test_image_verifier_prompt_rejects_unapproved_generated_english_text(self):
        from visual_evidence_vertex import _verification_prompt

        prompt = _verification_prompt(
            [{"claim_id": "c1", "statement": "A claim", "evidence_type": "concept", "values": []}],
            {"shot_id": "s1", "proves_claim_ids": ["c1"]},
        )
        self.assertIn("Reject generated Latin-script prose", prompt)
        self.assertIn("AI, XAI, and FYF", prompt)

    def test_semantically_incomplete_planned_motion_is_repaired(self):
        script = script_fixture()
        shot = script["segments"][0]["visual"]["evidence_shots"][0]
        shot["media_type"] = "motion_graphic"
        shot["motion_spec"] = {"layout": "count", "labels": ["ပစ္စည်း"], "values": ["၅"], "object_count": 5}
        client = MagicMock()
        client.models.generate_content.side_effect = [
            SimpleNamespace(text='{"passed":false,"proved_claim_ids":[],"observed_values":["5"],"issues":["meaning missing"]}'),
            SimpleNamespace(text='{"caption":"ပစ္စည်း ၅ ခုရှိသည်","motion_spec":{"layout":"count","labels":["ပစ္စည်း"],"values":["၅"],"object_count":5,"accent_index":null}}'),
            SimpleNamespace(text='{"passed":true,"proved_claim_ids":["c1"],"observed_values":["5"],"issues":[]}'),
        ]
        with tempfile.TemporaryDirectory() as temp_dir, patch("visual_evidence_vertex._client", return_value=client):
            result = generate_and_verify_visual_evidence(script, temp_dir)
        repaired = result["segments"][0]["visual"]["evidence_shots"][0]
        self.assertTrue(repaired["fallback_used"])
        self.assertEqual(repaired["verification_status"], "passed")

    def test_image_quota_exhaustion_repairs_to_motion_and_checkpoints(self):
        script = script_fixture()
        client = MagicMock()
        repair = SimpleNamespace(text='{"caption":"ပစ္စည်း ၅ ခု","motion_spec":{"layout":"count","labels":["ပစ္စည်း"],"values":["၅"],"object_count":5,"accent_index":null}}')
        repair_pass = SimpleNamespace(text='{"passed":true,"proved_claim_ids":["c1"],"observed_values":["5"],"issues":[]}')
        client.models.generate_content.side_effect = [RuntimeError("quota"), repair, repair_pass]
        with tempfile.TemporaryDirectory() as temp_dir, patch("visual_evidence_vertex._client", return_value=client):
            result = generate_and_verify_visual_evidence(script, temp_dir)
            checkpoint = Path(temp_dir) / "visual_evidence_checkpoint.json"
            self.assertTrue(checkpoint.is_file())
        shot = result["segments"][0]["visual"]["evidence_shots"][0]
        self.assertEqual(shot["media_type"], "motion_graphic")
        self.assertTrue(shot["fallback_used"])

    def test_matching_checkpoint_skips_completed_shot(self):
        client = MagicMock()
        client.models.generate_content.side_effect = [
            self.image_response(),
            SimpleNamespace(text='{"passed":true,"proved_claim_ids":["c1"],"observed_values":["5"],"issues":[]}'),
        ]
        with tempfile.TemporaryDirectory() as temp_dir, patch("visual_evidence_vertex._client", return_value=client):
            first = generate_and_verify_visual_evidence(script_fixture(), temp_dir)
            calls = client.models.generate_content.call_count
            second = generate_and_verify_visual_evidence(script_fixture(), temp_dir)
        self.assertEqual(first, second)
        self.assertEqual(client.models.generate_content.call_count, calls)

    def test_matching_checkpoint_skips_passed_motion_spec(self):
        script = script_fixture()
        shot = script["segments"][0]["visual"]["evidence_shots"][0]
        shot.update({
            "media_type": "motion_graphic",
            "motion_preset": "static",
            "motion_spec": {"layout": "count", "labels": ["ပစ္စည်း"], "values": ["၅"], "object_count": 5},
        })
        client = MagicMock()
        client.models.generate_content.return_value = SimpleNamespace(text='{"passed":true,"proved_claim_ids":["c1"],"observed_values":["5"],"issues":[]}')
        with tempfile.TemporaryDirectory() as temp_dir, patch("visual_evidence_vertex._client", return_value=client):
            generate_and_verify_visual_evidence(script, temp_dir)
            first_calls = client.models.generate_content.call_count
            generate_and_verify_visual_evidence(script, temp_dir)
        self.assertEqual(client.models.generate_content.call_count, first_calls)

    def test_motion_verifier_does_not_receive_caption_as_evidence(self):
        script = script_fixture()
        shot = script["segments"][0]["visual"]["evidence_shots"][0]
        shot.update({
            "caption": "CAPTION_ONLY_SECRET",
            "media_type": "motion_graphic",
            "motion_preset": "static",
            "motion_spec": {"layout": "count", "labels": ["ပစ္စည်း"], "values": ["၅"], "object_count": 5},
        })
        client = MagicMock()
        client.models.generate_content.return_value = SimpleNamespace(text='{"passed":true,"proved_claim_ids":["c1"],"observed_values":["5"],"issues":[]}')
        with tempfile.TemporaryDirectory() as temp_dir, patch("visual_evidence_vertex._client", return_value=client):
            generate_and_verify_visual_evidence(script, temp_dir)
        prompt = client.models.generate_content.call_args.kwargs["contents"]
        self.assertNotIn("CAPTION_ONLY_SECRET", prompt)
        self.assertIn("Burmese-speaking beginner", prompt)
        self.assertIn("do not require English", prompt)

    def test_transient_vertex_quota_is_retried(self):
        call = MagicMock(side_effect=[ClientError(429, {"error": {"message": "quota"}}), "ok"])
        with patch.dict("os.environ", {"FYF_VERTEX_RETRY_BASE_SECONDS": "0"}):
            self.assertEqual(_quota_retry(call, label="test"), "ok")
        self.assertEqual(call.call_count, 2)

    def test_quota_retry_uses_long_bounded_default_backoff(self):
        call = MagicMock(side_effect=[
            ClientError(429, {"error": {"message": "quota"}}),
            ClientError(504, {"error": {"message": "deadline"}}),
            "ok",
        ])
        with (
            patch.dict("os.environ", {}, clear=True),
            patch("visual_evidence_vertex.time.sleep") as sleep,
        ):
            self.assertEqual(_quota_retry(call, label="default-backoff"), "ok")
        self.assertEqual([item.args[0] for item in sleep.call_args_list], [10, 20])

    def test_quota_retry_honors_smaller_attempt_budget(self):
        call = MagicMock(side_effect=ClientError(429, {"error": {"message": "quota"}}))
        with patch.dict("os.environ", {"FYF_VERTEX_RETRY_BASE_SECONDS": "0"}):
            with self.assertRaises(ClientError):
                _quota_retry(call, label="quality", attempts=2)
        self.assertEqual(call.call_count, 2)

    def test_deadline_exceeded_is_retried_as_transient(self):
        call = MagicMock(side_effect=[ClientError(504, {"error": {"message": "deadline"}}), "ok"])
        with patch.dict("os.environ", {"FYF_VERTEX_RETRY_BASE_SECONDS": "0"}):
            self.assertEqual(_quota_retry(call, label="deadline"), "ok")
        self.assertEqual(call.call_count, 2)

    @patch("visual_evidence_vertex.genai.Client")
    def test_client_has_bounded_http_timeout(self, client_constructor):
        with patch.dict("os.environ", {"FYF_VERTEX_CALL_TIMEOUT_SECONDS": "45"}):
            _client()
        options = client_constructor.call_args.kwargs["http_options"]
        self.assertEqual(options.timeout, 45_000)

    def test_failed_generated_media_repairs_to_verified_motion_graphic(self):
        script = script_fixture()
        client = MagicMock()
        failed_image = SimpleNamespace(text='{"passed":false,"proved_claim_ids":[],"observed_values":["4"],"issues":["wrong count"]}')
        repair = SimpleNamespace(text='{"caption":"ပစ္စည်း ၅ ခု","motion_spec":{"layout":"count","labels":["ပစ္စည်း"],"values":["၅ ခု"],"object_count":5,"accent_index":null}}')
        repair_pass = SimpleNamespace(text='{"passed":true,"proved_claim_ids":["c1"],"observed_values":["5"],"issues":[]}')
        client.models.generate_content.side_effect = [
            self.image_response(), failed_image,
            self.image_response(), failed_image,
            repair, repair_pass,
        ]
        with tempfile.TemporaryDirectory() as temp_dir, patch("visual_evidence_vertex._client", return_value=client):
            shot = generate_and_verify_visual_evidence(script, temp_dir)["segments"][0]["visual"]["evidence_shots"][0]
        self.assertEqual(shot["media_type"], "motion_graphic")
        self.assertEqual(shot["motion_spec"]["object_count"], 5)
        self.assertEqual(shot["verification_status"], "passed")
        self.assertTrue(shot["fallback_used"])

    def test_invalid_motion_repair_contract_is_reprompted_with_validation_feedback(self):
        script = script_fixture()
        client = MagicMock()
        failed_image = SimpleNamespace(
            text='{"passed":false,"proved_claim_ids":[],"observed_values":["4"],"issues":["wrong count"]}'
        )
        invalid_repair = SimpleNamespace(text=json.dumps({
            "caption": "ပစ္စည်း ၅ ခု",
            "motion_spec": {
                "layout": "directional_branch",
                "labels": ["ပစ္စည်း"],
                "values": ["၅ ခု"],
                "object_count": None,
                "accent_index": None,
                "relation_mode": "directional",
            },
        }, ensure_ascii=False))
        valid_repair = SimpleNamespace(
            text='{"caption":"ပစ္စည်း ၅ ခု","motion_spec":{"layout":"count","labels":["ပစ္စည်း"],"values":["၅ ခု"],"object_count":5,"accent_index":null}}'
        )
        repair_pass = SimpleNamespace(
            text='{"passed":true,"proved_claim_ids":["c1"],"observed_values":["5"],"issues":[]}'
        )
        client.models.generate_content.side_effect = [
            self.image_response(), failed_image,
            self.image_response(), failed_image,
            invalid_repair, valid_repair, repair_pass,
        ]

        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "visual_evidence_vertex._client", return_value=client
        ):
            shot = generate_and_verify_visual_evidence(script, temp_dir)["segments"][0]["visual"]["evidence_shots"][0]

        second_repair_prompt = client.models.generate_content.call_args_list[5].kwargs["contents"]
        self.assertIn("relation_mode is allowed only for relationship layout", second_repair_prompt)
        self.assertEqual(shot["motion_spec"]["layout"], "count")
        self.assertEqual(shot["verification_status"], "passed")

    def test_motion_repair_can_use_third_feedback_round(self):
        script = script_fixture()
        client = MagicMock()
        failed_image = SimpleNamespace(
            text='{"passed":false,"proved_claim_ids":[],"observed_values":["4"],"issues":["wrong count"]}'
        )
        repair = SimpleNamespace(
            text='{"caption":"ပစ္စည်း ၅ ခု","motion_spec":{"layout":"count","labels":["ပစ္စည်း"],"values":["၅ ခု"],"object_count":5,"accent_index":null}}'
        )
        repair_fail_one = SimpleNamespace(
            text='{"passed":false,"proved_claim_ids":[],"observed_values":["5"],"issues":["missing condition"]}'
        )
        repair_fail_two = SimpleNamespace(
            text='{"passed":false,"proved_claim_ids":[],"observed_values":["5"],"issues":["missing actor"]}'
        )
        repair_pass = SimpleNamespace(
            text='{"passed":true,"proved_claim_ids":["c1"],"observed_values":["5"],"issues":[]}'
        )
        client.models.generate_content.side_effect = [
            self.image_response(), failed_image,
            self.image_response(), failed_image,
            repair, repair_fail_one,
            repair, repair_fail_two,
            repair, repair_pass,
        ]

        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "visual_evidence_vertex._client", return_value=client
        ):
            shot = generate_and_verify_visual_evidence(script, temp_dir)["segments"][0]["visual"]["evidence_shots"][0]

        third_repair_prompt = client.models.generate_content.call_args_list[8].kwargs["contents"]
        self.assertIn("missing actor", third_repair_prompt)
        self.assertEqual(shot["verification_status"], "passed")

    def test_motion_repair_escalates_to_pro_after_semantic_failure(self):
        script = script_fixture()
        client = MagicMock()
        failed_image = SimpleNamespace(
            text='{"passed":false,"proved_claim_ids":[],"observed_values":[],"issues":["missing qualifier"]}'
        )
        repair = SimpleNamespace(
            text='{"caption":"ပြင်ဆင်ထားသည်","motion_spec":{"layout":"count","labels":["ပစ္စည်း"],"values":["၅"],"object_count":5,"accent_index":null}}'
        )
        repair_fail = SimpleNamespace(
            text='{"passed":false,"proved_claim_ids":[],"observed_values":[],"issues":["without human oversight is missing"]}'
        )
        repair_pass = SimpleNamespace(
            text='{"passed":true,"proved_claim_ids":["c1"],"observed_values":[],"issues":[]}'
        )
        client.models.generate_content.side_effect = [
            self.image_response(), failed_image,
            self.image_response(), failed_image,
            repair, repair_fail,
            repair, repair_pass,
        ]

        routes = {
            "visual_generation": "image-model",
            "visual_generation_quality": "quality-image-model",
            "visual_verification": "verify-model",
            "repair": "flash-model",
            "storyboard_direction": "pro-model",
        }
        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "visual_evidence_vertex._client", return_value=client
        ), patch("visual_evidence_vertex.model_for", side_effect=lambda stage: routes[stage]):
            generate_and_verify_visual_evidence(script, temp_dir)

        first_repair = client.models.generate_content.call_args_list[4]
        second_repair = client.models.generate_content.call_args_list[6]
        self.assertEqual(first_repair.kwargs["model"], "flash-model")
        self.assertEqual(second_repair.kwargs["model"], "pro-model")
        self.assertIn("mandatory visible correction", second_repair.kwargs["contents"])
        self.assertIn("without human oversight is missing", second_repair.kwargs["contents"])

    def test_repair_creative_failures_changes_only_failed_cluster(self):
        script = copy.deepcopy(script_fixture())
        second = copy.deepcopy(script["segments"][0])
        second["id"] = "s2"
        second["text"] = "အခြားအကြောင်းအရာ။"
        second["visual"]["evidence_claims"][0]["claim_id"] = "c2"
        second["visual"]["evidence_shots"][0]["shot_id"] = "count-s2"
        second["visual"]["evidence_shots"][0]["proves_claim_ids"] = ["c2"]
        script["segments"].append(second)

        first_treatment = {"treatment_type": "object_action", "focal_object": "box", "action": "open", "change": "color", "visual_world": "studio", "motion_family": "object", "text_mode": "caption", "director_reason": "shows count", "attention_reset": False}
        second_treatment = {"treatment_type": "story_scene", "focal_object": "person", "action": "moves", "change": "position", "visual_world": "street", "motion_family": "camera", "text_mode": "none", "director_reason": "shows context", "attention_reset": True}
        for segment, treatment, asset_path in [(script["segments"][0], first_treatment, "job-visuals/s1.png"), (second, second_treatment, "job-visuals/s2.png")]:
            shot = segment["visual"]["evidence_shots"][0]
            shot["treatment"] = copy.deepcopy(treatment)
            shot["asset_path"] = asset_path
        script = VideoScript.model_validate(script).model_dump(mode="json")
        saved_second = copy.deepcopy(script["segments"][1])

        saved_claims = [(segment["id"], segment["text"], copy.deepcopy(segment["visual"]["evidence_claims"])) for segment in script["segments"]]

        comparison = {"treatment_type": "comparison_transform", "focal_object": "box", "action": "changes", "change": "before and after", "visual_world": "studio", "motion_family": "object", "text_mode": "caption", "director_reason": "shows contrast", "attention_reset": False}
        def plan_side_effect(script_data, _job_dir):
            self.assertIsNone(script_data["segments"][0]["visual"]["evidence_shots"][0]["treatment"])
            self.assertEqual(script_data["segments"][1], saved_second)
            script_data["segments"][0]["visual"]["evidence_shots"][0]["treatment"] = copy.deepcopy(comparison)
            return script_data

        with tempfile.TemporaryDirectory() as temp_dir, patch("visual_evidence_vertex.plan_visual_treatments", side_effect=plan_side_effect):
            result = repair_creative_failures(script, {"failed_clusters": [{"scene_ids": ["s1"]}]}, temp_dir)

        self.assertEqual(result["segments"][0]["visual"]["evidence_shots"][0]["treatment"]["treatment_type"], "comparison_transform")
        self.assertEqual(result["segments"][1], saved_second)
        self.assertEqual([(segment["id"], segment["text"], segment["visual"]["evidence_claims"]) for segment in result["segments"]], saved_claims)

    def test_structural_creative_repair_uses_local_rhythm_without_vertex_replanning(self):
        script = copy.deepcopy(script_fixture())
        treatment = {"treatment_type": "object_action", "focal_object": "box", "action": "open", "change": "color", "visual_world": "studio", "motion_family": "object", "text_mode": "caption", "director_reason": "shows count", "attention_reset": False}
        segments = []
        for index in range(1, 4):
            segment = copy.deepcopy(script["segments"][0])
            segment["id"] = f"s{index}"
            segment["text"] = f"စာပိုဒ် {index}။"
            claim = segment["visual"]["evidence_claims"][0]
            claim["claim_id"] = f"c{index}"
            shot = segment["visual"]["evidence_shots"][0]
            shot["shot_id"] = f"shot-{index}"
            shot["proves_claim_ids"] = [f"c{index}"]
            shot["treatment"] = copy.deepcopy(treatment)
            shot["asset_path"] = f"job-visuals/s{index}.png"
            segments.append(segment)
        script["segments"] = segments
        report = {
            "failure_codes": ["TREATMENT_RUN_REPEATED"],
            "failed_clusters": [{"scene_ids": ["s1", "s2", "s3"]}],
        }

        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "visual_evidence_vertex.plan_visual_treatments",
            side_effect=AssertionError("structural repair must not spend a Vertex call"),
        ):
            result = repair_creative_failures(script, report, temp_dir)

        kinds = [segment["visual"]["evidence_shots"][0]["treatment"]["treatment_type"] for segment in result["segments"]]
        self.assertNotEqual(kinds, ["object_action"] * 3)


if __name__ == "__main__":
    unittest.main()
