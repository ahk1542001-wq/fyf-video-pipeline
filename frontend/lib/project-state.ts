// Stage C-II Studio state: ONE canonical project version shared by chat + canvas.
//
// This module owns the reducer + the `useProjectStudio` controller hook. Neither
// pane keeps a private authoritative copy of the project: every mutation goes to
// the backend (POST /api/projects/{id}/commands | /chat | /undo | /variants |
// /locks | /results) and the canonical head is re-read afterwards. The chat
// transcript is held SEPARATELY from the versioned decision record (versions /
// events / locks), and cross-project preferences travel only via the explicit
// `saveToBrand()` action - never implicitly.

"use client";

import { useCallback, useEffect, useReducer } from "react";

import {
  ApiError,
  applyCommand,
  createVariant,
  describeSelection,
  getBudget,
  getVersion,
  getLocks,
  listEvents,
  listVersions,
  newIdempotencyKey,
  sendChat,
  setLock,
  undoToVersion,
  type BudgetStatus,
  type CommandScope,
  type LocksResponse,
  type ProjectCommand,
  type ProjectVersion,
  type ScriptSegment,
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
  selection: Selection | null;
  selectionMode: SelectionMode;
  selectedSceneId: string | null;
  objectDraft: string;
  timeRangeStart: string;
  timeRangeEnd: string;
  chat: ChatMessage[];
  chatDraft: string;
  sceneTextDraft: string;
  sceneVisualDraft: string;
  variantDraft: string;
  compareBefore: number | null;
  compareAfter: number | null;
  compareOpen: boolean;
  compareBeforeVersion: ProjectVersion | null;
  compareAfterVersion: ProjectVersion | null;
  pending: boolean;
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
  | { type: "scene/draft"; field: "text" | "visual"; value: string }
  | { type: "variant/draft"; value: string }
  | { type: "compare/set"; which: "before" | "after"; versionNo: number | null }
  | { type: "compare/toggle"; open: boolean }
  | { type: "compare/loaded"; before: ProjectVersion | null; after: ProjectVersion | null }
  | { type: "command/start" }
  | { type: "command/failure"; notice: Notice }
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
    selection: null,
    selectionMode: "scene",
    selectedSceneId: null,
    objectDraft: "",
    timeRangeStart: "0",
    timeRangeEnd: "4",
    chat: [],
    chatDraft: "",
    sceneTextDraft: "",
    sceneVisualDraft: "",
    variantDraft: "",
    compareBefore: null,
    compareAfter: null,
    compareOpen: false,
    compareBeforeVersion: null,
    compareAfterVersion: null,
    pending: false,
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
    case "load/success":
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
      };
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
    case "scene/draft":
      return action.field === "text"
        ? { ...state, sceneTextDraft: action.value }
        : { ...state, sceneVisualDraft: action.value };
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
  setSceneDraft: (field: "text" | "visual", value: string) => void;
  applySceneEdit: (field: "text" | "visual") => Promise<void>;
  setVariantDraft: (value: string) => void;
  saveVariant: () => Promise<void>;
  undo: (targetVersion: number) => Promise<void>;
  toggleLock: (scope: CommandScope, locked: boolean) => Promise<void>;
  setCompare: (which: "before" | "after", versionNo: number | null) => void;
  openComparison: () => Promise<void>;
  closeComparison: () => void;
  saveToBrand: () => void;
  dismissNotice: () => void;
}

export function useProjectStudio(projectId: string): ProjectStudioController {
  const [state, dispatch] = useReducer(
    studioReducer,
    projectId,
    initialStudioState,
  );

  const refresh = useCallback(async () => {
    const versionsRes = await listVersions(projectId);
    const headNo = versionsRes.head;
    const head =
      headNo >= 1 ? (await getVersion(projectId, headNo)).version : null;
    const [eventsRes, locks, budgetRes] = await Promise.all([
      listEvents(projectId),
      getLocks(projectId),
      getBudget(),
    ]);
    dispatch({
      type: "load/success",
      head,
      versions: versionsRes.versions,
      events: eventsRes.events,
      locks,
      budget: budgetRes.budget,
    });
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
    const pendingId = newIdempotencyKey("msg");
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
      const res = await sendChat(projectId, {
        message,
        selection: state.selection,
        baseVersion: state.head.version_no,
      });
      await refresh();
      dispatch({
        type: "chat/resolve",
        id: pendingId,
        text: res.summary,
        meta: `${res.operation} · now v${res.version.version_no}`,
      });
      dispatch({
        type: "notice/set",
        notice: {
          kind: "success",
          message: `Chat applied ${res.operation} — canonical head is v${res.version.version_no}.`,
        },
      });
    } catch (err) {
      const notice = toNotice(err, state.locks);
      dispatch({ type: "chat/resolve", id: pendingId, text: notice.message, meta: "blocked" });
      dispatch({ type: "command/failure", notice });
    }
  }

  async function applySceneEdit(field: "text" | "visual") {
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
    const value = (field === "text" ? state.sceneTextDraft : state.sceneVisualDraft).trim();
    if (!value) {
      dispatch({
        type: "notice/set",
        notice: { kind: "error", message: "Enter the new value before applying." },
      });
      return;
    }
    const updated: ScriptSegment =
      field === "text"
        ? { ...segment, text: value }
        : { ...segment, visual_action: value };
    const command: ProjectCommand = {
      project_id: projectId,
      base_version: head.version_no,
      actor: "canvas",
      operation: field === "text" ? "edit_script" : "edit_visual",
      selection: { kind: "scene", scene_ids: [sceneId] },
      payload: { kind: "script_segments", segments: [updated] },
      idempotency_key: newIdempotencyKey("canvas"),
    };
    await runMutation(
      () => applyCommand(projectId, command).then(() => undefined),
      {
        kind: "success",
        message: `Canvas applied a ${field} edit to ${sceneId} — chat sees the same version.`,
      },
    );
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
      () => createVariant(projectId, name).then(() => undefined),
      {
        kind: "success",
        message: `Saved named variant "${name}" as a server version — it survives reload.`,
      },
    );
    dispatch({ type: "variant/draft", value: "" });
  }

  async function undo(targetVersion: number) {
    await runMutation(
      () => undoToVersion(projectId, targetVersion).then(() => undefined),
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
    reload: refresh,
    setSelectionMode: (mode) => dispatch({ type: "selection/mode", mode }),
    selectScene: (sceneId) => dispatch({ type: "selection/scene", sceneId }),
    setObjectDraft: (value) => dispatch({ type: "selection/objectDraft", value }),
    setTimeDraft: (field, value) =>
      dispatch({ type: "selection/timeDraft", field, value }),
    clearSelection: () => dispatch({ type: "selection/clear" }),
    setChatDraft: (value) => dispatch({ type: "chat/draft", value }),
    sendChatMessage,
    setSceneDraft: (field, value) => dispatch({ type: "scene/draft", field, value }),
    applySceneEdit,
    setVariantDraft: (value) => dispatch({ type: "variant/draft", value }),
    saveVariant,
    undo,
    toggleLock,
    setCompare: (which, versionNo) =>
      dispatch({ type: "compare/set", which, versionNo }),
    openComparison,
    closeComparison: () => dispatch({ type: "compare/toggle", open: false }),
    saveToBrand,
    dismissNotice: () => dispatch({ type: "notice/clear" }),
  };
}
