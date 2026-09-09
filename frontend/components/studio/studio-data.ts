// Shared types, constants, and pure helpers for the Create Studio workspace.
// Extracted verbatim from app/page.tsx during the Stage C-I monolith
// decomposition. Values, shapes, and copy are intentionally unchanged so the
// refactor is behavior-preserving.

export const SCRIPT_JOB_STORAGE_KEY = "fyf-active-script-job";
export const WIZARD_TOPIC_STORAGE_KEY = "fyf-wizard-topic";
export const WIZARD_CONTEXT_STORAGE_KEY = "fyf-wizard-context";
export const LOCKED_SCRIPT_STORAGE_KEY = "fyf-locked-script";

export function formatElapsed(ms: number): string {
  const totalSeconds = Math.max(0, Math.floor(ms / 1000));
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return minutes > 0 ? `${minutes}m ${String(seconds).padStart(2, "0")}s` : `${seconds}s`;
}

export interface VideoScript {
  title: string;
  language: "my-MM" | "en-US";
  style_applied?: string;
  studio_name?: string;
  genre?: string;
  presenter_mode?: string;
  voice_actor?: string;
  segments: Array<{
    id: string;
    text: string;
    visual_action: string;
    scene_type: "whiteboard" | "demo";
    mascot_action: "present" | "explain" | "think" | "warn" | "approve";
    emotion: "neutral" | "warm" | "focused" | "concerned" | "confident";
    emphasis: string[];
  }>;
}
export interface StoryVariant { name: string; script: VideoScript; }

export interface VideoStyleOption {
  id: string;
  name: string;
  description: string;
}

export interface PersonaOption {
  id: string;
  name: string;
  description: string;
}

export const DEFAULT_PERSONAS: PersonaOption[] = [
  { id: "clear_educator", name: "The Clear Educator (Default)", description: "Friendly, patient, whiteboard-focused explanation." },
  { id: "dynamic_storyteller", name: "The Dynamic Storyteller", description: "Expressive, high-energy dramatic narrative." },
  { id: "objective_analyst", name: "The Objective Analyst", description: "Data-backed, high-fidelity inspection." },
  { id: "warm_companion", name: "The Warm Companion", description: "Encouraging, conversational peer voice." },
];

export const DEFAULT_STYLES: VideoStyleOption[] = [
  { id: "fyf_explainer", name: "FYF Explainer (Default)", description: "Standard high-clarity whiteboard with balanced mascot pacing." },
  { id: "cinematic_continuity", name: "Cinematic Continuity", description: "Dramatic push-ins and smooth continuous camera motion." },
  { id: "evidence_story", name: "Evidence Story", description: "Documentary inspection focus with high data fidelity." },
  { id: "cinematic_documentary", name: "Cinematic Documentary", description: "Atmospheric visuals, natural lighting, deep investigative pacing." },
  { id: "tech_explainer", name: "Tech & Product Explainer", description: "Crisp isometric diagrams, blueprint grids, dynamic product breakdown." },
  { id: "investigative", name: "Evidence & Investigative Cinema", description: "Evidence boards, archival footage, timeline sequences." },
  { id: "narrative", name: "Narrative Short", description: "Rich character-focused scenes, emotional beats, continuity." },
];

export const STUDIO_PRESETS = [
  {
    id: "burmese_flagship",
    label: "Brand Explainer (Flagship)",
    studioName: "FYF Studio",
    language: "my-MM",
    style: "fyf_explainer",
    presenterMode: "on_screen",
    voiceActor: "Sadaltager",
    ctaText: "",
  },
  {
    id: "social_ad",
    label: "High-Converting Social Ad",
    studioName: "Brand Ad Studio",
    language: "en-US",
    style: "tech_explainer",
    presenterMode: "voiceover_only",
    voiceActor: "Puck",
    ctaText: "Shop Now",
  },
  {
    id: "product_launch",
    label: "Product Launch Hype (Magnific)",
    studioName: "Launch Studio",
    language: "en-US",
    style: "cinematic_documentary",
    presenterMode: "on_screen",
    voiceActor: "Sadaltager",
    ctaText: "Pre-Order Now",
  },
] as const;

export type StudioPreset = typeof STUDIO_PRESETS[number];

export type JobStatus = "idle" | "queued" | "visuals" | "voice" | "rendering" | "qa" | "completed" | "failed";
export type VisualCacheState = "producer" | "waiting" | "hit" | "miss";
export type VisualProgress = {
  passed?: number;
  planned?: number;
  total: number;
  fallbacks?: number;
  percent?: number;
  completed_batches?: number;
  total_batches?: number;
  cache_state?: VisualCacheState | null;
  retry_count?: number;
  current_failed_ids?: string[];
};

export type ProductionTelemetry = {
  job: Record<string, unknown>;
  connected_to_cloud?: boolean;
  clickhouse_status?: string;
};

export async function fetchWithDeadline(
  input: RequestInfo | URL,
  init: RequestInit = {},
  timeoutMs = 15_000,
): Promise<Response> {
  const controller = new AbortController();
  let timedOut = false;
  const relayAbort = () => controller.abort();
  init.signal?.addEventListener("abort", relayAbort, { once: true });
  const timer = window.setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, timeoutMs);

  try {
    return await fetch(input, { ...init, signal: controller.signal });
  } catch (error) {
    if (timedOut) {
      throw new Error("The service did not respond in time. Check the telemetry ledger before retrying.");
    }
    throw error;
  } finally {
    window.clearTimeout(timer);
    init.signal?.removeEventListener("abort", relayAbort);
  }
}

export function isRecord(val: unknown): val is Record<string, unknown> {
  return typeof val === "object" && val !== null;
}

export function isVideoScript(val: unknown): val is VideoScript {
  if (!isRecord(val)) return false;
  if (
    typeof val.title !== "string"
    || !["my-MM", "en-US"].includes(val.language as string)
  ) return false;
  if (!Array.isArray(val.segments)) return false;

  const optionalStringFields = ["style_applied", "studio_name", "genre", "presenter_mode", "voice_actor"];
  if (optionalStringFields.some(field => val[field] !== undefined && typeof val[field] !== "string")) return false;

  return val.segments.every(seg =>
    isRecord(seg) &&
    typeof seg.id === "string" &&
    typeof seg.text === "string" &&
    typeof seg.visual_action === "string" &&
    (seg.scene_type === "whiteboard" || seg.scene_type === "demo") &&
    ["present", "explain", "think", "warn", "approve"].includes(seg.mascot_action as string) &&
    ["neutral", "warm", "focused", "concerned", "confident"].includes(seg.emotion as string) &&
    Array.isArray(seg.emphasis) && seg.emphasis.every(e => typeof e === "string")
  );
}

export function finiteMetric(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}
