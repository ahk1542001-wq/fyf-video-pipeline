import type { VisualTreatment, TreatmentType } from "./types";

export const CANONICAL_TREATMENTS: TreatmentType[] = [
  "story_scene",
  "object_action",
  "ui_proof",
  "editorial_data",
  "comparison_transform",
  "motion_diagram",
  "kinetic_type",
  "mascot_performance",
];

export type TreatmentRoute = {
  treatment: TreatmentType | null;
  grammar: string;
  [key: string]: unknown;
};

const GRAMMARS: Record<TreatmentType, string> = {
  story_scene: "cinematic-story",
  object_action: "observable-object-action",
  ui_proof: "interface-proof",
  editorial_data: "editorial-data-display",
  comparison_transform: "before-after-transform",
  motion_diagram: "motion-diagram",
  kinetic_type: "kinetic-typography",
  mascot_performance: "mascot-performance",
};

type TreatmentInput = {
  treatment?: VisualTreatment | TreatmentType | null;
  [key: string]: unknown;
};

type VisualGrammarInput = TreatmentInput & {
  media_type?: string | null;
  motion_spec?: {labels?: string[]; values?: string[]; layout?: string} | null;
};

export function comparisonTransformGroups(input: {
  labels: string[];
  values: string[];
  layout?: string;
}): {focalLabel: string | null; items: Array<{label: string; value?: string}>} {
  if (input.labels.length > 2) {
    return {
      focalLabel: input.labels[0] ?? null,
      items: input.labels.slice(1, 5).map((label, index) => ({
        label,
        value: input.values[index + 1],
      })),
    };
  }
  return {
    focalLabel: null,
    items: input.labels.slice(0, 2).map((label, index) => ({
      label,
      value: input.values[index],
    })),
  };
}

export function objectActionItems(labels: string[]): string[] {
  return [...labels];
}

export function shouldShowOverlayLabel(treatment?: VisualTreatment | TreatmentType | null): boolean {
  if (treatment == null) return true;
  if (typeof treatment === "string") return false;
  return treatment.text_mode === "caption";
}

export function visibleTreatmentLabels(
  _treatment: Pick<VisualTreatment, "focal_object" | "action" | "change" | "director_reason">,
  verifiedLabels: string[],
): string[] {
  return verifiedLabels;
}

export function treatmentEvidenceLabels(input: VisualGrammarInput): string[] {
  if (!input.treatment || typeof input.treatment === "string") {
    return input.motion_spec?.labels ?? [];
  }
  return visibleTreatmentLabels(input.treatment, input.motion_spec?.labels ?? []);
}

export function treatmentLabelFontSize(label: string, preferred: number): number {
  const length = Array.from(label.trim()).length;
  if (length > 42) return Math.min(preferred, 22);
  if (length > 30) return Math.min(preferred, 24);
  if (length > 20) return Math.min(preferred, 28);
  return preferred;
}

export function visibleDataValue(value: string | undefined): string {
  const normalized = (value ?? "").trim();
  return /^[0-9၀-၉]+(?:[.,][0-9၀-၉]+)?%?$/.test(normalized) ? normalized : "";
}

export function resolveTreatment(input: TreatmentInput): TreatmentRoute {
  const value = input.treatment;
  if (value == null) return { treatment: null, grammar: "legacy" };

  const treatment = typeof value === "string" ? value : value.treatment_type;
  if (!CANONICAL_TREATMENTS.includes(treatment as TreatmentType)) {
    throw new Error(`Unknown treatment: ${String(treatment)}`);
  }
  return { treatment: treatment as TreatmentType, grammar: GRAMMARS[treatment as TreatmentType] };
}

export function resolveVisualGrammar(input: VisualGrammarInput): string {
  const planned = resolveTreatment(input);
  if (planned.treatment != null) return planned.grammar;
  if (input.media_type === "motion_graphic" && input.motion_spec) return "motion-diagram";
  return "legacy";
}

export function selectActiveTreatment(
  shots: Array<{hold_fraction?: number; treatment?: VisualTreatment | TreatmentType | null}>,
  frame: number,
  durationInFrames: number,
): VisualTreatment | null {
  if (!shots.length) return null;
  const duration = Number.isFinite(durationInFrames) ? Math.max(1, durationInFrames) : 1;
  const boundedFrame = Number.isFinite(frame) ? Math.max(0, Math.min(frame, duration - 1)) : 0;
  const validShots = shots.map((shot) => ({
    shot,
    weight: Number.isFinite(shot.hold_fraction) ? Math.max(0, shot.hold_fraction ?? 0) : 0,
  })).filter(({shot}) => shot.treatment != null);
  const total = validShots.reduce((sum, item) => sum + item.weight, 0);
  if (total <= 0) return null;
  const position = boundedFrame / duration * total;
  let boundary = 0;
  for (const {shot, weight} of validShots) {
    boundary += weight;
    if (position < boundary) {
      const resolved = resolveTreatment(shot);
      return resolved.treatment == null ? null : shot.treatment && typeof shot.treatment !== "string" ? shot.treatment : null;
    }
  }
  const last = validShots[validShots.length - 1].shot.treatment;
  return last && typeof last !== "string" ? (resolveTreatment({treatment: last}), last) : null;
}

export function routeTreatment(input: TreatmentInput): TreatmentRoute {
  return { ...input, ...resolveTreatment(input) };
}
