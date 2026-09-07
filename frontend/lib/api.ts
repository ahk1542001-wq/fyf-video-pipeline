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
}

export type Selection =
  | { kind: "scene"; scene_ids: string[] }
  | { kind: "object"; object_refs: string[] }
  | { kind: "time_range"; start_seconds: number; end_seconds: number }
  | { kind: "all" };

export type CommandOperation =
  | "edit_script"
  | "edit_timing"
  | "edit_visual"
  | "update_surface"
  | "request_render";

export type CommandScope = "content" | "visual" | "timing";

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
  const response = await fetchWithDeadline(path, {
    ...init,
    headers: {
      "Content-Type": "application/json",
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
  return post(`/api/projects/${projectId}/chat`, {
    message: args.message,
    selection: args.selection ?? null,
    actor: args.actor ?? "creative-director",
    base_version: args.baseVersion ?? null,
  });
}

export async function undoToVersion(
  projectId: string,
  targetVersion: number,
  actor = "creative-director",
): Promise<UndoResponse> {
  return post(`/api/projects/${projectId}/undo`, {
    target_version: targetVersion,
    actor,
  });
}

export async function createVariant(
  projectId: string,
  variantName: string,
  baseVersion?: number,
  actor = "creative-director",
): Promise<VariantResponse> {
  return post(`/api/projects/${projectId}/variants`, {
    variant_name: variantName,
    actor,
    base_version: baseVersion ?? null,
  });
}

export async function getLocks(projectId: string): Promise<LocksResponse> {
  return request(`/api/projects/${projectId}/locks`);
}

export async function setLock(
  projectId: string,
  scope: CommandScope,
  locked: boolean,
  opts: { lockedBy?: string; reason?: string } = {},
): Promise<{ success: boolean; project_id: string; scope: string; record: LockScopeRecord; locked: string[] }> {
  return post(`/api/projects/${projectId}/locks`, {
    scope,
    locked,
    locked_by: opts.lockedBy ?? "creative-director",
    reason: opts.reason ?? null,
  });
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

export async function getBudget(): Promise<{ success: boolean; budget: BudgetStatus }> {
  return request("/api/budget");
}
