"""Google ADK Tools for FYF Video Producer."""

from __future__ import annotations

import logging
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

_repo_root = Path(__file__).resolve().parent.parent.parent
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))
_backend_root = _repo_root / "backend"
if str(_backend_root) not in sys.path:
    sys.path.insert(0, str(_backend_root))

from backend.video_director import apply_director_pass
from video_contract import StoryDraftScript, VideoScript

logger = logging.getLogger(__name__)


def _restore_storyboard_visual_variety(video_script: dict[str, Any]) -> dict[str, Any]:
    """Apply the storyboard-wide generated-scene guard after batch merging."""
    segments = video_script.get("segments", [])
    if len(segments) < 4:
        return video_script

    generated_count = sum(
        shot.get("media_type") in {"generated_image", "generated_video"}
        for segment in segments
        for shot in segment.get("visual", {}).get("evidence_shots", [])
    )
    if generated_count >= 2:
        return video_script

    for segment in segments:
        visual = segment.get("visual", {})
        claim_types = {
            claim.get("evidence_type")
            for claim in visual.get("evidence_claims", [])
        }
        if not claim_types or not claim_types.issubset({"concept", "relationship"}):
            continue
        for shot in visual.get("evidence_shots", []):
            if shot.get("media_type") != "motion_graphic":
                continue
            shot["media_type"] = "generated_image"
            shot["motion_spec"] = None
            generated_count += 1
            break
        if generated_count >= 2:
            break

    # Precision-first storyboards may legitimately need every primary shot to
    # remain a motion graphic (counts, comparisons, and sequences). Preserve
    # those evidence shots and add a generated cinematic companion instead of
    # failing the whole script job or weakening the precise visual proof.
    if generated_count < 2:
        for segment in segments:
            shots = segment.get("visual", {}).get("evidence_shots", [])
            if len(shots) >= 4:
                continue
            source = next(
                (shot for shot in shots if shot.get("media_type") == "motion_graphic"),
                None,
            )
            if source is None:
                continue
            companion = deepcopy(source)
            companion["shot_id"] = f"{source.get('shot_id', 'shot')}_cinematic"
            companion["media_type"] = "generated_image"
            companion["motion_spec"] = None
            companion["motion_preset"] = "slow_push"
            companion["transition"] = "crossfade"
            companion["caption"] = f"{source.get('caption', 'Scene')} · context"
            source_hold = float(source.get("hold_fraction", 1.0))
            source["hold_fraction"] = source_hold / 2
            companion["hold_fraction"] = source_hold / 2
            shots.append(companion)
            generated_count += 1
            if generated_count >= 2:
                break

    if generated_count < 2:
        raise ValueError(
            "Storyboard visual variety requires at least two generated story-scene shots; "
            "do not render every segment as cards or diagrams"
        )
    return video_script


def draft_story_segments(
    topic: str,
    duration_mode: str = "short",
    language: str = "my-MM",
    genre: str = "explainer",
    studio_name: str = "FYF Studio",
) -> dict[str, Any]:
    """Draft narration segments following hook-body-conclusion structure.

    Args:
        topic: Topic to write story narration for.
        duration_mode: Duration mode ("short" or "standard").
        language: Language code ("my-MM" or "en-US").
        genre: Cinematic genre (e.g. "explainer", "cinematic_documentary", "tech_explainer").
        studio_name: Name of the studio or channel brand.

    Returns:
        Validated StoryDraftScript as dictionary with title and segments.
    """
    from writer_agent_vertex import generate_narration_script
    raw_draft = generate_narration_script(
        topic, duration_mode=duration_mode, language=language, genre=genre
    )
    raw_draft = {
        **raw_draft,
        "studio_name": studio_name,
        "language": language,
        "genre": genre,
    }
    validated = StoryDraftScript.model_validate(raw_draft)
    return validated.model_dump(mode="json")


def audit_story_quality(draft: dict[str, Any]) -> dict[str, Any]:
    """Audit story draft for character lengths, script compliance, and segment pacing.

    Args:
        draft: The draft script containing title and segments.

    Returns:
        Quality audit report with passed flag and issues list.
    """
    issues: list[str] = []
    segments = draft.get("segments", [])

    if not segments:
        return {"passed": False, "issues": ["No segments in draft"]}

    title = draft.get("title", "")
    if not title:
        issues.append("Missing title")

    total_chars = 0
    for idx, seg in enumerate(segments):
        text = seg.get("text", "").strip()
        if not text:
            issues.append(f"Segment {idx + 1} has empty text")
        total_chars += len(text)

    passed = len(issues) == 0
    return {
        "passed": passed,
        "segment_count": len(segments),
        "total_characters": total_chars,
        "issues": issues,
    }


def plan_visual_shots(
    title: str,
    segments: list[dict[str, Any]],
    genre: str = "explainer",
    presenter_mode: str = "on_screen",
    language: str = "my-MM",
    studio_name: str = "FYF Studio",
) -> dict[str, Any]:
    """Plan visual storyboard treatments, scene types, mascot actions, and director passes.

    Args:
        title: Story title.
        segments: List of approved narration segments.
        genre: Visual genre (e.g. "cinematic_documentary", "tech_explainer", "investigative", "narrative", "explainer").
        presenter_mode: "on_screen" or "voiceover_only".
        language: "my-MM" or "en-US".
        studio_name: Name of the studio or channel brand.

    Returns:
        Fully planned and locked VideoScript dictionary.
    """
    from writer_agent_vertex import lock_narration_in_batches
    from backend.video_styles import apply_video_style

    draft_input = {
        "title": title,
        "language": language,
        "studio_name": studio_name,
        "genre": genre,
        "presenter_mode": presenter_mode,
        "segments": [
            {**s, "id": s.get("id", f"s{i+1}"), "text": s.get("text", "")}
            for i, s in enumerate(segments)
        ],
    }
    validated_draft = StoryDraftScript.model_validate(draft_input).model_dump(mode="json")
    raw_lock = lock_narration_in_batches(validated_draft, batch_size=2)

    video_script = VideoScript.model_validate({
        "title": title,
        "language": language,
        "studio_name": studio_name,
        "genre": genre,
        "presenter_mode": presenter_mode,
        "segments": raw_lock.get("segments", []),
    }).model_dump(mode="json")

    # When presenter_mode is voiceover_only, disable on-screen mascot presence in shots
    if presenter_mode == "voiceover_only":
        for seg in video_script.get("segments", []):
            visual = seg.get("visual") or {}
            for shot in visual.get("evidence_shots", []):
                shot["mascot_presence"] = "none"

    video_script = VideoScript.model_validate(
        _restore_storyboard_visual_variety(video_script)
    ).model_dump(mode="json")

    # Apply cinematic genre styling
    video_script = apply_video_style(video_script, style_id=genre)

    directed_script = apply_director_pass(video_script)
    return directed_script


def propose_project_command(
    project_id: str,
    base_version: int,
    message: str,
    version: dict[str, Any],
    selection: dict[str, Any] | None = None,
    actor: str = "creative-director",
) -> dict[str, Any]:
    """Map ONE Creative-Director chat message + current selection to ONE command.

    Stage C-II (Task 9) chat->command tool. This tool NEVER writes project state
    and NEVER calls a provider: it delegates to the pure mapper
    ``backend.projects.chat.map_message_to_command``, which emits a single
    ``ProjectCommand``. That command is applied elsewhere through
    ``backend.projects.commands.apply_command`` (the one mutating seam), so chat
    stays a command emitter, not a state writer.

    Args:
        project_id: 8-hex project id whose canonical head version is edited.
        base_version: the head version number the command rebases from.
        message: the operator's natural-language edit instruction.
        version: the current head ProjectVersion (dict) the message edits.
        selection: optional canvas selection (scene / object / time_range / all).
        actor: attribution for the proposed command.

    Returns:
        ``{"operation", "summary", "affected_segment_ids", "command"}`` on success,
        or ``{"error", "reason"}`` when the message/selection/version cannot be
        mapped to one unambiguous command (chat never guesses silently).
    """
    from pydantic import TypeAdapter

    from backend.projects import chat
    from backend.projects.models import ProjectVersion, Selection

    try:
        head = ProjectVersion.model_validate(version)
    except Exception as exc:  # surfaced honestly, never swallowed
        return {"error": "invalid_version", "reason": str(exc)}
    try:
        sel = TypeAdapter(Selection).validate_python(selection) if selection else None
    except Exception as exc:
        return {"error": "invalid_selection", "reason": str(exc)}
    try:
        mapping = chat.map_message_to_command(
            project_id=project_id,
            base_version=base_version,
            message=message,
            version=head,
            selection=sel,
            actor=actor,
        )
    except chat.ChatMappingError as exc:
        return {"error": "chat_mapping_failed", "reason": str(exc)}
    return {
        "operation": mapping.operation,
        "summary": mapping.summary,
        "affected_segment_ids": mapping.affected_segment_ids,
        "command": mapping.command.model_dump(mode="json"),
    }
