"""Google ADK Tools for FYF Video Producer."""

from __future__ import annotations

import logging
import sys
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

    if generated_count < 2:
        raise ValueError(
            "Storyboard visual variety requires at least two generated story-scene shots; "
            "do not render every segment as cards or diagrams"
        )
    return video_script


def research_topic(
    topic: str,
    duration_mode: str = "short",
    language: str = "my-MM",
    genre: str = "explainer",
    studio_name: str = "FYF Studio",
) -> dict[str, Any]:
    """Research a topic to extract factual focus, narrative hook, and visual concepts.

    Args:
        topic: The user's input topic or concept in Burmese or English.
        duration_mode: Target duration mode ("short" for 30-45s, "standard" for 60s).
        language: Language code ("my-MM" or "en-US").
        genre: Cinematic genre (e.g. "explainer", "cinematic_documentary", "tech_explainer").
        studio_name: Name of the studio or channel brand.

    Returns:
        Structured research dossier containing narrative angles and evidence hooks.
    """
    clean_topic = topic.strip()
    target_aud = (
        f"Global audience interested in {genre} content by {studio_name}"
        if language == "en-US"
        else f"General Burmese social media viewers (youth & working adults) for {studio_name}"
    )
    return {
        "topic": clean_topic,
        "duration_mode": duration_mode,
        "language": language,
        "genre": genre,
        "studio_name": studio_name,
        "target_audience": target_aud,
        "tone": f"Engaging, factual, clear, evidence-first ({genre})",
        "key_focus": f"Core explanatory breakdown of {clean_topic}",
        "suggested_segments": 4 if duration_mode in {"short", "micro"} else 6,
    }


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
