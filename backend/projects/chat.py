"""C3 - Chat -> command mapping (Stage C-II).

The Creative Director chat is a **command emitter**, never a state writer. This
module maps one chat ``message`` plus the caller's current ``selection`` and the
canonical head ``ProjectVersion`` onto exactly ONE :class:`ProjectCommand`. It
performs **no I/O, no store access, and no provider call**: it imports neither
``backend.projects.store`` nor ``backend.projects.commands``. The emitted command
is applied elsewhere through ``apply_command`` (the single mutating entry point),
so chat can never write project state directly.

Contract consumed by Task 10 / Task 12
--------------------------------------
``map_message_to_command(*, project_id, base_version, message, version,
selection=None, actor=..., idempotency_key=None) -> ChatCommandMapping``

The mapping is deterministic and honest:

* **operation** is chosen from the message vocabulary:
    timing  -> ``edit_timing``   (retime / duration / seconds / trim / pace …)
    visual  -> ``edit_visual``   (visual / shot / b-roll / reframe / camera …)
    default -> ``edit_script``   (rewrite / narration / text / line / retitle …)
* **selection** supplied by the canvas (scene / object / time-range / all) is
  carried verbatim onto the command so chat edits exactly what the operator has
  selected, and the resolved segment ids are reported back for the UI.
* the **new value** is taken from a quoted substring (``"…"`` / ``“…”``) or the
  tail after `` to ``; timing values are parsed from the numbers in the message.
* when a message cannot be mapped to a single unambiguous command, a
  :class:`ChatMappingError` is raised — chat never guesses silently and never
  fabricates content.

The chat transcript is NOT the decision record: this module emits commands only.
Project decisions / approved assumptions / rejected directions / locks live in the
versioned spine and the granular lock registry, stored separately from the
transcript.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Optional

from pydantic import TypeAdapter

from video_contract import ScriptSegment

from backend.projects.models import (
    ProjectCommand,
    ProjectVersion,
    ScriptSegmentsPayload,
    Selection,
    SegmentTimingsPayload,
    SegmentTiming,
)
from backend.projects.selection import UnknownSelectionTargetError, resolve_selection

# Accept either a validated Selection model or a plain dict so the pure mapper is
# easy to consume from HTTP (pydantic-validated) and ADK-tool (dict) callers alike.
_SELECTION_ADAPTER = TypeAdapter(Selection)

__all__ = [
    "ChatMappingError",
    "ChatCommandMapping",
    "map_message_to_command",
    "classify_operation",
]


class ChatMappingError(ValueError):
    """A chat message could not be mapped to one unambiguous command."""


@dataclass(frozen=True)
class ChatCommandMapping:
    """The single command a chat message maps to, plus honest UI metadata."""

    command: ProjectCommand
    operation: str
    summary: str
    affected_segment_ids: list[str]


# Vocabulary -> operation. Order matters: timing/visual are checked before the
# broad content default so "retime the shot" stays a timing edit.
_TIMING_WORDS = (
    "retime", "timing", "duration", "seconds", "second", "trim", "lengthen",
    "shorten", "pace", "pacing", "hold", "beat",
)
_VISUAL_WORDS = (
    "visual", "shot", "b-roll", "broll", "reframe", "camera", "storyboard",
    "scene type", "image", "footage", "frame",
)
_CONTENT_WORDS = (
    "rewrite", "rename", "retitle", "narration", "text", "script", "copy",
    "line", "say", "wording", "voiceover", "vo",
)

_QUOTED_RE = re.compile(r"[\"\u201C\u2018]([^\"\u201D\u2019]+)[\"\u201D\u2019]")
_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")


def classify_operation(message: str) -> str:
    """Return the ``CommandOperation`` a message most likely intends."""
    lowered = message.casefold()
    if any(word in lowered for word in _TIMING_WORDS):
        return "edit_timing"
    if any(word in lowered for word in _VISUAL_WORDS):
        return "edit_visual"
    return "edit_script"


def _extract_new_text(message: str) -> Optional[str]:
    match = _QUOTED_RE.search(message)
    if match:
        candidate = match.group(1).strip()
        if candidate:
            return candidate
    # Fall back to the tail after an explicit " to " / " as " directive.
    for marker in (" to ", " as "):
        idx = message.casefold().rfind(marker)
        if idx != -1:
            tail = message[idx + len(marker):].strip().strip("\"'\u201C\u201D\u2018\u2019")
            if tail:
                return tail
    return None


def _segment_by_id(version: ProjectVersion, segment_id: str) -> ScriptSegment:
    for segment in version.script.segments:
        if segment.id == segment_id:
            return segment
    raise ChatMappingError(f"selected scene {segment_id!r} is not in version {version.version_no}")


def _updated_segments(
    version: ProjectVersion, selected_ids: list[str], field: str, value: str
) -> list[ScriptSegment]:
    segments: list[ScriptSegment] = []
    for sid in selected_ids:
        base = _segment_by_id(version, sid)
        data = base.model_dump(mode="json")
        data[field] = value
        segments.append(ScriptSegment.model_validate(data))
    return segments


def _build_timing(
    version: ProjectVersion, selected_ids: list[str], message: str
) -> list[SegmentTiming]:
    # Scene ids (e.g. "s1") contain digits that are NOT timing values; strip every
    # known segment id first so only real second-values remain.
    cleaned = message
    for segment in version.script.segments:
        cleaned = cleaned.replace(segment.id, " ")
    numbers = [float(n) for n in _NUMBER_RE.findall(cleaned)]
    if not numbers:
        raise ChatMappingError(
            "a timing edit needs at least one number of seconds "
            '(e.g. "retime scene s1 from 0 to 4 seconds")'
        )
    existing = {t.segment_id: t for t in version.segment_timings}
    timings: list[SegmentTiming] = []
    for sid in selected_ids:
        current = existing.get(sid)
        if len(numbers) >= 2:
            start, end = numbers[0], numbers[1]
        else:
            duration = numbers[0]
            start = current.start_seconds if current is not None else 0.0
            end = start + duration
        if end <= start:
            raise ChatMappingError(
                f"timing for {sid} must end after it starts (got {start}->{end})"
            )
        timings.append(
            SegmentTiming.model_validate(
                {"segment_id": sid, "start_seconds": start, "end_seconds": end}
            )
        )
    return timings


def map_message_to_command(
    *,
    project_id: str,
    base_version: int,
    message: str,
    version: ProjectVersion,
    selection: Optional[Selection] = None,
    actor: str = "creative-director",
    idempotency_key: Optional[str] = None,
) -> ChatCommandMapping:
    """Map ONE chat message + selection onto ONE :class:`ProjectCommand`.

    Raises :class:`ChatMappingError` when the message is blank, resolves to an
    empty selection, or cannot yield a single unambiguous, valid command. This
    function never touches a store and never mutates ``version``.
    """
    if not message or not message.strip():
        raise ChatMappingError("the chat message must not be blank")

    if isinstance(selection, dict):
        selection = _SELECTION_ADAPTER.validate_python(selection)

    try:
        selected_ids = resolve_selection(version, selection)
    except UnknownSelectionTargetError as exc:
        raise ChatMappingError(str(exc)) from exc
    if not selected_ids:
        raise ChatMappingError(
            "the current selection resolves to no scene, so there is nothing to edit"
        )

    operation = classify_operation(message)
    key = idempotency_key or f"chat-{uuid.uuid4().hex}"

    if operation == "edit_timing":
        timings = _build_timing(version, selected_ids, message)
        payload = SegmentTimingsPayload(timings=timings)
        summary = (
            f"Retimed {len(timings)} scene(s): "
            + ", ".join(f"{t.segment_id} [{t.start_seconds:g}-{t.end_seconds:g}s]" for t in timings)
        )
    else:
        new_text = _extract_new_text(message)
        if not new_text:
            raise ChatMappingError(
                "I could not find the new text. Quote it, e.g. "
                'rewrite scene s1 as "Your new narration line".'
            )
        field = "visual_action" if operation == "edit_visual" else "text"
        segments = _updated_segments(version, selected_ids, field, new_text)
        payload = ScriptSegmentsPayload(segments=segments)
        label = "visual action" if operation == "edit_visual" else "narration"
        summary = (
            f"Updated {label} for {len(segments)} scene(s): "
            + ", ".join(selected_ids)
            + f' -> "{new_text}"'
        )

    command = ProjectCommand(
        project_id=project_id,
        base_version=base_version,
        actor=actor,
        operation=operation,  # type: ignore[arg-type]
        selection=selection,
        payload=payload,
        idempotency_key=key,
    )
    return ChatCommandMapping(
        command=command,
        operation=operation,
        summary=summary,
        affected_segment_ids=selected_ids,
    )
