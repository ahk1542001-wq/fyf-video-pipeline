import type { WorkflowStage } from "../../lib/video-ui";

type WorkflowStripProps = {
  stages: WorkflowStage[];
  activeStageId?: string;
  onStageSelect?: (stageId: string) => void;
};

export default function WorkflowStrip({ stages, activeStageId, onStageSelect }: WorkflowStripProps) {
  const currentStage = stages.find(stage => stage.id === activeStageId)
    || stages.find(stage => stage.state === "current")
    || stages[stages.length - 1];
  const currentIndex = Math.max(0, stages.findIndex(stage => stage.id === currentStage?.id));

  return (
    <section className="workflow-strip-shell" aria-label="Create workflow progress">
      <ol className="workflow-strip" aria-label="Brief to review workflow">
        {stages.map((stage, index) => (
          <li
            key={stage.id}
            className={`workflow-stage workflow-stage--${activeStageId ? (stage.id === currentStage?.id ? "current" : stage.state === "complete" ? "complete" : "pending") : stage.state}`}
            aria-current={stage.id === currentStage?.id ? "step" : undefined}
          >
            {onStageSelect ? (
              <button
                type="button"
                className="workflow-stage__button"
                aria-pressed={currentStage?.id === stage.id}
                onClick={() => onStageSelect(stage.id)}
              >
                <span className="workflow-stage__dot" aria-hidden="true">
                  {stage.state === "complete" ? "✓" : index + 1}
                </span>
                <span className="workflow-stage__label">{stage.label}</span>
              </button>
            ) : (
              <>
                <span className="workflow-stage__dot" aria-hidden="true">
                  {stage.state === "complete" ? "✓" : index + 1}
                </span>
                <span className="workflow-stage__label">{stage.label}</span>
              </>
            )}
          </li>
        ))}
      </ol>
      <div className="workflow-strip__mobile" aria-live="polite">
        <span className="workflow-stage__dot" aria-hidden="true">
          {currentStage?.state === "complete" ? "✓" : currentIndex + 1}
        </span>
        <span>
          <strong>{currentStage?.label || "Brief"}</strong>
          <span className="workflow-strip__count">{currentIndex + 1} / {stages.length}</span>
        </span>
      </div>
    </section>
  );
}
