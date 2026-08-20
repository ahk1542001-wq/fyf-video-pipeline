import json
import tempfile
import unittest
from pathlib import Path


class PairedVisualsTests(unittest.TestCase):
    def _script(self, *, text: str = "တူညီသော စာသား", label: str = "မူရင်း") -> dict:
        return {
            "title": "Locked story",
            "language": "my-MM",
            "segments": [
                {
                    "id": "S1",
                    "text": text,
                    "visual": {
                        "screen_text": [label],
                        "evidence_claims": [
                            {
                                "claim_id": "S1_C1",
                                "statement": "Locked claim",
                                "evidence_type": "concept",
                                "values": ["value"],
                            }
                        ],
                        "evidence_shots": [
                            {
                                "shot_id": "S1_S1",
                                "proves_claim_ids": ["S1_C1"],
                                "media_type": "generated_image",
                                "asset_path": "job-visuals/S1-S1_S1.png",
                                "fallback_asset_path": "job-visuals/S1-S1_S1.png",
                                "verification_status": "passed",
                            }
                        ],
                    },
                }
            ],
        }

    def _approved_source(self, root: Path) -> Path:
        from backend.job_store import initialize_job_status, update_job_status

        source = root / "1111aaaa"
        source.mkdir()
        initialize_job_status(source, source.name, "kaggle")
        script = self._script(label="အတည်ပြုပြီး")
        (source / "script.json").write_text(
            json.dumps(script, ensure_ascii=False), encoding="utf-8"
        )
        (source / "visuals").mkdir()
        (source / "visuals" / "S1-S1_S1.png").write_bytes(b"approved-visual")
        reports = {
            "qa_report": {"passed": True},
            "creative_qa": {"passed": True},
            "final_visual_qa": {"passed": True, "segments": [{"segment_id": "S1", "passed": True}]},
        }
        for name, report in reports.items():
            (source / f"{name}.json").write_text(json.dumps(report), encoding="utf-8")
        update_job_status(source, {"status": "completed", **reports})
        return source

    def test_adopt_completed_visual_plan_preserves_target_voice_and_writes_integrity_checkpoint(self):
        from backend.job_store import initialize_job_status
        from backend.paired_visuals import adopt_completed_visual_plan, load_adopted_visual_plan

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = self._approved_source(root)
            target = root / "2222bbbb"
            target.mkdir()
            initialize_job_status(target, target.name, "gemini")
            (target / "script.json").write_text(
                json.dumps(self._script(label="ဟောင်း"), ensure_ascii=False), encoding="utf-8"
            )
            (target / "voice.wav").write_bytes(b"provider-specific-voice")
            (target / "voice_checkpoint.json").write_text("{}", encoding="utf-8")

            adopted = adopt_completed_visual_plan(source, target)

            self.assertEqual(adopted, json.loads((source / "script.json").read_text(encoding="utf-8")))
            self.assertEqual((target / "voice.wav").read_bytes(), b"provider-specific-voice")
            self.assertTrue((target / "paired_visual_checkpoint.json").is_file())
            self.assertEqual(
                (target / "visuals" / "S1-S1_S1.png").read_bytes(), b"approved-visual"
            )
            self.assertEqual(load_adopted_visual_plan(target), adopted)

    def test_adopt_rejects_narration_mismatch_without_mutating_target(self):
        from backend.job_store import initialize_job_status
        from backend.paired_visuals import adopt_completed_visual_plan

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = self._approved_source(root)
            target = root / "2222bbbb"
            target.mkdir()
            initialize_job_status(target, target.name, "gemini")
            original = self._script(text="မတူသော စာသား", label="ဟောင်း")
            original_bytes = json.dumps(original, ensure_ascii=False).encode("utf-8")
            (target / "script.json").write_bytes(original_bytes)

            with self.assertRaisesRegex(ValueError, "narration"):
                adopt_completed_visual_plan(source, target)

            self.assertEqual((target / "script.json").read_bytes(), original_bytes)
            self.assertFalse((target / "paired_visual_checkpoint.json").exists())

    def test_load_adopted_visual_plan_rejects_tampered_asset(self):
        from backend.job_store import initialize_job_status
        from backend.paired_visuals import adopt_completed_visual_plan, load_adopted_visual_plan

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = self._approved_source(root)
            target = root / "2222bbbb"
            target.mkdir()
            initialize_job_status(target, target.name, "gemini")
            (target / "script.json").write_text(
                json.dumps(self._script(), ensure_ascii=False), encoding="utf-8"
            )
            adopt_completed_visual_plan(source, target)
            (target / "visuals" / "S1-S1_S1.png").write_bytes(b"tampered")

            self.assertIsNone(load_adopted_visual_plan(target))


if __name__ == "__main__":
    unittest.main()
