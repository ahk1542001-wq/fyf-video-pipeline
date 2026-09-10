"use client";

import { voiceProviderLabel } from "../../lib/video-ui";
import { DEFAULT_PERSONAS, STUDIO_PRESETS } from "./studio-data";
import BrandKitPanel from "./brand-kit-panel";
import type { CreateStudioController } from "./use-create-studio";

type BriefPanelProps = {
  studio: CreateStudioController;
  view?: "source" | "story" | "all";
};

export default function BriefPanel({ studio, view = "all" }: BriefPanelProps) {
  return (
    <section className={`workspace-panel source-panel source-panel--${view}`} aria-labelledby="source-title">
      <div className="section-heading">
        <div>
          <p className="eyebrow">{view === "story" ? "Choose the direction" : "Start here"}</p>
          <h2 id="source-title">{view === "story" ? "Story board" : "Source and story"}</h2>
        </div>
        <span className="step-note" aria-label={view === "story" ? "Step 2" : "Step 1"}>{view === "story" ? "02" : "01"}</span>
      </div>

      {view === "story" && studio.writingStatus === "writing" && (
        <div className="canvas-planning-state" role="status" aria-live="polite">
          <span className="canvas-planning-state__pulse" aria-hidden="true" />
          <div>
            <p className="eyebrow">Building your storyboard</p>
            <h3>Planning scenes and visual evidence</h3>
            <p>{studio.scriptProgress}</p>
          </div>
          <div className="canvas-planning-state__skeleton" aria-hidden="true">
            <span />
            <span />
            <span />
            <span />
          </div>
        </div>
      )}

      {/* Studio Quick Preset Pills */}
      <div className="studio-presets source-only" role="group" aria-label="Quick Studio Presets" style={{ marginBottom: "1.25rem", padding: "0.75rem 1rem", background: "var(--surface-soft)", border: "1px solid var(--hairline)", borderRadius: "8px" }}>
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: "0.5rem" }}>
          <span style={{ fontSize: "0.75rem", fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.05em", color: "var(--muted)" }}>
            Quick Studio Presets
          </span>
          <span style={{ fontSize: "0.72rem", color: "var(--muted)" }}>
            Business studio presets
          </span>
        </div>
        <div className="studio-preset-group" style={{ display: "flex", gap: "6px", flexWrap: "wrap" }}>
          {STUDIO_PRESETS.map((p) => (
            <button
              key={p.id}
              type="button"
              aria-pressed={studio.activePreset === p.id}
              onClick={() => studio.applyPreset(p)}
              className={`studio-preset-pill ${studio.activePreset === p.id ? "studio-preset-pill--active" : ""}`}
              style={{ padding: "5px 12px", fontSize: "0.75rem" }}
            >
              {p.label}
            </button>
          ))}
        </div>
      </div>

      <div className="field-group source-only">
        <div className="source-mode-control" role="group" aria-label="Source type">
          <button
            type="button"
            aria-pressed={studio.sourceMode === "full_script"}
            className={`pill-btn ${studio.sourceMode === "full_script" ? "pill-btn--active" : ""}`}
            onClick={() => studio.setSourceMode("full_script")}
          >
            Full script
          </button>
          <button
            type="button"
            aria-pressed={studio.sourceMode === "brief"}
            className={`pill-btn ${studio.sourceMode === "brief" ? "pill-btn--active" : ""}`}
            onClick={() => studio.setSourceMode("brief")}
          >
            Brief
          </button>
        </div>
        {studio.sourceMode === "full_script" && (
          <label className="field-group" htmlFor="video-title">
            <span className="field-label">Video title</span>
            <input
              id="video-title"
              className="field-control field-control--input"
              value={studio.videoTitle}
              onChange={(event) => studio.setVideoTitle(event.target.value)}
              placeholder="Give this video a working title"
            />
          </label>
        )}
        <label htmlFor="topic-source" className="field-label">
          {studio.sourceMode === "full_script" ? "Full script" : "Topic or brief"}
        </label>
        <textarea
          id="topic-source"
          className="field-control field-control--textarea"
          placeholder={studio.sourceMode === "full_script"
            ? "Paste the final narration. Separate scenes with a blank line."
            : "Describe the video you want."}
          value={studio.topic}
          onChange={(event) => studio.setTopic(event.target.value)}
        />
        {studio.sourceMode === "full_script" && (
          <p className="field-help">FYF preserves your narration and only plans scenes, visuals, voice, and render.</p>
        )}
      </div>

      <details className="production-controls source-only">
        <summary>Production controls <span>Brand, voice, format, and presenter</span></summary>
        <div className="control-grid">
        <div className="field-group">
          <label htmlFor="studio-name" className="field-label">Studio / Channel Name</label>
          <input
            id="studio-name"
            type="text"
            className="field-control"
            value={studio.studioName}
            onChange={(e) => studio.setStudioName(e.target.value)}
            placeholder="FYF Studio"
          />
        </div>

        <div className="field-group">
          <label htmlFor="studio-language" className="field-label">Language</label>
          <select
            id="studio-language"
            className="field-control"
            value={studio.selectedLanguage}
            onChange={(e) => {
              const nextLang = e.target.value;
              studio.setSelectedLanguage(nextLang);
              if (nextLang === "en-US" && studio.studioName === "FYF Studio") {
                studio.setStudioName("FYF Agentic Business Studio");
              }
            }}
          >
            <option value="my-MM">Burmese (Regional Pilot)</option>
            <option value="en-US">English (Global Cinema)</option>
          </select>
        </div>

        <div className="field-group">
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "0.25rem" }}>
            <label htmlFor="voice-actor" className="field-label" style={{ marginBottom: 0 }}>Gemini Voice Actor</label>
            <button
              type="button"
              id="voice-audition-btn"
              onClick={studio.auditionVoice}
              className="pill-btn"
              style={{ padding: "2px 8px", fontSize: "0.72rem", cursor: "pointer" }}
              title="Audition 2-second voice sample"
            >
              {studio.auditioning ? "🔊 Playing…" : "🔊 Audition"}
            </button>
          </div>
          <select
            id="voice-actor"
            className="field-control"
            value={studio.selectedVoiceActor}
            onChange={(e) => studio.setSelectedVoiceActor(e.target.value)}
          >
            <option value="Sadaltager">Sadaltager (Deep Narrator)</option>
            <option value="Puck">Puck (Dynamic Creator)</option>
            <option value="Aoede">Aoede (Warm Storyteller)</option>
            <option value="Fenrir">Fenrir (Authoritative)</option>
          </select>
        </div>

        <div className="field-group">
          <label htmlFor="voice-provider" className="field-label">Voice provider</label>
          <select
            id="voice-provider"
            className="field-control"
            value={studio.effectiveVoiceProvider}
            disabled
          >
            <option value="gemini">{voiceProviderLabel("gemini")}</option>
          </select>
        </div>

        <div className="field-group">
          <label htmlFor="duration-mode" className="field-label">Duration</label>
          <select id="duration-mode" value={studio.durationMode} disabled className="field-control">
            <option value="short">Short · 30–60 sec (Public hackathon)</option>
          </select>
        </div>

        <div className="field-group">
          <label htmlFor="video-style" className="field-label">Visual style / Genre</label>
          <select
            id="video-style"
            value={studio.selectedStyle}
            onChange={(event) => studio.setSelectedStyle(event.target.value)}
            className="field-control"
          >
            {studio.availableStyles.map((style) => (
              <option key={style.id} value={style.id}>
                {style.name}
              </option>
            ))}
          </select>
        </div>

        <div className="field-group">
          <label htmlFor="presenter-persona" className="field-label">Persona</label>
          <select
            id="presenter-persona"
            value={studio.selectedPersona}
            onChange={(event) => studio.setSelectedPersona(event.target.value)}
            className="field-control"
          >
            {DEFAULT_PERSONAS.map((p) => (
              <option key={p.id} value={p.id}>{p.name}</option>
            ))}
          </select>
        </div>

        <div className="field-group">
          <label htmlFor="presenter-mode" className="field-label">Presenter Mode</label>
          <select
            id="presenter-mode"
            className="field-control"
            value={
              studio.presenterMode === "voiceover_only"
                ? "none"
                : (studio.selectedLanguage === "en-US" ? "alex" : "ko_kyaw")
            }
            onChange={(e) => {
              const val = e.target.value;
              if (val === "ko_kyaw") {
                studio.setPresenterMode("on_screen");
                studio.setSelectedLanguage("my-MM");
                studio.setIncludeMascot(true);
              } else if (val === "alex") {
                studio.setPresenterMode("on_screen");
                studio.setSelectedLanguage("en-US");
                studio.setIncludeMascot(true);
              } else {
                studio.setPresenterMode("voiceover_only");
                studio.setIncludeMascot(false);
              }
            }}
          >
            <option value="ko_kyaw">Ko Kyaw (On-Screen Host)</option>
            <option value="alex">Alex (Global Host)</option>
            <option value="none">No Presenter (Pure Cinematic B-Roll)</option>
          </select>
        </div>

        <div className="field-group">
          <label htmlFor="mascot-toggle" className="field-label">Presenter / Host</label>
          <button
            type="button"
            id="mascot-toggle"
            role="switch"
            aria-checked={studio.includeMascot}
            onClick={() => {
              const next = !studio.includeMascot;
              studio.setIncludeMascot(next);
              studio.setPresenterMode(next ? "on_screen" : "voiceover_only");
            }}
            className={`toggle-button ${studio.includeMascot ? "toggle-button--active" : ""}`}
          >
            <span className="toggle-button__indicator" />
            <span>
              {studio.includeMascot
                ? (studio.selectedLanguage === "en-US" ? "Alex (Global Host / On-screen)" : "Ko Kyaw (On-screen Host)")
                : "Voice only · No Presenter (Pure Cinematic B-Roll)"}
            </span>
          </button>
        </div>
        </div>

        <BrandKitPanel studio={studio} />
      </details>

      {!studio.generationReady && (
        <div className="notice-banner notice-banner--warning source-only" role="status">
          <p><strong>Generation unavailable:</strong> {studio.runtime.generation_message}</p>
          {studio.runtimeSource === "api" && studio.runtime.generation_access_required && (
            <label className="field-group">
              <span className="field-label">Private generation access</span>
              <input
                type="password"
                value={studio.generationAccessToken}
                onChange={(event) => studio.updateGenerationAccessToken(event.target.value)}
                className="field-control field-control--input"
                autoComplete="off"
                placeholder="Enter the operator access code"
              />
            </label>
          )}
        </div>
      )}

      <p className="action-hint source-only" role="note">
        {studio.sourceMode === "full_script"
          ? "Separate narration scenes with blank lines. FYF will not research or rewrite them."
          : "Describe the direction here, then submit it from the Creative Director."}
      </p>

      {studio.writingStatus === "writing" && (
        <div className="script-status-card source-only" role="status" aria-live="polite" aria-atomic="true">
          <span className="script-status-card__marker" aria-hidden="true" />
          <div>
            <p className="script-status-card__label">Script workspace</p>
            <p className="script-status-card__message">{studio.scriptProgress}</p>
          </div>
        </div>
      )}

      {studio.writingStatus === "needs_attention" && studio.resumableScriptJobId && (
        <div className="notice-banner notice-banner--warning source-only" role="alert">
          <p><strong>Generation Paused:</strong> Provider encountered a temporary rate limit or timeout. Checkpoint is safely preserved.</p>
          <button
            type="button"
            onClick={() => studio.resumeScriptJob(studio.resumableScriptJobId!)}
            className="button button--primary button--compact"
          >
            Retry from checkpoint
          </button>
        </div>
      )}

      {studio.variants.length > 0 && (
        <div className="story-section story-only">
          <div className="section-heading section-heading--compact">
            <div>
              <p className="eyebrow">Story</p>
              <h3>Compare and choose one</h3>
            </div>
            {studio.storyModel && <span className="section-meta">Vertex · {studio.storyModel}</span>}
          </div>
          <div className="story-options">
            {studio.variants.map((variant, index) => (
              <button
                type="button"
                key={variant.name}
                onClick={() => studio.setSelectedVariant(index)}
                aria-pressed={studio.selectedVariant === index}
                className={`story-option${studio.selectedVariant === index ? " story-option--selected" : ""}`}
              >
                <span className="story-option__name">{variant.name}</span>
                <span className="story-option__title">{variant.script.title}</span>
              </button>
            ))}
          </div>

          {studio.selectedVariant !== null && studio.variants[studio.selectedVariant] && (
            <div className="story-editor">
              <p className="story-editor__hint">
                Edit the {studio.variants[studio.selectedVariant].script.language === "en-US" ? "English" : "Burmese"} narration directly before approving. Changes stay locked for video generation.
              </p>
              <div className="segment-list">
                {studio.variants[studio.selectedVariant].script.segments.map((segment, index) => (
                  <div key={segment.id} className="segment-card">
                    <div className="segment-card__header">
                      <span className="segment-row__index">{index + 1}</span>
                      <div className="segment-tags">
                        <span className="segment-badge">{segment.scene_type}</span>
                        <span className="segment-badge segment-badge--mascot">Mascot: {segment.mascot_action}</span>
                        <span className="segment-badge segment-badge--emotion">{segment.emotion}</span>
                        <span className="segment-badge" style={{ background: "#EEF2FF", color: "#3730A3", borderColor: "#C7D2FE", fontWeight: 600 }}>
                          {index === 0 ? "🚁 Drone Aerial" : index === 1 ? "🔎 Macro Close-up" : index === 2 ? "🔍 Push-in" : "🎥 Tracking Shot"}
                        </span>
                      </div>
                    </div>
                    <div className="segment-card__body">
                      <label htmlFor={`segment-${segment.id}-narration`} className="field-label-sm">
                        {studio.variants[studio.selectedVariant!].script.language === "en-US" ? "English Narration" : "Burmese Narration"}
                      </label>
                      <input
                        id={`segment-${segment.id}-narration`}
                        type="text"
                        value={segment.text}
                        onChange={(event) => studio.updateSelectedNarration(index, event.target.value)}
                        className="field-control field-control--input"
                        placeholder="Narration text..."
                      />
                    </div>
                    <div className="segment-card__controls">
                      <div className="segment-control-group">
                        <span className="segment-control-label">Mascot Action:</span>
                        <div className="pill-group" role="group" aria-label={`Segment ${index + 1} mascot action`}>
                          {(["present", "explain", "think", "warn", "approve"] as const).map((act) => (
                            <button
                              key={act}
                              type="button"
                              onClick={() => studio.updateSegmentField(index, "mascot_action", act)}
                              className={`pill-btn ${segment.mascot_action === act ? "pill-btn--active" : ""}`}
                              aria-pressed={segment.mascot_action === act}
                            >
                              {act}
                            </button>
                          ))}
                        </div>
                      </div>
                      <div className="segment-control-group">
                        <span className="segment-control-label">Emotion:</span>
                        <div className="pill-group" role="group" aria-label={`Segment ${index + 1} emotion`}>
                          {(["neutral", "warm", "focused", "concerned", "confident"] as const).map((emo) => (
                            <button
                              key={emo}
                              type="button"
                              onClick={() => studio.updateSegmentField(index, "emotion", emo)}
                              className={`pill-btn ${segment.emotion === emo ? "pill-btn--active" : ""}`}
                              aria-pressed={segment.emotion === emo}
                            >
                              {emo}
                            </button>
                          ))}
                        </div>
                      </div>
                      <div className="segment-control-group">
                        <span className="segment-control-label">Scene:</span>
                        <div className="pill-group" role="group" aria-label={`Segment ${index + 1} scene type`}>
                          {(["whiteboard", "demo"] as const).map((st) => (
                            <button
                              key={st}
                              type="button"
                              onClick={() => studio.updateSegmentField(index, "scene_type", st)}
                              className={`pill-btn ${segment.scene_type === st ? "pill-btn--active" : ""}`}
                              aria-pressed={segment.scene_type === st}
                            >
                              {st}
                            </button>
                          ))}
                        </div>
                      </div>
                    </div>
                  </div>
                ))}
              </div>
              <p className="helper-text">The approved text is preserved exactly. Vertex adds visual metadata only.</p>
            </div>
          )}
          <button
            type="button"
            onClick={studio.approveAndLock}
            disabled={studio.selectedVariant === null || studio.writingStatus === "writing" || !studio.generationReady}
            className="button button--dark"
          >
            Approve selected story &amp; lock narration
          </button>
        </div>
      )}

      {studio.error && (
        <p className="error-message" role="alert" title={studio.error}>{studio.error}</p>
      )}
    </section>
  );
}
