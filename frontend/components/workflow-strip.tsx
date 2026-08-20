import type { WorkflowStage } from "../lib/video-ui";

type WorkflowStripProps = {
  stages: WorkflowStage[];
};

export default function WorkflowStrip({ stages }: WorkflowStripProps) {
  const currentStage = stages.find(stage => stage.state === "current") || stages[stages.length - 1];
  const currentIndex = Math.max(0, stages.findIndex(stage => stage.id === currentStage?.id));

  return (
    <section className="workflow-strip-shell" aria-label="Create workflow progress">
      <ol className="workflow-strip" aria-label="Source to render workflow">
        {stages.map((stage, index) => (
          <li
            key={stage.id}
            className={`workflow-stage workflow-stage--${stage.state}`}
            aria-current={stage.state === "current" ? "step" : undefined}
          >
            <span className="workflow-stage__dot" aria-hidden="true">
              {stage.state === "complete" ? "✓" : index + 1}
            </span>
            <span className="workflow-stage__label">{stage.label}</span>
          </li>
        ))}
      </ol>
      <div className="workflow-strip__mobile" aria-live="polite">
        <span className="workflow-stage__dot" aria-hidden="true">
          {currentStage?.state === "complete" ? "✓" : currentIndex + 1}
        </span>
        <span>
          <strong>{currentStage?.label || "Source"}</strong>
          <span className="workflow-strip__count">{currentIndex + 1} / {stages.length}</span>
        </span>
      </div>
    </section>
  );
}
