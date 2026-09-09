"use client";

import type { ProjectStudioController } from "../../../lib/project-state";

type StoryboardPanelProps = {
  studio: ProjectStudioController;
};

// The storyboard (right pane). It renders the SAME canonical head version the
// chat edits - there is no private copy. Selecting a scene drives the shared
// selection carried into every command (canvas apply + chat send).
export default function StoryboardPanel({ studio }: StoryboardPanelProps) {
  const head = studio.head;
  const timings = new Map(
    (head?.segment_timings ?? []).map((timing) => [timing.segment_id, timing]),
  );

  return (
    <section className="workspace-panel storyboard-panel" aria-labelledby="storyboard-title">
      <div className="section-heading">
        <div>
          <p className="eyebrow">Canonical version</p>
          <h2 id="storyboard-title">Storyboard</h2>
        </div>
        <span className="status-badge" data-testid="head-version">
          {head ? `v${head.version_no}` : "—"}
        </span>
      </div>

      {head ? (
        <p className="storyboard-title" data-testid="project-title">
          {head.script.title}
          {head.variant_name ? <span className="variant-chip">{head.variant_name}</span> : null}
        </p>
      ) : null}

      {head && head.script.segments.length > 0 ? (
        <ol className="storyboard-grid" data-testid="storyboard">
          {head.script.segments.map((segment, index) => {
            const timing = timings.get(segment.id);
            const selected = studio.selectedSceneId === segment.id;
            return (
              <li key={segment.id} className="storyboard-scene" data-testid={`scene-${segment.id}`}>
                <button
                  type="button"
                  className={`scene-select${selected ? " scene-select--active" : ""}`}
                  data-testid={`scene-select-${segment.id}`}
                  aria-pressed={selected}
                  onClick={() => studio.selectScene(segment.id)}
                >
                  <span className="scene-select__index">Scene {index + 1}</span>
                  <span className="scene-select__id">{segment.id}</span>
                </button>
                <p className="scene-text" data-testid={`scene-text-${segment.id}`}>
                  {segment.text}
                </p>
                <p className="scene-visual" data-testid={`scene-visual-${segment.id}`}>
                  {segment.visual_action}
                </p>
                <p className="scene-caption" data-testid={`scene-caption-${segment.id}`}>
                  {segment.caption || "No caption"}
                </p>
                <p className="scene-voice" data-testid={`scene-voice-${segment.id}`}>
                  {segment.voice || "No voice direction"}
                </p>
                <p className="scene-meta">
                  <span>{segment.scene_type}</span>
                  <span>{segment.emotion}</span>
                  {segment.duration_seconds != null ? (
                    <span data-testid={`scene-duration-${segment.id}`}>
                      {segment.duration_seconds}s
                    </span>
                  ) : null}
                  {timing ? (
                    <span data-testid={`scene-timing-${segment.id}`}>
                      {timing.start_seconds}–{timing.end_seconds}s
                    </span>
                  ) : (
                    <span className="scene-meta__muted">no timing</span>
                  )}
                </p>
              </li>
            );
          })}
        </ol>
      ) : (
        <p className="empty-state" data-testid="storyboard-empty">
          This version has no scenes yet.
        </p>
      )}
    </section>
  );
}
