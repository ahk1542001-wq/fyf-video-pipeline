"use client";

import type { CreateStudioController } from "./use-create-studio";

type PreviewPanelProps = {
  studio: CreateStudioController;
};

export default function PreviewPanel({ studio }: PreviewPanelProps) {
  return (
    <section className="workspace-panel preview-panel" aria-labelledby="preview-title">
      <div className="section-heading">
        <div>
          <p className="eyebrow">Render output</p>
          <h2 id="preview-title">Preview</h2>
        </div>
        <div className="preview-panel__actions" style={{ display: "flex", alignItems: "center", gap: "6px" }}>
          {studio.scriptLocked && studio.script && (
            <button
              type="button"
              className="button button--primary button--compact"
              onClick={() => void studio.openSharedStudio()}
              disabled={studio.openingStudio}
              data-testid="open-shared-studio"
            >
              {studio.openingStudio ? "Opening Studio…" : "Continue in Chat + Canvas Studio"}
            </button>
          )}
          <div className="aspect-ratio-selector" role="radiogroup" aria-label="Aspect Ratio Switcher" style={{ display: "flex", gap: "4px" }}>
            {(["9:16", "16:9", "1:1"] as const).map((ratio) => (
              <button
                key={ratio}
                type="button"
                role="radio"
                aria-checked={studio.aspectRatio === ratio}
                onClick={() => studio.setAspectRatio(ratio)}
                className={`studio-preset-pill ${studio.aspectRatio === ratio ? "studio-preset-pill--active" : ""}`}
                style={{ padding: "3px 8px", fontSize: "0.72rem" }}
                title={`Switch to ${ratio} format`}
              >
                {ratio === "9:16" ? "📱 9:16" : ratio === "16:9" ? "🖥️ 16:9" : "⬛ 1:1"}
              </button>
            ))}
          </div>
          {studio.videoUrl && <a href={studio.videoUrl} download className="text-action">Download MP4</a>}
          {studio.renderStatus === "completed" && <span className="status-badge">Ready</span>}
        </div>
      </div>

      {studio.script && (
        <div className="render-actions">
          <button
            type="button"
            onClick={studio.generateVideo}
            disabled={!studio.scriptLocked || !studio.scriptLockId || studio.renderBusy || !studio.generationReady}
            className="button button--accent"
          >
            {studio.renderStatus === "queued" ? "Job queued…"
              : studio.renderStatus === "visuals" ? "Creating visuals…"
                : studio.renderStatus === "voice" ? "Generating voice…"
                  : studio.renderStatus === "rendering" ? "Rendering video…"
                    : studio.renderStatus === "qa" ? "Checking output…"
                      : studio.scriptLocked ? "Generate locked video" : "Approve and lock before video"}
          </button>
          <p className="helper-text helper-text--center">Script, visual, voice, and render stages are checkpointed and restart-resumable.</p>
        </div>
      )}

      <div className="preview-window">
        <div className="preview-window__bar" aria-hidden="true">
          <span />
          <span />
          <span />
        </div>
        <div className="preview-window__stage" aria-live="polite" aria-atomic="true">
          {studio.videoUrl ? (
            <video controls playsInline className="preview-window__video" src={studio.videoUrl} aria-label="Rendered FYF video preview" />
          ) : studio.renderStatus === "queued" ? (
            <p className="preview-status preview-status--active">Queued… waiting for the available worker.</p>
          ) : studio.renderStatus === "visuals" ? (
            <div className="preview-status-group">
              <p className="preview-status preview-status--active">{studio.renderProgress || "Creating and verifying story visuals with Vertex…"}</p>
              {studio.visualProgress && (studio.visualProgress.fallbacks ?? 0) > 0 && <p className="preview-status__detail">Verified fallbacks: {studio.visualProgress.fallbacks}</p>}
            </div>
          ) : studio.renderStatus === "voice" ? (
            <p className="preview-status preview-status--active">{studio.renderProgress || "Synthesizing AI mascot voice…"}</p>
          ) : studio.renderStatus === "rendering" ? (
            <p className="preview-status preview-status--active">{studio.renderProgress || "Rendering video…"}</p>
          ) : studio.renderStatus === "qa" ? (
            <p className="preview-status preview-status--active">{studio.renderProgress || "Checking video, audio, narration, and mouth cues…"}</p>
          ) : studio.renderStatus === "failed" ? (
            <p className="preview-status">No playable video was produced. Review the message beside the source controls.</p>
          ) : (
            <p className="preview-empty">A playable MP4 appears here only after render and quality checks pass.</p>
          )}
        </div>
      </div>

      {studio.videoUrl && (
        <div
          id="clickhouse-production-audit"
          aria-live="polite"
          style={{
            marginTop: "0.75rem",
            padding: "0.75rem 1rem",
            background: "var(--surface-soft)",
            border: "1px solid var(--hairline)",
            borderRadius: "8px",
            display: "flex",
            flexDirection: "column",
            gap: "0.5rem",
          }}
        >
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", flexWrap: "wrap", gap: "6px" }}>
            <span style={{ fontSize: "0.75rem", fontWeight: 700, color: "var(--muted)", textTransform: "uppercase", letterSpacing: "0.05em" }}>
              📊 ClickHouse Production Audit
            </span>
            <span style={{ fontSize: "0.7rem", color: studio.auditCloudConfirmed ? "#16856B" : "var(--muted)", fontWeight: 600, background: studio.auditCloudConfirmed ? "#E6F4EA" : "var(--surface)", padding: "2px 6px", borderRadius: "4px" }}>
              {studio.auditCloudLabel}
            </span>
          </div>
          <div style={{ display: "flex", gap: "8px", flexWrap: "wrap", fontSize: "0.75rem" }}>
            <span style={{ padding: "4px 8px", background: "var(--surface)", border: "1px solid var(--hairline)", borderRadius: "4px" }}>
              ⚡ <strong>Render:</strong> {studio.auditRenderMs === null ? "Unavailable" : `${(studio.auditRenderMs / 1000).toFixed(1)}s`}
            </span>
            <span style={{ padding: "4px 8px", background: "var(--surface)", border: "1px solid var(--hairline)", borderRadius: "4px" }}>
              🪙 <strong>Tokens:</strong> {studio.auditTokens === null ? "Unavailable" : studio.auditTokens.toLocaleString()}
            </span>
            <span style={{ padding: "4px 8px", background: "var(--surface)", border: "1px solid var(--hairline)", borderRadius: "4px" }}>
              💵 <strong>Cost:</strong> {studio.auditCost === null ? "Unavailable" : `$${studio.auditCost.toFixed(4)}`}
            </span>
            <span style={{ padding: "4px 8px", background: "var(--surface)", border: "1px solid var(--hairline)", borderRadius: "4px" }}>
              🎬 <strong>Aspect:</strong> {studio.renderedAspectRatio}
            </span>
            <span style={{ padding: "4px 8px", background: "var(--surface)", border: "1px solid var(--hairline)", borderRadius: "4px" }}>
              🧾 <strong>Job:</strong> {studio.currentJobId || "Unavailable"}
            </span>
          </div>
          {studio.telemetryError && <p className="helper-text">{studio.telemetryError}</p>}
          <div style={{ display: "flex", justifyContent: "flex-end", marginTop: "2px" }}>
            <a
              href="/telemetry"
              style={{ fontSize: "0.75rem", color: "var(--focus)", fontWeight: 600, textDecoration: "none", display: "inline-flex", alignItems: "center", gap: "4px" }}
            >
              Ask Studio Data Officer (ClickHouse MCP) →
            </a>
          </div>
        </div>
      )}

      {studio.script && (
        <details className="technical-disclosure">
          <summary>{studio.scriptLocked ? "Approved narration locked · view technical JSON" : "View technical JSON"}</summary>
          <pre>{JSON.stringify(studio.script, null, 2)}</pre>
        </details>
      )}
    </section>
  );
}
