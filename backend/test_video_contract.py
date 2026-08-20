import math
import struct
import tempfile
import unittest
import wave
from pathlib import Path

from pydantic import ValidationError

from backend.mouth_cues import (
    allocate_segment_frames,
    allocate_segment_frames_from_wav,
    build_render_input,
    generate_amplitude_mouth_cues,
    generate_burmese_text_audio_mouth_cues,
)
from video_contract import MotionGraphicSpec, ScriptSegment, VideoScript, VisualTreatment

SCRIPT = {
    "title": "စမ်းသပ်မှု",
    "language": "my-MM",
    "segments": [
        {"id": "s1", "text": "ပထမ စာသား", "visual_action": "Show map", "scene_type": "whiteboard", "mascot_action": "explain", "emotion": "focused", "emphasis": ["ပထမ"]},
        {"id": "s2", "text": "ဒုတိယ", "visual_action": "Show result", "scene_type": "demo", "mascot_action": "approve", "emotion": "confident", "emphasis": []},
    ],
}


def write_test_wav(path: Path, seconds: float = 0.4, rate: int = 8000) -> None:
    samples = []
    for index in range(round(seconds * rate)):
        time = index / rate
        amplitude = 0.0 if time < 0.1 else 0.15 if time < 0.25 else 0.75
        samples.append(round(math.sin(2 * math.pi * 220 * time) * amplitude * 32767))
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(rate)
        wav_file.writeframes(struct.pack(f"<{len(samples)}h", *samples))


class VideoContractTests(unittest.TestCase):
    def validate_visual(self, visual):
        return ScriptSegment.model_validate(
            {**SCRIPT["segments"][0], "visual": visual}
        ).visual

    def test_all_visual_treatments_validate(self):
        for treatment_type in ("story_scene", "object_action", "ui_proof", "editorial_data", "comparison_transform", "motion_diagram", "kinetic_type", "mascot_performance"):
            with self.subTest(treatment_type=treatment_type):
                treatment = VisualTreatment.model_validate({
                    "treatment_type": treatment_type,
                    "focal_object": "object",
                    "action": "changes",
                    "change": "object changes" if treatment_type != "kinetic_type" else "",
                    "visual_world": "world",
                    "motion_family": "camera",
                    "text_mode": "none",
                    "attention_reset": False,
                    "director_reason": "reason",
                })
                self.assertEqual(treatment.treatment_type, treatment_type)

    def test_unknown_treatment_rejects(self):
        with self.assertRaises(ValidationError):
            VisualTreatment.model_validate({"treatment_type": "popup", "focal_object": "object", "action": "changes", "visual_world": "world", "motion_family": "camera", "text_mode": "none", "attention_reset": False, "director_reason": "reason"})

    def test_non_kinetic_treatment_requires_observable_evidence(self):
        with self.assertRaises(ValidationError):
            VisualTreatment.model_validate({"treatment_type": "story_scene", "focal_object": "", "action": "", "visual_world": "world", "motion_family": "camera", "text_mode": "none", "attention_reset": False, "director_reason": "reason"})

    def test_non_kinetic_treatment_requires_a_nonblank_change(self):
        valid = {"treatment_type": "story_scene", "focal_object": "object", "action": "changes", "change": "object changes", "visual_world": "world", "motion_family": "camera", "text_mode": "none", "attention_reset": False, "director_reason": "reason"}
        self.assertEqual(VisualTreatment.model_validate(valid).treatment_type, "story_scene")
        with self.assertRaises(ValidationError):
            VisualTreatment.model_validate({**valid, "change": "  "})

    def test_kinetic_type_allows_missing_observable_evidence(self):
        treatment = VisualTreatment.model_validate({"treatment_type": "kinetic_type", "focal_object": "", "action": "", "visual_world": "world", "motion_family": "typography", "text_mode": "kinetic", "attention_reset": True, "director_reason": "reveal"})
        self.assertEqual(treatment.treatment_type, "kinetic_type")

    def test_legacy_shot_allows_missing_treatment(self):
        visual = self.validate_visual({
            "kind": "generic",
            "phase": "setup",
            "camera": "wide",
            "screen_text": ["legacy"],
            "evidence_claims": [{
                "claim_id": "claim-1",
                "statement": "legacy claim",
                "evidence_type": "concept",
            }],
            "evidence_shots": [{
                "shot_id": "legacy-shot",
                "proves_claim_ids": ["claim-1"],
                "prompt": "legacy prompt",
                "caption": "legacy",
                "hold_fraction": 1.0,
            }],
        })
        self.assertIsNone(visual.evidence_shots[0].treatment)

    def test_legacy_script_validates(self):
        VideoScript.model_validate(SCRIPT)

    def test_directional_branch_supports_one_cause_with_parallel_outcomes(self):
        spec = MotionGraphicSpec.model_validate({
            "layout": "directional_branch",
            "labels": ["လူသားကြီးကြပ်မှု", "AI အကျိုးကျေးဇူး", "ပြဿနာရှောင်ရှားမှု"],
            "values": [],
        })
        self.assertEqual(spec.layout, "directional_branch")

    def test_timeline_is_contiguous_and_exact(self):
        timed, total = allocate_segment_frames(SCRIPT["segments"], 1.0)
        self.assertEqual(total, 30)
        self.assertEqual(timed[0]["startFrame"], 0)
        self.assertEqual(timed[0]["endFrame"], timed[1]["startFrame"])
        self.assertEqual(timed[-1]["endFrame"], total)

    def test_timeline_is_never_shorter_than_audio(self):
        timed, total = allocate_segment_frames(SCRIPT["segments"], 45.010958, fps=30)
        self.assertEqual(total, 1351)
        self.assertGreaterEqual(total / 30, 45.010958)
        self.assertEqual(timed[-1]["endFrame"], total)

    def test_two_minute_timeline_and_mouth_cues_remain_complete(self):
        long_script = {
            "title": "နှစ်မိနစ်ကျော် စမ်းသပ်မှု",
            "language": "my-MM",
            "segments": [
                {**SCRIPT["segments"][index % 2], "id": f"long-{index:02d}"}
                for index in range(20)
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            wav_path = Path(directory) / "long-voice.wav"
            write_test_wav(wav_path, seconds=125.0, rate=8000)
            result = build_render_input(long_script, wav_path)
        self.assertEqual(result["durationInFrames"], 3750)
        self.assertEqual(result["segments"][0]["startFrame"], 0)
        self.assertEqual(result["segments"][-1]["endFrame"], 3750)
        self.assertEqual(result["mouthCueSource"], "burmese-text-audio")
        self.assertAlmostEqual(result["mouthCues"][-1]["end"], 125.0, places=2)

    def test_segment_boundary_snaps_to_real_wav_pause(self):
        with tempfile.TemporaryDirectory() as directory:
            wav_path = Path(directory) / "phrases.wav"
            rate = 8000
            samples = []
            for index in range(rate * 4):
                second = index / rate
                amplitude = 0.0 if 1.55 <= second < 2.25 else 0.35
                samples.append(round(math.sin(2 * math.pi * 220 * second) * amplitude * 32767))
            with wave.open(str(wav_path), "wb") as wav_file:
                wav_file.setnchannels(1); wav_file.setsampwidth(2); wav_file.setframerate(rate)
                wav_file.writeframes(struct.pack(f"<{len(samples)}h", *samples))
            equal_segments = [{"text": "same"}, {"text": "same"}]
            timed, total, source = allocate_segment_frames_from_wav(equal_segments, wav_path)
        self.assertEqual(source, "wav-silence-snap")
        self.assertEqual(total, 120)
        self.assertAlmostEqual(timed[0]["endFrame"] / 30, 1.9, delta=0.12)

    def test_cues_follow_real_wav_amplitude(self):
        with tempfile.TemporaryDirectory() as directory:
            wav_path = Path(directory) / "voice.wav"
            write_test_wav(wav_path)
            cues = generate_amplitude_mouth_cues(wav_path)
            self.assertEqual(cues[0]["value"], "X")
            self.assertIn("D", {cue["value"] for cue in cues})
            self.assertAlmostEqual(cues[-1]["end"], 0.4, places=2)

    def test_render_input_contains_audio_owned_timing(self):
        with tempfile.TemporaryDirectory() as directory:
            wav_path = Path(directory) / "voice.wav"
            write_test_wav(wav_path)
            result = build_render_input(SCRIPT, wav_path)
            self.assertEqual(result["durationInFrames"], 12)
            self.assertEqual(result["segments"][-1]["endFrame"], 12)
            self.assertEqual(result["audioSrc"], "voice.wav")
            self.assertEqual(result["mouthCueSource"], "burmese-text-audio")

    def test_burmese_text_audio_uses_more_than_amplitude_open_shapes(self):
        with tempfile.TemporaryDirectory() as directory:
            wav_path = Path(directory) / "voice.wav"
            write_test_wav(wav_path, seconds=1.0)
            segments = [{"text": "မူလ အရေးကြီး", "startFrame": 0, "endFrame": 30}]
            cues = generate_burmese_text_audio_mouth_cues(wav_path, segments)
            values = {cue["value"] for cue in cues}
            self.assertIn("B", values)
            self.assertTrue(values.intersection({"E", "H"}))
            self.assertEqual(cues[0]["value"], "X")
            self.assertAlmostEqual(cues[-1]["end"], 1.0, places=2)

    def test_contract_rejects_vertex_timing_fields(self):
        invalid = {**SCRIPT, "segments": [{**SCRIPT["segments"][0], "startFrame": 0}]}
        with self.assertRaises(ValidationError):
            VideoScript.model_validate(invalid)

    def test_pilot_scene1(self):
        # Mismatch
        segment = {
            **SCRIPT["segments"][0],
            "visual": {
                "kind": "inventory_mismatch",
                "physical_stock": 12,
                "system_stock": 2,
                "phase": "alert",
                "camera": "wide",
                "screen_text": ["Physical stock: 12", "System stock: 2"],
            },
        }
        parsed = ScriptSegment.model_validate(segment)
        self.assertEqual(parsed.visual.physical_stock, 12)
        self.assertEqual(parsed.visual.system_stock, 2)

    def test_pilot_scene5(self):
        # Approval gate
        segment = {
            **SCRIPT["segments"][0],
            "visual": {
                "kind": "approval_gate",
                "actor": "both",
                "physical_stock": 12,
                "system_stock": 2,
                "phase": "in_progress",
                "camera": "close_up",
                "screen_text": ["ဂိုဒေါင် 12", "စနစ် 2 — လူက စစ်ဆေးနေသည်"],
            },
        }
        ScriptSegment.model_validate(segment)

    def test_pilot_scene10(self):
        # Inventory correction
        segment = {
            **SCRIPT["segments"][0],
            "visual": {
                "kind": "inventory_correction",
                "from_value": 2,
                "to_value": 12,
                "phase": "completed",
                "camera": "push_in",
                "screen_text": ["System stock", "2 → 12"],
                "completion_ui": True,
            },
        }
        ScriptSegment.model_validate(segment)

    def test_three_screen_lines_rejects(self):
        segment = {
            **SCRIPT["segments"][0],
            "visual": {
                "kind": "generic",
                "phase": "setup",
                "camera": "wide",
                "screen_text": ["1", "2", "3"],
            },
        }
        with self.assertRaises(ValidationError):
            ScriptSegment.model_validate(segment)

    def test_unlabeled_fact_number_rejects(self):
        segment = {
            **SCRIPT["segments"][0],
            "visual": {
                "kind": "inventory_mismatch",
                "physical_stock": 12,
                "system_stock": 2,
                "phase": "alert",
                "camera": "wide",
                "screen_text": ["Mismatch found", "Please check"]
            }
        }
        with self.assertRaises(ValidationError):
            ScriptSegment.model_validate(segment)

    def test_system_two_is_not_mislabeled_by_physical_twelve(self):
        segment = {
            **SCRIPT["segments"][0],
            "visual": {
                "kind": "inventory_mismatch",
                "physical_stock": 12,
                "system_stock": 2,
                "phase": "alert",
                "camera": "wide",
                "screen_text": ["Physical stock: 12", "System stock missing"],
            },
        }
        with self.assertRaises(ValidationError):
            ScriptSegment.model_validate(segment)

    def test_blank_screen_line_rejects(self):
        segment = {
            **SCRIPT["segments"][0],
            "visual": {
                "kind": "generic",
                "phase": "setup",
                "camera": "wide",
                "screen_text": ["Valid", "   "],
            },
        }
        with self.assertRaises(ValidationError):
            ScriptSegment.model_validate(segment)

    def test_zero_inventory_fact_rejects(self):
        segment = {
            **SCRIPT["segments"][0],
            "visual": {
                "kind": "inventory_mismatch",
                "physical_stock": 0,
                "system_stock": 2,
                "phase": "alert",
                "camera": "wide",
                "screen_text": ["Physical stock: 0", "System stock: 2"],
            },
        }
        with self.assertRaises(ValidationError):
            ScriptSegment.model_validate(segment)

    def test_in_progress_correction_validates(self):
        segment = {
            **SCRIPT["segments"][0],
            "visual": {
                "kind": "inventory_correction",
                "from_value": 2,
                "to_value": 12,
                "phase": "in_progress",
                "camera": "wide",
                "screen_text": ["Updating system stock", "2 → 12"],
                "completion_ui": False,
            },
        }
        parsed = ScriptSegment.model_validate(segment)
        self.assertFalse(parsed.visual.completion_ui)

    def test_invalid_completed_correction_rejects(self):
        segment = {
            **SCRIPT["segments"][0],
            "visual": {
                "kind": "inventory_correction",
                "from_value": 10,
                "to_value": 10,
                "phase": "completed",
                "camera": "wide",
                "screen_text": ["Updated system stock", "10 → 10"],
            },
        }
        with self.assertRaises(ValidationError):
            ScriptSegment.model_validate(segment)

    def test_full_scene_visual_kinds_validate(self):
        visuals = [
            {
                "kind": "auto_action",
                "action": "reorder",
                "severity": "mistake",
                "phase": "alert",
                "camera": "push_in",
                "screen_text": ["အလိုအလျောက် ထပ်မှာမှု"],
            },
            {
                "kind": "consequence",
                "mode": "three_impacts",
                "items": ["ငွေစီးဆင်းမှု", "ဂိုဒေါင်နေရာ", "အစီရင်ခံစာ"],
                "phase": "in_progress",
                "camera": "wide",
                "screen_text": ["အကျိုးဆက် သုံးမျိုး"],
            },
            {
                "kind": "process_timeline",
                "step": "detect",
                "active_step": 1,
                "total_steps": 3,
                "phase": "in_progress",
                "camera": "push_in",
                "screen_text": ["အဆင့် 1", "ကွာဟချက် သတိပေး"],
            },
            {
                "kind": "human_verification",
                "mode": "checklist",
                "options": ["စာရင်းမသွင်းရသေး", "စနစ်ချို့ယွင်း"],
                "phase": "in_progress",
                "camera": "close_up",
                "screen_text": ["လူက အကြောင်းရင်း စစ်ဆေးမည်"],
            },
            {
                "kind": "approval_record",
                "reviewer": "တာဝန်ခံ",
                "evidence": "ဂိုဒေါင်စာရင်း",
                "decision": "ပြင်ဆင်ခွင့်ပြု",
                "phase": "completed",
                "camera": "close_up",
                "screen_text": ["အတည်ပြု မှတ်တမ်း"],
            },
            {
                "kind": "balance_pair",
                "left_label": "အမြန်နှုန်း",
                "right_label": "တိကျမှု",
                "phase": "in_progress",
                "camera": "wide",
                "screen_text": ["AI နှင့် လူ ဟန်ချက်ညီမှု"],
            },
            {
                "kind": "outro",
                "tagline": "Understand AI. Build Real Systems.",
                "phase": "completed",
                "camera": "wide",
                "screen_text": ["AI + လူ ပူးပေါင်းမှု"],
            },
        ]
        for visual in visuals:
            with self.subTest(kind=visual["kind"]):
                self.assertEqual(self.validate_visual(visual).kind, visual["kind"])

    def test_full_scene_visual_invalid_states_reject(self):
        invalid_visuals = [
            {
                "kind": "consequence",
                "mode": "three_impacts",
                "items": ["ငွေ", "   "],
                "phase": "in_progress",
                "camera": "wide",
                "screen_text": ["အကျိုးဆက်"],
            },
            {
                "kind": "human_verification",
                "mode": "checklist",
                "options": ["   "],
                "phase": "in_progress",
                "camera": "wide",
                "screen_text": ["စစ်ဆေးမှု"],
            },
            {
                "kind": "process_timeline",
                "step": "detect",
                "active_step": 4,
                "total_steps": 3,
                "phase": "in_progress",
                "camera": "wide",
                "screen_text": ["အဆင့် 4"],
            },
            {
                "kind": "process_timeline",
                "step": "audit",
                "active_step": 2,
                "total_steps": 3,
                "phase": "completed",
                "camera": "wide",
                "screen_text": ["အဆင့် 2"],
            },
            {
                "kind": "approval_record",
                "reviewer": "တာဝန်ခံ",
                "evidence": "စာရင်း",
                "decision": "စောင့်ဆိုင်း",
                "phase": "in_progress",
                "camera": "wide",
                "screen_text": ["မှတ်တမ်း"],
            },
            {
                "kind": "outro",
                "tagline": "FYF",
                "phase": "in_progress",
                "camera": "wide",
                "screen_text": ["နိဂုံး"],
            },
        ]
        for visual in invalid_visuals:
            with self.subTest(kind=visual["kind"]):
                with self.assertRaises(ValidationError):
                    self.validate_visual(visual)

if __name__ == "__main__":
    unittest.main()
