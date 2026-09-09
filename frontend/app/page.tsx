"use client";

import { useState } from "react";
import { ArrowRight, CheckCircle2, MessageSquareText, Send } from "lucide-react";
import StudioHeader from "../components/studio-header";
import WorkflowStrip from "../components/studio/workflow-strip";
import BriefPanel from "../components/studio/brief-panel";
import PreviewPanel from "../components/studio/preview-panel";
import { useCreateStudio, type CreateStudioController } from "../components/studio/use-create-studio";

function directorStatus(studio: CreateStudioController) {
  if (studio.renderStatus === "completed") return "Your final cut passed the render pipeline. Review it on the canvas, then inspect the measured production audit.";
  if (studio.renderBusy) return studio.renderProgress || "The production pipeline is working through the approved story.";
  if (studio.scriptLocked) return "The narration is locked. You can generate here, or open the shared Studio for scene-level chat edits and versions.";
  if (studio.variants.length > 0) return "I built real story options. Compare them on the canvas, edit any scene, then approve the direction you want.";
  if (studio.writingStatus === "writing") return studio.scriptProgress;
  if (studio.error) return `I need your attention before we continue: ${studio.error}`;
  if (studio.topic.trim() && studio.submittedMessages.length === 0) return studio.sourceMode === "full_script"
    ? "Your researched script is ready. Add its title, then send it; I will preserve the narration and plan production."
    : "Your brief is ready. Press Build script to send it, or ask for alternatives in the message.";
  if (studio.topic.trim()) return studio.sourceMode === "full_script"
    ? "Your latest script is ready to send for visual planning."
    : "Your latest brief is ready. Press Build script to send it, or ask for alternatives in the message.";
  return "Tell me what the video needs to achieve. I’ll shape the brief here while the production canvas stays editable beside us.";
}

function currentStudioStep(studio: CreateStudioController) {
  if (studio.renderStatus === "completed") return "review";
  if (studio.renderBusy || studio.renderStatus === "failed") return "render";
  if (studio.scriptLocked) return "render";
  if (studio.writingStatus === "writing") return "story";
  if (studio.variants.length > 0) return "storyboard";
  if (studio.script) return "storyboard";
  return "brief";
}

function CreativeDirectorPanel({ studio }: { studio: CreateStudioController }) {
  const canWrite = studio.directorMessage.trim().length > 0
    && (studio.sourceMode === "brief" || studio.videoTitle.trim().length > 0)
    && studio.writingStatus !== "writing"
    && studio.generationReady;

  return (
    <aside className="director-pane" aria-labelledby="director-title">
      <div className="director-pane__header">
        <div className="director-avatar" aria-hidden="true"><MessageSquareText size={18} strokeWidth={1.8} /></div>
        <div>
          <p className="director-pane__kicker">Creative Director</p>
          <h2 id="director-title">Build with FYF</h2>
        </div>
        <span className={`director-presence${studio.generationReady ? " director-presence--ready" : ""}`}>
          {studio.generationReady ? "Ready" : "Offline"}
        </span>
      </div>

      <div className="director-thread" aria-live="polite">
        {studio.submittedMessages.length === 0 && (
          <article className="director-message director-message--assistant">
            <span className="director-message__role">FYF</span>
            <p>{directorStatus(studio)}</p>
          </article>
        )}
        {studio.submittedMessages.map((message, index) => (
          <article className="director-message director-message--user" key={`${message}-${index}`}>
            <span className="director-message__role">You</span>
            <p>{message}</p>
          </article>
        ))}
        {studio.submittedMessages.length > 0 && (
          <article className="director-message director-message--assistant">
            <span className="director-message__role">FYF</span>
            <p>{directorStatus(studio)}</p>
          </article>
        )}
        {studio.scriptLocked && (
          <article className="director-message director-message--system">
            <CheckCircle2 size={15} aria-hidden="true" />
            <p>Story lock is active. Render and later edits use this approved source.</p>
          </article>
        )}
      </div>

      <div className="director-next-step" aria-label="Current production step">
        <span>Current step</span>
        <strong>{studio.workflowStages.find((stage) => stage.id === currentStudioStep(studio))?.label || "Brief"}</strong>
      </div>

      <form className="director-composer" onSubmit={(event) => { event.preventDefault(); if (canWrite) void studio.submitDirectorMessage(studio.directorMessage); }}>
        <label htmlFor="director-brief" className="director-composer__label">
          {studio.sourceMode === "full_script" ? "Paste your finished narration" : "What should we make?"}
        </label>
        <textarea
          id="director-brief"
          value={studio.directorMessage}
          onChange={(event) => studio.setDirectorMessage(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && !event.shiftKey) {
              event.preventDefault();
              event.currentTarget.form?.requestSubmit();
            }
          }}
          placeholder={studio.sourceMode === "full_script"
            ? "Use a blank line between each scene…"
            : "Describe the audience, message, offer, and desired outcome…"}
          rows={4}
        />
        <div className="director-composer__actions">
          <button type="submit" className="director-action director-action--primary" disabled={!canWrite}>
            <Send size={15} aria-hidden="true" />
            {studio.writingStatus === "writing" ? "Building…" : studio.sourceMode === "full_script" ? "Plan video" : "Build script"}
          </button>
        </div>
        <p className="director-composer__hint">
          {studio.sourceMode === "full_script"
            ? "Your words stay unchanged; FYF plans scenes, visuals, voice, render, and QA."
            : "Need alternatives? Ask for “3 options” here and I’ll bring them into the story board."}
        </p>
      </form>

      {studio.scriptLocked && studio.script && (
        <div className="director-handoff">
          <p>Need conversational scene edits, variants, undo, and versions?</p>
          <button type="button" onClick={() => void studio.openSharedStudio()} disabled={studio.openingStudio}>
            {studio.openingStudio ? "Opening shared Studio…" : "Open shared Studio"}
            <ArrowRight size={15} aria-hidden="true" />
          </button>
        </div>
      )}
      <p className="director-disclaimer">AI can make mistakes. Approvals stay with you.</p>
    </aside>
  );
}

function CreationCanvas({ studio, initialStep }: { studio: CreateStudioController; initialStep: string }) {
  const [activeStep, setActiveStep] = useState(initialStep);
  const showingPreview = activeStep === "render" || activeStep === "review";
  const canvasLabel = activeStep === "brief"
    ? "Creative brief"
    : activeStep === "story"
      ? "Story direction"
      : activeStep === "storyboard"
        ? "Storyboard approval"
        : activeStep === "render"
          ? "Production render"
          : "Review and evidence";

  return (
    <section className="creation-canvas" aria-labelledby="creation-canvas-title">
      <div className="creation-canvas__topline">
        <div>
          <p className="eyebrow">Living production canvas</p>
          <h1 id="creation-canvas-title">Turn a draft into a finished video.</h1>
        </div>
        <p>Everything here remains directly editable.</p>
      </div>

      <WorkflowStrip stages={studio.workflowStages} activeStageId={activeStep} onStageSelect={setActiveStep} />

      <div className="creation-surface">
        <div className="creation-surface__label">
          <span>{canvasLabel}</span>
          <span>{showingPreview ? (studio.scriptLocked ? "Approved source" : "Not locked yet") : "Edit here or in chat"}</span>
        </div>
        {showingPreview
          ? <PreviewPanel studio={studio} />
          : <BriefPanel studio={studio} view={activeStep === "brief" ? "source" : "story"} />}
      </div>
    </section>
  );
}

// Composition root for the Create Studio workspace. All state, effects, and
// async job handling live in the useCreateStudio controller hook; the markup is
// split across the studio panel components. This file wires the conversational
// director to the editable production canvas.
export default function Home() {
  const studio = useCreateStudio();
  const derivedStep = currentStudioStep(studio);

  return (
    <div className="app-shell create-studio-shell">
      <StudioHeader runtime={studio.runtime} runtimeSource={studio.runtimeSource} />

      <main className="conversation-workspace">
        <CreativeDirectorPanel studio={studio} />

        <CreationCanvas key={derivedStep} studio={studio} initialStep={derivedStep} />
      </main>
    </div>
  );
}
