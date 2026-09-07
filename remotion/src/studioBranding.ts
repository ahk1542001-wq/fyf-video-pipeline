export type StudioBranding = {
  name: string;
  tagline: string;
};

const DEFAULT_TAGLINE = "Understand AI. Build Real Systems.";

export function studioBranding(studioName?: string): StudioBranding {
  const name = studioName?.trim() || "FYF";
  const isLegacyName = name === "FYF" || name === "FYF Studio";
  return {
    name,
    tagline: isLegacyName ? DEFAULT_TAGLINE : `${name} • Agentic Cinema`,
  };
}
