"use client";

import type { ProjectStudioController } from "../../lib/project-state";

type ChatPanelProps = {
  studio: ProjectStudioController;
};

function formatProposalValue(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

function proposalDiffValue(root: Record<string, unknown>, field: string): unknown {
  if (Object.prototype.hasOwnProperty.call(root, field)) {
    return root[field];
  }
  return field.split(".").reduce<unknown>((current, part) => {
    if (!current || typeof current !== "object" || Array.isArray(current)) {
      return undefined;
    }
    const record = current as Record<string, unknown>;
    return Object.prototype.hasOwnProperty.call(record, part) ? record[part] : undefined;
  }, root);
}

// The persistent Creative Director chat (left pane). Chat is a proposal
// emitter: it never writes project state directly. Each note plus the current
// canvas selection is mapped server-side to ONE ProjectCommand, then waits for
// an explicit approval before the canonical version changes. The transcript is
// NOT the decision record - versions / events / locks are.
export default function ChatPanel({ studio }: ChatPanelProps) {
  const disabled = studio.pending || studio.status !== "ready";

  return (
    <section className="studio-chat" aria-labelledby="chat-title" data-testid="chat-panel">
      <div className="section-heading">
        <div>
          <p className="eyebrow">Persistent</p>
          <h2 id="chat-title">Creative Director</h2>
        </div>
        <span className="status-badge" data-testid="chat-head">
          {studio.head ? `v${studio.head.version_no}` : "—"}
        </span>
      </div>

      <div className="chat-context" data-testid="chat-context" aria-live="polite">
        <p className="chat-context__label">Shared context (same canonical version as the canvas)</p>
        <p className="chat-context__value" data-testid="chat-context-value">
          {studio.chatContextText}
        </p>
        <p className="chat-context__selection" data-testid="chat-context-selection">
          Selection: {studio.selectionDescription}
        </p>
      </div>

      <ol className="chat-transcript" data-testid="chat-transcript" aria-live="polite">
        {studio.chat.length === 0 ? (
          <li className="chat-empty" data-testid="chat-empty">
            No notes yet. Select a scene on the canvas, then tell the Director what to change.
          </li>
        ) : (
          studio.chat.map((message) => (
            <li
              key={message.id}
              className={`chat-message chat-message--${message.role}`}
              data-testid="chat-message"
              data-role={message.role}
            >
              <span className="chat-message__role">
                {message.role === "user" ? "You" : "Director"}
              </span>
              <span className="chat-message__text">{message.text}</span>
              {message.meta ? (
                <span className="chat-message__meta" data-testid="chat-message-meta">
                  {message.meta}
                </span>
              ) : null}
              {message.pending ? <span className="chat-message__pending">working…</span> : null}
            </li>
          ))
        )}
      </ol>

      <section className="chat-context" aria-labelledby="proposal-title" data-testid="proposal-review">
        <p className="chat-context__label" id="proposal-title">Proposed changes</p>
        {studio.proposals.length === 0 ? (
          <p className="chat-context__selection" data-testid="proposal-empty">
            Chat changes will appear here for review before they can change the canvas.
          </p>
        ) : (
          <div style={{ display: "grid", gap: "0.65rem" }}>
            {studio.proposals.map((proposal) => {
              const isProposed = proposal.status === "proposed";
              const isBusy = disabled && isProposed;
              return (
                <article
                  key={proposal.proposal_id}
                  className="chat-message chat-message--assistant"
                  data-testid="chat-proposal-card"
                  data-proposal-id={proposal.proposal_id}
                >
                  <span className="chat-message__role">Proposed change</span>
                  <strong>{proposal.diff.summary}</strong>
                  <span className="chat-message__meta" data-testid="proposal-status">
                    {proposal.status} · base v{proposal.base_version} · {proposal.command.operation}
                  </span>
                  <span className="chat-context__selection">
                    Affected scopes: {proposal.affected_scopes.join(", ") || "none"}
                  </span>
                  <div style={{ display: "grid", gap: "0.25rem" }} data-testid="proposal-affected-fields">
                    <span className="chat-context__selection">Affected fields</span>
                    {proposal.diff.affected_fields.map((field) => (
                      <div key={field} className="chat-context__selection">
                        <strong>{field}</strong>: {formatProposalValue(proposalDiffValue(proposal.diff.before, field))} → {formatProposalValue(proposalDiffValue(proposal.diff.after, field))}
                      </div>
                    ))}
                  </div>
                  {isProposed ? (
                    <div className="chat-composer__actions">
                      <button
                        type="button"
                        className="btn btn--primary"
                        data-testid="proposal-approve"
                        disabled={isBusy}
                        onClick={() => void studio.approveProposal(proposal.proposal_id)}
                      >
                        Approve
                      </button>
                      <button
                        type="button"
                        className="btn btn--ghost"
                        data-testid="proposal-reject"
                        disabled={isBusy}
                        onClick={() => void studio.rejectProposal(proposal.proposal_id)}
                      >
                        Reject
                      </button>
                    </div>
                  ) : (
                    <span className="chat-message__meta">
                      {proposal.status === "approved"
                        ? `Applied as v${proposal.target_version ?? "—"}`
                        : proposal.reason ?? `No project change was made (${proposal.status}).`}
                    </span>
                  )}
                </article>
              );
            })}
          </div>
        )}
      </section>

      <div className="chat-composer">
        <label htmlFor="chat-input" className="field-label">
          Note to the Director
        </label>
        <textarea
          id="chat-input"
          data-testid="chat-input"
          className="field-control chat-input"
          rows={3}
          value={studio.chatDraft}
          disabled={disabled}
          placeholder='e.g. rewrite the narration as "Your new line"'
          onChange={(event) => studio.setChatDraft(event.target.value)}
          onKeyDown={(event) => {
            if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
              event.preventDefault();
              void studio.sendChatMessage();
            }
          }}
        />
        <div className="chat-composer__actions">
          <button
            type="button"
            className="btn btn--primary"
            data-testid="chat-send"
            disabled={disabled || studio.chatDraft.trim().length === 0}
            onClick={() => void studio.sendChatMessage()}
          >
            {studio.pending ? "Preparing…" : "Propose change"}
          </button>
          <button
            type="button"
            className="btn btn--ghost"
            data-testid="save-to-brand"
            disabled={studio.status !== "ready"}
            onClick={studio.saveToBrand}
            title="Cross-project preferences travel only through this explicit action"
          >
            Save to Brand
          </button>
        </div>
        <p className="helper-text">
          Chat maps your note plus the current selection to one proposed command. Review
          the affected fields above and explicitly Approve or Reject it. It never edits a
          private copy and never moves a preference to another project unless you press
          Save to Brand.
        </p>
      </div>
    </section>
  );
}
