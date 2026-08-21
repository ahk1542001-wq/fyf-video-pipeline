export type VoiceProvider = "kaggle" | "gemini" | "dual";

export type RuntimeInfo = {
  runtime_mode: "hackathon" | "product";
  allowed_voice_providers: VoiceProvider[];
  script_model: string;
  fallback_model: string;
};

export type RecentApprovedVideo = {
  job_id: string;
  title: string;
  voice_provider: "kaggle" | "gemini";
  updated_at: string;
  video_url: string;
};

export type WorkflowStageId = "source" | "story" | "lock" | "render";
export type WorkflowStageState = "complete" | "current" | "upcoming";

export type WorkflowStage = {
  id: WorkflowStageId;
  label: string;
  state: WorkflowStageState;
};

// Use the hosted frontend origin by default; Next rewrites /api and /health to
// the colocated FastAPI process. An explicit URL remains available for local
// split-process development or a separately hosted API.
export const API_URL = process.env.NEXT_PUBLIC_API_URL || "";

const HACKATHON_MODE = process.env.NEXT_PUBLIC_FYF_RUNTIME_MODE === "hackathon";

export const STATIC_RUNTIME_FALLBACK: RuntimeInfo = {
  runtime_mode: HACKATHON_MODE ? "hackathon" : "product",
  allowed_voice_providers: HACKATHON_MODE ? ["gemini"] : ["kaggle", "gemini", "dual"],
  script_model: process.env.NEXT_PUBLIC_FYF_SCRIPT_MODEL || "Unavailable until the runtime API responds",
  fallback_model: process.env.NEXT_PUBLIC_FYF_FALLBACK_MODEL || "Unavailable until the runtime API responds",
};

function isRecordValue(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

export function isRuntimeInfo(value: unknown): value is RuntimeInfo {
  if (!isRecordValue(value)) return false;
  const providers = value.allowed_voice_providers;
  return (
    (value.runtime_mode === "hackathon" || value.runtime_mode === "product") &&
    Array.isArray(providers) &&
    providers.length > 0 &&
    providers.every(provider => provider === "kaggle" || provider === "gemini" || provider === "dual") &&
    typeof value.script_model === "string" &&
    typeof value.fallback_model === "string"
  );
}

export function isRecentApprovedVideo(value: unknown): value is RecentApprovedVideo {
  if (!isRecordValue(value)) return false;
  if (typeof value.job_id !== "string" || !/^[0-9a-f]{8}$/.test(value.job_id)) return false;
  return (
    typeof value.title === "string" &&
    value.title.trim().length > 0 &&
    (value.voice_provider === "kaggle" || value.voice_provider === "gemini") &&
    typeof value.updated_at === "string" &&
    value.updated_at.trim().length > 0 &&
    value.video_url === `/api/jobs/${value.job_id}/video`
  );
}

export function mergeRecentVideos(
  current: RecentApprovedVideo[],
  incoming: RecentApprovedVideo[],
): RecentApprovedVideo[] {
  const byJobId = new Map(current.map(video => [video.job_id, video]));
  incoming.forEach(video => byJobId.set(video.job_id, video));
  return Array.from(byJobId.values())
    .sort((left, right) => (Date.parse(right.updated_at) || 0) - (Date.parse(left.updated_at) || 0))
    .slice(0, 6);
}

export function voiceProviderLabel(provider: VoiceProvider): string {
  if (provider === "kaggle") return "Partner voice";
  if (provider === "dual") return "Partner + AI";
  return "AI voice";
}

type WorkflowStateInput = {
  hasSource: boolean;
  hasStory: boolean;
  narrationLocked: boolean;
  hasCompletedVideo: boolean;
  viewingApprovedVideo?: boolean;
};

export function deriveWorkflowStages({
  hasSource,
  hasStory,
  narrationLocked,
  hasCompletedVideo,
  viewingApprovedVideo = false,
}: WorkflowStateInput): WorkflowStage[] {
  const completedResult = hasCompletedVideo || viewingApprovedVideo;
  const done = [
    completedResult || hasSource,
    completedResult || hasStory,
    completedResult || narrationLocked,
    completedResult,
  ];
  const firstIncomplete = done.every(Boolean)
    ? done.length - 1
    : done.findIndex(stepDone => !stepDone);
  const definitions: Array<{ id: WorkflowStageId; label: string }> = [
    { id: "source", label: "Source" },
    { id: "story", label: "Story" },
    { id: "lock", label: "Lock" },
    { id: "render", label: "Render" },
  ];

  return definitions.map((stage, index) => ({
    ...stage,
    state: done[index] ? "complete" : index === firstIncomplete ? "current" : "upcoming",
  }));
}

export type VertexUsage = {
  input_tokens: number | null;
  output_tokens: number | null;
  total_tokens: number | null;
  cached_input_tokens: number | null;
  thoughts_tokens: number | null;
};

export type VertexCallTelemetry = {
  call_id: string;
  stage: string;
  model: string | null;
  operation: string;
  attempt: number;
  billable: boolean;
  status: "succeeded" | "failed" | string;
  duration_ms: number;
  usage: VertexUsage;
  input_characters?: number | null;
  audio_output_bytes?: number | null;
  error_type?: string;
  http_status?: number | string | null;
};

export type JobTelemetrySummary = {
  total_calls: number;
  billable_calls: number;
  operation_poll_calls: number;
  successful_calls: number;
  failed_calls: number;
  retry_calls: number;
  total_input_tokens: number;
  total_output_tokens: number;
  total_tokens: number;
  total_cached_input_tokens: number;
  total_thoughts_tokens: number;
  token_status: string;
  estimated_cost_usd: number | null;
  cost_status: string;
  pricing_version: string;
  visual_fallbacks: number | null;
  job_status: string | null;
};

export type JobTelemetry = {
  job_id: string;
  title?: string;
  duration_sec?: number;
  voice_mode?: string;
  status?: string;
  total_render_time_ms?: number;
  total_tokens_used?: number;
  cost_usd?: number;
  qa_passed?: number;
  created_at?: string;
  job_kind?: string;
  schema_version?: number;
  started_at?: string;
  finished_at?: string;
  calls?: VertexCallTelemetry[];
  summary?: JobTelemetrySummary;
  privacy?: {
    prompts_recorded: boolean;
    response_text_recorded: boolean;
    credentials_recorded: boolean;
    raw_provider_errors_recorded: boolean;
  };
};

export type SceneTelemetry = {
  job_id: string;
  scene_id: string;
  treatment_type: string;
  render_time_ms: number;
  vertex_latency_ms: number;
  evidence_claim_count: number;
  created_at: string;
};

export type TelemetrySummary = {
  total_jobs: number;
  total_tokens_used: number;
  total_cost_usd: number;
  avg_render_time_sec: number;
  total_vertex_calls?: number;
  jobs: JobTelemetry[];
  clickhouse_status: string;
};
