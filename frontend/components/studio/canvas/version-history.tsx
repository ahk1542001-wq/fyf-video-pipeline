"use client";

import type { ProjectStudioController } from "../../../lib/project-state";

type VersionHistoryProps = {
  studio: ProjectStudioController;
};

// Persistent version history (right pane). Undo APPENDS a new version restoring
// the target content - never a destructive rollback. Named variants are promoted
// to server-persisted versions (they survive reload). Before/after renders two
// immutable versions side by side.
export default function VersionHistory({ studio }: VersionHistoryProps) {
  const versions = studio.versions;
  const disabled = studio.pending || studio.status !== "ready";

  return (
    <section className="workspace-panel version-history" aria-labelledby="version-history-title">
      <div className="section-heading">
        <div>
          <p className="eyebrow">Immutable spine</p>
          <h2 id="version-history-title">Version history</h2>
        </div>
        <span className="status-badge" data-testid="version-count">
          {versions.length}
        </span>
      </div>

      <ol className="version-list" data-testid="version-history">
        {versions.length === 0 ? (
          <li className="empty-state" data-testid="version-empty">
            No versions yet.
          </li>
        ) : (
          [...versions].reverse().map((version) => (
            <li
              key={version.version_no}
              className="version-row"
              data-testid={`version-${version.version_no}`}
            >
              <div className="version-row__main">
                <span className="version-row__no">v{version.version_no}</span>
                {version.variant_name ? (
                  <span className="variant-chip" data-testid={`variant-name-${version.version_no}`}>
                    {version.variant_name}
                  </span>
                ) : null}
                <span className="version-row__actor">{version.actor}</span>
                <span className="version-row__ops">
                  {version.applied_operations.join(", ") || version.source_command_operation || "—"}
                </span>
              </div>
              <button
                type="button"
                className="btn btn--small"
                data-testid={`undo-${version.version_no}`}
                disabled={disabled}
                onClick={() => void studio.undo(version.version_no)}
                title={`Append a new version restoring the content of v${version.version_no}`}
              >
                Undo to v{version.version_no}
              </button>
            </li>
          ))
        )}
      </ol>

      <div className="variant-composer">
        <label htmlFor="variant-name-input" className="field-label">
          Name a variant (persisted as a version)
        </label>
        <div className="variant-composer__row">
          <input
            id="variant-name-input"
            data-testid="variant-name-input"
            type="text"
            maxLength={80}
            className="field-control field-control--input"
            value={studio.variantDraft}
            disabled={disabled}
            placeholder="e.g. Golden Cut"
            onChange={(event) => studio.setVariantDraft(event.target.value)}
          />
          <button
            type="button"
            className="btn btn--primary"
            data-testid="variant-create"
            disabled={disabled || studio.variantDraft.trim().length === 0}
            onClick={() => void studio.saveVariant()}
          >
            Save variant
          </button>
        </div>
      </div>

      <div className="compare-composer" data-testid="compare-composer">
        <p className="compare-composer__label">Before / after comparison</p>
        <div className="compare-composer__row">
          <label htmlFor="compare-before-select" className="field-label">
            Before
          </label>
          <select
            id="compare-before-select"
            data-testid="compare-before-select"
            className="field-control field-control--select"
            value={studio.compareBefore ?? ""}
            disabled={disabled}
            onChange={(event) =>
              studio.setCompare(
                "before",
                event.target.value === "" ? null : Number(event.target.value),
              )
            }
          >
            <option value="">Select…</option>
            {versions.map((version) => (
              <option key={version.version_no} value={version.version_no}>
                v{version.version_no}
                {version.variant_name ? ` · ${version.variant_name}` : ""}
              </option>
            ))}
          </select>

          <label htmlFor="compare-after-select" className="field-label">
            After
          </label>
          <select
            id="compare-after-select"
            data-testid="compare-after-select"
            className="field-control field-control--select"
            value={studio.compareAfter ?? ""}
            disabled={disabled}
            onChange={(event) =>
              studio.setCompare(
                "after",
                event.target.value === "" ? null : Number(event.target.value),
              )
            }
          >
            <option value="">Select…</option>
            {versions.map((version) => (
              <option key={version.version_no} value={version.version_no}>
                v{version.version_no}
                {version.variant_name ? ` · ${version.variant_name}` : ""}
              </option>
            ))}
          </select>

          <button
            type="button"
            className="btn btn--ghost"
            data-testid="compare-toggle"
            disabled={disabled}
            onClick={() => void studio.openComparison()}
          >
            Compare
          </button>
        </div>

        {studio.compareOpen ? (
          <div className="compare-panes" data-testid="compare-panes">
            <div className="compare-pane" data-testid="compare-before">
              <p className="compare-pane__head" data-testid="compare-before-version">
                {studio.compareBeforeVersion
                  ? `v${studio.compareBeforeVersion.version_no}`
                  : "Before: not loaded"}
              </p>
              {studio.compareBeforeVersion ? (
                <ul className="compare-pane__scenes">
                  {studio.compareBeforeVersion.script.segments.map((segment) => (
                    <li key={segment.id} data-testid={`before-${segment.id}`}>
                      <strong>{segment.id}</strong>: {segment.text}
                    </li>
                  ))}
                </ul>
              ) : null}
            </div>
            <div className="compare-pane" data-testid="compare-after">
              <p className="compare-pane__head" data-testid="compare-after-version">
                {studio.compareAfterVersion
                  ? `v${studio.compareAfterVersion.version_no}`
                  : "After: not loaded"}
              </p>
              {studio.compareAfterVersion ? (
                <ul className="compare-pane__scenes">
                  {studio.compareAfterVersion.script.segments.map((segment) => (
                    <li key={segment.id} data-testid={`after-${segment.id}`}>
                      <strong>{segment.id}</strong>: {segment.text}
                    </li>
                  ))}
                </ul>
              ) : null}
            </div>
          </div>
        ) : null}
      </div>
    </section>
  );
}
