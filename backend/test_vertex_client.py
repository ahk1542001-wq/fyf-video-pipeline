import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.vertex_client import vertex_client_kwargs


class TestVertexClient(unittest.TestCase):
    def test_express_mode_key_does_not_require_adc_project_fields(self):
        with patch.dict(os.environ, {"FYF_VERTEX_API_KEY": "secret-value"}, clear=True):
            kwargs = vertex_client_kwargs(location="global")
        self.assertEqual(kwargs, {"vertexai": True, "api_key": "secret-value"})

    def test_adc_mode_keeps_project_and_location(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            os.environ, {"GOOGLE_CLOUD_PROJECT": "project-a"}, clear=True
        ):
            kwargs = vertex_client_kwargs(
                location="global",
                credential_file=Path(temp_dir) / "missing-key.json",
            )
        self.assertEqual(kwargs, {"vertexai": True, "project": "project-a", "location": "global"})

    def test_local_ignored_env_file_can_supply_express_key(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            env_file = Path(temp_dir) / ".env"
            env_file.write_text("FYF_VERTEX_API_KEY=local-secret\n")
            with patch.dict(os.environ, {}, clear=True):
                kwargs = vertex_client_kwargs(location="global", env_file=env_file)
                os.environ.pop("FYF_VERTEX_API_KEY", None)
        self.assertEqual(kwargs, {"vertexai": True, "api_key": "local-secret"})

    def test_local_ignored_service_account_is_used_without_manual_export(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            credential_file = Path(temp_dir) / "gcp-key.json"
            credential_file.write_text("{}", encoding="utf-8")
            credentials = object()
            with patch.dict(os.environ, {}, clear=True), patch(
                "backend.vertex_client.service_account.Credentials.from_service_account_file",
                return_value=credentials,
            ) as load_credentials:
                kwargs = vertex_client_kwargs(
                    location="global",
                    credential_file=credential_file,
                )

        load_credentials.assert_called_once_with(
            str(credential_file),
            scopes=["https://www.googleapis.com/auth/cloud-platform"],
        )
        self.assertEqual(kwargs, {
            "vertexai": True,
            "location": "global",
            "credentials": credentials,
        })


if __name__ == "__main__":
    unittest.main()
