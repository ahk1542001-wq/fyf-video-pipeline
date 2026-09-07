"use client";

import StudioHeader from "../components/studio-header";
import WorkflowStrip from "../components/studio/workflow-strip";
import BriefPanel from "../components/studio/brief-panel";
import PreviewPanel from "../components/studio/preview-panel";
import { useCreateStudio } from "../components/studio/use-create-studio";

// Composition root for the Create Studio workspace. All state, effects, and
// async job handling live in the useCreateStudio controller hook; the markup is
// split across the studio panel components. This file only wires them together
// so the rendered DOM, copy, and selectors are unchanged from the original.
export default function Home() {
  const studio = useCreateStudio();

  return (
    <div className="app-shell">
      <StudioHeader runtime={studio.runtime} runtimeSource={studio.runtimeSource} />

      <main className="create-main">
        <div className="page-intro">
          <div>
            <p className="eyebrow">Create workspace</p>
            <h1>Turn a draft into a finished video.</h1>
            <p className="page-intro__lede">Source the idea, approve the story, then render a production-ready FYF cut.</p>
          </div>
        </div>

        <WorkflowStrip stages={studio.workflowStages} />

        <div className="workspace-grid">
          <BriefPanel studio={studio} />
          <PreviewPanel studio={studio} />
        </div>
      </main>
    </div>
  );
}
