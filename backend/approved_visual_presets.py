"""Content-addressed visual presets explicitly approved by the owner."""

from __future__ import annotations

import hashlib
import json
from typing import Any


APPROVED_INVENTORY_V3_SIGNATURE = "262508a6053362d4d02986de98016030dfdfbf316ba63333effab6b4b2b6f495"

APPROVED_INVENTORY_V3 = {
    "v3SceneAssets": [
        ["fyf-v2/scene-a1.png", "fyf-v2/scene-a1-system.png"],
        ["fyf-v2/scene-a2.png", "v3-preview-overflow.png"],
        ["fyf-v2/scene-a3.png", "v3-full/A3-data-blindness.png"],
        ["fyf-v2/scene-a4.png", "v3-full/A4-human-approval.png"],
        ["fyf-v2/scene-a5.png"],
    ],
    "v3MascotSegments": [2, 4],
}


def visual_content_signature(render_input: dict[str, Any]) -> str:
    semantic = {
        "title": render_input.get("title"),
        "language": render_input.get("language"),
        "segments": [
            {
                "id": segment.get("id"),
                "text": segment.get("text"),
                "screen_text": (segment.get("visual") or {}).get("screen_text"),
            }
            for segment in render_input.get("segments", [])
            if isinstance(segment, dict)
        ],
    }
    payload = json.dumps(semantic, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def approved_visual_preset(render_input: dict[str, Any]) -> dict[str, Any] | None:
    if visual_content_signature(render_input) != APPROVED_INVENTORY_V3_SIGNATURE:
        return None
    return json.loads(json.dumps(APPROVED_INVENTORY_V3))
