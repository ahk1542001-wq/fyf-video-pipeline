"""B6 - Pure selection resolver (document line 19).

``resolve_selection`` maps a :class:`Selection` (scene / object / time-range /
all) onto the ordered list of segment ids it targets, using ONLY the data already
present in the supplied :class:`ProjectVersion`. It performs no I/O and holds no
state, so it is fully unit-testable.

Matching rules (deterministic):
* ``scene``      -> the given segment ids (all must exist), returned in script order.
* ``object``     -> segments whose visual focal object, emphasis, or visual_action
                    mentions one of the object refs (case-insensitive substring).
* ``time_range`` -> segments whose ``[start, end)`` timing overlaps
                    ``[start_seconds, end_seconds)``; segments without timing
                    never match a time range.
* ``all`` / None -> every segment id in script order.
"""

from __future__ import annotations

from typing import Optional

from video_contract import ScriptSegment

from backend.projects.models import (
    AllSelection,
    ObjectSelection,
    ProjectVersion,
    SceneSelection,
    Selection,
    TimeRangeSelection,
)

__all__ = ["resolve_selection", "UnknownSelectionTargetError"]


class UnknownSelectionTargetError(ValueError):
    """Raised when a selection names a target absent from the version."""


def _segment_mentions_object(segment: ScriptSegment, ref: str) -> bool:
    needle = ref.strip().casefold()
    if not needle:
        return False
    if needle in segment.visual_action.casefold():
        return True
    if any(needle in item.casefold() for item in segment.emphasis):
        return True
    visual = segment.visual
    if visual is not None:
        focal = getattr(visual, "focal_object", "") or ""
        if needle in focal.casefold():
            return True
    return False


def _resolve_scene(version: ProjectVersion, selection: SceneSelection) -> list[str]:
    ordered = version.segment_ids()
    known = set(ordered)
    missing = [sid for sid in selection.scene_ids if sid not in known]
    if missing:
        raise UnknownSelectionTargetError(
            f"scene selection references unknown segment id(s): {sorted(missing)}"
        )
    wanted = set(selection.scene_ids)
    return [sid for sid in ordered if sid in wanted]


def _resolve_object(version: ProjectVersion, selection: ObjectSelection) -> list[str]:
    matched: list[str] = []
    for segment in version.script.segments:
        if any(_segment_mentions_object(segment, ref) for ref in selection.object_refs):
            matched.append(segment.id)
    if not matched:
        raise UnknownSelectionTargetError(
            f"object selection matched no segment: {selection.object_refs}"
        )
    return matched


def _resolve_time_range(
    version: ProjectVersion, selection: TimeRangeSelection
) -> list[str]:
    if not version.segment_timings:
        return []
    timings = {t.segment_id: t for t in version.segment_timings}
    matched: list[str] = []
    for sid in version.segment_ids():
        timing = timings.get(sid)
        if timing is None:
            continue
        # Half-open overlap: [t.start, t.end) intersects [sel.start, sel.end).
        if timing.start_seconds < selection.end_seconds and timing.end_seconds > selection.start_seconds:
            matched.append(sid)
    return matched


def resolve_selection(
    version: ProjectVersion, selection: Optional[Selection]
) -> list[str]:
    """Return the ordered, de-duplicated segment ids targeted by ``selection``."""
    ordered = version.segment_ids()
    if selection is None or isinstance(selection, AllSelection):
        return list(ordered)
    if isinstance(selection, SceneSelection):
        return _resolve_scene(version, selection)
    if isinstance(selection, ObjectSelection):
        return _resolve_object(version, selection)
    if isinstance(selection, TimeRangeSelection):
        return _resolve_time_range(version, selection)
    raise UnknownSelectionTargetError(
        f"unsupported selection type: {type(selection).__name__}"
    )
