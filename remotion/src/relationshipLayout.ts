const normalized = (value: string) => value.normalize("NFKC").toLocaleLowerCase().replace(/[\s\p{P}\p{S}]+/gu, "");

export const visibleMotionValues = (labels: string[], values: string[]) => {
  const labelTokens = new Set(labels.map(normalized));
  return values.map((value) => {
    if (labelTokens.has(normalized(value))) return "";
    const withoutAllowedAcronyms = value.replace(/\b(?:AI|XAI|FYF)\b/gi, "");
    return /[A-Za-z]/.test(withoutAllowedAcronyms) ? "" : value;
  });
};

export const relationshipPresentation = (spec: {
  labels: string[];
  values: string[];
  relation_mode?: "directional" | "bidirectional" | "non_replacement" | null;
}) => ({
  nodes: spec.labels,
  relations: visibleMotionValues(spec.labels, spec.values).filter(Boolean),
  connector: spec.relation_mode === "non_replacement"
    ? "↮"
    : spec.relation_mode === "bidirectional"
      ? "↔"
      : "→",
});
