import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from backend.job_store import initialize_job_status, read_job_status, update_job_status


class PairedPipelineTests(unittest.TestCase):
    def test_run_paired_pipeline_adopts_completed_source_before_target(self):
        from backend.paired_pipeline import run_paired_pipeline

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "1234abcd"
            target = root / "5678abcd"
            source.mkdir()
            target.mkdir()
            locked = {
                "title": "Locked", "language": "my-MM",
                "segments": [{"id": "S1", "text": "တူညီသော စာသား"}],
            }
            approved = json.loads(json.dumps(locked))
            approved["segments"][0]["visual"] = {
                "screen_text": ["အတည်ပြုပြီး"], "evidence_shots": [],
            }
            initialize_job_status(source, source.name, "kaggle")
            initialize_job_status(target, target.name, "gemini")
            (source / "script.json").write_text(json.dumps(approved, ensure_ascii=False))
            (target / "script.json").write_text(json.dumps(locked, ensure_ascii=False))
            reports = {
                "qa_report": {"passed": True},
                "creative_qa": {"passed": True},
                "final_visual_qa": {
                    "passed": True,
                    "segments": [{"segment_id": "S1", "passed": True}],
                },
            }
            update_job_status(source, {"status": "completed", **reports})

            async def complete_target(job_id, script, provider, jobs_root, **_kwargs):
                self.assertEqual(job_id, target.name)
                self.assertEqual(provider, "gemini")
                self.assertEqual(
                    json.loads((target / "script.json").read_text()), approved
                )
                update_job_status(target, {"status": "completed", **reports})

            with patch("backend.paired_pipeline.run_pipeline", new=AsyncMock(side_effect=complete_target)) as run:
                asyncio.run(run_paired_pipeline(
                    source.name, target.name, locked, root,
                    source_provider="kaggle", target_provider="gemini",
                ))

            run.assert_awaited_once()
            self.assertEqual(read_job_status(target)["status"], "completed")
            self.assertTrue((target / "paired_visual_checkpoint.json").is_file())

    def test_run_paired_pipeline_does_not_run_target_when_adoption_integrity_fails(self):
        from backend.paired_pipeline import run_paired_pipeline

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "1234abcd"
            target = root / "5678abcd"
            source.mkdir()
            target.mkdir()
            locked = {
                "title": "Locked", "language": "my-MM",
                "segments": [{"id": "S1", "text": "တူညီသော စာသား"}],
            }
            initialize_job_status(source, source.name, "kaggle")
            initialize_job_status(target, target.name, "gemini")
            (source / "script.json").write_text(json.dumps(locked, ensure_ascii=False))
            (target / "script.json").write_text(json.dumps(locked, ensure_ascii=False))
            update_job_status(source, {
                "status": "completed",
                "qa_report": {"passed": True},
                "creative_qa": {"passed": False},
                "final_visual_qa": {"passed": True, "segments": [{"segment_id": "S1", "passed": True}]},
            })

            with patch("backend.paired_pipeline.run_pipeline", new_callable=AsyncMock) as run:
                asyncio.run(run_paired_pipeline(
                    source.name, target.name, locked, root,
                    source_provider="kaggle", target_provider="gemini",
                ))

            run.assert_not_awaited()
            status = read_job_status(target)
            self.assertEqual(status["status"], "failed")
            self.assertTrue(status["restart_resumable"])
            self.assertNotIn("creative", status["error"].lower())

    def test_run_paired_pipeline_does_not_force_retry_unresumable_source(self):
        from backend.paired_pipeline import run_paired_pipeline

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "1234abcd"
            target = root / "5678abcd"
            source.mkdir()
            target.mkdir()
            script = {"language": "my-MM", "segments": [{"id": "S1", "text": "စာသား"}]}
            for job_dir, provider in ((source, "kaggle"), (target, "gemini")):
                initialize_job_status(job_dir, job_dir.name, provider)
                (job_dir / "script.json").write_text(json.dumps(script, ensure_ascii=False))
            update_job_status(source, {
                "status": "failed", "restart_resumable": False,
            })

            with patch("backend.paired_pipeline.run_pipeline", new_callable=AsyncMock) as run:
                asyncio.run(run_paired_pipeline(
                    source.name, target.name, script, root,
                    source_provider="kaggle", target_provider="gemini",
                ))

            run.assert_not_awaited()
            status = read_job_status(target)
            self.assertEqual(status["status"], "failed")
            self.assertFalse(status["restart_resumable"])


if __name__ == "__main__":
    unittest.main()
