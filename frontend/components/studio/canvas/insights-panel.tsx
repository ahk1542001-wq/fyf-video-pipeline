"use client";

import type { ProjectStudioController } from "../../../lib/project-state";
import type { CommandScope } from "../../../lib/api";

type InsightsPanelProps = {
  studio: ProjectStudioController;
};

const SCOPES: Array<{ id: CommandScope; label: string; testId: string }> = [
  { id: "content", label: "Content (narration)", testId: "lock-toggle-content" },
  { id: "visual", label: "Visual (shot action)", testId: "lock-toggle-visual" },
  { id: "timing", label: "Timing (seconds)", testId: "lock-toggle-timing" },
];

// Insights / Audit (right pane). Honest progress is driven by
// WorkflowEvent.progress_source: an actual percentage is shown only when the
// source is "actual"; an estimate is labelled "Estimated" plus the current stage
// and never rendered as a number. Budget uses the fail-closed fields and shows
// unknown cost as "Unavailable", never 0.
export default function InsightsPanel({ studio }: InsightsPanelProps) {
  const events = studio.events;
  const latest = events.length > 0 ? events[events.length - 1] : null;
  const completed = events.filter((event) => event.status === "completed").length;
  const actualPct =
    events.length > 0 ? Math.round((completed / events.length) * 100) : null;
  const isActual = latest?.progress_source === "actual";

  const budget = studio.budget;
  const remaining = budget?.remaining_usd ?? null;

  return (
    <section className="workspace-panel insights-panel" aria-labelledby="insights-title">
      <div className="section-heading">
        <div>
          <p className="eyebrow">Audit</p>
          <h2 id="insights-title">Insights</h2>
        </div>
      </div>

      <div className="progress-region" data-testid="progress-region" aria-live="polite">
        <p className="progress-region__label">Workflow progress</p>
        {latest ? (
          isActual ? (
            <p className="progress-actual" data-testid="progress-actual">
              <strong>{actualPct}%</strong> actual · {latest.stage ?? latest.event_type} (
              {latest.status})
              <span className="progress-source" data-testid="progress-source">
                source: actual
              </span>
            </p>
          ) : (
            <p className="progress-estimated" data-testid="progress-estimated">
              <strong>Estimated</strong> · current stage: {latest.stage ?? latest.event_type}
              <span className="progress-source" data-testid="progress-source">
                source: estimated
              </span>
            </p>
          )
        ) : (
          <p className="empty-state" data-testid="progress-empty">
            No workflow events yet.
          </p>
        )}
      </div>

      <div className="budget-region" data-testid="budget-region">
        <p className="budget-region__label">Budget (fail-closed)</p>
        <p className="budget-remaining" data-testid="budget-remaining">
          Remaining:{" "}
          {remaining === null ? (
            <span className="budget-unavailable">Unavailable</span>
          ) : (
            `$${remaining.toFixed(2)}`
          )}
        </p>
        <p className="budget-status" data-testid="budget-status">
          Paid production: {budget ? (budget.paid_production_enabled ? "enabled" : "disabled") : "unknown"}
          {" · "}
          {budget?.budget_exceeded ? "budget exceeded" : "within budget"}
        </p>
        <p className="helper-text">
          Unknown cost is shown as Unavailable, never as $0. Estimated spend is
          {" "}
          {budget?.estimated_usd === null || budget?.estimated_usd === undefined
            ? "unavailable"
            : `$${budget.estimated_usd.toFixed(4)}`}
          .
        </p>
      </div>

      <div className="locks-region" data-testid="locks-region">
        <p className="locks-region__label">Granular locks</p>
        <div className="locks-toggles">
          {SCOPES.map((scope) => {
            const locked = studio.locks?.locked.includes(scope.id) ?? false;
            const reason = studio.locks?.scopes?.[scope.id]?.reason ?? null;
            return (
              <label key={scope.id} className="lock-toggle" htmlFor={scope.testId}>
                <input
                  id={scope.testId}
                  data-testid={scope.testId}
                  type="checkbox"
                  checked={locked}
                  disabled={studio.pending || studio.status !== "ready"}
                  onChange={(event) => void studio.toggleLock(scope.id, event.target.checked)}
                />
                <span className="field-label">{scope.label}</span>
                {locked && reason ? (
                  <span className="lock-reason" data-testid={`lock-reason-${scope.id}`}>
                    {reason}
                  </span>
                ) : null}
              </label>
            );
          })}
        </div>
        <p className="helper-text" data-testid="locked-scopes">
          Locked scopes: {studio.locks && studio.locks.locked.length > 0 ? studio.locks.locked.join(", ") : "none"}
        </p>
      </div>

      <details className="technical-disclosure">
        <summary>Workflow events ({events.length})</summary>
        <ol className="insights-events" data-testid="insights-events">
          {events.map((event) => (
            <li key={event.event_id} className="insight-event" data-testid="insight-event">
              <span className="insight-event__seq">#{event.sequence}</span>
              <span className="insight-event__type">{event.event_type}</span>
              <span className="insight-event__stage">{event.stage ?? "—"}</span>
              <span
                className="insight-event__source"
                data-testid="event-progress-source"
                data-source={event.progress_source}
              >
                {event.progress_source}
              </span>
            </li>
          ))}
        </ol>
      </details>
    </section>
  );
}
