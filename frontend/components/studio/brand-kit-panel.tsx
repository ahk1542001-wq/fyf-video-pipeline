"use client";

import type { CreateStudioController } from "./use-create-studio";

type BrandKitPanelProps = {
  studio: CreateStudioController;
};

export default function BrandKitPanel({ studio }: BrandKitPanelProps) {
  return (
    <section
      className="brand-kit-card"
      aria-labelledby="essential-brand-kit-title"
      style={{
        marginTop: "1.25rem",
        padding: "1rem",
        background: "var(--surface-soft)",
        border: "1px solid var(--hairline)",
        borderRadius: "8px",
      }}
    >
      <div className="section-heading section-heading--compact">
        <div>
          <p className="eyebrow">Brand controls</p>
          <h3 id="essential-brand-kit-title">Essential Brand Kit</h3>
        </div>
      </div>
      <div className="field-group">
        <label htmlFor="cta-button-text" className="field-label">CTA button text</label>
        <input
          id="cta-button-text"
          type="text"
          className="field-control field-control--input"
          value={studio.ctaText}
          maxLength={80}
          onChange={(event) => studio.setCtaText(event.target.value)}
          placeholder="Learn More"
        />
      </div>
      <div className="brand-kit-toggles" style={{ display: "grid", gap: "0.5rem", marginTop: "0.75rem" }}>
        <label htmlFor="retention-progress-bar-toggle" style={{ display: "flex", alignItems: "center", gap: "0.5rem", cursor: "pointer" }}>
          <input
            id="retention-progress-bar-toggle"
            type="checkbox"
            checked={studio.retentionProgressBar}
            onChange={(event) => studio.setRetentionProgressBar(event.target.checked)}
          />
          <span className="field-label" style={{ margin: 0 }}>Retention Progress Bar</span>
        </label>
        <label htmlFor="animated-lower-thirds-toggle" style={{ display: "flex", alignItems: "center", gap: "0.5rem", cursor: "pointer" }}>
          <input
            id="animated-lower-thirds-toggle"
            type="checkbox"
            checked={studio.animatedLowerThirds}
            onChange={(event) => studio.setAnimatedLowerThirds(event.target.checked)}
          />
          <span className="field-label" style={{ margin: 0 }}>Animated Lower Thirds</span>
        </label>
      </div>
    </section>
  );
}
