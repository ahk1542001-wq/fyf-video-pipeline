// Stage C-II Studio state: ONE canonical project version shared by chat + canvas.
//
// This module owns the reducer + the `useProjectStudio` controller hook. Neither
// pane keeps a private authoritative copy of the project: every mutation goes to
// the backend (POST /api/projects/{id}/commands | /chat/proposals | /undo |
// /variants | /locks | /results) and the canonical head is re-read afterwards.
// The chat transcript and proposal records are held SEPARATELY from the
// versioned decision record (versions / events / locks), and cross-project
// preferences travel only via the explicit `saveToBrand()` action - never
// implicitly.

"use client";

import { useCallback, useEffect, useReducer } from "react";

import {
  ApiError,
  approveProposal,
  createVariant,
  describeSelection,
  editScene,
  getBudget,
  getVersion,
  getLocks,
  getVideoJobStatus,
  listProposals,
  listEvents,
  listVersions,
  newIdempotencyKey,
  proposeChat,
  regenerateScenes,
  rejectProposal,
  renderProject,
  getProjectAcceptance,
  setProjectAcceptance,
  setProjectBudget as setProjectBudgetApi,
  setLock,
  undoToVersion,
  type BudgetStatus,
  type CommandScope,
  type LocksResponse,
  type ProjectProposal,
  type ProjectAcceptanceResponse,
  type ProjectVersion,
  type Selection,
  type VersionSummary,
  type WorkflowEvent,
} from "./api";

export const BRAND_PREF_KEY = "fyf-brand-preference";

export type SelectionMode = "scene" | "object" | "time_range" | "all";
export type StudioStatus = "loading" | "ready" | "error" | "offline";
export type NoticeKind =
  | "info"
  | "success"
  | "error"
  | "stale"
  | "cancelled"
  | "partial"
  | "locked";

export interface Notice {
  kind: NoticeKind;
  message: string;
}

export interface ChatMessage {
  id: string;
  role: "user" | "assistant" | "system";
  text: string;
  meta?: string;
  pending?: boolean;
}

export interface StudioState {
  status: StudioStatus;
  projectId: string;
  head: ProjectVersion | null;
  versions: VersionSummary[];
  events: WorkflowEvent[];
  locks: LocksResponse | null;
  budget: BudgetStatus | null;
  humanAcceptance: ProjectAcceptanceResponse | null;
  selection: Selection | null;
  selectionMode: SelectionMode;
  selectedSceneId: string | null;
  objectDraft: string;
  timeRangeStart: string;
  timeRangeEnd: string;
  chat: ChatMessage[];
  proposals: ProjectProposal[];
  chatDraft: string;
  sceneTextDraft: string;
  sceneVisualDraft: string;
  sceneCaptionDraft: string;
  sceneVoiceDraft: string;
  sceneDurationDraft: string;
  variantDraft: string;
  compareBefore: number | null;
  compareAfter: number | null;
  compareOpen: boolean;
  compareBeforeVersion: ProjectVersion | null;
  compareAfterVersion: ProjectVersion | null;
  pending: boolean;
  renderJobId: string | null;
  renderDispatchedVersion: number | null;
  renderStatus: string | null;
  videoUrl: string | null;
  notice: Notice | null;
  error: string | null;
}

type Action =
  | { type: "load/start" }
  | {
      type: "load/success";
      head: ProjectVersion | null;
      versions: VersionSummary[];
      events: WorkflowEvent[];
      locks: LocksResponse;
      budget: BudgetStatus;
      proposals: ProjectProposal[];
      humanAcceptance?: ProjectAcceptanceResponse | null;
    }
  | { type: "load/failure"; error: string; offline: boolean }
  | { type: "selection/mode"; mode: SelectionMode }
  | { type: "selection/scene"; sceneId: string }
  | { type: "selection/objectDraft"; value: string }
  | { type: "selection/timeDraft"; field: "start" | "end"; value: string }
  | { type: "selection/clear" }
  | { type: "chat/draft"; value: string }
  | { type: "chat/push"; message: ChatMessage }
  | { type: "chat/resolve"; id: string; text: string; meta?: string }
  | { type: "proposals/set"; proposals: ProjectProposal[] }
  | { type: "proposal/add"; proposal: ProjectProposal }
  | { type: "proposal/update"; proposal: ProjectProposal }
  | { type: "scene/draft"; field: "text" | "visual" | "caption" | "voice" | "duration"; value: string }
  | { type: "variant/draft"; value: string }
  | { type: "compare/set"; which: "before" | "after"; versionNo: number | null }
  | { type: "compare/toggle"; open: boolean }
  | { type: "compare/loaded"; before: ProjectVersion | null; after: ProjectVersion | null }
  | { type: "command/start" }
  | { type: "command/failure"; notice: Notice }
  | { type: "render/queued"; jobId: string; status: string; dispatchedVersion: number | null }
  | {
      type: "render/status";
      status: string;
      videoUrl?: string | null;
      error?: string | null;
      dispatchedVersion?: number | null;
    }
  | { type: "notice/set"; notice: Notice }
  | { type: "notice/clear" };

export function initialStudioState(projectId: string): StudioState {
  return {
    status: "loading",
    projectId,
    head: null,
    versions: [],
    events: [],
    locks: null,
    budget: null,
    humanAcceptance: null,
    selection: null,
    selectionMode: "scene",
    selectedSceneId: null,
    objectDraft: "",
    timeRangeStart: "0",
    timeRangeEnd: "4",
    chat: [],
    proposals: [],
    chatDraft: "",
    sceneTextDraft: "",
    sceneVisualDraft: "",
    sceneCaptionDraft: "",
    sceneVoiceDraft: "",
    sceneDurationDraft: "",
    variantDraft: "",
    compareBefore: null,
    compareAfter: null,
    compareOpen: false,
    compareBeforeVersion: null,
    compareAfterVersion: null,
    pending: false,
    renderJobId: null,
    renderDispatchedVersion: null,
    renderStatus: null,
    videoUrl: null,
    notice: null,
    error: null,
  };
}

function selectionFrom(
  mode: SelectionMode,
  sceneId: string | null,
  objectDraft: string,
  start: string,
  end: string,
): Selection | null {
  if (mode === "scene") {
    return sceneId ? { kind: "scene", scene_ids: [sceneId] } : null;
  }
  if (mode === "object") {
    const ref = objectDraft.trim();
    return ref ? { kind: "object", object_refs: [ref] } : null;
  }
  if (mode === "time_range") {
    const s = Number(start);
    const e = Number(end);
    return Number.isFinite(s) && Number.isFinite(e) && e > s
      ? { kind: "time_range", start_seconds: s, end_seconds: e }
      : null;
  }
  return { kind: "all" };
}

export function studioReducer(state: StudioState, action: Action): StudioState {
  switch (action.type) {
    case "load/start":
      return { ...state, status: "loading", error: null, pending: true };
    case "load/success": {
      const attachedVideo = action.head?.asset_references.find(
        (asset) => asset.kind === "video" && typeof asset.uri === "string",
      );
      const attachedJobMatch = attachedVideo?.asset_id.match(/^job:([0-9a-f]{8}):video$/);
      const clearUnattachedRender = !attachedVideo && state.renderStatus === "completed";
      return {
        ...state,
        status: "ready",
        error: null,
        pending: false,
        head: action.head,
        versions: action.versions,
        events: action.events,
        locks: action.locks,
        budget: action.budget,
        humanAcceptance: action.humanAcceptance ?? null,
        proposals: action.proposals,
        renderStatus: attachedVideo
          ? "completed"
          : clearUnattachedRender
            ? null
            : state.renderStatus,
        renderJobId: attachedVideo
          ? attachedJobMatch?.[1] ?? null
          : clearUnattachedRender
            ? null
            : state.renderJobId,
        renderDispatchedVersion: attachedVideo
          ? action.head?.version_no ?? null
          : clearUnattachedRender
            ? null
            : state.renderDispatchedVersion,
        videoUrl: attachedVideo?.uri ?? (clearUnattachedRender ? null : state.videoUrl),
        ...(state.selectedSceneId && action.head
          ? (() => {
              const selected = action.head.script.segments.find(
                (segment) => segment.id === state.selectedSceneId,
              );
              return selected
                ? {
                    sceneTextDraft: selected.text,
                    sceneVisualDraft: selected.visual_action,
                    sceneCaptionDraft: selected.caption ?? "",
                    sceneVoiceDraft: selected.voice ?? "",
                    sceneDurationDraft:
                      selected.duration_seconds == null
                        ? ""
                        : String(selected.duration_seconds),
                  }
                : {};
            })()
          : {}),
      };
    }
    case "load/failure":
      return {
        ...state,
        status: action.offline ? "offline" : "error",
        error: action.error,
        pending: false,
      };
    case "selection/mode":
      return {
        ...state,
        selectionMode: action.mode,
        selection: selectionFrom(
          action.mode,
          state.selectedSceneId,
          state.objectDraft,
          state.timeRangeStart,
          state.timeRangeEnd,
        ),
      };
    case "selection/scene": {
      const segment =
        state.head?.script.segments.find((s) => s.id === action.sceneId) ?? null;
      return {
        ...state,
        selectionMode: "scene",
        selectedSceneId: action.sceneId,
        selection: { kind: "scene", scene_ids: [action.sceneId] },
        sceneTextDraft: segment ? segment.text : "",
        sceneVisualDraft: segment ? segment.visual_action : "",
        sceneCaptionDraft: segment?.caption ?? "",
        sceneVoiceDraft: segment?.voice ?? "",
        sceneDurationDraft:
          segment?.duration_seconds == null ? "" : String(segment.duration_seconds),
      };
    }
    case "selection/objectDraft":
      return {
        ...state,
        selectionMode: "object",
        objectDraft: action.value,
        selection: selectionFrom(
          "object",
          state.selectedSceneId,
          action.value,
          state.timeRangeStart,
          state.timeRangeEnd,
        ),
      };
    case "selection/timeDraft": {
      const timeRangeStart =
        action.field === "start" ? action.value : state.timeRangeStart;
      const timeRangeEnd =
        action.field === "end" ? action.value : state.timeRangeEnd;
      return {
        ...state,
        selectionMode: "time_range",
        timeRangeStart,
        timeRangeEnd,
        selection: selectionFrom(
          "time_range",
          state.selectedSceneId,
          state.objectDraft,
          timeRangeStart,
          timeRangeEnd,
        ),
      };
    }
    case "selection/clear":
      return { ...state, selection: null, selectedSceneId: null };
    case "chat/draft":
      return { ...state, chatDraft: action.value };
    case "chat/push":
      return { ...state, chat: [...state.chat, action.message] };
    case "chat/resolve":
      return {
        ...state,
        chat: state.chat.map((m) =>
          m.id === action.id
            ? { ...m, text: action.text, meta: action.meta, pending: false }
            : m,
        ),
      };
    case "proposals/set":
      return { ...state, proposals: action.proposals };
    case "proposal/add":
      return {
        ...state,
        proposals: [
          action.proposal,
          ...state.proposals.filter((proposal) => proposal.proposal_id !== action.proposal.proposal_id),
        ],
      };
    case "proposal/update":
      return {
        ...state,
        proposals: state.proposals.map((proposal) =>
          proposal.proposal_id === action.proposal.proposal_id ? action.proposal : proposal,
        ),
      };
    case "scene/draft":
      switch (action.field) {
        case "text":
          return { ...state, sceneTextDraft: action.value };
        case "visual":
          return { ...state, sceneVisualDraft: action.value };
        case "caption":
          return { ...state, sceneCaptionDraft: action.value };
        case "voice":
          return { ...state, sceneVoiceDraft: action.value };
        case "duration":
          return { ...state, sceneDurationDraft: action.value };
      }
    case "variant/draft":
      return { ...state, variantDraft: action.value };
    case "compare/set":
      return action.which === "before"
        ? { ...state, compareBefore: action.versionNo }
        : { ...state, compareAfter: action.versionNo };
    case "compare/toggle":
      return { ...state, compareOpen: action.open };
    case "compare/loaded":
      return {
        ...state,
        compareBeforeVersion: action.before,
        compareAfterVersion: action.after,
      };
    case "command/start":
      return { ...state, pending: true, notice: null };
    case "command/failure":
      return { ...state, pending: false, notice: action.notice };
    case "render/queued":
      return {
        ...state,
        pending: false,
        renderJobId: action.jobId,
        renderDispatchedVersion: action.dispatchedVersion,
        renderStatus: action.status,
        videoUrl: null,
      };
    case "render/status":
      return {
        ...state,
        pending: false,
        renderStatus: action.status,
        ...(action.dispatchedVersion === undefined
          ? {}
          : { renderDispatchedVersion: action.dispatchedVersion }),
        videoUrl: action.videoUrl === undefined ? state.videoUrl : action.videoUrl,
        notice: action.error
          ? { kind: "error", message: action.error }
          : state.notice,
      };
    case "notice/set":
      return { ...state, notice: action.notice };
    case "notice/clear":
      return { ...state, notice: null };
    default:
      return state;
  }
}

function toNotice(err: unknown, locks: LocksResponse | null): Notice {
  if (err instanceof ApiError) {
    if (err.code === "lock_conflict") {
      const scopes = err.conflicts.length ? err.conflicts : ["this scope"];
      const reasons = err.lockReasons;
      const described = scopes
        .map((scope) => {
          const reason =
            reasons[scope] ?? locks?.scopes?.[scope]?.reason ?? null;
          return reason ? `${scope} (${reason})` : scope;
        })
        .join(", ");
      return {
        kind: "locked",
        message: `Edit blocked — locked scope: ${described}. Unlock it or edit a different scope.`,
      };
    }
    if (err.code === "stale_version") {
      return {
        kind: "stale",
        message: `Stale version — ${err.message} Re-select the current head and retry.`,
      };
    }
    if (err.code === "chat_mapping_failed") {
      return {
        kind: "error",
        message: `Chat could not map that to one command: ${err.message}`,
      };
    }
    return { kind: "error", message: err.message };
  }
  if (typeof navigator !== "undefined" && !navigator.onLine) {
    return { kind: "error", message: "You appear to be offline. Reconnect and retry." };
  }
  return {
    kind: "error",
    message: err instanceof Error ? err.message : "Something went wrong.",
  };
}

export interface ProjectStudioController extends StudioState {
  selectionDescription: string;
  chatContextText: string;
  reload: () => Promise<void>;
  setSelectionMode: (mode: SelectionMode) => void;
  selectScene: (sceneId: string) => void;
  setObjectDraft: (value: string) => void;
  setTimeDraft: (field: "start" | "end", value: string) => void;
  clearSelection: () => void;
  setChatDraft: (value: string) => void;
  sendChatMessage: () => Promise<void>;
  approveProposal: (proposalId: string) => Promise<void>;
  rejectProposal: (proposalId: string) => Promise<void>;
  setSceneDraft: (
    field: "text" | "visual" | "caption" | "voice" | "duration",
    value: string,
  ) => void;
  applySceneEdit: (
    field: "text" | "visual" | "caption" | "voice" | "duration",
  ) => Promise<void>;
  regenerateScene: () => Promise<void>;
  setVariantDraft: (value: string) => void;
  saveVariant: () => Promise<void>;
  undo: (targetVersion: number) => Promise<void>;
  toggleLock: (scope: CommandScope, locked: boolean) => Promise<void>;
  toggleSceneLock: (scope: CommandScope, locked: boolean) => Promise<void>;
  setCompare: (which: "before" | "after", versionNo: number | null) => void;
  openComparison: () => Promise<void>;
  closeComparison: () => void;
  saveToBrand: () => void;
  setHumanAcceptance: (
    accepted: boolean,
    automatedQa?: Record<string, unknown> | null,
    note?: string,
  ) => Promise<void>;
  setProjectBudget: (budgetUsd: number) => Promise<void>;
  startRender: (args: {
    approvedSpendUsd: number;
    aspectRatio: "9:16" | "16:9" | "1:1";
    reducedMotion: boolean;
  }) => Promise<void>;
  dismissNotice: () => void;
}

export function useProjectStudio(projectId: string): ProjectStudioController {
  const [state, dispatch] = useReducer(
    studioReducer,
    projectId,
    initialStudioState,
  );

  const refresh = useCallback(async (): Promise<ProjectVersion | null> => {
    const versionsRes = await listVersions(projectId);
    const headNo = versionsRes.head;
    const head =
      headNo >= 1 ? (await getVersion(projectId, headNo)).version : null;
    const [eventsRes, locks, budgetRes, proposalsRes, acceptanceRes] = await Promise.all([
      listEvents(projectId),
      getLocks(projectId),
      getBudget(projectId),
      listProposals(projectId),
      head ? getProjectAcceptance(projectId, head.version_no) : Promise.resolve(null),
    ]);
    dispatch({
      type: "load/success",
      head,
      versions: versionsRes.versions,
      events: eventsRes.events,
      locks,
      budget: budgetRes.budget,
      proposals: proposalsRes.proposals,
      humanAcceptance: acceptanceRes,
    });
    return head;
  }, [projectId]);

  useEffect(() => {
    let active = true;
    dispatch({ type: "load/start" });
    refresh().catch((err: unknown) => {
      if (!active) return;
      const offline =
        (typeof navigator !== "undefined" && !navigator.onLine) ||
        !(err instanceof ApiError);
      dispatch({
        type: "load/failure",
        error: err instanceof Error ? err.message : "Failed to load project",
        offline,
      });
    });
    return () => {
      active = false;
    };
  }, [refresh]);

  useEffect(() => {
    const jobId = state.renderJobId;
    const activeStatuses = new Set([
      "queued",
      "visuals",
      "voice",
      "rendering",
      "qa",
      "retrying",
    ]);
    if (!jobId || !state.renderStatus || !activeStatuses.has(state.renderStatus)) return;
    let cancelled = false;
    const timer = window.setInterval(() => {
      void getVideoJobStatus(jobId)
        .then(async (job) => {
          if (cancelled) return;
          if (job.status === "completed") {
            window.clearInterval(timer);
            // A job can finish after its originating project version has gone
            // stale.  Refresh first and only expose a video when the backend
            // attached this exact job to the current project head.
            const refreshedHead = await refresh();
            if (cancelled) return;
            const attached = refreshedHead?.asset_references.some(
              (asset) =>
                asset.asset_id === `job:${jobId}:video` && typeof asset.uri === "string",
            );
            if (attached) {
              dispatch({
                type: "render/status",
                status: "completed",
                dispatchedVersion: refreshedHead?.version_no ?? null,
                videoUrl: job.video_url ?? `/api/jobs/${jobId}/video`,
              });
              dispatch({
                type: "notice/set",
                notice: { kind: "success", message: "Render completed and attached to this project." },
              });
            } else {
              dispatch({
                type: "render/status",
                status: "needs_attention",
                dispatchedVersion: null,
                videoUrl: null,
                error:
                  "Render completed for an earlier project version and was kept isolated; no current preview was changed.",
              });
            }
          } else if (["failed", "cancelled", "needs_attention", "needs_human_review"].includes(job.status)) {
            window.clearInterval(timer);
            dispatch({
              type: "render/status",
              status: job.status,
              dispatchedVersion: null,
              videoUrl: null,
              error:
                job.error ??
                (job.status === "needs_human_review"
                  ? "Render needs human review before it can be approved."
                  : job.status === "needs_attention"
                    ? "Render completed outside the current project head; no current preview was changed."
                  : `Render ${job.status}.`),
            });
          } else {
            dispatch({ type: "render/status", status: job.status });
          }
        })
        .catch((err: unknown) => {
          if (!cancelled) {
            dispatch({
              type: "notice/set",
              notice: {
                kind: "partial",
                message:
                  (err instanceof Error ? err.message : "Could not read render status.") +
                  " Polling will retry.",
              },
            });
          }
        });
    }, 2000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [refresh, state.renderJobId, state.renderStatus]);

  async function runMutation(fn: () => Promise<void>, success?: Notice) {
    dispatch({ type: "command/start" });
    try {
      await fn();
    } catch (err) {
      dispatch({ type: "command/failure", notice: toNotice(err, state.locks) });
      return;
    }
    try {
      await refresh();
      if (success) dispatch({ type: "notice/set", notice: success });
    } catch {
      dispatch({
        type: "notice/set",
        notice: {
          kind: "partial",
          message:
            "The change was saved, but the studio could not refresh. Reload to see the latest version.",
        },
      });
    }
  }

  async function sendChatMessage() {
    const message = state.chatDraft.trim();
    if (!message || !state.head) return;
    const pendingId = newIdempotencyKey("proposal");
    dispatch({
      type: "chat/push",
      message: { id: newIdempotencyKey("msg"), role: "user", text: message },
    });
    dispatch({
      type: "chat/push",
      message: {
        id: pendingId,
        role: "assistant",
        text: "Mapping your note to one command…",
        pending: true,
      },
    });
    dispatch({ type: "chat/draft", value: "" });
    dispatch({ type: "command/start" });
    try {
      const res = await proposeChat(projectId, {
        message,
        selection: state.selection,
        baseVersion: state.head.version_no,
        idempotencyKey: pendingId,
      });
      dispatch({ type: "proposal/add", proposal: res.proposal });
      await refresh();
      dispatch({
        type: "chat/resolve",
        id: pendingId,
        text: `Proposed change ready for review: ${res.diff.summary}`,
        meta: `${res.command.operation} · pending approval`,
      });
      dispatch({
        type: "notice/set",
        notice: {
          kind: "info",
          message: "Chat prepared a proposed change. Review its affected fields, then Approve or Reject.",
        },
      });
    } catch (err) {
      const notice = toNotice(err, state.locks);
      dispatch({ type: "chat/resolve", id: pendingId, text: notice.message, meta: "blocked" });
      dispatch({ type: "command/failure", notice });
    }
  }

  async function approveChatProposal(proposalId: string) {
    const proposal = state.proposals.find((item) => item.proposal_id === proposalId);
    if (!proposal || proposal.status !== "proposed") return;
    dispatch({ type: "command/start" });
    try {
      const res = await approveProposal(projectId, proposalId);
      await refresh();
      dispatch({ type: "proposal/update", proposal: res.proposal });
      dispatch({
        type: "notice/set",
        notice: {
          kind: "success",
          message: `Approved ${proposal.command.operation}; canonical head is v${res.target_version ?? res.proposal.target_version}.`,
        },
      });
    } catch (err) {
      dispatch({ type: "command/failure", notice: toNotice(err, state.locks) });
    }
  }

  async function rejectChatProposal(proposalId: string) {
    const proposal = state.proposals.find((item) => item.proposal_id === proposalId);
    if (!proposal || proposal.status !== "proposed") return;
    dispatch({ type: "command/start" });
    try {
      const res = await rejectProposal(projectId, proposalId);
      await refresh();
      dispatch({ type: "proposal/update", proposal: res.proposal });
      dispatch({
        type: "notice/set",
        notice: { kind: "info", message: "Proposed change rejected; the project head was not changed." },
      });
    } catch (err) {
      dispatch({ type: "command/failure", notice: toNotice(err, state.locks) });
    }
  }

  async function applySceneEdit(
    field: "text" | "visual" | "caption" | "voice" | "duration",
  ) {
    const head = state.head;
    const sceneId = state.selectedSceneId;
    if (!head || !sceneId) {
      dispatch({
        type: "notice/set",
        notice: { kind: "error", message: "Select a scene on the storyboard first." },
      });
      return;
    }
    const segment = head.script.segments.find((s) => s.id === sceneId);
    if (!segment) return;

    const textValue =
      field === "text"
        ? state.sceneTextDraft.trim()
        : field === "visual"
          ? state.sceneVisualDraft.trim()
          : field === "caption"
            ? state.sceneCaptionDraft.trim()
            : field === "voice"
              ? state.sceneVoiceDraft.trim()
              : state.sceneDurationDraft.trim();
    if (!textValue) {
      dispatch({
        type: "notice/set",
        notice: { kind: "error", message: "Enter the new value before applying." },
      });
      return;
    }

    const edit: Parameters<typeof editScene>[1] = {
      base_version: head.version_no,
      scene_ids: [sceneId],
      actor: "canvas",
      idempotency_key: newIdempotencyKey("canvas"),
    };
    if (field === "duration") {
      const duration = Number(textValue);
      if (!Number.isFinite(duration) || duration <= 0) {
        dispatch({
          type: "notice/set",
          notice: { kind: "error", message: "Duration must be a finite number greater than zero." },
        });
        return;
      }
      edit.duration_seconds = duration;
    } else if (field === "text") {
      edit.text = textValue;
    } else if (field === "visual") {
      edit.visual_action = textValue;
    } else if (field === "caption") {
      edit.caption = textValue;
    } else {
      edit.voice = textValue;
    }
    await runMutation(
      () => editScene(projectId, edit).then(() => undefined),
      {
        kind: "success",
        message: `Canvas applied a ${field} edit to ${sceneId} — chat sees the same version.`,
      },
    );
  }

  async function regenerateScene() {
    const head = state.head;
    const sceneId = state.selectedSceneId;
    if (!head || !sceneId) {
      dispatch({
        type: "notice/set",
        notice: { kind: "error", message: "Select a scene on the storyboard first." },
      });
      return;
    }
    await runMutation(async () => {
      const response = await regenerateScenes(projectId, {
        base_version: head.version_no,
        scene_ids: [sceneId],
        actor: "creative-director",
        idempotency_key: newIdempotencyKey("regenerate-scene"),
      });
      dispatch({
        type: "notice/set",
        notice:
          response.status === "blocked"
            ? {
                kind: "locked",
                message: `Regeneration blocked by locked descendants: ${response.plan.blocked.join(", ")}.`,
              }
            : {
                kind: "info",
                message: `Regeneration planned from exact head v${response.base_version}; only selected-scene descendants are dirty.`,
              },
      });
    });
  }

  async function saveVariant() {
    const name = state.variantDraft.trim();
    if (!name) {
      dispatch({
        type: "notice/set",
        notice: { kind: "error", message: "Name the variant before saving." },
      });
      return;
    }
    await runMutation(
      () =>
        createVariant(projectId, name, state.head?.version_no).then(() => undefined),
      {
        kind: "success",
        message: `Saved named variant "${name}" as a server version — it survives reload.`,
      },
    );
    dispatch({ type: "variant/draft", value: "" });
  }

  async function undo(targetVersion: number) {
    await runMutation(
      () =>
        undoToVersion(projectId, targetVersion, "creative-director", state.head?.version_no).then(
          () => undefined,
        ),
      {
        kind: "info",
        message: `Undo complete — content of v${targetVersion} restored as a NEW version. Nothing was deleted.`,
      },
    );
  }

  async function toggleLock(scope: CommandScope, locked: boolean) {
    await runMutation(
      () =>
        setLock(projectId, scope, locked, {
          reason: locked ? "Locked by Creative Director" : undefined,
        }).then(() => undefined),
      {
        kind: locked ? "info" : "success",
        message: locked ? `Locked ${scope} edits.` : `Unlocked ${scope} edits.`,
      },
    );
  }

  async function toggleSceneLock(scope: CommandScope, locked: boolean) {
    const sceneId = state.selectedSceneId;
    if (!sceneId) {
      dispatch({
        type: "notice/set",
        notice: { kind: "error", message: "Select a scene before changing a scene lock." },
      });
      return;
    }
    await runMutation(
      () =>
        setLock(projectId, scope, locked, {
          sceneId,
          reason: locked ? "Locked by Creative Director" : undefined,
        }).then(() => undefined),
      {
        kind: locked ? "info" : "success",
        message: locked
          ? `Locked ${scope} edits for ${sceneId}.`
          : `Unlocked ${scope} edits for ${sceneId}.`,
      },
    );
  }

  async function openComparison() {
    dispatch({ type: "compare/toggle", open: true });
    const before = state.compareBefore;
    const after = state.compareAfter;
    if (before == null || after == null) {
      dispatch({ type: "compare/loaded", before: null, after: null });
      return;
    }
    try {
      const [b, a] = await Promise.all([
        getVersion(projectId, before),
        getVersion(projectId, after),
      ]);
      dispatch({ type: "compare/loaded", before: b.version, after: a.version });
    } catch (err) {
      dispatch({ type: "notice/set", notice: toNotice(err, state.locks) });
    }
  }

  function saveToBrand() {
    const head = state.head;
    if (!head) return;
    const preference = {
      language: head.script.language,
      cta_text: head.script.cta_text ?? null,
      aspect_ratio: head.script.aspect_ratio ?? null,
      style_applied: head.script.style_applied ?? null,
      saved_at: new Date().toISOString(),
    };
    try {
      if (typeof window !== "undefined") {
        window.localStorage.setItem(BRAND_PREF_KEY, JSON.stringify(preference));
      }
      dispatch({
        type: "notice/set",
        notice: {
          kind: "success",
          message:
            "Saved to Brand — this preference now travels to other projects by your explicit action only.",
        },
      });
    } catch {
      dispatch({
        type: "notice/set",
        notice: { kind: "error", message: "Could not write to Brand storage." },
      });
    }
  }

  async function setHumanAcceptance(
    accepted: boolean,
    automatedQa: Record<string, unknown> | null = null,
    note?: string,
  ) {
    const head = state.head;
    if (!head) return;
    await runMutation(
      () =>
        setProjectAcceptance(projectId, head.version_no, {
          accepted,
          automatedQa,
          note,
        }).then(() => undefined),
      {
        kind: accepted ? "success" : "info",
        message: accepted
          ? `Version ${head.version_no} accepted; readiness still requires current verified media.`
          : `Version ${head.version_no} rejected; downloads remain blocked.`,
      },
    );
  }

  async function setProjectBudget(budgetUsd: number) {
    if (!Number.isFinite(budgetUsd) || budgetUsd < 0) {
      dispatch({
        type: "notice/set",
        notice: { kind: "error", message: "Project budget must be a finite non-negative amount." },
      });
      return;
    }
    await runMutation(
      () => setProjectBudgetApi(projectId, budgetUsd).then(() => undefined),
      {
        kind: "success",
        message: `Project budget updated to $${budgetUsd.toFixed(2)}; account caps still apply.`,
      },
    );
  }

  async function startRender(args: {
    approvedSpendUsd: number;
    aspectRatio: "9:16" | "16:9" | "1:1";
    reducedMotion: boolean;
  }) {
    if (!state.head) return;
    if (!Number.isFinite(args.approvedSpendUsd) || args.approvedSpendUsd <= 0) {
      dispatch({
        type: "notice/set",
        notice: { kind: "error", message: "Enter a valid positive budget before approval." },
      });
      return;
    }
    dispatch({ type: "command/start" });
    try {
      const response = await renderProject(
        projectId,
        {
          baseVersion: state.head.version_no,
          approvedSpendUsd: args.approvedSpendUsd,
          aspectRatio: args.aspectRatio,
          reducedMotion: args.reducedMotion,
          ctaText: state.head.script.cta_text ?? "",
          retentionProgressBar: state.head.script.retention_progress_bar ?? true,
          animatedLowerThirds: state.head.script.animated_lower_thirds ?? true,
        },
        newIdempotencyKey("studio-render"),
      );
      dispatch({
        type: "render/queued",
        jobId: response.job_id,
        status: "queued",
        dispatchedVersion: response.dispatched_version,
      });
      await refresh();
      dispatch({
        type: "notice/set",
        notice: {
          kind: "info",
          message: `Render queued from v${response.dispatched_version}. Estimated cost $${response.estimated_cost_usd.toFixed(2)}; approved ceiling $${response.approved_spend_usd.toFixed(2)}.`,
        },
      });
    } catch (err) {
      dispatch({ type: "command/failure", notice: toNotice(err, state.locks) });
    }
  }

  const chatContextScene =
    state.selectedSceneId && state.head
      ? state.head.script.segments.find((s) => s.id === state.selectedSceneId) ?? null
      : null;

  return {
    ...state,
    selectionDescription: describeSelection(state.selection),
    chatContextText: chatContextScene
      ? `${chatContextScene.id}: ${chatContextScene.text}`
      : state.head
        ? `${state.head.script.title} — v${state.head.version_no}`
        : "Loading project…",
    reload: async () => {
      await refresh();
    },
    setSelectionMode: (mode) => dispatch({ type: "selection/mode", mode }),
    selectScene: (sceneId) => dispatch({ type: "selection/scene", sceneId }),
    setObjectDraft: (value) => dispatch({ type: "selection/objectDraft", value }),
    setTimeDraft: (field, value) =>
      dispatch({ type: "selection/timeDraft", field, value }),
    clearSelection: () => dispatch({ type: "selection/clear" }),
    setChatDraft: (value) => dispatch({ type: "chat/draft", value }),
    sendChatMessage,
    approveProposal: approveChatProposal,
    rejectProposal: rejectChatProposal,
    setSceneDraft: (field, value) => dispatch({ type: "scene/draft", field, value }),
    applySceneEdit,
    regenerateScene,
    setVariantDraft: (value) => dispatch({ type: "variant/draft", value }),
    saveVariant,
    undo,
    toggleLock,
    toggleSceneLock,
    setCompare: (which, versionNo) =>
      dispatch({ type: "compare/set", which, versionNo }),
    openComparison,
    closeComparison: () => dispatch({ type: "compare/toggle", open: false }),
    saveToBrand,
    setHumanAcceptance,
    setProjectBudget,
    startRender,
    dismissNotice: () => dispatch({ type: "notice/clear" }),
  };
}
