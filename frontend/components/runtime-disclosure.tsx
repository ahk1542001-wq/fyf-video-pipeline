"use client";

import { useId, useState } from "react";
import { voiceProviderLabel, type RuntimeInfo } from "../lib/video-ui";

type RuntimeDisclosureProps = {
  runtime: RuntimeInfo;
  runtimeSource: "api" | "fallback";
};

export default function RuntimeDisclosure({ runtime, runtimeSource }: RuntimeDisclosureProps) {
  const [open, setOpen] = useState(false);
  const disclosureId = useId();
  const titleId = `${disclosureId}-title`;
  const panelId = `${disclosureId}-panel`;
  const generationReady = runtimeSource === "api" && runtime.generation_available;
  const statusLabel = runtimeSource !== "api"
    ? "Runtime unavailable"
    : generationReady
      ? runtime.generation_access_required ? "Private generation" : "Vertex ready"
      : "Generation unavailable";

  return (
    <div className="runtime-disclosure">
      <button
        type="button"
        className="runtime-disclosure__trigger"
        aria-expanded={open}
        aria-controls={panelId}
        aria-labelledby={titleId}
        onClick={() => setOpen(current => !current)}
      >
        <span className={`runtime-disclosure__status${generationReady ? "" : " runtime-disclosure__status--inactive"}`} aria-hidden="true" />
        <span id={titleId}>{statusLabel}</span>
        <span className="runtime-disclosure__chevron" aria-hidden="true">{open ? "−" : "+"}</span>
      </button>

      {open && (
        <div id={panelId} className="runtime-disclosure__panel" role="region" aria-labelledby={titleId}>
          <p className="runtime-disclosure__source">
            {runtimeSource === "api" ? "Live data from the video backend." : "Using local fallback until the runtime API responds."}
          </p>
          <p className="runtime-disclosure__source">{runtime.generation_message}</p>
          <dl className="runtime-disclosure__details">
            <div>
              <dt>Primary model</dt>
              <dd>{runtime.script_model}</dd>
            </div>
            <div>
              <dt>Fallback model</dt>
              <dd>{runtime.fallback_model}</dd>
            </div>
            <div>
              <dt>Allowed voice modes</dt>
              <dd>{runtime.allowed_voice_providers.map(voiceProviderLabel).join(" · ")}</dd>
            </div>
          </dl>
        </div>
      )}
    </div>
  );
}
