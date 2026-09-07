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

const SCRIPT_JOB_STORAGE_KEY = "fyf-active-script-job";
const WIZARD_TOPIC_STORAGE_KEY = "fyf-wizard-topic";
const LOCKED_SCRIPT_STORAGE_KEY = "fyf-locked-script";

function formatElapsed(ms: number): string {
  const totalSeconds = Math.max(0, Math.floor(ms / 1000));
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return minutes > 0 ? `${minutes}m ${String(seconds).padStart(2, "0")}s` : `${seconds}s`;
}

interface VideoScript {
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
interface StoryVariant { name: string; script: VideoScript; }

interface VideoStyleOption {
  id: string;
  name: string;
  description: string;
}

interface PersonaOption {
  id: string;
  name: string;
  description: string;
}

const DEFAULT_PERSONAS: PersonaOption[] = [
  { id: "clear_educator", name: "The Clear Educator (Default)", description: "Friendly, patient, whiteboard-focused explanation." },
  { id: "dynamic_storyteller", name: "The Dynamic Storyteller", description: "Expressive, high-energy dramatic narrative." },
  { id: "objective_analyst", name: "The Objective Analyst", description: "Data-backed, high-fidelity inspection." },
  { id: "warm_companion", name: "The Warm Companion", description: "Encouraging, conversational peer voice." },
];

const DEFAULT_STYLES: VideoStyleOption[] = [
  { id: "fyf_explainer", name: "FYF Explainer (Default)", description: "Standard high-clarity whiteboard with balanced mascot pacing." },
  { id: "cinematic_continuity", name: "Cinematic Continuity", description: "Dramatic push-ins and smooth continuous camera motion." },
  { id: "evidence_story", name: "Evidence Story", description: "Documentary inspection focus with high data fidelity." },
  { id: "cinematic_documentary", name: "Cinematic Documentary", description: "Atmospheric visuals, natural lighting, deep investigative pacing." },
  { id: "tech_explainer", name: "Tech & Product Explainer", description: "Crisp isometric diagrams, blueprint grids, dynamic product breakdown." },
  { id: "investigative", name: "Evidence & Investigative Cinema", description: "Evidence boards, archival footage, timeline sequences." },
  { id: "narrative", name: "Narrative Short", description: "Rich character-focused scenes, emotional beats, continuity." },
];

const STUDIO_PRESETS = [
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

type ProductionTelemetry = {
  job: Record<string, unknown>;
  connected_to_cloud?: boolean;
  clickhouse_status?: string;
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
  const [selectedPersona, setSelectedPersona] = useState<string>("clear_educator");
  const [includeMascot, setIncludeMascot] = useState<boolean>(true);
  const [activePreset, setActivePreset] = useState<string>("burmese_flagship");
  const [studioName, setStudioName] = useState<string>("FYF Studio");
  const [selectedLanguage, setSelectedLanguage] = useState<string>("my-MM");
  const [presenterMode, setPresenterMode] = useState<string>("on_screen");
  const [selectedVoiceActor, setSelectedVoiceActor] = useState<string>("Sadaltager");
  const [ctaText, setCtaText] = useState<string>("");
  const [retentionProgressBar, setRetentionProgressBar] = useState<boolean>(true);
  const [animatedLowerThirds, setAnimatedLowerThirds] = useState<boolean>(true);
  const [aspectRatio, setAspectRatio] = useState<"9:16" | "16:9" | "1:1">("9:16");
  const [auditioning, setAuditioning] = useState<boolean>(false);

  function auditionVoice() {
    if (typeof window === "undefined") return;
    setAuditioning(true);
    const audioSampleText = selectedLanguage === "my-MM"
      ? `${studioName} မှ ကြိုဆိုပါတယ်။ ကျွန်တော်က ${selectedVoiceActor} ဖြစ်ပါတယ်။`
      : `Welcome to ${studioName}. I am ${selectedVoiceActor}, your AI voice director.`;

    if ("speechSynthesis" in window) {
      window.speechSynthesis.cancel();
      const utterance = new SpeechSynthesisUtterance(audioSampleText);
      utterance.rate = selectedVoiceActor === "Puck" ? 1.15 : selectedVoiceActor === "Sadaltager" ? 0.9 : 1.0;
      utterance.pitch = selectedVoiceActor === "Aoede" ? 1.2 : selectedVoiceActor === "Fenrir" ? 0.8 : 1.0;
      utterance.onend = () => setAuditioning(false);
      utterance.onerror = () => setAuditioning(false);
      window.speechSynthesis.speak(utterance);
    } else {
      setTimeout(() => setAuditioning(false), 800);
    }
  }

  function applyPreset(preset: typeof STUDIO_PRESETS[number]) {
    setActivePreset(preset.id);
    setStudioName(preset.studioName);
    setSelectedLanguage(preset.language);
    setSelectedStyle(preset.style);
    setPresenterMode(preset.presenterMode);
    setIncludeMascot(preset.presenterMode === "on_screen");
    setSelectedVoiceActor(preset.voiceActor);
    setCtaText(preset.ctaText);
  }
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
  const [currentJobId, setCurrentJobId] = useState<string | null>(null);
  const [currentJobTelemetry, setCurrentJobTelemetry] = useState<ProductionTelemetry | null>(null);
  const [telemetryLoading, setTelemetryLoading] = useState(false);
  const [telemetryError, setTelemetryError] = useState<string | null>(null);
  const [renderedAspectRatio, setRenderedAspectRatio] = useState<"9:16" | "16:9" | "1:1">("9:16");
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

  useEffect(() => {
    if (typeof window === "undefined") return;

    async function restoreWizardState() {
      const savedTopic = window.sessionStorage.getItem(WIZARD_TOPIC_STORAGE_KEY);
      if (savedTopic) setTopic(savedTopic);

      const savedLock = window.sessionStorage.getItem(LOCKED_SCRIPT_STORAGE_KEY);
      if (savedLock) {
        try {
          const parsed: unknown = JSON.parse(savedLock);
          if (
            isRecord(parsed) &&
            isVideoScript(parsed.script) &&
            typeof parsed.lockId === "string" &&
            /^[0-9a-f]{8}$/.test(parsed.lockId)
          ) {
            setScript(parsed.script);
            setScriptLockId(parsed.lockId);
            setScriptLocked(true);
            setWritingStatus("done");
            setScriptProgress("Script locked and ready.");
          }
        } catch {
          window.sessionStorage.removeItem(LOCKED_SCRIPT_STORAGE_KEY);
        }
      }

      const activeJob = window.sessionStorage.getItem(SCRIPT_JOB_STORAGE_KEY);
      if (!activeJob) return;
      setWritingStatus("writing");
      setScriptProgress("Reconnecting to the running script job…");
      try {
        const statusRes = await fetchWithDeadline(`${API_URL}/api/script-jobs/${activeJob}/status`);
        const job: unknown = statusRes.ok ? await statusRes.json() : null;
        if (isRecord(job) && job.status === "needs_attention") {
          setWritingStatus("needs_attention");
          setResumableScriptJobId(activeJob);
          setScriptProgress(typeof job.error === "string" ? job.error : "Temporary provider issue. Checkpoint saved.");
          window.sessionStorage.removeItem(SCRIPT_JOB_STORAGE_KEY);
          return;
        }
        await pollScriptJob(activeJob, { isCancelled: () => cancelled });
      } catch (err) {
        setError((err as Error).message || "Lost track of the running script job.");
        setWritingStatus("error");
        window.sessionStorage.removeItem(SCRIPT_JOB_STORAGE_KEY);
      }
    }

    let cancelled = false;
    void restoreWizardState();
    return () => {
      cancelled = true;
    };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function isRecord(val: unknown): val is Record<string, unknown> {
    return typeof val === "object" && val !== null;
  }

  function isVideoScript(val: unknown): val is VideoScript {
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

  function finiteMetric(value: unknown): number | null {
    return typeof value === "number" && Number.isFinite(value) ? value : null;
  }

  async function loadCurrentJobTelemetry(jobId: string, signal?: AbortSignal) {
    setTelemetryLoading(true);
    setTelemetryError(null);
    try {
      const response = await fetchWithDeadline(
        `${API_URL}/api/jobs/${jobId}/telemetry`,
        signal ? { signal } : {},
      );
      const data: unknown = await response.json();
      if (!response.ok || !isRecord(data) || !isRecord(data.job)) {
        throw new Error("Production telemetry is not available yet.");
      }
      setCurrentJobTelemetry({
        job: data.job,
        connected_to_cloud: typeof data.connected_to_cloud === "boolean" ? data.connected_to_cloud : undefined,
        clickhouse_status: typeof data.clickhouse_status === "string" ? data.clickhouse_status : undefined,
      });
    } catch (caught) {
      if ((caught as Error).name === "AbortError") return;
      setCurrentJobTelemetry(null);
      setTelemetryError(caught instanceof Error ? caught.message : "Production telemetry is not available yet.");
    } finally {
      setTelemetryLoading(false);
    }
  }

  function updateGenerationAccessToken(value: string) {
    setGenerationAccessToken(value);
    if (value.trim()) {
      window.sessionStorage.setItem("fyf-generation-access", value);
    } else {
      window.sessionStorage.removeItem("fyf-generation-access");
    }
  }

  async function pollScriptJob(jobId: string, opts: { isCancelled?: () => boolean } = {}) {
    const startTime = Date.now();
    while (Date.now() - startTime < 45 * 60 * 1000) {
      if (opts.isCancelled?.()) return;
      const statusRes = await fetchWithDeadline(`${API_URL}/api/script-jobs/${jobId}/status`);
      if (!statusRes.ok) throw new Error("Could not check script job status");
      const job: unknown = await statusRes.json();
      if (!isRecord(job) || typeof job.status !== "string") throw new Error("Malformed script job payload");

      const elapsed = `· ${formatElapsed(Date.now() - startTime)} elapsed`;
      const progress = typeof job.progress === "number" ? ` (${job.progress}%)` : "";
      if (job.stage === "adk_producer") {
        setScriptProgress(`Google ADK Producer Agent running… ${progress} ${elapsed}. This stage researches and drafts the full story, so it can take several minutes.`);
      } else if (job.stage === "narration") {
        setScriptProgress(`Writing narration with Vertex… ${progress} ${elapsed}`);
      } else if (job.stage === "storyboard" || job.stage === "visual_lock") {
        const batch = typeof job.batch === "number" ? job.batch : 1;
        const count = typeof job.batch_count === "number" ? job.batch_count : "?";
        setScriptProgress(`Building visual story batch ${batch}/${count}… ${progress} ${elapsed}`);
      } else if (job.stage === "retrying") {
        setScriptProgress(`Provider hiccup — auto-retrying from the saved checkpoint… ${elapsed}`);
      } else {
        setScriptProgress(`Preparing the script job… ${progress} ${elapsed}`);
      }

      if (job.status === "completed" && isVideoScript(job.data) && typeof job.lock_id === "string" && /^[0-9a-f]{8}$/.test(job.lock_id)) {
        setScript(job.data);
        setScriptLockId(job.lock_id);
        setScriptLocked(true);
        setWritingStatus("done");
        setResumableScriptJobId(null);
        setScriptProgress("Script locked and ready.");
        window.sessionStorage.removeItem(SCRIPT_JOB_STORAGE_KEY);
        window.sessionStorage.setItem(
          LOCKED_SCRIPT_STORAGE_KEY,
          JSON.stringify({ script: job.data, lockId: job.lock_id }),
        );
        return;
      }

      if (job.status === "needs_attention") {
        setWritingStatus("needs_attention");
        setResumableScriptJobId(jobId);
        window.sessionStorage.removeItem(SCRIPT_JOB_STORAGE_KEY);
        setScriptProgress(typeof job.error === "string" ? job.error : "Temporary provider issue. Checkpoint saved.");
        return;
      }

      if (job.status === "failed") {
        window.sessionStorage.removeItem(SCRIPT_JOB_STORAGE_KEY);
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
    window.sessionStorage.setItem(WIZARD_TOPIC_STORAGE_KEY, topic);
    window.sessionStorage.removeItem(LOCKED_SCRIPT_STORAGE_KEY);

    try {
      const res = await fetchWithDeadline(`${API_URL}/api/generate-script`, {
        method: "POST",
        headers: generationRequestHeaders(),
        body: JSON.stringify({
          topic,
          duration_mode: durationMode,
          style: selectedStyle,
          studio_name: studioName,
          language: selectedLanguage,
          genre: selectedStyle,
          presenter_mode: presenterMode,
          voice_actor: selectedVoiceActor,
        }),
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
        window.sessionStorage.setItem(SCRIPT_JOB_STORAGE_KEY, jobId);
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
        body: JSON.stringify({
          topic_or_draft: topic,
          studio_name: studioName,
          language: selectedLanguage,
          genre: selectedStyle,
          presenter_mode: presenterMode,
          voice_actor: selectedVoiceActor,
        }),
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
          studio_name: studioName,
          language: selectedLanguage,
          genre: selectedStyle,
          presenter_mode: presenterMode,
          voice_actor: selectedVoiceActor,
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

  function updateSegmentField<K extends keyof VideoScript["segments"][0]>(
    segmentIndex: number,
    field: K,
    value: VideoScript["segments"][0][K],
  ) {
    if (selectedVariant === null) return;
    setVariants((current) => current.map((variant, variantIndex) => {
      if (variantIndex !== selectedVariant) return variant;
      return {
        ...variant,
        script: {
          ...variant.script,
          segments: variant.script.segments.map((segment, index) =>
            index === segmentIndex ? { ...segment, [field]: value } : segment
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
    setCurrentJobId(null);
    setCurrentJobTelemetry(null);
    setTelemetryLoading(false);
    setTelemetryError(null);
    setRenderedAspectRatio(aspectRatio);
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

    try {
      let data: unknown;
      try {
        const res = await fetchWithDeadline(`${API_URL}/api/generate-video`, {
          method: "POST",
          headers: generationRequestHeaders(),
          body: JSON.stringify({
            lock_id: scriptLockId,
            voice_provider: effectiveVoiceProvider,
            style: selectedStyle,
            studio_name: studioName,
            language: selectedLanguage,
            genre: selectedStyle,
            presenter_mode: presenterMode,
            voice_actor: selectedVoiceActor,
            cta_text: ctaText,
            retention_progress_bar: retentionProgressBar,
            animated_lower_thirds: animatedLowerThirds,
            aspect_ratio: aspectRatio,
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
      setCurrentJobId(jobId);
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
                await loadCurrentJobTelemetry(jobId, signal);
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
    } finally {
      if (activeVideoControllerRef.current === abortController) {
        activeVideoControllerRef.current = null;
      }
    }
  }

  const auditJob = currentJobTelemetry?.job;
  const auditSummary = auditJob && isRecord(auditJob.summary) ? auditJob.summary : null;
  const rawRenderMs = finiteMetric(auditJob?.render_duration_ms) ?? finiteMetric(auditJob?.total_render_time_ms);
  const auditRenderMs = rawRenderMs !== null && rawRenderMs > 0 ? rawRenderMs : null;
  const auditTokenStatus = typeof auditSummary?.token_status === "string" ? auditSummary.token_status : null;
  const rawTokens = finiteMetric(auditSummary?.total_tokens) ?? finiteMetric(auditJob?.total_tokens_used);
  const auditTokens = rawTokens !== null && !["none", "unavailable", "pending"].includes(auditTokenStatus || "")
    ? rawTokens
    : null;
  const auditCostStatus = typeof auditSummary?.cost_status === "string"
    ? auditSummary.cost_status
    : typeof auditJob?.cost_status === "string" ? auditJob.cost_status : null;
  const rawCost = finiteMetric(auditSummary?.estimated_cost_usd)
    ?? finiteMetric(auditJob?.estimated_cost_usd)
    ?? finiteMetric(auditJob?.cost_usd);
  const auditCost = rawCost !== null && !["unavailable", "unpriced", "pending"].includes(auditCostStatus || "")
    ? rawCost
    : null;
  const auditCloudConfirmed = currentJobTelemetry?.connected_to_cloud === true
    || currentJobTelemetry?.clickhouse_status === "connected";
  const auditCloudLabel = auditCloudConfirmed
    ? "Dual-write confirmed"
    : currentJobTelemetry?.connected_to_cloud === false || currentJobTelemetry?.clickhouse_status
      ? "Local telemetry only"
      : telemetryLoading ? "Telemetry pending" : "Unavailable";

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

            {/* Studio Quick Preset Pills */}
            <div className="studio-presets" role="group" aria-label="Quick Studio Presets" style={{ marginBottom: "1.25rem", padding: "0.75rem 1rem", background: "var(--surface-soft)", border: "1px solid var(--hairline)", borderRadius: "8px" }}>
              <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: "0.5rem" }}>
                <span style={{ fontSize: "0.75rem", fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.05em", color: "var(--muted)" }}>
                  Quick Studio Presets
                </span>
                <span style={{ fontSize: "0.72rem", color: "var(--muted)" }}>
                  Business studio presets
                </span>
              </div>
              <div className="studio-preset-group" style={{ display: "flex", gap: "6px", flexWrap: "wrap" }}>
                {STUDIO_PRESETS.map((p) => (
                  <button
                    key={p.id}
                    type="button"
                    aria-pressed={activePreset === p.id}
                    onClick={() => applyPreset(p)}
                    className={`studio-preset-pill ${activePreset === p.id ? "studio-preset-pill--active" : ""}`}
                    style={{ padding: "5px 12px", fontSize: "0.75rem" }}
                  >
                    {p.label}
                  </button>
                ))}
              </div>
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
                <label htmlFor="studio-name" className="field-label">Studio / Channel Name</label>
                <input
                  id="studio-name"
                  type="text"
                  className="field-control"
                  value={studioName}
                  onChange={(e) => setStudioName(e.target.value)}
                  placeholder="FYF Studio"
                />
              </div>

              <div className="field-group">
                <label htmlFor="studio-language" className="field-label">Language</label>
                <select
                  id="studio-language"
                  className="field-control"
                  value={selectedLanguage}
                  onChange={(e) => {
                    const nextLang = e.target.value;
                    setSelectedLanguage(nextLang);
                    if (nextLang === "en-US" && studioName === "FYF Studio") {
                      setStudioName("FYF Agentic Business Studio");
                    }
                  }}
                >
                  <option value="my-MM">Burmese (Regional Pilot)</option>
                  <option value="en-US">English (Global Cinema)</option>
                </select>
              </div>

              <div className="field-group">
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "0.25rem" }}>
                  <label htmlFor="voice-actor" className="field-label" style={{ marginBottom: 0 }}>Gemini Voice Actor</label>
                  <button
                    type="button"
                    id="voice-audition-btn"
                    onClick={auditionVoice}
                    className="pill-btn"
                    style={{ padding: "2px 8px", fontSize: "0.72rem", cursor: "pointer" }}
                    title="Audition 2-second voice sample"
                  >
                    {auditioning ? "🔊 Playing…" : "🔊 Audition"}
                  </button>
                </div>
                <select
                  id="voice-actor"
                  className="field-control"
                  value={selectedVoiceActor}
                  onChange={(e) => setSelectedVoiceActor(e.target.value)}
                >
                  <option value="Sadaltager">Sadaltager (Deep Narrator)</option>
                  <option value="Puck">Puck (Dynamic Creator)</option>
                  <option value="Aoede">Aoede (Warm Storyteller)</option>
                  <option value="Fenrir">Fenrir (Authoritative)</option>
                </select>
              </div>

              <div className="field-group">
                <label htmlFor="voice-provider" className="field-label">Voice provider</label>
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
                <label htmlFor="video-style" className="field-label">Visual style / Genre</label>
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

              <div className="field-group">
                <label htmlFor="presenter-persona" className="field-label">Persona</label>
                <select
                  id="presenter-persona"
                  value={selectedPersona}
                  onChange={(event) => setSelectedPersona(event.target.value)}
                  className="field-control"
                >
                  {DEFAULT_PERSONAS.map((p) => (
                    <option key={p.id} value={p.id}>{p.name}</option>
                  ))}
                </select>
              </div>

              <div className="field-group">
                <label htmlFor="presenter-mode" className="field-label">Presenter Mode</label>
                <select
                  id="presenter-mode"
                  className="field-control"
                  value={
                    presenterMode === "voiceover_only"
                      ? "none"
                      : (selectedLanguage === "en-US" ? "alex" : "ko_kyaw")
                  }
                  onChange={(e) => {
                    const val = e.target.value;
                    if (val === "ko_kyaw") {
                      setPresenterMode("on_screen");
                      setSelectedLanguage("my-MM");
                      setIncludeMascot(true);
                    } else if (val === "alex") {
                      setPresenterMode("on_screen");
                      setSelectedLanguage("en-US");
                      setIncludeMascot(true);
                    } else {
                      setPresenterMode("voiceover_only");
                      setIncludeMascot(false);
                    }
                  }}
                >
                  <option value="ko_kyaw">Ko Kyaw (On-Screen Host)</option>
                  <option value="alex">Alex (Global Host)</option>
                  <option value="none">No Presenter (Pure Cinematic B-Roll)</option>
                </select>
              </div>

              <div className="field-group">
                <label htmlFor="mascot-toggle" className="field-label">Presenter / Host</label>
                <button
                  type="button"
                  id="mascot-toggle"
                  role="switch"
                  aria-checked={includeMascot}
                  onClick={() => {
                    const next = !includeMascot;
                    setIncludeMascot(next);
                    setPresenterMode(next ? "on_screen" : "voiceover_only");
                  }}
                  className={`toggle-button ${includeMascot ? "toggle-button--active" : ""}`}
                >
                  <span className="toggle-button__indicator" />
                  <span>
                    {includeMascot
                      ? (selectedLanguage === "en-US" ? "Alex (Global Host / On-screen)" : "Ko Kyaw (On-screen Host)")
                      : "Voice only · No Presenter (Pure Cinematic B-Roll)"}
                  </span>
                </button>
              </div>
            </div>

            <section
              className="brand-kit-card"
              aria-labelledby="essential-brand-kit-title"
              style={{
                marginTop: "1.25rem",
                padding: "1rem",
                background: "var(--surface-soft)",
                border: "1px solid var(--hairline)",
                borderRadius: "8px",
              }}
            >
              <div className="section-heading section-heading--compact">
                <div>
                  <p className="eyebrow">Brand controls</p>
                  <h3 id="essential-brand-kit-title">Essential Brand Kit</h3>
                </div>
              </div>
              <div className="field-group">
                <label htmlFor="cta-button-text" className="field-label">CTA button text</label>
                <input
                  id="cta-button-text"
                  type="text"
                  className="field-control field-control--input"
                  value={ctaText}
                  maxLength={80}
                  onChange={(event) => setCtaText(event.target.value)}
                  placeholder="Learn More"
                />
              </div>
              <div className="brand-kit-toggles" style={{ display: "grid", gap: "0.5rem", marginTop: "0.75rem" }}>
                <label htmlFor="retention-progress-bar-toggle" style={{ display: "flex", alignItems: "center", gap: "0.5rem", cursor: "pointer" }}>
                  <input
                    id="retention-progress-bar-toggle"
                    type="checkbox"
                    checked={retentionProgressBar}
                    onChange={(event) => setRetentionProgressBar(event.target.checked)}
                  />
                  <span className="field-label" style={{ margin: 0 }}>Retention Progress Bar</span>
                </label>
                <label htmlFor="animated-lower-thirds-toggle" style={{ display: "flex", alignItems: "center", gap: "0.5rem", cursor: "pointer" }}>
                  <input
                    id="animated-lower-thirds-toggle"
                    type="checkbox"
                    checked={animatedLowerThirds}
                    onChange={(event) => setAnimatedLowerThirds(event.target.checked)}
                  />
                  <span className="field-label" style={{ margin: 0 }}>Animated Lower Thirds</span>
                </label>
              </div>
            </section>

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
              {!topic.trim() && writingStatus !== "writing" && (
                <p className="action-hint" role="note">
                  Enter a topic above to enable script generation.
                </p>
              )}
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

            {writingStatus === "writing" && (
              <div className="script-status-card" role="status" aria-live="polite" aria-atomic="true">
                <span className="script-status-card__marker" aria-hidden="true" />
                <div>
                  <p className="script-status-card__label">Script workspace</p>
                  <p className="script-status-card__message">{scriptProgress}</p>
                </div>
              </div>
            )}

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
                    <p className="story-editor__hint">
                      Edit the {variants[selectedVariant].script.language === "en-US" ? "English" : "Burmese"} narration directly before approving. Changes stay locked for video generation.
                    </p>
                    <div className="segment-list">
                      {variants[selectedVariant].script.segments.map((segment, index) => (
                        <div key={segment.id} className="segment-card">
                          <div className="segment-card__header">
                            <span className="segment-row__index">{index + 1}</span>
                            <div className="segment-tags">
                              <span className="segment-badge">{segment.scene_type}</span>
                              <span className="segment-badge segment-badge--mascot">Mascot: {segment.mascot_action}</span>
                              <span className="segment-badge segment-badge--emotion">{segment.emotion}</span>
                              <span className="segment-badge" style={{ background: "#EEF2FF", color: "#3730A3", borderColor: "#C7D2FE", fontWeight: 600 }}>
                                {index === 0 ? "🚁 Drone Aerial" : index === 1 ? "🔎 Macro Close-up" : index === 2 ? "🔍 Push-in" : "🎥 Tracking Shot"}
                              </span>
                            </div>
                          </div>
                          <div className="segment-card__body">
                            <label htmlFor={`segment-${segment.id}-narration`} className="field-label-sm">
                              {variants[selectedVariant].script.language === "en-US" ? "English Narration" : "Burmese Narration"}
                            </label>
                            <input
                              id={`segment-${segment.id}-narration`}
                              type="text"
                              value={segment.text}
                              onChange={(event) => updateSelectedNarration(index, event.target.value)}
                              className="field-control field-control--input"
                              placeholder="Narration text..."
                            />
                          </div>
                          <div className="segment-card__controls">
                            <div className="segment-control-group">
                              <span className="segment-control-label">Mascot Action:</span>
                              <div className="pill-group" role="group" aria-label={`Segment ${index + 1} mascot action`}>
                                {(["present", "explain", "think", "warn", "approve"] as const).map((act) => (
                                  <button
                                    key={act}
                                    type="button"
                                    onClick={() => updateSegmentField(index, "mascot_action", act)}
                                    className={`pill-btn ${segment.mascot_action === act ? "pill-btn--active" : ""}`}
                                    aria-pressed={segment.mascot_action === act}
                                  >
                                    {act}
                                  </button>
                                ))}
                              </div>
                            </div>
                            <div className="segment-control-group">
                              <span className="segment-control-label">Emotion:</span>
                              <div className="pill-group" role="group" aria-label={`Segment ${index + 1} emotion`}>
                                {(["neutral", "warm", "focused", "concerned", "confident"] as const).map((emo) => (
                                  <button
                                    key={emo}
                                    type="button"
                                    onClick={() => updateSegmentField(index, "emotion", emo)}
                                    className={`pill-btn ${segment.emotion === emo ? "pill-btn--active" : ""}`}
                                    aria-pressed={segment.emotion === emo}
                                  >
                                    {emo}
                                  </button>
                                ))}
                              </div>
                            </div>
                            <div className="segment-control-group">
                              <span className="segment-control-label">Scene:</span>
                              <div className="pill-group" role="group" aria-label={`Segment ${index + 1} scene type`}>
                                {(["whiteboard", "demo"] as const).map((st) => (
                                  <button
                                    key={st}
                                    type="button"
                                    onClick={() => updateSegmentField(index, "scene_type", st)}
                                    className={`pill-btn ${segment.scene_type === st ? "pill-btn--active" : ""}`}
                                    aria-pressed={segment.scene_type === st}
                                  >
                                    {st}
                                  </button>
                                ))}
                              </div>
                            </div>
                          </div>
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
              <div className="preview-panel__actions" style={{ display: "flex", alignItems: "center", gap: "6px" }}>
                <div className="aspect-ratio-selector" role="radiogroup" aria-label="Aspect Ratio Switcher" style={{ display: "flex", gap: "4px" }}>
                  {(["9:16", "16:9", "1:1"] as const).map((ratio) => (
                    <button
                      key={ratio}
                      type="button"
                      role="radio"
                      aria-checked={aspectRatio === ratio}
                      onClick={() => setAspectRatio(ratio)}
                      className={`studio-preset-pill ${aspectRatio === ratio ? "studio-preset-pill--active" : ""}`}
                      style={{ padding: "3px 8px", fontSize: "0.72rem" }}
                      title={`Switch to ${ratio} format`}
                    >
                      {ratio === "9:16" ? "📱 9:16" : ratio === "16:9" ? "🖥️ 16:9" : "⬛ 1:1"}
                    </button>
                  ))}
                </div>
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
                {videoUrl ? (
                  <video controls playsInline className="preview-window__video" src={videoUrl} aria-label="Rendered FYF video preview" />
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
                  <p className="preview-status">No playable video was produced. Review the message beside the source controls.</p>
                ) : (
                  <p className="preview-empty">A playable MP4 appears here only after render and quality checks pass.</p>
                )}
              </div>
            </div>

            {videoUrl && (
              <div
                id="clickhouse-production-audit"
                aria-live="polite"
                style={{
                  marginTop: "0.75rem",
                  padding: "0.75rem 1rem",
                  background: "var(--surface-soft)",
                  border: "1px solid var(--hairline)",
                  borderRadius: "8px",
                  display: "flex",
                  flexDirection: "column",
                  gap: "0.5rem",
                }}
              >
                <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", flexWrap: "wrap", gap: "6px" }}>
                  <span style={{ fontSize: "0.75rem", fontWeight: 700, color: "var(--muted)", textTransform: "uppercase", letterSpacing: "0.05em" }}>
                    📊 ClickHouse Production Audit
                  </span>
                  <span style={{ fontSize: "0.7rem", color: auditCloudConfirmed ? "#16856B" : "var(--muted)", fontWeight: 600, background: auditCloudConfirmed ? "#E6F4EA" : "var(--surface)", padding: "2px 6px", borderRadius: "4px" }}>
                    {auditCloudLabel}
                  </span>
                </div>
                <div style={{ display: "flex", gap: "8px", flexWrap: "wrap", fontSize: "0.75rem" }}>
                  <span style={{ padding: "4px 8px", background: "var(--surface)", border: "1px solid var(--hairline)", borderRadius: "4px" }}>
                    ⚡ <strong>Render:</strong> {auditRenderMs === null ? "Unavailable" : `${(auditRenderMs / 1000).toFixed(1)}s`}
                  </span>
                  <span style={{ padding: "4px 8px", background: "var(--surface)", border: "1px solid var(--hairline)", borderRadius: "4px" }}>
                    🪙 <strong>Tokens:</strong> {auditTokens === null ? "Unavailable" : auditTokens.toLocaleString()}
                  </span>
                  <span style={{ padding: "4px 8px", background: "var(--surface)", border: "1px solid var(--hairline)", borderRadius: "4px" }}>
                    💵 <strong>Cost:</strong> {auditCost === null ? "Unavailable" : `$${auditCost.toFixed(4)}`}
                  </span>
                  <span style={{ padding: "4px 8px", background: "var(--surface)", border: "1px solid var(--hairline)", borderRadius: "4px" }}>
                    🎬 <strong>Aspect:</strong> {renderedAspectRatio}
                  </span>
                  <span style={{ padding: "4px 8px", background: "var(--surface)", border: "1px solid var(--hairline)", borderRadius: "4px" }}>
                    🧾 <strong>Job:</strong> {currentJobId || "Unavailable"}
                  </span>
                </div>
                {telemetryError && <p className="helper-text">{telemetryError}</p>}
                <div style={{ display: "flex", justifyContent: "flex-end", marginTop: "2px" }}>
                  <a
                    href="/telemetry"
                    style={{ fontSize: "0.75rem", color: "var(--focus)", fontWeight: 600, textDecoration: "none", display: "inline-flex", alignItems: "center", gap: "4px" }}
                  >
                    Ask Studio Data Officer (ClickHouse MCP) →
                  </a>
                </div>
              </div>
            )}

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
