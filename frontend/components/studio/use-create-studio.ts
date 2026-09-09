"use client";

import { useState, useRef, useEffect } from "react";
import { useRouter } from "next/navigation";
import {
  API_URL,
  deriveWorkflowStages,
  isRuntimeInfo,
  STATIC_RUNTIME_FALLBACK,
  type RuntimeInfo,
  type VoiceProvider,
} from "../../lib/video-ui";
import {
  DEFAULT_STYLES,
  SCRIPT_JOB_STORAGE_KEY,
  WIZARD_CONTEXT_STORAGE_KEY,
  WIZARD_TOPIC_STORAGE_KEY,
  LOCKED_SCRIPT_STORAGE_KEY,
  fetchWithDeadline,
  finiteMetric,
  formatElapsed,
  isRecord,
  isVideoScript,
  type JobStatus,
  type ProductionTelemetry,
  type StoryVariant,
  type StudioPreset,
  type VideoScript,
  type VideoStyleOption,
  type VisualCacheState,
  type VisualProgress,
} from "./studio-data";

function isAlternativeRequest(message: string) {
  const normalized = message.toLowerCase();
  return (
    /\b(?:3|three)\s+(?:story\s+)?(?:options?|directions?|alternatives?|variants?)\b/.test(normalized) ||
    /\b(?:alternative|alternatives|different directions|story options|story variants)\b/.test(normalized)
  );
}

// Controller hook for the Create Studio workspace. All state, effects, async
// job handlers, and derived audit values live here so the panels stay
// presentational while draft input and submitted director history remain
// separate.
export function useCreateStudio() {
  const router = useRouter();
  const [topic, setTopic] = useState("");
  const [directorMessage, setDirectorMessage] = useState("");
  const [sourceMode, setSourceMode] = useState<"brief" | "full_script">("full_script");
  const [videoTitle, setVideoTitle] = useState("");
  const [submittedMessages, setSubmittedMessages] = useState<string[]>([]);
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

  function applyPreset(preset: StudioPreset) {
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
  const [openingStudio, setOpeningStudio] = useState(false);
  const [runtime, setRuntime] = useState<RuntimeInfo>(STATIC_RUNTIME_FALLBACK);
  const [runtimeSource, setRuntimeSource] = useState<"api" | "fallback">("fallback");
  const [generationAccessToken, setGenerationAccessToken] = useState(() => (
    typeof window === "undefined" ? "" : window.sessionStorage.getItem("fyf-generation-access") || ""
  ));

  const activeVideoControllerRef = useRef<AbortController | null>(null);
  const activeStoryActionRef = useRef(false);
  const effectiveVoiceProvider: VoiceProvider = "gemini";

  async function openSharedStudio() {
    if (!script || !scriptLocked || !scriptLockId || openingStudio) return;
    setOpeningStudio(true);
    setError(null);
    try {
      const storageKey = `fyf-project-for-lock:${scriptLockId}`;
      let projectId = window.sessionStorage.getItem(storageKey);
      if (!projectId || !/^[0-9a-f]{8}$/.test(projectId)) {
        const bytes = new Uint8Array(4);
        window.crypto.getRandomValues(bytes);
        projectId = Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("");
        window.sessionStorage.setItem(storageKey, projectId);
      }
      const response = await fetchWithDeadline(`${API_URL}/api/projects`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          ...(generationAccessToken.trim()
            ? { "X-FYF-Access-Token": generationAccessToken.trim() }
            : {}),
        },
        body: JSON.stringify({
          project_id: projectId,
          script,
          actor: "creative-director",
          idempotency_key: `story-lock:${scriptLockId}`,
          pinned_production_config: {
            voice_provider: "gemini",
            voice_actor: selectedVoiceActor,
            language: selectedLanguage,
            aspect_ratio: aspectRatio,
            style_id: selectedStyle,
          },
        }),
      });
      const body: unknown = await response.json();
      if (!response.ok || !isRecord(body) || body.success !== true) {
        const detail = isRecord(body) ? body.detail : null;
        throw new Error(typeof detail === "string" ? detail : "Could not create the shared Studio project.");
      }
      router.push(`/project/${projectId}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not open the shared Studio.");
      setOpeningStudio(false);
    }
  }

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
      let restoredStartedAt: number | undefined;
      const savedContext = window.sessionStorage.getItem(WIZARD_CONTEXT_STORAGE_KEY);
      if (savedContext) {
        try {
          const parsed: unknown = JSON.parse(savedContext);
          if (isRecord(parsed)) {
            if (typeof parsed.topic === "string") setTopic(parsed.topic);
            if (parsed.sourceMode === "brief" || parsed.sourceMode === "full_script") {
              setSourceMode(parsed.sourceMode);
            }
            if (typeof parsed.videoTitle === "string") setVideoTitle(parsed.videoTitle);
            if (Array.isArray(parsed.submittedMessages)) {
              setSubmittedMessages(parsed.submittedMessages.filter(
                (message): message is string => typeof message === "string",
              ));
            }
            if (typeof parsed.startedAt === "number") restoredStartedAt = parsed.startedAt;
          }
        } catch {
          window.sessionStorage.removeItem(WIZARD_CONTEXT_STORAGE_KEY);
        }
      }
      const savedTopic = window.sessionStorage.getItem(WIZARD_TOPIC_STORAGE_KEY);
      if (savedTopic && !savedContext) setTopic(savedTopic);

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
            setSelectedLanguage(parsed.script.language);
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
        await pollScriptJob(activeJob, {
          isCancelled: () => cancelled,
          startedAt: restoredStartedAt,
        });
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
  }, []);

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

  async function pollScriptJob(jobId: string, opts: { isCancelled?: () => boolean; startedAt?: number } = {}) {
    const startTime = opts.startedAt || Date.now();
    while (Date.now() - startTime < 45 * 60 * 1000) {
      if (opts.isCancelled?.()) return;
      const statusRes = await fetchWithDeadline(`${API_URL}/api/script-jobs/${jobId}/status`);
      if (!statusRes.ok) throw new Error("Could not check script job status");
      const job: unknown = await statusRes.json();
      if (!isRecord(job) || typeof job.status !== "string") throw new Error("Malformed script job payload");

      const elapsed = `· ${formatElapsed(Date.now() - startTime)} elapsed`;
      const progress = typeof job.progress === "number" ? ` (${job.progress}%)` : "";
      if (job.stage === "adk_producer") {
        setScriptProgress(`Google ADK Producer Agent running… ${progress} ${elapsed}. This stage drafts and structures the full story, so it can take several minutes.`);
      } else if (job.stage === "narration") {
        setScriptProgress(`Writing narration with Vertex… ${progress} ${elapsed}`);
      } else if (job.stage === "storyboard" || job.stage === "visual_lock") {
        const batch = typeof job.batch === "number" ? job.batch : 1;
        const count = typeof job.batch_count === "number" ? job.batch_count : "?";
        setScriptProgress(sourceMode === "full_script"
          ? `Planning scenes and visuals from your supplied script… ${progress} ${elapsed}`
          : `Building visual story batch ${batch}/${count}… ${progress} ${elapsed}`);
      } else if (job.stage === "retrying") {
        setScriptProgress(`Provider hiccup — auto-retrying from the saved checkpoint… ${elapsed}`);
      } else {
        setScriptProgress(`Preparing the script job… ${progress} ${elapsed}`);
      }

      if (job.status === "completed" && isVideoScript(job.data) && typeof job.lock_id === "string" && /^[0-9a-f]{8}$/.test(job.lock_id)) {
        setScript(job.data);
        setSelectedLanguage(job.data.language);
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

  async function generateScript(topicOverride?: string) {
    const requestTopic = (topicOverride ?? topic).trim();
    const requestTitle = videoTitle.trim();
    if (!requestTopic || (sourceMode === "full_script" && !requestTitle) || !generationReady || activeStoryActionRef.current) return;
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
    window.sessionStorage.setItem(WIZARD_TOPIC_STORAGE_KEY, requestTopic);
    const startedAt = Date.now();
    window.sessionStorage.setItem(WIZARD_CONTEXT_STORAGE_KEY, JSON.stringify({
      topic: requestTopic,
      sourceMode,
      videoTitle: requestTitle,
      submittedMessages: [
        ...submittedMessages,
        ...(submittedMessages.at(-1) === requestTopic ? [] : [requestTopic]),
      ],
      startedAt,
    }));
    window.sessionStorage.removeItem(LOCKED_SCRIPT_STORAGE_KEY);

    try {
      const res = await fetchWithDeadline(`${API_URL}/api/generate-script`, {
        method: "POST",
        headers: generationRequestHeaders(),
        body: JSON.stringify({
          topic: requestTopic,
          source_mode: sourceMode,
          title: sourceMode === "full_script" ? requestTitle : undefined,
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
        await pollScriptJob(jobId, { startedAt });
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

  async function polishStory(topicOverride?: string) {
    const requestTopic = (topicOverride ?? topic).trim();
    if (!requestTopic || !generationReady || activeStoryActionRef.current) return;
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
          topic_or_draft: requestTopic,
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

  async function submitDirectorMessage(message: string) {
    const submittedMessage = message.trim();
    if (!submittedMessage || !generationReady || writingStatus === "writing" || activeStoryActionRef.current) return;

    setSubmittedMessages((current) => (
      current.at(-1) === submittedMessage ? current : [...current, submittedMessage]
    ));
    setDirectorMessage("");
    if (sourceMode === "brief" && isAlternativeRequest(submittedMessage)) {
      await polishStory(submittedMessage);
    } else {
      await generateScript(submittedMessage);
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

  return {
    // Source + story inputs
    topic,
    setTopic: (value: string) => {
      setTopic(value);
      setDirectorMessage(value);
    },
    directorMessage,
    setDirectorMessage: (value: string) => {
      setDirectorMessage(value);
      setTopic(value);
    },
    sourceMode,
    setSourceMode,
    videoTitle,
    setVideoTitle,
    submittedMessages,
    durationMode,
    availableStyles,
    selectedStyle,
    setSelectedStyle,
    selectedPersona,
    setSelectedPersona,
    includeMascot,
    setIncludeMascot,
    activePreset,
    studioName,
    setStudioName,
    selectedLanguage,
    setSelectedLanguage,
    presenterMode,
    setPresenterMode,
    selectedVoiceActor,
    setSelectedVoiceActor,
    effectiveVoiceProvider,
    auditioning,
    auditionVoice,
    applyPreset,
    // Brand kit
    ctaText,
    setCtaText,
    retentionProgressBar,
    setRetentionProgressBar,
    animatedLowerThirds,
    setAnimatedLowerThirds,
    // Generation gating + runtime
    generationReady,
    generationAccessToken,
    updateGenerationAccessToken,
    runtime,
    runtimeSource,
    // Story state
    script,
    variants,
    selectedVariant,
    setSelectedVariant,
    scriptLocked,
    scriptLockId,
    openingStudio,
    openSharedStudio,
    storyModel,
    writingStatus,
    resumableScriptJobId,
    scriptProgress,
    updateSelectedNarration,
    updateSegmentField,
    generateScript,
    polishStory,
    submitDirectorMessage,
    resumeScriptJob,
    approveAndLock,
    // Render + preview
    aspectRatio,
    setAspectRatio,
    videoUrl,
    renderStatus,
    renderBusy,
    visualProgress,
    renderProgress,
    renderedAspectRatio,
    currentJobId,
    telemetryError,
    generateVideo,
    error,
    // Workflow strip
    workflowStages,
    // Production audit (ClickHouse) derived metrics
    auditRenderMs,
    auditTokens,
    auditCost,
    auditCloudConfirmed,
    auditCloudLabel,
  };
}

export type CreateStudioController = ReturnType<typeof useCreateStudio>;
