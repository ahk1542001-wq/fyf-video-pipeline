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

const HACKATHON_MODE = process.env.NEXT_PUBLIC_FYF_RUNTIME_MODE === "hackathon";
// The page owns create-job state. Shared runtime/data and workflow presentation live in small modules.
export default function Home() {
  const [topic, setTopic] = useState("");
  const [durationMode, setDurationMode] = useState<"short" | "medium" | "long">("short");
  const [script, setScript] = useState<VideoScript | null>(null);
  const [variants, setVariants] = useState<StoryVariant[]>([]);
  const [selectedVariant, setSelectedVariant] = useState<number | null>(null);
  const [scriptLocked, setScriptLocked] = useState(false);
  const [scriptLockId, setScriptLockId] = useState<string | null>(null);
  const [storyModel, setStoryModel] = useState<string | null>(null);
  const [videoUrl, setVideoUrl] = useState<string | null>(null);
  const [pairedVideos, setPairedVideos] = useState<Array<{voice: "kaggle" | "gemini"; url: string}>>([]);
  const [writingStatus, setWritingStatus] = useState<"idle" | "writing" | "done" | "error">("idle");
  const [scriptProgress, setScriptProgress] = useState("Waiting for the script worker…");
  const [renderStatus, setRenderStatus] = useState<JobStatus>("idle");
  const [visualProgress, setVisualProgress] = useState<VisualProgress | null>(null);
  const [renderProgress, setRenderProgress] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [voiceProvider, setVoiceProvider] = useState<VoiceProvider>(HACKATHON_MODE ? "gemini" : "kaggle");
  const [runtime, setRuntime] = useState<RuntimeInfo>(STATIC_RUNTIME_FALLBACK);
  const [runtimeSource, setRuntimeSource] = useState<"api" | "fallback">("fallback");

  const activeVideoControllerRef = useRef<AbortController | null>(null);
  const effectiveVoiceProvider: VoiceProvider = runtime.allowed_voice_providers.includes(voiceProvider)
    ? voiceProvider
    : runtime.allowed_voice_providers[0] || "gemini";

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
        const response = await fetch(`${API_URL}/api/runtime`);
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
    void loadRuntime();
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

  const hasCompletedVideo = renderStatus === "completed" && Boolean(videoUrl || pairedVideos.length > 0);
  const workflowStages = deriveWorkflowStages({
    hasSource: topic.trim().length > 0,
    hasStory: Boolean(script || variants.length > 0),
    narrationLocked: scriptLocked,
    hasCompletedVideo,
  });
  const renderBusy = ["queued", "visuals", "voice", "rendering", "qa"].includes(renderStatus);

  async function generateScript() {
    if (!topic.trim()) return;

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
    setPairedVideos([]);
    setError(null);
    try {
      const res = await fetch(`${API_URL}/api/generate-script`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ topic, duration_mode: durationMode }),
      });
      let data: unknown;
      try {
        data = await res.json();
      } catch {
        throw new Error(`Failed to parse response: ${res.statusText}`);
      }

      if (res.ok && isRecord(data) && data.success && typeof data.job_id === "string" && /^[0-9a-f]{8}$/.test(data.job_id) && typeof data.status_url === "string") {
        const started = Date.now();
        while (Date.now() - started < 45 * 60 * 1000) {
          const statusRes = await fetch(`${API_URL}${data.status_url}`);
          if (!statusRes.ok) throw new Error("Could not read script job status");
          const job: unknown = await statusRes.json();
          if (!isRecord(job)) throw new Error("Malformed script job status");
          const progress = typeof job.progress === "number" ? ` ${Math.round(job.progress)}%` : "";
          if (job.stage === "narration") {
            setScriptProgress(`Writing narration with Vertex…${progress}`);
          } else if (job.stage === "storyboard") {
            const batch = typeof job.batch === "number" ? job.batch : 1;
            const count = typeof job.batch_count === "number" ? job.batch_count : "?";
            setScriptProgress(`Building visual story batch ${batch}/${count}…${progress}`);
          } else if (job.stage === "retrying") {
            setScriptProgress("Retrying from the latest saved checkpoint…");
          } else {
            setScriptProgress(`Preparing the script job…${progress}`);
          }
          if (job.status === "completed" && isVideoScript(job.data) && typeof job.lock_id === "string" && /^[0-9a-f]{8}$/.test(job.lock_id)) {
            setScript(job.data); setScriptLockId(job.lock_id); setScriptLocked(true); setWritingStatus("done");
            setScriptProgress("Script locked and ready.");
            return;
          }
          if (job.status === "failed") throw new Error(typeof job.error === "string" ? job.error : "Script production failed");
          if (!(["queued", "writing"] as unknown[]).includes(job.status)) throw new Error("Unknown script job status");
          await new Promise(resolve => setTimeout(resolve, 3000));
        }
        throw new Error("Script production timed out after 45 minutes");
      } else {
        const errorMsg = isRecord(data) ? (data.error || data.detail) : undefined;
        setError(typeof errorMsg === 'string' ? errorMsg : "Generation failed or invalid script format");
        setWritingStatus("error");
      }
    } catch (err) {
      const e = err as Error;
      setError(e.message || "Cannot reach backend. Is FastAPI running?");
      setWritingStatus("error");
    }
  }

  async function polishStory() {
    if (!topic.trim()) return;
    setWritingStatus("writing"); setError(null); setScript(null); setVariants([]); setSelectedVariant(null); setScriptLocked(false); setScriptLockId(null); setStoryModel(null);
    try {
      const res = await fetch(`${API_URL}/api/story-polish`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ topic_or_draft: topic }) });
      const data: unknown = await res.json();
      if (!res.ok) {
        const detail = isRecord(data) && typeof data.detail === "string" ? data.detail : "Story polish failed";
        throw new Error(detail);
      }
      if (!isRecord(data) || data.success !== true || !Array.isArray(data.variants) || !data.variants.every(v => isRecord(v) && typeof v.name === "string" && isVideoScript(v.script))) throw new Error("Vertex returned invalid story variants");
      setVariants(data.variants as StoryVariant[]); setSelectedVariant(0); setStoryModel(typeof data.model_used === "string" ? data.model_used : null); setWritingStatus("done");
    } catch (err) { setError((err as Error).message || "Story polish failed"); setWritingStatus("error"); }
  }

  async function approveAndLock() {
    if (selectedVariant === null || !variants[selectedVariant]) return;
    const chosen = variants[selectedVariant].script;
    setWritingStatus("writing"); setError(null);
    try {
      const res = await fetch(`${API_URL}/api/story-lock`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ title: chosen.title, approved_segments: chosen.segments.map(({id, text}) => ({id, text})) }) });
      const data: unknown = await res.json();
      if (!res.ok || !isRecord(data) || data.success !== true || !isVideoScript(data.data) || typeof data.lock_id !== "string" || !/^[0-9a-f]{8}$/.test(data.lock_id)) throw new Error("Approved narration could not be locked");
      setScript(data.data); setScriptLockId(data.lock_id); setScriptLocked(true); setWritingStatus("done");
    } catch (err) { setError((err as Error).message || "Story lock failed"); setWritingStatus("error"); }
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
    if (!script || !scriptLocked || !scriptLockId) return;
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
    setError(null);
    setVideoUrl(null);

    try {
      const dual = effectiveVoiceProvider === "dual";
      const res = await fetch(`${API_URL}${dual ? "/api/generate-dual-video" : "/api/generate-video"}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(dual ? {lock_id: scriptLockId} : {
          lock_id: scriptLockId,
          voice_provider: effectiveVoiceProvider
        }),
        signal
      });

      if (res.status !== 202) {
        throw new Error(`Failed to generate video: expected 202, got ${res.status}`);
      }

      let data: unknown;
      try {
        data = await res.json();
      } catch {
        throw new Error(`Failed to parse response: ${res.statusText}`);
      }

      if (!isRecord(data) || data.success !== true) {
        throw new Error("Invalid response: success must be true");
      }

      if (dual) {
        if (!Array.isArray(data.jobs) || data.jobs.length !== 2) throw new Error("Invalid paired video jobs");
        const jobs = data.jobs.map((job) => {
          if (!isRecord(job) || (job.voice_provider !== "kaggle" && job.voice_provider !== "gemini") || typeof job.job_id !== "string" || !/^[0-9a-f]{8}$/.test(job.job_id) || job.status_url !== `/api/jobs/${job.job_id}/status`) throw new Error("Invalid paired video job");
          return {voice: job.voice_provider as "kaggle" | "gemini", jobId: job.job_id, statusUrl: job.status_url as string};
        });
        await Promise.all(jobs.map(job => pollJobStatus(job.statusUrl, job.jobId, signal, job.voice)));
        return;
      }

      if (typeof data.job_id !== "string" || !/^[0-9a-f]{8}$/.test(data.job_id)) {
        throw new Error("Invalid response: job_id must be exactly 8 lowercase hex characters");
      }

      if (data.status_url !== `/api/jobs/${data.job_id}/status`) {
        throw new Error(`Invalid response: status_url must be exactly /api/jobs/${data.job_id}/status`);
      }

      if (data.restart_resumable !== true) {
         throw new Error("Invalid response: restart_resumable must be exactly true");
      }

      const statusUrl = data.status_url as string;
      const jobId = data.job_id as string;
      await pollJobStatus(statusUrl, jobId, signal);

    } catch (err) {
      if ((err as Error).name === 'AbortError') return;
      if (signal.aborted) return;
      const e = err as Error;
      setError(e.message || "Cannot reach backend. Is FastAPI running?");
      setRenderStatus("failed");
    } finally {
      if (activeVideoControllerRef.current === abortController) {
         activeVideoControllerRef.current = null;
      }
    }
  }

  function abortableDelay(ms: number, signal: AbortSignal): Promise<void> {
    return new Promise((resolve, reject) => {
      if (signal.aborted) {
        return reject(new DOMException("Aborted", "AbortError"));
      }
      const timeout = setTimeout(() => {
        signal.removeEventListener("abort", onAbort);
        resolve();
      }, ms);
      const onAbort = () => {
        clearTimeout(timeout);
        reject(new DOMException("Aborted", "AbortError"));
      };
      signal.addEventListener("abort", onAbort, { once: true });
    });
  }

  async function pollJobStatus(statusUrl: string, jobId: string, signal: AbortSignal, pairedVoice?: "kaggle" | "gemini") {
    const startTime = Date.now();
    const MAX_TIME = 45 * 60 * 1000; // 45 minutes

    while (Date.now() - startTime < MAX_TIME) {
      if (signal.aborted) return;

      try {
        const res = await fetch(`${API_URL}${statusUrl}`, { signal });

        if (res.status === 400 || res.status === 404) {
          if (signal.aborted) return;
          setError(`Terminal error fetching status: ${res.statusText}`);
          setRenderStatus("failed");
          return;
        }

        if (!res.ok) {
           await abortableDelay(5000, signal);
           continue; // Retry transient errors
        }

        let data: unknown;
        try {
          data = await res.json();
        } catch {
          await abortableDelay(5000, signal);
          continue;
        }

        if (isRecord(data)) {
          const status = data.status;

          if (status === "failed") {
            if (signal.aborted) return;
            setError(typeof data.error === 'string' ? data.error : "Job failed");
            setRenderStatus("failed");
            return;
          }

          if (status === "completed") {
            if (signal.aborted) return;
            if (data.video_url !== `/api/jobs/${jobId}/video`) {
               setError(`Invalid response: video_url must be exactly /api/jobs/${jobId}/video`);
               setRenderStatus("failed");
               return;
            }
            const videoPath = data.video_url as string;
            const readyUrl = `${API_URL}${videoPath}`;
            if (pairedVoice) {
              setPairedVideos(current => [...current.filter(item => item.voice !== pairedVoice), {voice: pairedVoice, url: readyUrl}]);
            } else {
              setVideoUrl(readyUrl);
            }
            setRenderStatus("completed");
            return;
          }

          if (status === "queued" || status === "visuals" || status === "voice" || status === "rendering" || status === "qa") {
            if (!signal.aborted) {
               setRenderStatus(status);
               const progress = data.visual_progress;
               if (status === "visuals" && isRecord(progress) &&
                   typeof progress.total === "number" && Number.isFinite(progress.total) && progress.total >= 0) {
                 const cacheStates: VisualCacheState[] = ["producer", "waiting", "hit", "miss"];
                 const cacheState = cacheStates.includes(progress.cache_state as VisualCacheState)
                   ? progress.cache_state as VisualCacheState
                   : null;
                 const numberOrUndefined = (value: unknown) =>
                   typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : undefined;
                 const safeProgress: VisualProgress = {
                   total: progress.total,
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
                 setRenderProgress("Generating the selected voice…");
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
         if ((err as Error).name === 'AbortError') return;
         // Transient network error, just wait and retry
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
                  onChange={(event) => {
                    const value = event.target.value;
                    if (value === "kaggle" || value === "gemini" || value === "dual") setVoiceProvider(value);
                  }}
                >
                  {runtime.allowed_voice_providers.map(provider => (
                    <option key={provider} value={provider}>{voiceProviderLabel(provider)}</option>
                  ))}
                </select>
              </div>
              <div className="field-group">
                <label htmlFor="duration-mode" className="field-label">Duration</label>
                <select id="duration-mode" value={durationMode} onChange={(event) => setDurationMode(event.target.value as "short" | "medium" | "long")} className="field-control">
                  <option value="short">Short · 30–60 sec</option>
                  <option value="medium">Standard · 1–2 min</option>
                  <option value="long">Long · over 2 min</option>
                </select>
              </div>
            </div>

            <div className="action-stack">
              <button type="button" onClick={generateScript} disabled={writingStatus === "writing" || !topic.trim()} className="button button--primary">
                {writingStatus === "writing" ? "Generating script…" : "Generate script"}
              </button>
              <button type="button" onClick={polishStory} disabled={writingStatus === "writing" || !topic.trim()} className="button button--secondary">
                {writingStatus === "writing" ? "Creating FYF story options…" : "FYF Polish — create 3 story options"}
              </button>
            </div>

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
                      <span className="story-option__summary">{variant.script.segments.map(segment => segment.text).join(" ")}</span>
                    </button>
                  ))}
                </div>

                {selectedVariant !== null && variants[selectedVariant] && (
                  <div className="story-review">
                    <p className="field-label">Review exact narration before lock</p>
                    {variants[selectedVariant].script.segments.map((segment, index) => (
                      <label key={segment.id} className="field-group">
                        <span className="field-label field-label--muted">Part {index + 1}</span>
                        <textarea
                          aria-label={`Narration part ${index + 1}`}
                          className="field-control field-control--narration"
                          value={segment.text}
                          onChange={(event) => updateSelectedNarration(index, event.target.value)}
                        />
                      </label>
                    ))}
                    <p className="helper-text">The approved text is preserved exactly. Vertex adds visual metadata only.</p>
                  </div>
                )}
                <button type="button" onClick={approveAndLock} disabled={selectedVariant === null || writingStatus === "writing"} className="button button--dark">
                  Approve selected story &amp; lock narration
                </button>
              </div>
            )}

            {script && (
              <div className="render-actions">
                <button
                  type="button"
                  onClick={generateVideo}
                  disabled={!scriptLocked || !scriptLockId || renderBusy}
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
                  <p className="preview-status preview-status--active">{renderProgress || (effectiveVoiceProvider === "kaggle" ? "Synthesizing partner narration…" : "Synthesizing AI mascot voice…")}</p>
                ) : renderStatus === "rendering" ? (
                  <p className="preview-status preview-status--active">{renderProgress || "Rendering video…"}</p>
                ) : renderStatus === "qa" ? (
                  <p className="preview-status preview-status--active">{renderProgress || "Checking video, audio, narration, and mouth cues…"}</p>
                ) : renderStatus === "failed" ? (
                  <p className="preview-status">Render stopped. Review the message beside the source controls.</p>
                ) : pairedVideos.length > 0 ? (
                  <div className="paired-preview">
                    {pairedVideos.map(item => (
                      <div key={item.voice} className="paired-preview__item">
                        <div className="paired-preview__heading">
                          <p>{voiceProviderLabel(item.voice)}</p>
                          <a href={item.url} download className="text-action text-action--inverse">Download MP4</a>
                        </div>
                        <video controls playsInline className="paired-preview__video" src={item.url} aria-label={`${voiceProviderLabel(item.voice)} preview`} />
                      </div>
                    ))}
                  </div>
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
