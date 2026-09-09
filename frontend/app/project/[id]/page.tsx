"use client";

import { useParams } from "next/navigation";
import { useEffect, useState } from "react";

import ChatPanel from "../../../components/studio/chat-panel";
import StoryboardPanel from "../../../components/studio/canvas/storyboard-panel";
import SceneControls from "../../../components/studio/canvas/scene-controls";
import InsightsPanel from "../../../components/studio/canvas/insights-panel";
import VersionHistory from "../../../components/studio/canvas/version-history";
import WorkflowStrip from "../../../components/studio/workflow-strip";
import { useProjectStudio } from "../../../lib/project-state";
import type { WorkflowStage, WorkflowStageId } from "../../../lib/video-ui";
import {
  createExports,
  listExportKinds,
  type ExportKind,
  type ExportResult,
} from "../../../lib/api";

import "../../styles/studio.css";

// Stage C-II Studio route: ONE canonical project version edited by TWO panes.
// Left = persistent Creative Director chat (a command emitter). Right =
// storyboard, scene controls, preview, Brand Kit and Insights/Audit. Neither
// pane holds a private authoritative copy: every change is a ProjectCommand
// applied through the backend, and the head is re-read afterwards.
export default function ProjectStudioPage() {
  const params = useParams<{ id: string }>();
  const rawId = params?.id;
  const projectId = Array.isArray(rawId) ? rawId[0] : (rawId ?? "");
  const studio = useProjectStudio(projectId);
  const [activeStage, setActiveStage] = useState<WorkflowStageId>("storyboard");
  const [budgetDraft, setBudgetDraft] = useState<string | null>(null);
  const [aspectRatio, setAspectRatio] = useState<"9:16" | "16:9" | "1:1">("9:16");
  const [reducedMotion, setReducedMotion] = useState(false);
  const [exportKinds, setExportKinds] = useState<ExportKind[]>([]);
  const [selectedExports, setSelectedExports] = useState<string[]>([]);
  const [exportResults, setExportResults] = useState<ExportResult[]>([]);
  const [exportBusy, setExportBusy] = useState(false);
  const [exportError, setExportError] = useState<string | null>(null);

  const resolvedBudgetDraft =
    budgetDraft ??
    (typeof studio.budget?.project_default_budget_usd === "number"
      ? studio.budget.project_default_budget_usd.toFixed(2)
      : "");
  const hasCurrentVideo = Boolean(studio.videoUrl);
  const studioStages: WorkflowStage[] = [
    { id: "brief", label: "Brief", state: "complete" },
    { id: "story", label: "Story", state: "complete" },
    { id: "storyboard", label: "Storyboard", state: "complete" },
    { id: "render", label: "Render", state: hasCurrentVideo ? "complete" : "current" },
    { id: "review", label: "Review", state: hasCurrentVideo ? "current" : "upcoming" },
  ];

  useEffect(() => {
    if (!studio.videoUrl) return;
    let active = true;
    void listExportKinds()
      .then((response) => {
        if (active) setExportKinds(response.exports);
      })
      .catch((error: unknown) => {
        if (active) {
          setExportError(error instanceof Error ? error.message : "Could not load export formats.");
        }
      });
    return () => {
      active = false;
    };
  }, [studio.videoUrl]);

  async function generateSelectedExports() {
    if (!studio.renderJobId || selectedExports.length === 0) return;
    setExportBusy(true);
    setExportError(null);
    try {
      const response = await createExports(studio.renderJobId, selectedExports);
      setExportResults(response.exports);
    } catch (error) {
      setExportError(error instanceof Error ? error.message : "Export generation failed.");
    } finally {
      setExportBusy(false);
    }
  }

  return (
    <main className="studio-route" data-testid="studio-route">
      <header className="studio-route__header">
        <div>
          <p className="eyebrow">Shared chat + canvas Studio</p>
          <h1 className="studio-route__title">Project {projectId || "unknown"}</h1>
        </div>
        <div className="studio-route__state">
          <span className="status-badge" data-testid="studio-status">
            {studio.status}
          </span>
          {studio.head ? (
            <span className="status-badge" data-testid="studio-head">
              head v{studio.head.version_no}
            </span>
          ) : null}
        </div>
      </header>

      {studio.notice ? (
        <div
          className={`studio-notice studio-notice--${studio.notice.kind}`}
          data-testid="studio-notice"
          data-kind={studio.notice.kind}
          role="status"
        >
          <span data-testid="studio-notice-message">{studio.notice.message}</span>
          <button
            type="button"
            className="btn btn--small btn--ghost"
            data-testid="studio-notice-dismiss"
            onClick={studio.dismissNotice}
          >
            Dismiss
          </button>
        </div>
      ) : null}

      {studio.status === "loading" ? (
        <p className="studio-loading" data-testid="studio-loading">
          Loading the canonical project version…
        </p>
      ) : null}

      {studio.status === "offline" ? (
        <div className="studio-offline" data-testid="studio-offline">
          <p>You appear to be offline. The last known version is read-only until you reconnect.</p>
          <button type="button" className="btn btn--primary" onClick={() => void studio.reload()}>
            Retry
          </button>
        </div>
      ) : null}

      {studio.status === "error" ? (
        <div className="studio-error" data-testid="studio-error">
          <p>This project could not be loaded.</p>
          <p className="helper-text" data-testid="studio-error-message">
            {studio.error}
          </p>
          <button type="button" className="btn btn--primary" onClick={() => void studio.reload()}>
            Try again
          </button>
        </div>
      ) : null}

      {studio.status === "ready" && studio.head ? (
        <div className="studio-panes">
          <div className="studio-pane studio-pane--chat">
            <ChatPanel studio={studio} />
          </div>

          <div className="studio-pane studio-pane--canvas">
            <div className="studio-canvas-header" data-testid="studio-workflow">
              <div>
                <p className="eyebrow">Living production canvas</p>
                <h2>{studioStages.find((stage) => stage.id === activeStage)?.label}</h2>
              </div>
              <p>Edit here or ask the Creative Director on the left.</p>
            </div>
            <WorkflowStrip
              stages={studioStages}
              activeStageId={activeStage}
              onStageSelect={(stageId) => setActiveStage(stageId as WorkflowStageId)}
            />

            {activeStage === "brief" ? (
              <section className="workspace-panel brand-card" aria-labelledby="studio-brand-title">
                <div className="section-heading">
                  <div>
                    <p className="eyebrow">Project foundation</p>
                    <h2 id="studio-brand-title">Brief &amp; Brand Kit</h2>
                  </div>
                </div>
                <dl className="brand-grid" data-testid="studio-brand-kit">
                  <div><dt>Project</dt><dd>{studio.head.script.title}</dd></div>
                  <div><dt>Language</dt><dd data-testid="brand-language">{studio.head.script.language}</dd></div>
                  <div><dt>CTA text</dt><dd data-testid="brand-cta">{studio.head.script.cta_text || "—"}</dd></div>
                  <div><dt>Style</dt><dd data-testid="brand-style">{studio.head.script.style_applied || "—"}</dd></div>
                  <div><dt>Progress bar</dt><dd data-testid="brand-progress-bar">{studio.head.script.retention_progress_bar === false ? "off" : "on"}</dd></div>
                </dl>
                <p className="helper-text">Use chat for broad changes or move to Storyboard for exact scene edits.</p>
              </section>
            ) : null}

            {activeStage === "story" ? <VersionHistory studio={studio} /> : null}

            {activeStage === "storyboard" ? (
              <>
                <StoryboardPanel studio={studio} />
                <SceneControls studio={studio} />
                <InsightsPanel studio={studio} mode="locks" />
              </>
            ) : null}

            {activeStage === "render" || activeStage === "review" ? (
            <section className="workspace-panel preview-card" aria-labelledby="studio-preview-title">
              <div className="section-heading">
                <div>
                  <p className="eyebrow">Render output</p>
                  <h2 id="studio-preview-title">Preview</h2>
                </div>
                <span className="status-badge">{studio.head.script.aspect_ratio ?? "9:16"}</span>
              </div>
              <div className="preview-placeholder" data-testid="studio-preview">
                {studio.videoUrl ? (
                  <>
                    <video
                      className={`studio-video studio-video--${aspectRatio.replace(":", "-")}`}
                      src={studio.videoUrl}
                      controls
                      playsInline
                      preload="metadata"
                      data-testid="studio-video"
                    />
                    <a className="btn btn--ghost" href={studio.videoUrl} download>
                      Download MP4
                    </a>
                    <p className="helper-text" data-testid="studio-preview-source">
                      Current head v{studio.renderDispatchedVersion ?? studio.head.version_no} ·
                      exact attached render {studio.renderJobId ? `(${studio.renderJobId})` : ""}
                    </p>
                  </>
                ) : (
                  <p>
                    Approve a budget ceiling to dispatch the real pipeline. Progress and failures
                    shown here come from the persisted job status, never a simulated percentage.
                  </p>
                )}
              </div>
              {activeStage === "render" ? <div className="render-controls" data-testid="studio-render-controls">
                <label className="render-control">
                  <span>Budget ceiling (USD)</span>
                  <input
                    className="field-control field-control--input"
                    inputMode="decimal"
                    value={resolvedBudgetDraft}
                    onChange={(event) => setBudgetDraft(event.target.value)}
                    data-testid="render-budget-input"
                  />
                </label>
                <label className="render-control">
                  <span>Aspect ratio</span>
                  <select
                    className="field-control field-control--select"
                    value={aspectRatio}
                    onChange={(event) =>
                      setAspectRatio(event.target.value as "9:16" | "16:9" | "1:1")
                    }
                    data-testid="render-aspect-select"
                  >
                    <option value="9:16">9:16 vertical</option>
                    <option value="16:9">16:9 landscape</option>
                    <option value="1:1">1:1 square</option>
                  </select>
                </label>
                <label className="render-check">
                  <input
                    type="checkbox"
                    checked={reducedMotion}
                    onChange={(event) => setReducedMotion(event.target.checked)}
                    data-testid="render-reduced-motion"
                  />
                  <span>Reduced motion</span>
                </label>
                <button
                  type="button"
                  className="btn btn--primary"
                  data-testid="render-approve"
                  disabled={
                    studio.pending ||
                    studio.budget?.paid_production_enabled !== true ||
                    (studio.renderStatus != null &&
                      !["completed", "failed", "cancelled", "needs_human_review"].includes(
                        studio.renderStatus,
                      ))
                  }
                  onClick={() =>
                    void studio.startRender({
                      approvedSpendUsd: Number(resolvedBudgetDraft),
                      aspectRatio,
                      reducedMotion,
                    })
                  }
                >
                  Approve budget &amp; render
                </button>
              </div> : null}
              <p className="helper-text" data-testid="render-status">
                {studio.renderStatus
                  ? `Actual job state: ${studio.renderStatus}`
                  : studio.budget?.paid_production_enabled
                    ? "Ready for explicit approval. Server estimate starts at $0.06."
                    : studio.budget?.reason || "Paid production availability is loading."}
              </p>
              {studio.videoUrl && activeStage === "review" ? (
                <div className="export-panel" data-testid="export-panel">
                  <div>
                    <p className="eyebrow">Selected only</p>
                    <h3>Exports</h3>
                  </div>
                  <div className="export-options">
                    {exportKinds.map((item) => (
                      <label className="export-option" key={item.kind}>
                        <input
                          type="checkbox"
                          checked={selectedExports.includes(item.kind)}
                          onChange={(event) =>
                            setSelectedExports((current) =>
                              event.target.checked
                                ? [...current, item.kind]
                                : current.filter((kind) => kind !== item.kind),
                            )
                          }
                        />
                        <span>
                          <strong>{item.kind}</strong>
                          <small>{item.description}</small>
                        </span>
                      </label>
                    ))}
                  </div>
                  <button
                    type="button"
                    className="btn btn--primary"
                    disabled={exportBusy || selectedExports.length === 0}
                    onClick={() => void generateSelectedExports()}
                    data-testid="export-generate"
                  >
                    {exportBusy ? "Generating selected exports…" : "Generate selected exports"}
                  </button>
                  {exportError ? <p className="export-error">{exportError}</p> : null}
                  {exportResults.length ? (
                    <ul className="export-results">
                      {exportResults.map((result) => (
                        <li key={result.kind}>
                          <a href={result.url} download>
                            {result.filename}
                          </a>
                          <span>{Math.ceil(result.bytes / 1024)} KB</span>
                        </li>
                      ))}
                    </ul>
                  ) : null}
                </div>
              ) : null}
            </section>
            ) : null}

            {activeStage === "review" ? (
              <section className="workspace-panel acceptance-card" aria-labelledby="studio-acceptance-title">
                <div className="section-heading">
                  <div><p className="eyebrow">Final gate</p><h2 id="studio-acceptance-title">Human review</h2></div>
                  <span className="status-badge">{studio.humanAcceptance?.readiness.status ?? "not ready"}</span>
                </div>
                <p className="helper-text">Automated QA and current media must pass before acceptance can unlock downloads.</p>
                <div className="review-actions">
                  <button type="button" className="btn btn--ghost" disabled={studio.pending} onClick={() => void studio.setHumanAcceptance(false)}>Request changes</button>
                  <button type="button" className="btn btn--primary" disabled={studio.pending || studio.humanAcceptance?.readiness.automated_qa_passed !== true} onClick={() => void studio.setHumanAcceptance(true, studio.humanAcceptance?.readiness.automated_qa ?? null)}>Accept final video</button>
                </div>
              </section>
            ) : null}

            {activeStage === "review" ? <InsightsPanel studio={studio} /> : null}
            {activeStage === "review" ? <VersionHistory studio={studio} /> : null}
          </div>
        </div>
      ) : null}

      {studio.status === "ready" && !studio.head ? (
        <p className="empty-state" data-testid="studio-empty">
          This project has no committed version yet.
        </p>
      ) : null}
    </main>
  );
}
