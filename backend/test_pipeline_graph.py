from __future__ import annotations

from backend.pipeline_graph import build_pipeline_graph, plan_recompute


def _script(second_text: str = "second") -> dict:
    return {
        "language": "en-US",
        "segments": [
            {"id": "s1", "text": "first", "visual": {"kind": "title"}},
            {"id": "s2", "text": second_text, "visual": {"kind": "chart"}},
        ],
    }


def test_single_track_voice_graph_depends_on_all_script_nodes():
    graph = build_pipeline_graph(_script(), per_segment_voice=False)
    expected = ("script:s1", "script:s2")
    assert graph.nodes["voice:s1"].depends_on == expected
    assert graph.nodes["voice:s2"].depends_on == expected


def test_single_track_text_edit_invalidates_all_voice_and_render_nodes():
    before = build_pipeline_graph(_script(), per_segment_voice=False)
    after = build_pipeline_graph(_script("changed"), per_segment_voice=False)
    plan = plan_recompute(before, after)

    assert "script:s1" in plan.cache_hits
    assert "script:s2" in plan.dirty
    assert {"voice:s1", "voice:s2", "render:s1", "render:s2"}.issubset(plan.dirty)


def test_per_segment_voice_edit_keeps_other_scene_downstream_cached():
    before = build_pipeline_graph(_script(), per_segment_voice=True)
    after = build_pipeline_graph(_script("changed"), per_segment_voice=True)
    plan = plan_recompute(before, after)

    assert "voice:s1" in plan.cache_hits
    assert "render:s1" in plan.cache_hits
    assert "voice:s2" in plan.dirty
    assert "render:s2" in plan.dirty
