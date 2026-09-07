"use client";

import { useParams } from "next/navigation";

import ChatPanel from "../../../components/studio/chat-panel";
import StoryboardPanel from "../../../components/studio/canvas/storyboard-panel";
import SceneControls from "../../../components/studio/canvas/scene-controls";
import InsightsPanel from "../../../components/studio/canvas/insights-panel";
import VersionHistory from "../../../components/studio/canvas/version-history";
import { useProjectStudio } from "../../../lib/project-state";

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
            <StoryboardPanel studio={studio} />
            <SceneControls studio={studio} />

            <section className="workspace-panel preview-card" aria-labelledby="studio-preview-title">
              <div className="section-heading">
                <div>
                  <p className="eyebrow">Render output</p>
                  <h2 id="studio-preview-title">Preview</h2>
                </div>
                <span className="status-badge">{studio.head.script.aspect_ratio ?? "9:16"}</span>
              </div>
              <div className="preview-placeholder" data-testid="studio-preview">
                <p>
                  No render has been dispatched. Stage C edits the script spine only; a playable
                  MP4 appears here after a paid render is approved and produced (Stage D).
                </p>
              </div>
            </section>

            <section className="workspace-panel brand-card" aria-labelledby="studio-brand-title">
              <div className="section-heading">
                <div>
                  <p className="eyebrow">Brand controls</p>
                  <h2 id="studio-brand-title">Brand Kit</h2>
                </div>
              </div>
              <dl className="brand-grid" data-testid="studio-brand-kit">
                <div>
                  <dt>Language</dt>
                  <dd data-testid="brand-language">{studio.head.script.language}</dd>
                </div>
                <div>
                  <dt>CTA text</dt>
                  <dd data-testid="brand-cta">{studio.head.script.cta_text || "—"}</dd>
                </div>
                <div>
                  <dt>Style</dt>
                  <dd data-testid="brand-style">{studio.head.script.style_applied || "—"}</dd>
                </div>
                <div>
                  <dt>Progress bar</dt>
                  <dd data-testid="brand-progress-bar">
                    {studio.head.script.retention_progress_bar === false ? "off" : "on"}
                  </dd>
                </div>
              </dl>
              <p className="helper-text">
                These preferences stay in this project unless you explicitly press Save to Brand
                in the chat pane.
              </p>
            </section>

            <InsightsPanel studio={studio} />
            <VersionHistory studio={studio} />
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
