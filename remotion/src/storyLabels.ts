export const nonDuplicateStoryLabels = (
  screenText: string[],
  evidenceLabels: string[],
) => {
  const normalize = (value: string) => value.replace(/\s+/g, "").toLocaleLowerCase();
  const evidence = evidenceLabels.map(normalize);
  const filtered = screenText.filter((label) => {
    const normalized = normalize(label);
    return !evidence.some((item) => normalized.includes(item) || item.includes(normalized));
  });
  return filtered.length ? filtered : [""];
};
