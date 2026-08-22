"use client";

import { useState, useRef, useEffect } from "react";
import StudioHeader from "../components/studio-header";
import WorkflowStrip from "../components/workflow-strip";
import {
  API_URL,
  deriveWorkflowStages,
  isRuntimeInfo,
  STATIC_RUNTIME_FALLBACK,
  voiceProviderLabel,
  type RuntimeInfo,
  type VoiceProvider,
} from "../lib/video-ui";

interface VideoScript {
  title: string;
  language: "my-MM";
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
interface StoryVariant { name: string; script: VideoScript; }

interface VideoStyleOption {
  id: string;
  name: string;
  description: string;
}

const DEFAULT_STYLES: VideoStyleOption[] = [
  { id: "fyf_explainer", name: "FYF Explainer (Default)", description: "Standard high-clarity whiteboard with balanced mascot pacing." },
  { id: "cinematic_continuity", name: "Cinematic Continuity", description: "Dramatic push-ins and smooth continuous camera motion." },
  { id: "evidence_story", name: "Evidence Story", description: "Documentary inspection focus with high data fidelity." },
];

type JobStatus = "idle" | "queued" | "visuals" | "voice" | "rendering" | "qa" | "completed" | "failed";
type VisualCacheState = "producer" | "waiting" | "hit" | "miss";
type VisualProgress = {
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

async function fetchWithDeadline(
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

export default function Home() {
  const [topic, setTopic] = useState("");
  const durationMode = "short";
  const [availableStyles, setAvailableStyles] = useState<VideoStyleOption[]>(DEFAULT_STYLES);
  const [selectedStyle, setSelectedStyle] = useState<string>("fyf_explainer");
  const [script, setScript] = useState<VideoScript | null>(null);
  const [variants, setVariants] = useState<StoryVariant[]>([]);
  const [selectedVariant, setSelectedVariant] = useState<number | null>(null);
  const [scriptLocked, setScriptLocked] = useState(false);
  const [scriptLockId, setScriptLockId] = useState<string | null>(null);
  const [storyModel, setStoryModel] = useState<string | null>(null);
  const [videoUrl, setVideoUrl] = useState<string | null>(null);
  const [writingStatus, setWritingStatus] = useState<"idle" | "writing" | "done" | "needs_attention" | "error">("idle");
  const [resumableScriptJobId, setResumableScriptJobId] = useState<string | null>(null);
  const [scriptProgress, setScriptProgress] = useState("Waiting for the script worker…");
  const [renderStatus, setRenderStatus] = useState<JobStatus>("idle");
  const [visualProgress, setVisualProgress] = useState<VisualProgress | null>(null);
  const [renderProgress, setRenderProgress] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [runtime, setRuntime] = useState<RuntimeInfo>(STATIC_RUNTIME_FALLBACK);
  const [runtimeSource, setRuntimeSource] = useState<"api" | "fallback">("fallback");
  const [generationAccessToken, setGenerationAccessToken] = useState(() => (
    typeof window === "undefined" ? "" : window.sessionStorage.getItem("fyf-generation-access") || ""
  ));

  const activeVideoControllerRef = useRef<AbortController | null>(null);
  const activeStoryActionRef = useRef(false);
  const effectiveVoiceProvider: VoiceProvider = "gemini";

  useEffect(() => {
    return () => {
      if (activeVideoControllerRef.current) {
        activeVideoControllerRef.current.abort();
        activeVideoControllerRef.current = null;
      }
    };
  }, []);

  useEffect(() => {
    let mounted = true;
    async function loadRuntime() {
      try {
        const response = await fetchWithDeadline(`${API_URL}/api/runtime`);
        const data: unknown = await response.json();
        if (!response.ok || !isRuntimeInfo(data)) throw new Error("Runtime API unavailable");
        if (mounted) {
          setRuntime(data);
          setRuntimeSource("api");
        }
      } catch {
        if (mounted) {
          setRuntime(STATIC_RUNTIME_FALLBACK);
          setRuntimeSource("fallback");
        }
      }
    }

    async function loadStyles() {
      try {
        const res = await fetchWithDeadline(`${API_URL}/api/video-styles`);
        if (res.ok) {
          const data: unknown = await res.json();
          if (mounted && isRecord(data) && Array.isArray(data.styles) && data.styles.length > 0) {
            setAvailableStyles(data.styles as VideoStyleOption[]);
          }
        }
      } catch {
        // Keep default styles on fallback
      }
    }

    void loadRuntime();
    void loadStyles();
    return () => {
      mounted = false;
    };
  }, []);

  function isRecord(val: unknown): val is Record<string, unknown> {
    return typeof val === "object" && val !== null;
  }

  function isVideoScript(val: unknown): val is VideoScript {
    if (!isRecord(val)) return false;
    if (typeof val.title !== "string" || val.language !== "my-MM") return false;
    if (!Array.isArray(val.segments)) return false;

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

  const hasCompletedVideo = renderStatus === "completed" && Boolean(videoUrl);
  const generationReady = runtimeSource === "api"
    && runtime.generation_available
    && (!runtime.generation_access_required || Boolean(generationAccessToken.trim()));
  const workflowStages = deriveWorkflowStages({
    hasSource: topic.trim().length > 0,
    hasStory: Boolean(script || variants.length > 0),
    narrationLocked: scriptLocked,
    hasCompletedVideo,
  });
  const renderBusy = ["queued", "visuals", "voice", "rendering", "qa"].includes(renderStatus);

  function generationRequestHeaders(): HeadersInit {
    const headers: Record<string, string> = { "Content-Type": "application/json" };
    if (generationAccessToken.trim()) {
      headers["X-FYF-Access-Token"] = generationAccessToken.trim();
    }
    return headers;
  }

  function updateGenerationAccessToken(value: string) {
    setGenerationAccessToken(value);
    if (value.trim()) {
      window.sessionStorage.setItem("fyf-generation-access", value);
    } else {
      window.sessionStorage.removeItem("fyf-generation-access");
    }
  }

  async function pollScriptJob(jobId: string) {
    const startTime = Date.now();
    while (Date.now() - startTime < 45 * 60 * 1000) {
      const statusRes = await fetchWithDeadline(`${API_URL}/api/script-jobs/${jobId}/status`);
      if (!statusRes.ok) throw new Error("Could not check script job status");
      const job: unknown = await statusRes.json();
      if (!isRecord(job) || typeof job.status !== "string") throw new Error("Malformed script job payload");

      const progress = typeof job.progress === "number" ? ` (${job.progress}%)` : "";
      if (job.stage === "adk_producer") {
        setScriptProgress(`Google ADK Producer Agent running…${progress}`);
      } else if (job.stage === "narration") {
        setScriptProgress(`Writing narration with Vertex…${progress}`);
      } else if (job.stage === "storyboard" || job.stage === "visual_lock") {
        const batch = typeof job.batch === "number" ? job.batch : 1;
        const count = typeof job.batch_count === "number" ? job.batch_count : "?";
        setScriptProgress(`Building visual story batch ${batch}/${count}…${progress}`);
      } else if (job.stage === "retrying") {
        setScriptProgress("Retrying from the latest saved checkpoint…");
      } else {
        setScriptProgress(`Preparing the script job…${progress}`);
      }

      if (job.status === "completed" && isVideoScript(job.data) && typeof job.lock_id === "string" && /^[0-9a-f]{8}$/.test(job.lock_id)) {
        setScript(job.data);
        setScriptLockId(job.lock_id);
        setScriptLocked(true);
        setWritingStatus("done");
        setResumableScriptJobId(null);
        setScriptProgress("Script locked and ready.");
        return;
      }

      if (job.status === "needs_attention") {
        setWritingStatus("needs_attention");
        setResumableScriptJobId(jobId);
        setScriptProgress(typeof job.error === "string" ? job.error : "Temporary provider issue. Checkpoint saved.");
        return;
      }

      if (job.status === "failed") {
        throw new Error(typeof job.error === "string" ? job.error : "Script production failed");
      }

      if (!["queued", "writing", "retrying"].includes(job.status)) {
        throw new Error("Unknown script job status");
      }

      await new Promise(resolve => setTimeout(resolve, 3000));
    }
    throw new Error("Script production timed out after 45 minutes");
  }

  async function generateScript() {
    if (!topic.trim() || !generationReady || activeStoryActionRef.current) return;
    activeStoryActionRef.current = true;

    if (activeVideoControllerRef.current) {
      activeVideoControllerRef.current.abort();
      activeVideoControllerRef.current = null;
    }

    setWritingStatus("writing");
    setScriptProgress("Queuing a resumable Vertex script job…");
    setRenderStatus("idle");
    setScript(null);
    setScriptLocked(false);
    setScriptLockId(null);
    setVideoUrl(null);
    setError(null);
    setResumableScriptJobId(null);

    try {
      const res = await fetchWithDeadline(`${API_URL}/api/generate-script`, {
        method: "POST",
        headers: generationRequestHeaders(),
        body: JSON.stringify({ topic, duration_mode: durationMode, style: selectedStyle }),
      }, 20_000);
      let data: unknown;
      try {
        data = await res.json();
      } catch {
        throw new Error(`Failed to parse response: ${res.statusText}`);
      }

      if (!res.ok) {
        const errorMsg = isRecord(data) ? (data.detail || data.error) : undefined;
        throw new Error(typeof errorMsg === "string" ? errorMsg : "Script generation request failed");
      }

      if (isRecord(data) && typeof data.job_id === "string") {
        const jobId = data.job_id;
        await pollScriptJob(jobId);
      } else {
        setError("Invalid response format from script generation");
        setWritingStatus("error");
      }
    } catch (err) {
      const e = err as Error;
      setError(e.message || "Cannot reach backend. Is FastAPI running?");
      setWritingStatus("error");
    } finally {
      activeStoryActionRef.current = false;
    }
  }

  async function resumeScriptJob(jobId: string) {
    if (!generationReady || activeStoryActionRef.current) return;
    activeStoryActionRef.current = true;
    setWritingStatus("writing");
    setScriptProgress("Resuming script job from preserved checkpoint…");
    setError(null);
    try {
      const res = await fetchWithDeadline(`${API_URL}/api/script-jobs/${jobId}/resume`, {
        method: "POST",
        headers: generationRequestHeaders(),
      }, 20_000);
      const data: unknown = await res.json();
      if (!res.ok) {
        const detail = isRecord(data) && typeof data.detail === "string" ? data.detail : "Failed to resume script job";
        throw new Error(detail);
      }
      await pollScriptJob(jobId);
    } catch (err) {
      setError((err as Error).message);
      setWritingStatus("error");
    } finally {
      activeStoryActionRef.current = false;
    }
  }

  async function polishStory() {
    if (!topic.trim() || !generationReady || activeStoryActionRef.current) return;
    activeStoryActionRef.current = true;
    setWritingStatus("writing");
    setError(null);
    setScript(null);
    setVariants([]);
    setSelectedVariant(null);
    setScriptLocked(false);
    setScriptLockId(null);
    setStoryModel(null);
    setResumableScriptJobId(null);

    try {
      const res = await fetchWithDeadline(`${API_URL}/api/story-polish`, {
        method: "POST",
        headers: generationRequestHeaders(),
        body: JSON.stringify({ topic_or_draft: topic }),
      }, 150_000);
      const data: unknown = await res.json();
      if (!res.ok) {
        const detail = isRecord(data) && typeof data.detail === "string" ? data.detail : "Story polish failed";
        throw new Error(detail);
      }
      if (!isRecord(data) || data.success !== true || !Array.isArray(data.variants) || !data.variants.every(v => isRecord(v) && typeof v.name === "string" && isVideoScript(v.script))) {
        throw new Error("Vertex returned invalid story variants");
      }
      setVariants(data.variants as StoryVariant[]);
      setSelectedVariant(0);
      setStoryModel(typeof data.model_used === "string" ? data.model_used : null);
      setWritingStatus("done");
    } catch (err) {
      setError((err as Error).message || "Story polish failed");
      setWritingStatus("error");
    } finally {
      activeStoryActionRef.current = false;
    }
  }

  async function approveAndLock() {
    if (selectedVariant === null || !variants[selectedVariant] || !generationReady || activeStoryActionRef.current) return;
    activeStoryActionRef.current = true;
    const chosen = variants[selectedVariant].script;
    setWritingStatus("writing");
    setError(null);
    try {
      const res = await fetchWithDeadline(`${API_URL}/api/story-lock`, {
        method: "POST",
        headers: generationRequestHeaders(),
        body: JSON.stringify({
          title: chosen.title,
          approved_segments: chosen.segments.map(({ id, text }) => ({ id, text })),
        }),
      }, 150_000);
      const data: unknown = await res.json();
      if (!res.ok || !isRecord(data) || data.success !== true || !isVideoScript(data.data) || typeof data.lock_id !== "string" || !/^[0-9a-f]{8}$/.test(data.lock_id)) {
        throw new Error("Approved narration could not be locked");
      }
      setScript(data.data);
      setScriptLockId(data.lock_id);
      setScriptLocked(true);
      setWritingStatus("done");
    } catch (err) {
      setError((err as Error).message || "Story lock failed");
      setWritingStatus("error");
    } finally {
      activeStoryActionRef.current = false;
    }
  }

  function updateSelectedNarration(segmentIndex: number, text: string) {
    if (selectedVariant === null) return;
    setVariants((current) => current.map((variant, variantIndex) => {
      if (variantIndex !== selectedVariant) return variant;
      return {
        ...variant,
        script: {
          ...variant.script,
          segments: variant.script.segments.map((segment, index) =>
            index === segmentIndex ? { ...segment, text } : segment
          ),
        },
      };
    }));
    setScriptLocked(false);
    setScriptLockId(null);
  }

  async function generateVideo() {
    if (!script || !scriptLocked || !scriptLockId || !generationReady) return;
    if (renderStatus === "queued" || renderStatus === "visuals" || renderStatus === "voice" || renderStatus === "rendering" || renderStatus === "qa") return;

    if (activeVideoControllerRef.current) {
      return;
    }
    const abortController = new AbortController();
    activeVideoControllerRef.current = abortController;
    const { signal } = abortController;

    setRenderStatus("queued");
    setVisualProgress(null);
    setRenderProgress(null);
    setVideoUrl(null);
    setError(null);

    const abortableDelay = (ms: number, abortSignal: AbortSignal): Promise<void> => {
      return new Promise((resolve, reject) => {
        if (abortSignal.aborted) {
          reject(new DOMException("Aborted", "AbortError"));
          return;
        }
        const timer = setTimeout(() => {
          abortSignal.removeEventListener("abort", onAbort);
          resolve();
        }, ms);
        const onAbort = () => {
          clearTimeout(timer);
          reject(new DOMException("Aborted", "AbortError"));
        };
        abortSignal.addEventListener("abort", onAbort, { once: true });
      });
    };

    let data: unknown;
    try {
      const res = await fetchWithDeadline(`${API_URL}/api/generate-video`, {
        method: "POST",
        headers: generationRequestHeaders(),
        body: JSON.stringify({
          lock_id: scriptLockId,
          voice_provider: effectiveVoiceProvider,
          style: selectedStyle,
        }),
        signal,
      }, 20_000);
      data = await res.json();
      if (!res.ok) {
        const errorMsg = isRecord(data) ? (data.detail || data.error) : undefined;
        throw new Error(typeof errorMsg === "string" ? errorMsg : "Video generation failed");
      }
    } catch (err) {
      if ((err as Error).name === "AbortError") return;
      setError((err as Error).message || "Generation request failed");
      setRenderStatus("failed");
      return;
    }

    if (!isRecord(data) || !data.success || typeof data.job_id !== "string") {
      setError("Failed to queue video generation job.");
      setRenderStatus("failed");
      return;
    }

    const jobId = data.job_id;
    const startTime = Date.now();

    while (Date.now() - startTime < 45 * 60 * 1000) {
      if (signal.aborted) return;

      try {
        const statusRes = await fetchWithDeadline(`${API_URL}/api/jobs/${jobId}/status`, { signal });
        const data: unknown = await statusRes.json();

        if (statusRes.status === 404) {
          if (signal.aborted) return;
          setError("Job not found.");
          setRenderStatus("failed");
          return;
        }

        if (!statusRes.ok) {
          await abortableDelay(3000, signal);
          continue;
        }

        if (isRecord(data)) {
          const status = data.status;
          if (typeof status === "string") {
            if (status === "completed") {
              if (signal.aborted) return;
              setVideoUrl(`${API_URL}/api/jobs/${jobId}/video`);
              setRenderStatus("completed");
              setRenderProgress(null);
              return;
            } else if (status === "failed") {
              if (signal.aborted) return;
              setError((typeof data.error === "string" && data.error) ? data.error : "Video generation failed");
              setRenderStatus("failed");
              setRenderProgress(null);
              return;
            } else if (["queued", "visuals", "voice", "rendering", "qa"].includes(status)) {
              if (signal.aborted) return;
              setRenderStatus(status as JobStatus);
              if (status === "visuals") {
                const rawProgress = data.visual_progress;
                const progress: Record<string, unknown> = isRecord(rawProgress) ? rawProgress : {};
                const rawCache = progress.cache_state;
                const cacheState: VisualCacheState | null =
                  typeof rawCache === "string" && ["producer", "waiting", "hit", "miss"].includes(rawCache)
                    ? rawCache as VisualCacheState
                    : null;
                const numberOrUndefined = (value: unknown) =>
                  typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : undefined;
                const safeProgress: VisualProgress = {
                  total: typeof progress.total === "number" ? progress.total : 0,
                  passed: numberOrUndefined(progress.passed),
                  planned: numberOrUndefined(progress.planned),
                  fallbacks: numberOrUndefined(progress.fallbacks),
                  percent: numberOrUndefined(progress.percent),
                  completed_batches: numberOrUndefined(progress.completed_batches),
                  total_batches: numberOrUndefined(progress.total_batches),
                  retry_count: numberOrUndefined(progress.retry_count),
                  cache_state: cacheState,
                  current_failed_ids: Array.isArray(progress.current_failed_ids)
                    ? progress.current_failed_ids.filter((value): value is string => typeof value === "string")
                    : [],
                };
                setVisualProgress(safeProgress);
                if (cacheState === "waiting") {
                  setRenderProgress("Waiting for the shared visual story…");
                } else if (cacheState === "hit") {
                  setRenderProgress("Reusing the approved visual story…");
                } else if (safeProgress.completed_batches !== undefined && safeProgress.total_batches !== undefined) {
                  const failed = safeProgress.current_failed_ids?.length ?? 0;
                  setRenderProgress(failed > 0
                    ? `Repairing ${failed} invalid visual plan${failed === 1 ? "" : "s"}…`
                    : `Planning visual story batch ${safeProgress.completed_batches}/${safeProgress.total_batches}… ${safeProgress.planned ?? 0}/${safeProgress.total} shots`);
                } else if (safeProgress.passed !== undefined) {
                  setRenderProgress(`Verifying visual evidence… ${safeProgress.passed}/${safeProgress.total} shots`);
                }
              } else if (status === "voice") {
                setRenderProgress("Generating the Gemini mascot voice…");
              } else if (status === "rendering") {
                setRenderProgress("Rendering the audio-driven video…");
              } else if (status === "qa") {
                setRenderProgress("Checking visuals, audio, captions, and lip sync…");
              }
            }
          } else {
            if (signal.aborted) return;
            setError("Unknown or malformed status received.");
            setRenderStatus("failed");
            return;
          }
        } else {
          if (signal.aborted) return;
          setError(`Malformed payload: expected a record, received ${typeof data}`);
          setRenderStatus("failed");
          return;
        }

      } catch (err) {
        if ((err as Error).name === "AbortError") return;
      }

      await abortableDelay(5000, signal);
    }

    if (signal.aborted) return;
    setError("Job polling timed out after 45 minutes");
    setRenderStatus("failed");
  }

  return (
    <div className="app-shell">
      <StudioHeader runtime={runtime} runtimeSource={runtimeSource} />

      <main className="create-main">
        <div className="page-intro">
          <div>
            <p className="eyebrow">Create workspace</p>
            <h1>Turn a draft into a finished video.</h1>
            <p className="page-intro__lede">Source the idea, approve the story, then render a production-ready FYF cut.</p>
          </div>
        </div>

        <WorkflowStrip stages={workflowStages} />

        <div className="workspace-grid">
          <section className="workspace-panel source-panel" aria-labelledby="source-title">
            <div className="section-heading">
              <div>
                <p className="eyebrow">Start here</p>
                <h2 id="source-title">Source and story</h2>
              </div>
              <span className="step-note" aria-label="Step 1">01</span>
            </div>

            <div className="field-group">
              <label htmlFor="topic-source" className="field-label">Topic or draft</label>
              <textarea
                id="topic-source"
                className="field-control field-control--textarea"
                placeholder="Paste a draft, an article, or describe the video you want."
                value={topic}
                onChange={(event) => setTopic(event.target.value)}
              />
            </div>

            <div className="control-grid">
              <div className="field-group">
                <label htmlFor="voice-provider" className="field-label">Voice</label>
                <select
                  id="voice-provider"
                  className="field-control"
                  value={effectiveVoiceProvider}
                  disabled
                >
                  <option value="gemini">{voiceProviderLabel("gemini")}</option>
                </select>
              </div>
              <div className="field-group">
                <label htmlFor="duration-mode" className="field-label">Duration</label>
                <select id="duration-mode" value={durationMode} disabled className="field-control">
                  <option value="short">Short · 30–60 sec (Public hackathon)</option>
                </select>
              </div>
              <div className="field-group">
                <label htmlFor="video-style" className="field-label">Visual style</label>
                <select
                  id="video-style"
                  value={selectedStyle}
                  onChange={(event) => setSelectedStyle(event.target.value)}
                  className="field-control"
                >
                  {availableStyles.map((style) => (
                    <option key={style.id} value={style.id}>
                      {style.name}
                    </option>
                  ))}
                </select>
              </div>
            </div>

            {!generationReady && (
              <div className="notice-banner notice-banner--warning" role="status">
                <p><strong>Generation unavailable:</strong> {runtime.generation_message}</p>
                {runtimeSource === "api" && runtime.generation_access_required && (
                  <label className="field-group">
                    <span className="field-label">Private generation access</span>
                    <input
                      type="password"
                      value={generationAccessToken}
                      onChange={(event) => updateGenerationAccessToken(event.target.value)}
                      className="field-control field-control--input"
                      autoComplete="off"
                      placeholder="Enter the operator access code"
                    />
                  </label>
                )}
              </div>
            )}

            <div className="action-stack">
              <button
                type="button"
                onClick={generateScript}
                disabled={writingStatus === "writing" || !topic.trim() || !generationReady}
                className="button button--primary"
              >
                {writingStatus === "writing" ? "Generating script…" : "Generate script"}
              </button>
              <button
                type="button"
                onClick={polishStory}
                disabled={writingStatus === "writing" || !topic.trim() || !generationReady}
                className="button button--secondary"
              >
                {writingStatus === "writing" ? "Creating FYF story options…" : "FYF Polish — create 3 story options"}
              </button>
            </div>

            {writingStatus === "needs_attention" && resumableScriptJobId && (
              <div className="notice-banner notice-banner--warning" role="alert">
                <p><strong>Generation Paused:</strong> Provider encountered a temporary rate limit or timeout. Checkpoint is safely preserved.</p>
                <button
                  type="button"
                  onClick={() => resumeScriptJob(resumableScriptJobId)}
                  className="button button--primary button--compact"
                >
                  Retry from checkpoint
                </button>
              </div>
            )}

            {variants.length > 0 && (
              <div className="story-section">
                <div className="section-heading section-heading--compact">
                  <div>
                    <p className="eyebrow">Story</p>
                    <h3>Compare and choose one</h3>
                  </div>
                  {storyModel && <span className="section-meta">Vertex · {storyModel}</span>}
                </div>
                <div className="story-options">
                  {variants.map((variant, index) => (
                    <button
                      type="button"
                      key={variant.name}
                      onClick={() => setSelectedVariant(index)}
                      aria-pressed={selectedVariant === index}
                      className={`story-option${selectedVariant === index ? " story-option--selected" : ""}`}
                    >
                      <span className="story-option__name">{variant.name}</span>
                      <span className="story-option__title">{variant.script.title}</span>
                    </button>
                  ))}
                </div>

                {selectedVariant !== null && variants[selectedVariant] && (
                  <div className="story-editor">
                    <p className="story-editor__hint">Edit the Burmese narration directly before approving. Changes stay locked for video generation.</p>
                    <div className="segment-list">
                      {variants[selectedVariant].script.segments.map((segment, index) => (
                        <div key={segment.id} className="segment-row">
                          <span className="segment-row__index">{index + 1}</span>
                          <input
                            type="text"
                            value={segment.text}
                            onChange={(event) => updateSelectedNarration(index, event.target.value)}
                            className="field-control field-control--input"
                          />
                        </div>
                      ))}
                    </div>
                    <p className="helper-text">The approved text is preserved exactly. Vertex adds visual metadata only.</p>
                  </div>
                )}
                <button
                  type="button"
                  onClick={approveAndLock}
                  disabled={selectedVariant === null || writingStatus === "writing" || !generationReady}
                  className="button button--dark"
                >
                  Approve selected story &amp; lock narration
                </button>
              </div>
            )}

            {script && (
              <div className="render-actions">
                <button
                  type="button"
                  onClick={generateVideo}
                  disabled={!scriptLocked || !scriptLockId || renderBusy || !generationReady}
                  className="button button--accent"
                >
                  {renderStatus === "queued" ? "Job queued…"
                    : renderStatus === "visuals" ? "Creating visuals…"
                      : renderStatus === "voice" ? "Generating voice…"
                        : renderStatus === "rendering" ? "Rendering video…"
                          : renderStatus === "qa" ? "Checking output…"
                            : scriptLocked ? "Generate locked video" : "Approve and lock before video"}
                </button>
                <p className="helper-text helper-text--center">Script, visual, voice, and render stages are checkpointed and restart-resumable.</p>
              </div>
            )}

            {error && (
              <p className="error-message" role="alert" title={error}>{error}</p>
            )}
          </section>

          <section className="workspace-panel preview-panel" aria-labelledby="preview-title">
            <div className="section-heading">
              <div>
                <p className="eyebrow">Render output</p>
                <h2 id="preview-title">Preview</h2>
              </div>
              <div className="preview-panel__actions">
                {videoUrl && <a href={videoUrl} download className="text-action">Download MP4</a>}
                {renderStatus === "completed" && <span className="status-badge">Ready</span>}
              </div>
            </div>

            <div className="preview-window">
              <div className="preview-window__bar" aria-hidden="true">
                <span />
                <span />
                <span />
              </div>
              <div className="preview-window__stage" aria-live="polite" aria-atomic="true">
                {writingStatus === "writing" ? (
                  <p className="preview-status preview-status--active">{scriptProgress}</p>
                ) : renderStatus === "queued" ? (
                  <p className="preview-status preview-status--active">Queued… waiting for the available worker.</p>
                ) : renderStatus === "visuals" ? (
                  <div className="preview-status-group">
                    <p className="preview-status preview-status--active">{renderProgress || "Creating and verifying story visuals with Vertex…"}</p>
                    {visualProgress && (visualProgress.fallbacks ?? 0) > 0 && <p className="preview-status__detail">Verified fallbacks: {visualProgress.fallbacks}</p>}
                  </div>
                ) : renderStatus === "voice" ? (
                  <p className="preview-status preview-status--active">{renderProgress || "Synthesizing AI mascot voice…"}</p>
                ) : renderStatus === "rendering" ? (
                  <p className="preview-status preview-status--active">{renderProgress || "Rendering video…"}</p>
                ) : renderStatus === "qa" ? (
                  <p className="preview-status preview-status--active">{renderProgress || "Checking video, audio, narration, and mouth cues…"}</p>
                ) : renderStatus === "failed" ? (
                  <p className="preview-status">Render stopped. Review the message beside the source controls.</p>
                ) : videoUrl ? (
                  <video controls playsInline className="preview-window__video" src={videoUrl} aria-label="Rendered FYF video preview" />
                ) : (
                  <p className="preview-empty">Rendered video appears here.</p>
                )}
              </div>
            </div>

            {script && (
              <details className="technical-disclosure">
                <summary>{scriptLocked ? "Approved narration locked · view technical JSON" : "View technical JSON"}</summary>
                <pre>{JSON.stringify(script, null, 2)}</pre>
              </details>
            )}
          </section>
        </div>
      </main>
    </div>
  );
}
