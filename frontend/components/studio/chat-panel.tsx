"use client";

import type { ProjectStudioController } from "../../lib/project-state";

type ChatPanelProps = {
  studio: ProjectStudioController;
};

// The persistent Creative Director chat (left pane). Chat is a COMMAND EMITTER:
// it never writes project state directly. Each note plus the current canvas
// selection is mapped server-side to ONE ProjectCommand and applied through the
// same seam the canvas uses, so both panes edit the SAME canonical version. The
// transcript below is NOT the decision record - versions / events / locks are.
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
            {studio.pending ? "Applying…" : "Send to canvas"}
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
          Chat maps your note plus the current selection to one command. It never edits a
          private copy and never moves a preference to another project unless you press
          Save to Brand.
        </p>
      </div>
    </section>
  );
}
