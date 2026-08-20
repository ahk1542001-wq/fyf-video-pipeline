import json
import os
import tempfile
import unittest
from pathlib import Path

from backend.visual_artifact_store import (
    claim_artifact,
    fail_artifact,
    materialize_artifact,
    seal_artifact,
    visual_artifact_key,
)


SCRIPT = {
    "title": "Shared visuals",
    "language": "my-MM",
    "segments": [{"id": "s1", "text": "စမ်းသပ်ချက်"}],
}
KEY = "a" * 64


class VisualArtifactStoreTests(unittest.TestCase):
    def test_artifact_key_changes_with_script_policy_or_model_route(self):
        base = visual_artifact_key(SCRIPT, "policy-v1", {"director": "model-a"})
        changed_script = {**SCRIPT, "title": "Different"}
        self.assertNotEqual(
            base, visual_artifact_key(changed_script, "policy-v1", {"director": "model-a"})
        )
        self.assertNotEqual(
            base, visual_artifact_key(SCRIPT, "policy-v2", {"director": "model-a"})
        )
        self.assertNotEqual(
            base, visual_artifact_key(SCRIPT, "policy-v1", {"director": "model-b"})
        )
        self.assertRegex(base, r"^[0-9a-f]{64}$")

    def test_two_owners_elect_one_producer_and_one_waiter(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self.assertEqual(claim_artifact(root, KEY, "job-a"), "producer")
            self.assertEqual(claim_artifact(root, KEY, "job-b"), "waiting")
            self.assertEqual(claim_artifact(root, KEY, "job-a"), "producer")

    def test_approved_artifact_returns_hit(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self.assertEqual(claim_artifact(root, KEY, "job-a"), "producer")
            artifact = root / KEY
            (artifact / "script.json").write_text(json.dumps(SCRIPT))
            seal_artifact(root, KEY, "job-a", {
                "fingerprint_inputs": {"policy_version": "policy-v1"},
                "files": ["script.json"],
            })
            self.assertEqual(claim_artifact(root, KEY, "job-b"), "hit")

    def test_failed_or_corrupt_artifact_is_not_a_hit(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            artifact = root / KEY
            artifact.mkdir(parents=True)
            (artifact / "script.json").write_text(json.dumps(SCRIPT))
            (artifact / "manifest.json").write_text(json.dumps({
                "state": "approved",
                "files": [{"path": "script.json", "bytes": 999, "sha256": "0" * 64}],
            }))
            self.assertEqual(claim_artifact(root, KEY, "job-b"), "producer")

    def test_stale_lease_is_reclaimed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self.assertEqual(claim_artifact(root, KEY, "job-a"), "producer")
            lease = root / KEY / "producer.lease"
            os.utime(lease, (1, 1))
            self.assertEqual(
                claim_artifact(root, KEY, "job-b", stale_after_seconds=1), "producer"
            )

    def test_producer_failure_releases_lease_for_next_owner(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self.assertEqual(claim_artifact(root, KEY, "job-a"), "producer")
            fail_artifact(root, KEY, "job-a", "vertex_transient")
            self.assertFalse((root / KEY / "producer.lease").exists())
            failed = json.loads((root / KEY / "manifest.json").read_text())
            self.assertEqual(failed["state"], "failed")
            self.assertEqual(failed["failure_code"], "vertex_transient")
            self.assertEqual(claim_artifact(root, KEY, "job-b"), "producer")

    def test_materialize_copies_only_manifest_files_and_preserves_hashes(self):
        with tempfile.TemporaryDirectory() as temp_dir, tempfile.TemporaryDirectory() as job_temp:
            root = Path(temp_dir)
            job_dir = Path(job_temp)
            self.assertEqual(claim_artifact(root, KEY, "job-a"), "producer")
            artifact = root / KEY
            (artifact / "visuals").mkdir()
            (artifact / "script.json").write_text(json.dumps(SCRIPT))
            (artifact / "visuals" / "scene.png").write_bytes(b"verified-image")
            (artifact / "undeclared.txt").write_text("do not copy")
            seal_artifact(root, KEY, "job-a", {
                "fingerprint_inputs": {"policy_version": "policy-v1"},
                "files": ["script.json", "visuals/scene.png"],
            })

            manifest = materialize_artifact(root, KEY, job_dir)

            self.assertEqual(manifest["state"], "approved")
            self.assertEqual((job_dir / "visuals" / "scene.png").read_bytes(), b"verified-image")
            self.assertTrue((job_dir / "script.json").is_file())
            self.assertFalse((job_dir / "undeclared.txt").exists())

    def test_materialize_rejects_manifest_path_traversal(self):
        with tempfile.TemporaryDirectory() as temp_dir, tempfile.TemporaryDirectory() as job_temp:
            root = Path(temp_dir)
            artifact = root / KEY
            artifact.mkdir(parents=True)
            (artifact / "manifest.json").write_text(json.dumps({
                "state": "approved",
                "files": [{"path": "../secret", "bytes": 1, "sha256": "0" * 64}],
            }))
            with self.assertRaises(ValueError):
                materialize_artifact(root, KEY, Path(job_temp))


if __name__ == "__main__":
    unittest.main()
