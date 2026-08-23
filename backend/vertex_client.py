"""Shared authentication selection for Vertex AI production clients."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from google.oauth2 import service_account


def vertex_client_kwargs(
    *,
    location: str = "global",
    env_file: Path | None = None,
    credential_file: Path | None = None,
) -> dict[str, Any]:
    """Prefer a configured ADC file, then explicit and generic API keys."""
    load_dotenv(
        dotenv_path=env_file or Path(__file__).resolve().parents[1] / ".env",
        override=False,
    )
    configured_adc = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
    if configured_adc:
        configured_path = Path(configured_adc).expanduser()
        if not configured_path.is_absolute():
            configured_path = Path.cwd() / configured_path
        if configured_path.is_file():
            credentials = service_account.Credentials.from_service_account_file(
                str(configured_path),
                scopes=["https://www.googleapis.com/auth/cloud-platform"],
            )
            kwargs: dict[str, Any] = {
                "vertexai": True,
                "location": location,
                "credentials": credentials,
            }
            project = os.getenv("GOOGLE_CLOUD_PROJECT", "").strip() or getattr(credentials, "project_id", "")
            if project:
                kwargs["project"] = project
            return kwargs
    explicit_api_key = os.getenv("FYF_VERTEX_API_KEY")
    if explicit_api_key:
        return {"vertexai": True, "api_key": explicit_api_key}
    local_credentials = credential_file or Path(__file__).resolve().parents[1] / "gcp-key.json"
    if not configured_adc and local_credentials.is_file():
        credentials = service_account.Credentials.from_service_account_file(
            str(local_credentials),
            scopes=["https://www.googleapis.com/auth/cloud-platform"],
        )
        kwargs: dict[str, Any] = {
            "vertexai": True,
            "location": location,
            "credentials": credentials,
        }
        project = os.getenv("GOOGLE_CLOUD_PROJECT", "").strip() or getattr(credentials, "project_id", "")
        if project:
            kwargs["project"] = project
        return kwargs
    generic_api_key = os.getenv("GOOGLE_API_KEY")
    if not configured_adc and generic_api_key:
        return {"vertexai": True, "api_key": generic_api_key}
    kwargs = {
        "vertexai": True,
        "location": location,
    }
    project = os.getenv("GOOGLE_CLOUD_PROJECT", "").strip()
    if project:
        kwargs["project"] = project
    return kwargs
