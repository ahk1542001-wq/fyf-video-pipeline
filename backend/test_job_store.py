import unittest
import tempfile
import json
import os
import uuid
from pathlib import Path
from backend.job_store import (
    generate_job_id, is_valid_job_id, create_job_dir,
    write_json_atomically, read_job_status, initialize_job_status,
    update_job_status, begin_job_attempt, acquire_job_lease, release_job_lease
)

class TestJobStore(unittest.TestCase):
    def test_render_progress_persists_rejects_impossible_counts_and_resets_for_new_attempt(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            job_dir = Path(temp_dir) / "1234abcd"
            job_dir.mkdir()
            initialize_job_status(job_dir, job_dir.name, "gemini")
            progress = {
                "strategy": "segmented",
                "total": 3,
                "rendered": 1,
                "cache_hits": 2,
                "manifest_fingerprint": "a" * 64,
            }
            updated = update_job_status(job_dir, {"render_progress": progress})
            self.assertEqual(updated["render_progress"], progress)
            self.assertEqual(read_job_status(job_dir)["render_progress"], progress)

            invalid = [
                {**progress, "total": -1},
                {**progress, "rendered": -1},
                {**progress, "cache_hits": -1},
                {**progress, "rendered": 4},
                {**progress, "cache_hits": 4},
                {**progress, "rendered": 2, "cache_hits": 2},
                {**progress, "rendered": "1"},
            ]
            for candidate in invalid:
                with self.subTest(candidate=candidate), self.assertRaises(ValueError):
                    update_job_status(job_dir, {"render_progress": candidate})

            restarted = begin_job_attempt(job_dir)
            self.assertNotIn("render_progress", restarted)
            self.assertNotIn("render_progress", read_job_status(job_dir))

    def test_qa_progress_persists_rejects_impossible_counts_and_resets_for_new_attempt(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            job_dir = Path(temp_dir) / "1234abcd"
            job_dir.mkdir()
            initialize_job_status(job_dir, job_dir.name, "gemini")
            progress = {"total": 4, "verified": 2, "cache_hits": 1, "batches": 1}
            updated = update_job_status(job_dir, {"qa_progress": progress})
            self.assertEqual(updated["qa_progress"], progress)
            self.assertEqual(read_job_status(job_dir)["qa_progress"], progress)

            invalid = [
                {**progress, "total": -1},
                {**progress, "verified": -1},
                {**progress, "cache_hits": -1},
                {**progress, "batches": -1},
                {**progress, "verified": 5},
                {**progress, "cache_hits": 5},
                {**progress, "verified": "2"},
            ]
            for candidate in invalid:
                with self.subTest(candidate=candidate), self.assertRaises(ValueError):
                    update_job_status(job_dir, {"qa_progress": candidate})

            restarted = begin_job_attempt(job_dir)
            self.assertNotIn("qa_progress", restarted)
            self.assertNotIn("qa_progress", read_job_status(job_dir))

    def test_job_id_generation_and_validation(self):
        job_id = generate_job_id()
        self.assertTrue(is_valid_job_id(job_id))
        self.assertEqual(len(job_id), 8)

        self.assertFalse(is_valid_job_id("1234567"))
        self.assertFalse(is_valid_job_id("123456789"))
        self.assertFalse(is_valid_job_id("1234567G"))
        self.assertFalse(is_valid_job_id("1234567g"))
        self.assertFalse(is_valid_job_id("ABCDEFGH"))

    def test_create_job_dir(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            job_id = create_job_dir(temp_path)
            self.assertTrue(is_valid_job_id(job_id))
            job_dir = temp_path / job_id
            self.assertTrue(job_dir.exists())
            self.assertTrue(job_dir.is_dir())

    def test_write_json_atomically(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            file_path = Path(temp_dir) / "test.json"
            data = {"key": "value"}
            write_json_atomically(file_path, data)
            self.assertTrue(file_path.exists())
            with open(file_path, "r") as f:
                self.assertEqual(json.load(f), data)

            # Verify it works with overwriting
            data2 = {"key": "value2"}
            write_json_atomically(file_path, data2)
            with open(file_path, "r") as f:
                self.assertEqual(json.load(f), data2)

            # No leftover temps
            files = list(Path(temp_dir).iterdir())
            self.assertEqual(len(files), 1)

    def test_job_status_lifecycle(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            job_id = "1234abcd"
            job_dir = temp_path / job_id
            job_dir.mkdir()

            # Initialize
            status1 = initialize_job_status(job_dir, job_id)
            self.assertEqual(status1["status"], "queued")
            self.assertTrue(status1["restart_resumable"])
            self.assertIsNotNone(status1["created_at"])

            # Read
            status2 = read_job_status(job_dir)
            self.assertEqual(status1, status2)

            # Update valid
            status3 = update_job_status(job_dir, {"status": "voice"})
            self.assertEqual(status3["status"], "voice")
            self.assertTrue(status3["restart_resumable"])

            status4 = read_job_status(job_dir)
            self.assertEqual(status3, status4)

            status_qa = update_job_status(job_dir, {"status": "qa", "qa_report": {"passed": True}})
            self.assertEqual(status_qa["status"], "qa")
            self.assertTrue(status_qa["qa_report"]["passed"])

            status_creative_qa = update_job_status(job_dir, {"status": "creative_qa"})
            self.assertEqual(status_creative_qa["status"], "creative_qa")
            self.assertTrue(status_creative_qa["restart_resumable"])

            status_done = update_job_status(job_dir, {"status": "completed"})

            self.assertFalse(status_done["restart_resumable"])

            # Update invalid status
            with self.assertRaises(ValueError):
                update_job_status(job_dir, {"status": "invalid_status"})

    def test_job_status_preserves_paired_source_identity(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            job_dir = Path(temp_dir) / "1234abcd"
            job_dir.mkdir()
            initialize_job_status(job_dir, job_dir.name, "gemini")

            status = update_job_status(job_dir, {"paired_source_job_id": "5678abcd"})

            self.assertEqual(status["paired_source_job_id"], "5678abcd")

    def test_creative_qa_status_lifecycle(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            job_dir = Path(temp_dir) / "1234abcd"
            job_dir.mkdir()

            status = initialize_job_status(job_dir, "1234abcd")
            self.assertIsNone(status["creative_qa"])

            report = {"passed": True, "notes": ["ready"]}
            status = update_job_status(job_dir, {
                "status": "qa",
                "creative_qa": report,
            })
            self.assertEqual(status["creative_qa"], report)
            self.assertEqual(read_job_status(job_dir)["creative_qa"], report)

            status = update_job_status(job_dir, {
                "status": "needs_human_review",
                "video_url": None,
                "restart_resumable": False,
            })
            self.assertEqual(status["status"], "needs_human_review")
            self.assertEqual(status["creative_qa"], report)
            self.assertIsNone(status["video_url"])
            self.assertFalse(status["restart_resumable"])

    def test_begin_job_attempt_clears_current_creative_qa_but_preserves_history(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            job_dir = Path(temp_dir) / "1234abcd"
            job_dir.mkdir()
            initialize_job_status(job_dir, "1234abcd")
            current_report = {"passed": False, "notes": ["retry"]}
            history_report = {"passed": True, "notes": ["attempt 1"]}
            update_job_status(job_dir, {"creative_qa": current_report})
            history = job_dir / "creative_qa.attempt-1.json"
            write_json_atomically(history, history_report)

            status = begin_job_attempt(job_dir)

            self.assertIsNone(status["creative_qa"])
            self.assertTrue(history.exists())
            with open(history, "r", encoding="utf-8") as history_file:
                self.assertEqual(json.load(history_file), history_report)

    def test_read_job_status_errors(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            job_dir = temp_path / "1234abcd"
            job_dir.mkdir()

            # Not found
            with self.assertRaises(FileNotFoundError):
                read_job_status(job_dir)

            # Corrupt
            status_path = job_dir / "status.json"
            with open(status_path, "w") as f:
                f.write("invalid json")

            with self.assertRaises(ValueError):
                read_job_status(job_dir)

    def test_status_derives_visual_progress_and_preserves_final_qa_seal(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            job_dir = Path(temp_dir) / "1234abcd"
            job_dir.mkdir()
            initialize_job_status(job_dir, "1234abcd", "gemini")
            write_json_atomically(job_dir / "visual_evidence_checkpoint.json", {
                "script": {"segments": [{"visual": {"evidence_shots": [
                    {"verification_status": "passed", "fallback_used": True},
                    {"verification_status": "passed", "fallback_used": False},
                    {"verification_status": "planned", "fallback_used": False},
                ]}}]},
            })
            sealed = {"passed": True, "segments": []}
            update_job_status(job_dir, {"final_visual_qa": sealed})
            update_job_status(job_dir, {"final_visual_qa": None})
            status = read_job_status(job_dir)
            self.assertEqual(status["final_visual_qa"], sealed)
            self.assertEqual(status["visual_progress"], {
                "passed": 2, "total": 3, "fallbacks": 1, "percent": 67,
            })

    def test_status_exposes_dynamic_director_batch_and_cache_progress(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            job_dir = Path(temp_dir) / "1234abcd"
            job_dir.mkdir()
            initialize_job_status(job_dir, "1234abcd", "gemini")
            update_job_status(job_dir, {
                "visual_artifact_key": "a" * 64,
                "visual_cache_state": "producer",
            })
            write_json_atomically(job_dir / "director_treatment_checkpoint.json", {
                "total_shot_count": 27,
                "completed_shot_ids": [f"s{i}/shot-{i}" for i in range(1, 15)],
                "completed_batch_count": 3,
                "batch_size": 5,
                "complete": False,
                "retry_count": 1,
                "current_failed_ids": ["s15/shot-15"],
            })

            status = read_job_status(job_dir)

            self.assertEqual(status["visual_artifact_key"], "a" * 64)
            self.assertEqual(status["visual_cache_state"], "producer")
            self.assertEqual(status["visual_progress"], {
                "planned": 14,
                "total": 27,
                "completed_batches": 3,
                "total_batches": 6,
                "cache_state": "producer",
                "retry_count": 1,
                "current_failed_ids": ["s15/shot-15"],
            })

    def test_status_uses_shared_batch_progress_when_local_checkpoint_is_legacy(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            jobs_root = Path(temp_dir) / "jobs"
            job_dir = jobs_root / "1234abcd"
            job_dir.mkdir(parents=True)
            initialize_job_status(job_dir, "1234abcd", "gemini")
            artifact_key = "b" * 64
            update_job_status(job_dir, {
                "visual_artifact_key": artifact_key,
                "visual_cache_state": "producer",
            })
            write_json_atomically(job_dir / "director_treatment_checkpoint.json", {
                "input_fingerprint": "legacy",
                "script": {},
            })
            shared_dir = Path(temp_dir) / "visual-artifacts" / artifact_key
            shared_dir.mkdir(parents=True)
            write_json_atomically(shared_dir / "director_treatment_checkpoint.json", {
                "total_shot_count": 27,
                "completed_shot_ids": [f"s{i}/shot-{i}" for i in range(1, 11)],
                "completed_batch_count": 2,
                "batch_size": 5,
                "complete": False,
            })

            progress = read_job_status(job_dir)["visual_progress"]

            self.assertEqual(progress["planned"], 10)
            self.assertEqual(progress["total_batches"], 6)

    def test_status_switches_from_completed_director_to_evidence_progress(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            jobs_root = Path(temp_dir) / "jobs"
            job_dir = jobs_root / "1234abcd"
            job_dir.mkdir(parents=True)
            initialize_job_status(job_dir, "1234abcd", "gemini")
            artifact_key = "c" * 64
            update_job_status(job_dir, {"visual_artifact_key": artifact_key})
            shared_dir = Path(temp_dir) / "visual-artifacts" / artifact_key
            shared_dir.mkdir(parents=True)
            write_json_atomically(shared_dir / "director_treatment_checkpoint.json", {
                "total_shot_count": 2,
                "completed_shot_ids": ["s1/shot-1", "s2/shot-2"],
                "completed_batch_count": 1,
                "batch_size": 5,
                "complete": True,
            })
            write_json_atomically(shared_dir / "visual_evidence_checkpoint.json", {
                "script": {"segments": [
                    {"visual": {"evidence_shots": [
                        {"verification_status": "passed", "fallback_used": False},
                    ]}},
                    {"visual": {"evidence_shots": [
                        {"verification_status": "planned", "fallback_used": False},
                    ]}},
                ]},
            })

            progress = read_job_status(job_dir)["visual_progress"]

            self.assertEqual(progress, {
                "passed": 1,
                "total": 2,
                "fallbacks": 0,
                "percent": 50,
            })

    def test_begin_job_attempt_clears_stale_current_results_but_not_history_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            job_dir = Path(temp_dir) / "1234abcd"
            job_dir.mkdir()
            initialize_job_status(job_dir, "1234abcd", "gemini")
            sealed = {"passed": False, "segments": [{"segment_id": "S1"}]}
            update_job_status(job_dir, {
                "status": "failed",
                "video_url": "/api/jobs/1234abcd/video",
                "qa_report": {"passed": True},
                "final_visual_qa": sealed,
                "error": "old failure",
            })
            history = job_dir / "final_visual_qa.attempt-1.json"
            write_json_atomically(history, sealed)

            status = begin_job_attempt(job_dir)

            self.assertEqual(status["status"], "visuals")
            self.assertIsNone(status["video_url"])
            self.assertIsNone(status["qa_report"])
            self.assertIsNone(status["final_visual_qa"])
            self.assertIsNone(status["error"])
            self.assertTrue(history.exists())

    def test_job_lease_is_exclusive_and_releasable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            job_dir = Path(temp_dir) / "1234abcd"
            job_dir.mkdir()

            first = acquire_job_lease(job_dir)
            second = acquire_job_lease(job_dir)

            self.assertIsNotNone(first)
            self.assertIsNone(second)
            release_job_lease(job_dir, first)
            replacement = acquire_job_lease(job_dir)
            self.assertIsNotNone(replacement)
            release_job_lease(job_dir, replacement)

if __name__ == "__main__":
    unittest.main()
