"use client";

import type { ProjectStudioController } from "../../../lib/project-state";
import type { SelectionMode } from "../../../lib/project-state";

type SceneControlsProps = {
  studio: ProjectStudioController;
};

const MODES: Array<{ id: SelectionMode; label: string; testId: string }> = [
  { id: "scene", label: "Scene", testId: "selection-mode-scene" },
  { id: "object", label: "Object", testId: "selection-mode-object" },
  { id: "time_range", label: "Time range", testId: "selection-mode-time-range" },
  { id: "all", label: "All scenes", testId: "selection-mode-all" },
];

// Scene controls (right pane). Selection is expressible as scene / object /
// time-range / all and is carried verbatim into every command scope+selection so
// chat understands exactly what the operator has selected. Canvas edits build a
// ProjectCommand against the canonical head and apply it through the SAME seam
// chat uses (POST /api/projects/{id}/commands).
export default function SceneControls({ studio }: SceneControlsProps) {
  const ready = studio.status === "ready";
  const disabled = studio.pending || !ready;
  const sceneLocked = studio.locks?.locked.includes("content") ?? false;
  const visualLocked = studio.locks?.locked.includes("visual") ?? false;

  return (
    <section className="workspace-panel scene-controls" aria-labelledby="scene-controls-title">
      <div className="section-heading">
        <div>
          <p className="eyebrow">Edit the canonical version</p>
          <h2 id="scene-controls-title">Scene controls</h2>
        </div>
      </div>

      <div
        className="selection-modes"
        role="radiogroup"
        aria-label="Selection mode"
        data-testid="selection-modes"
      >
        {MODES.map((mode) => (
          <button
            key={mode.id}
            type="button"
            role="radio"
            aria-checked={studio.selectionMode === mode.id}
            className={`studio-preset-pill${
              studio.selectionMode === mode.id ? " studio-preset-pill--active" : ""
            }`}
            data-testid={mode.testId}
            disabled={!ready}
            onClick={() => studio.setSelectionMode(mode.id)}
          >
            {mode.label}
          </button>
        ))}
      </div>

      {studio.selectionMode === "object" ? (
        <div className="field-group">
          <label htmlFor="object-input" className="field-label">
            Object reference
          </label>
          <input
            id="object-input"
            data-testid="object-input"
            type="text"
            className="field-control field-control--input"
            value={studio.objectDraft}
            disabled={disabled}
            placeholder="e.g. ledger_chart"
            onChange={(event) => studio.setObjectDraft(event.target.value)}
          />
        </div>
      ) : null}

      {studio.selectionMode === "time_range" ? (
        <div className="field-group field-group--row">
          <div>
            <label htmlFor="time-range-start" className="field-label">
              Start (s)
            </label>
            <input
              id="time-range-start"
              data-testid="time-range-start"
              type="number"
              step="0.5"
              min="0"
              className="field-control field-control--input"
              value={studio.timeRangeStart}
              disabled={disabled}
              onChange={(event) => studio.setTimeDraft("start", event.target.value)}
            />
          </div>
          <div>
            <label htmlFor="time-range-end" className="field-label">
              End (s)
            </label>
            <input
              id="time-range-end"
              data-testid="time-range-end"
              type="number"
              step="0.5"
              min="0"
              className="field-control field-control--input"
              value={studio.timeRangeEnd}
              disabled={disabled}
              onChange={(event) => studio.setTimeDraft("end", event.target.value)}
            />
          </div>
        </div>
      ) : null}

      <div className="field-group">
        <label htmlFor="scene-text-input" className="field-label">
          Narration text {sceneLocked ? <span className="lock-chip">content locked</span> : null}
        </label>
        <input
          id="scene-text-input"
          data-testid="scene-text-input"
          type="text"
          className="field-control field-control--input"
          value={studio.sceneTextDraft}
          disabled={disabled}
          placeholder="Select a scene, then edit its narration"
          onChange={(event) => studio.setSceneDraft("text", event.target.value)}
        />
      </div>

      <div className="field-group">
        <label htmlFor="scene-visual-input" className="field-label">
          Visual action {visualLocked ? <span className="lock-chip">visual locked</span> : null}
        </label>
        <input
          id="scene-visual-input"
          data-testid="scene-visual-input"
          type="text"
          className="field-control field-control--input"
          value={studio.sceneVisualDraft}
          disabled={disabled}
          placeholder="Select a scene, then edit its visual action"
          onChange={(event) => studio.setSceneDraft("visual", event.target.value)}
        />
      </div>

      <div className="scene-controls__actions">
        <button
          type="button"
          className="btn btn--primary"
          data-testid="scene-apply"
          disabled={disabled || !studio.selectedSceneId}
          onClick={() => void studio.applySceneEdit("text")}
        >
          Apply narration to canvas
        </button>
        <button
          type="button"
          className="btn btn--ghost"
          data-testid="scene-apply-visual"
          disabled={disabled || !studio.selectedSceneId}
          onClick={() => void studio.applySceneEdit("visual")}
        >
          Apply visual to canvas
        </button>
      </div>
      <p className="helper-text" data-testid="selection-summary">
        Current selection: {studio.selectionDescription}
      </p>
    </section>
  );
}
