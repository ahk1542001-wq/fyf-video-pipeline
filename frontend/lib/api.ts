// Typed fetch wrappers for the Stage C-II shared chat/canvas Studio.
//
// Every pane (chat + canvas) edits the SAME canonical project version through
// these wrappers; none of them holds a private authoritative copy of the
// project. Paths are relative ("/api/...") so they flow through the Next.js
// rewrite to the FastAPI backend in dev, prod and the browser E2E alike.
//
// Errors are surfaced honestly: an ApiError carries the HTTP status plus the
// backend's structured detail (lock_conflict reasons, stale_version hint,
// chat_mapping_failed reason) so the UI can explain WHY something was refused.

import { fetchWithDeadline, isRecord } from "../components/studio/studio-data";

// ---------------------------------------------------------------------------
// Backend JSON shapes (mirror backend/projects/models.py + video_contract.py)
// ---------------------------------------------------------------------------

export type SceneType = "whiteboard" | "demo";
export type MascotAction = "present" | "explain" | "think" | "warn" | "approve";
export type Emotion = "neutral" | "warm" | "focused" | "concerned" | "confident";

export interface ScriptSegment {
  id: string;
  text: string;
  visual_action: string;
  scene_type: SceneType;
  mascot_action: MascotAction;
  emotion: Emotion;
  emphasis: string[];
  /** Optional scene-level contracts; omitted by legacy projects. */
  caption?: string | null;
  voice?: string | null;
  duration_seconds?: number | null;
  visual?: unknown;
}

export interface VideoScript {
  title: string;
  language: string;
  segments: ScriptSegment[];
  style_applied?: string | null;
  studio_name?: string | null;
  genre?: string | null;
  presenter_mode?: string | null;
  voice_actor?: string | null;
  cta_text?: string | null;
  aspect_ratio?: string | null;
  retention_progress_bar?: boolean | null;
  animated_lower_thirds?: boolean | null;
}

export interface LockState {
  content: boolean;
  visual: boolean;
  timing: boolean;
  voice: boolean;
  lock_id?: string | null;
  locked_by?: string | null;
  locked_at?: string | null;
}

export interface SegmentTiming {
  segment_id: string;
  start_seconds: number;
  end_seconds: number;
}

export interface AssetReference {
  asset_id: string;
  kind: "image" | "video" | "audio" | "font" | "motion_graphic" | "document";
  uri?: string | null;
  sha256?: string | null;
}

export interface ProjectVersion {
  project_id: string;
  version_no: number;
  parent_version: number | null;
  script: VideoScript;
  locks: LockState;
  variant_name?: string | null;
  segment_timings: SegmentTiming[];
  asset_references: AssetReference[];
  actor: string;
  created_at: string;
  applied_operations: string[];
  source_command_operation?: string | null;
  render_manifest?: RenderManifest | null;
}

export interface RenderManifest {
  video_spec_version: string;
  asset_hashes: Record<string, string>;
  fonts: string[];
  renderer_version?: string | null;
  skill_versions: Record<string, string>;
  seeds: Record<string, number>;
  output: {
    aspect_ratio?: "9:16" | "16:9" | "1:1" | null;
    width?: number | null;
    height?: number | null;
    duration_seconds?: number | null;
    fps?: number | null;
    codec?: string | null;
  };
}

export type Selection =
  | { kind: "scene"; scene_ids: string[] }
  | { kind: "object"; object_refs: string[] }
  | { kind: "time_range"; start_seconds: number; end_seconds: number }
  | { kind: "all" };

export type CommandOperation =
  | "edit_script"
  | "edit_scene"
  | "edit_timing"
  | "edit_visual"
  | "edit_voice"
  | "update_surface"
  | "request_render"
  | "reframe_aspect"
  | "reedit_duration";

export type CommandScope = "content" | "visual" | "timing" | "voice";

export type CommandPayload =
  | { kind: "script_segments"; segments: ScriptSegment[] }
  | { kind: "segment_timings"; timings: SegmentTiming[] };

export interface ProjectCommand {
  project_id: string;
  base_version: number;
  actor: string;
  operation: CommandOperation;
  scope?: CommandScope | null;
  selection?: Selection | null;
  payload?: CommandPayload | null;
  idempotency_key: string;
}

export interface VersionSummary {
  version_no: number;
  parent_version: number | null;
  variant_name?: string | null;
  actor: string;
  created_at: string;
  applied_operations: string[];
  source_command_operation?: string | null;
  segment_ids: string[];
  locks: LockState;
}

export interface WorkflowEvent {
  event_id: string;
  project_id: string;
  version_no?: number | null;
  event_type: string;
  stage?: string | null;
  status: string;
  sequence: number;
  progress_source: "actual" | "estimated";
  artifact_refs: string[];
  actor?: string | null;
  timestamp: string;
}

export interface LockScopeRecord {
  locked: boolean;
  scope?: string;
  locked_by?: string | null;
  locked_at?: string | null;
  reason?: string | null;
}

export interface LocksResponse {
  success: boolean;
  project_id: string;
  scopes: Record<string, LockScopeRecord>;
  locked: string[];
  available_scopes: string[];
  scene_locks?: Record<string, Record<string, LockScopeRecord>>;
  available_scene_scopes?: string[];
}

export interface BudgetStatus {
  paid_production_enabled: boolean;
  budget_exceeded: boolean;
  remaining_usd: number | null;
  reason?: string | null;
  estimated_usd: number | null;
  actual_usd: number | null;
  provider_reported_usd: number | null;
  invoice_confirmed_usd: number | null;
  project_default_budget_usd?: number | null;
  project_id?: string | null;
  project_budget_usd?: number | null;
  project_budget_source?: string | null;
  project_budget_is_custom?: boolean;
  project_budget_updated_at?: string | null;
  effective_project_cap_usd?: number | null;
  project_spend_usd?: number | null;
  project_reserved_usd?: number | null;
  project_remaining_usd?: number | null;
  project_budget_exceeded?: boolean | null;
  project_budget_available?: boolean | null;
}

export interface ProjectRenderResponse {
  success: boolean;
  status: "queued" | "replayed";
  job_id: string;
  status_url: string;
  dispatched_version: number | null;
  estimated_cost_usd: number;
  approved_spend_usd: number;
  approval_id?: string;
  approval_target?: string;
}

export interface HumanAcceptance {
  schema_version: number;
  project_id: string;
  version_no: number;
  accepted: boolean;
  status: "accepted" | "rejected";
  actor: string;
  decided_at: string;
  qa_fingerprint?: string | null;
  automated_qa?: Record<string, unknown> | null;
  note?: string | null;
}

export interface AcceptanceReadiness {
  project_id: string;
  version_no: number;
  current_version?: number | null;
  automated_qa_passed: boolean;
  human_accepted: boolean;
  download_ready: boolean;
  status: string;
  reason?: string | null;
  video_current?: boolean;
  manifest_verified?: boolean;
  media?: Record<string, unknown>;
  automated_qa?: Record<string, unknown>;
  qa_fingerprint?: string | null;
}

export interface ProjectAcceptanceResponse {
  success: boolean;
  project_id: string;
  version_no: number;
  acceptance: HumanAcceptance | null;
  readiness: AcceptanceReadiness;
}

export interface TelemetryReconciliationResponse {
  success: boolean;
  source: "local_mirror" | string;
  local_mirror: { available: boolean; label: string; event_count: number };
  cloud: {
    connected: boolean;
    schema_ready: boolean | null;
    status: string;
    label: string;
  };
  outbox: {
    pending: number | null;
    failed: number | null;
    delivered: number | null;
    total: number | null;
    schema_ready?: boolean | null;
    cloud_connected?: boolean;
    drain_blocked_reason?: string | null;
    ingestion_lag_seconds?: number | null;
    corrupted?: boolean;
  };
  reconciliation: Record<string, unknown>;
  completeness: Record<string, unknown>;
  integrity: Record<string, unknown>;
  freshness: Record<string, unknown>;
}

export interface VideoJobStatus {
  job_id?: string;
  status: string;
  video_url?: string | null;
  error?: string | null;
}

export interface ExportKind {
  kind: string;
  family: string;
  extension: string;
  media_type: string;
  description: string;
  aspect_ratio?: string | null;
}

export interface ExportResult {
  kind: string;
  family: string;
  filename: string;
  media_type: string;
  bytes: number;
  sha256: string;
  warnings: string[];
  url: string;
}

export interface ChatResponse {
  success: boolean;
  operation: string;
  summary: string;
  affected_segment_ids: string[];
  command: ProjectCommand;
  status: string;
  changeset: unknown;
  version: ProjectVersion;
}

export type ProposalStatus = "proposed" | "approved" | "rejected" | "expired" | "revoked";

export interface ProposalDiff {
  operation: CommandOperation;
  summary: string;
  affected_fields: string[];
  before: Record<string, unknown>;
  after: Record<string, unknown>;
}

export interface ProjectProposal {
  proposal_id: string;
  project_id: string;
  base_version: number;
  command: ProjectCommand;
  diff: ProposalDiff;
  affected_scopes: CommandScope[];
  affected_segment_ids: string[];
  actor: string;
  status: ProposalStatus;
  idempotency_key: string;
  created_at: string;
  updated_at: string;
  expires_at?: string | null;
  decision_actor?: string | null;
  decision_at?: string | null;
  target_version?: number | null;
  reason?: string | null;
}

export interface ChatProposalResponse {
  success: boolean;
  status: "proposed" | "replayed";
  proposal_id: string;
  project_id: string;
  base_version: number;
  affected_segment_ids: string[];
  affected_scopes: CommandScope[];
  diff: ProposalDiff;
  command: ProjectCommand;
  proposal: ProjectProposal;
}

export interface ProposalDecisionResponse {
  success: boolean;
  status: ProposalStatus | "replayed";
  proposal_id: string;
  project_id: string;
  base_version: number;
  affected_segment_ids: string[];
  affected_scopes: CommandScope[];
  diff: ProposalDiff;
  command: ProjectCommand;
  proposal: ProjectProposal;
  target_version?: number;
  changeset?: unknown;
  reason?: string;
}

export interface ProposalsResponse {
  success: boolean;
  project_id: string;
  proposals: ProjectProposal[];
}

export interface UndoResponse {
  success: boolean;
  status: string;
  restored_from: number;
  previous_head?: number;
  version: ProjectVersion;
}

export interface VariantResponse {
  success: boolean;
  status: string;
  variant_name: string | null;
  version: ProjectVersion;
}

export interface ResultResponse {
  success: boolean;
  status: "attached" | "stale_isolated" | "replayed";
  applied: boolean;
  dispatched_version?: number;
  current_version?: number;
  target_version?: number;
  reason?: string;
  hint?: string;
  version?: ProjectVersion;
}

export interface SceneEditRequest {
  base_version: number;
  scene_ids: string[];
  actor?: string;
  text?: string;
  visual_action?: string;
  caption?: string;
  voice?: string;
  duration_seconds?: number;
  idempotency_key: string;
}

export interface SceneEditResponse {
  success: boolean;
  status: string;
  base_version: number;
  target_version: number;
  affected_segment_ids: string[];
  changeset: unknown;
  version: ProjectVersion;
}

export interface SceneRegenerationRequest {
  base_version: number;
  scene_ids: string[];
  actor?: string;
  idempotency_key: string;
  per_segment_voice?: boolean;
  renderer_identity?: string | null;
  voice_identity?: string | null;
  visual_policy?: string | null;
}

export interface PipelinePlan {
  dirty: string[];
  cache_hits: string[];
  blocked: string[];
  reasons: Record<string, string>;
  graph_hash_before: string;
  graph_hash_after: string;
}

export interface SceneRegenerationResponse {
  success: boolean;
  status: "planned" | "blocked";
  project_id: string;
  base_version: number;
  current_version: number;
  scene_ids: string[];
  idempotency_key: string;
  plan: PipelinePlan;
}

export interface VersionsResponse {
  success: boolean;
  project_id: string;
  head: number;
  versions: VersionSummary[];
}

// ---------------------------------------------------------------------------
// Error handling
// ---------------------------------------------------------------------------

export class ApiError extends Error {
  readonly status: number;
  readonly detail: unknown;

  constructor(status: number, message: string, detail: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }

  /** The backend error code when the detail is a structured object. */
  get code(): string | null {
    if (isRecord(this.detail) && typeof this.detail.error === "string") {
      return this.detail.error;
    }
    return null;
  }

  /** Conflicting lock scopes reported by a lock_conflict refusal. */
  get conflicts(): string[] {
    if (isRecord(this.detail) && Array.isArray(this.detail.conflicts)) {
      return this.detail.conflicts.filter((c): c is string => typeof c === "string");
    }
    return [];
  }

  /** Per-scope human reasons reported by the chat lock_conflict refusal. */
  get lockReasons(): Record<string, string> {
    const out: Record<string, string> = {};
    if (isRecord(this.detail) && isRecord(this.detail.reasons)) {
      for (const [scope, reason] of Object.entries(this.detail.reasons)) {
        if (typeof reason === "string") out[scope] = reason;
      }
    }
    return out;
  }
}

function safeJson(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

function errorMessage(body: unknown, status: number): string {
  if (isRecord(body)) {
    if (typeof body.detail === "string") return body.detail;
    if (isRecord(body.detail)) {
      const detail = body.detail;
      if (typeof detail.reason === "string") return detail.reason;
      if (typeof detail.hint === "string") return detail.hint;
      if (typeof detail.error === "string") return detail.error;
    }
  }
  return `Request failed with status ${status}`;
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const accessToken =
    typeof window === "undefined"
      ? ""
      : window.sessionStorage.getItem("fyf-generation-access")?.trim() ?? "";
  const response = await fetchWithDeadline(path, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(accessToken ? { "X-FYF-Access-Token": accessToken } : {}),
      ...(init.headers as Record<string, string> | undefined),
    },
  });
  const text = await response.text();
  const body = text ? safeJson(text) : null;
  if (!response.ok) {
    // FastAPI wraps an HTTPException payload as {"detail": <string | object>};
    // the structured error (code / conflicts / reasons / hint) lives at body.detail.
    const detail = isRecord(body) && "detail" in body ? body.detail : body;
    throw new ApiError(response.status, errorMessage(body, response.status), detail);
  }
  return body as T;
}

function post<T>(path: string, body: unknown): Promise<T> {
  return request<T>(path, { method: "POST", body: JSON.stringify(body) });
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

export function newIdempotencyKey(prefix = "ui"): string {
  const cryptoObj = globalThis.crypto;
  if (cryptoObj && typeof cryptoObj.randomUUID === "function") {
    return `${prefix}-${cryptoObj.randomUUID()}`;
  }
  return `${prefix}-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

export function describeSelection(selection: Selection | null): string {
  if (!selection) return "No selection (chat will edit every scene)";
  if (selection.kind === "scene") return `Scene ${selection.scene_ids.join(", ")}`;
  if (selection.kind === "object") return `Object ${selection.object_refs.join(", ")}`;
  if (selection.kind === "time_range") {
    return `Time range ${selection.start_seconds}-${selection.end_seconds}s`;
  }
  return "All scenes";
}

// ---------------------------------------------------------------------------
// Endpoints
// ---------------------------------------------------------------------------

export async function createProject(
  script: VideoScript,
  actor: string,
  opts: { projectId?: string; variantName?: string; idempotencyKey?: string } = {},
): Promise<{ success: boolean; project_id: string; version_no: number; version: ProjectVersion }> {
  return post("/api/projects", {
    script,
    actor,
    project_id: opts.projectId,
    variant_name: opts.variantName,
    idempotency_key: opts.idempotencyKey,
  });
}

export async function getVersion(
  projectId: string,
  versionNo: number,
): Promise<{ success: boolean; project_id: string; version_no: number; version: ProjectVersion }> {
  return request(`/api/projects/${projectId}/versions/${versionNo}`);
}

export async function listVersions(projectId: string): Promise<VersionsResponse> {
  return request(`/api/projects/${projectId}/versions`);
}

export async function listEvents(
  projectId: string,
  afterSequence = 0,
): Promise<{ success: boolean; project_id: string; events: WorkflowEvent[] }> {
  return request(`/api/projects/${projectId}/events?after_sequence=${afterSequence}`);
}

export async function applyCommand(
  projectId: string,
  command: ProjectCommand,
): Promise<{ success: boolean; status: string; changeset: unknown }> {
  return post(`/api/projects/${projectId}/commands`, command);
}

export async function sendChat(
  projectId: string,
  args: { message: string; selection?: Selection | null; baseVersion?: number; actor?: string },
): Promise<ChatResponse> {
  // Explicit legacy apply-now compatibility. The Studio uses proposeChat so
  // chat edits always wait for an approval decision.
  return post(`/api/projects/${projectId}/chat`, {
    message: args.message,
    selection: args.selection ?? null,
    actor: args.actor ?? "creative-director",
    base_version: args.baseVersion ?? null,
  });
}

export async function proposeChat(
  projectId: string,
  args: {
    message: string;
    selection?: Selection | null;
    baseVersion?: number;
    actor?: string;
    idempotencyKey?: string;
  },
): Promise<ChatProposalResponse> {
  return post(`/api/projects/${projectId}/chat/proposals`, {
    message: args.message,
    selection: args.selection ?? null,
    actor: args.actor ?? "creative-director",
    base_version: args.baseVersion ?? null,
    idempotency_key: args.idempotencyKey,
  });
}

export async function listProposals(projectId: string): Promise<ProposalsResponse> {
  return request(`/api/projects/${projectId}/proposals`);
}

export async function approveProposal(
  projectId: string,
  proposalId: string,
  actor = "creative-director",
): Promise<ProposalDecisionResponse> {
  return post(`/api/projects/${projectId}/proposals/${proposalId}/approve`, { actor });
}

export async function rejectProposal(
  projectId: string,
  proposalId: string,
  actor = "creative-director",
  reason?: string,
): Promise<ProposalDecisionResponse> {
  return post(`/api/projects/${projectId}/proposals/${proposalId}/reject`, { actor, reason });
}

export async function expireProposal(
  projectId: string,
  proposalId: string,
  actor = "system",
  reason?: string,
): Promise<ProposalDecisionResponse> {
  return post(`/api/projects/${projectId}/proposals/${proposalId}/expire`, { actor, reason });
}

export async function revokeProposal(
  projectId: string,
  proposalId: string,
  actor = "system",
  reason?: string,
): Promise<ProposalDecisionResponse> {
  return post(`/api/projects/${projectId}/proposals/${proposalId}/revoke`, { actor, reason });
}

export async function undoToVersion(
  projectId: string,
  targetVersion: number,
  actor = "creative-director",
  baseVersion?: number,
  idempotencyKey = newIdempotencyKey("undo"),
): Promise<UndoResponse> {
  return post(`/api/projects/${projectId}/undo`, {
    target_version: targetVersion,
    actor,
    ...(baseVersion === undefined ? {} : { base_version: baseVersion }),
    idempotency_key: idempotencyKey,
  });
}

export async function createVariant(
  projectId: string,
  variantName: string,
  baseVersion?: number,
  actor = "creative-director",
  idempotencyKey = newIdempotencyKey("variant"),
): Promise<VariantResponse> {
  return post(`/api/projects/${projectId}/variants`, {
    variant_name: variantName,
    actor,
    base_version: baseVersion ?? null,
    idempotency_key: idempotencyKey,
  });
}

export async function getLocks(projectId: string): Promise<LocksResponse> {
  return request(`/api/projects/${projectId}/locks`);
}

export async function setLock(
  projectId: string,
  scope: CommandScope,
  locked: boolean,
  opts: { lockedBy?: string; reason?: string; sceneId?: string; sceneIds?: string[] } = {},
): Promise<{
  success: boolean;
  project_id: string;
  scope: string;
  scene_ids?: string[];
  record: LockScopeRecord;
  locked: string[];
  scene_locks?: Record<string, Record<string, LockScopeRecord>>;
}> {
  return post(`/api/projects/${projectId}/locks`, {
    scope,
    locked,
    ...(opts.sceneId === undefined ? {} : { scene_id: opts.sceneId }),
    ...(opts.sceneIds === undefined ? {} : { scene_ids: opts.sceneIds }),
    locked_by: opts.lockedBy ?? "creative-director",
    reason: opts.reason ?? null,
  });
}

export async function editScene(
  projectId: string,
  args: SceneEditRequest,
): Promise<SceneEditResponse> {
  return post(`/api/projects/${projectId}/scenes/edit`, args);
}

export async function regenerateScenes(
  projectId: string,
  args: SceneRegenerationRequest,
): Promise<SceneRegenerationResponse> {
  return post(`/api/projects/${projectId}/scenes/regenerate`, args);
}

export async function attachResult(
  projectId: string,
  dispatchedVersion: number,
  assetReferences: AssetReference[] = [],
  actor = "generation",
): Promise<ResultResponse> {
  return post(`/api/projects/${projectId}/results`, {
    dispatched_version: dispatchedVersion,
    actor,
    asset_references: assetReferences,
  });
}

export async function getBudget(
  projectId?: string,
): Promise<{ success: boolean; budget: BudgetStatus }> {
  const query = projectId ? `?project_id=${encodeURIComponent(projectId)}` : "";
  return request(`/api/budget${query}`);
}

export async function setProjectBudget(
  projectId: string,
  budgetUsd: number,
  actor = "operator",
  idempotencyKey?: string,
): Promise<{ success: boolean; project_id: string; configured: Record<string, unknown>; budget: BudgetStatus }> {
  return post(`/api/projects/${projectId}/budget`, {
    budget_usd: budgetUsd,
    actor,
    ...(idempotencyKey ? { idempotency_key: idempotencyKey } : {}),
  });
}

export async function getProjectAcceptance(
  projectId: string,
  versionNo: number,
): Promise<ProjectAcceptanceResponse> {
  return request(`/api/projects/${projectId}/versions/${versionNo}/acceptance`);
}

export async function setProjectAcceptance(
  projectId: string,
  versionNo: number,
  args: {
    accepted: boolean;
    actor?: string;
    automatedQa?: Record<string, unknown> | null;
    qaFingerprint?: string;
    note?: string;
  },
): Promise<ProjectAcceptanceResponse> {
  return post(`/api/projects/${projectId}/versions/${versionNo}/acceptance`, {
    accepted: args.accepted,
    actor: args.actor ?? "creative-director",
    automated_qa: args.automatedQa ?? null,
    ...(args.qaFingerprint ? { qa_fingerprint: args.qaFingerprint } : {}),
    ...(args.note ? { note: args.note } : {}),
  });
}

export async function getTelemetryReconciliation(): Promise<TelemetryReconciliationResponse> {
  return request("/api/telemetry/reconciliation");
}

export async function renderProject(
  projectId: string,
  args: {
    baseVersion: number;
    approvedSpendUsd: number;
    aspectRatio: "9:16" | "16:9" | "1:1";
    reducedMotion: boolean;
    ctaText?: string;
    retentionProgressBar?: boolean;
    animatedLowerThirds?: boolean;
  },
  idempotencyKey: string,
): Promise<ProjectRenderResponse> {
  return request(`/api/projects/${projectId}/render`, {
    method: "POST",
    headers: { "X-FYF-Idempotency-Key": idempotencyKey },
    body: JSON.stringify({
      base_version: args.baseVersion,
      approved_spend_usd: args.approvedSpendUsd,
      aspect_ratio: args.aspectRatio,
      reduced_motion: args.reducedMotion,
      cta_text: args.ctaText ?? "",
      retention_progress_bar: args.retentionProgressBar ?? true,
      animated_lower_thirds: args.animatedLowerThirds ?? true,
    }),
  });
}

export async function getVideoJobStatus(jobId: string): Promise<VideoJobStatus> {
  return request(`/api/jobs/${jobId}/status`);
}

export async function listExportKinds(): Promise<{ success: boolean; exports: ExportKind[] }> {
  return request("/api/export-kinds");
}

export async function createExports(
  jobId: string,
  kinds: string[],
): Promise<{ success: boolean; job_id: string; exports: ExportResult[] }> {
  return post(`/api/jobs/${jobId}/exports`, { kinds });
}
