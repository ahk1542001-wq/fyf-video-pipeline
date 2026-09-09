"use client";

import { useCallback, useEffect, useMemo, useState, type FormEvent } from "react";
import StudioHeader from "../../components/studio-header";
import {
  API_URL,
  STATIC_RUNTIME_FALLBACK,
  type JobTelemetry,
  type RuntimeInfo,
  type SceneTelemetry,
  type TelemetrySummary,
} from "../../lib/video-ui";

type JobTelemetryResponse = {
  job: JobTelemetry;
  scenes: SceneTelemetry[];
  scene_count?: number;
  qa_records?: Array<Record<string, unknown>>;
  qa_count?: number;
  connected_to_cloud?: boolean;
};

type DeliveryInfo = {
  pending: number | null;
  failed: number | null;
  delivered: number | null;
  schema_ready: boolean | null;
  cloud_connected: boolean;
  drain_blocked_reason: string | null;
  ingestion_lag_seconds: number | null;
  last_delivered_at: string | null;
};

type TelemetrySummaryWithDelivery = TelemetrySummary & {
  source?: string;
  clickhouse_status?: string;
  delivery?: DeliveryInfo;
  ingestion_lag_seconds?: number | null;
  cloud?: {
    connected?: boolean;
    status?: string;
    label?: string;
  };
};

type QueryResult = {
  columns: string[];
  rows: Array<Array<string | number | boolean | null>>;
  row_count: number;
  duration_ms: number;
  source: string;
  availability?: string;
  delivery?: DeliveryInfo;
  ingestion_lag_seconds?: number | null;
  freshness?: string | null;
};

type QueryId =
  | "creation_timeline"
  | "cost_summary"
  | "quality_tracking"
  | "editing_friction"
  | "version_comparison"
  | "grounded_recommendations"
  | "jobs_overview"
  | "model_calls"
  | "scene_latency";

const PRESET_QUERIES: Array<{ label: string; queryId: QueryId }> = [
  { label: "Creation Timeline", queryId: "creation_timeline" },
  { label: "Cost Intelligence", queryId: "cost_summary" },
  { label: "Quality Tracking", queryId: "quality_tracking" },
  { label: "Editing Friction", queryId: "editing_friction" },
  { label: "Version Comparison", queryId: "version_comparison" },
  { label: "Grounded Recommendations", queryId: "grounded_recommendations" },
  { label: "Jobs Summary", queryId: "jobs_overview" },
  { label: "Model Usage", queryId: "model_calls" },
  { label: "Scene Latencies", queryId: "scene_latency" },
];

const EMPTY_JOBS: JobTelemetry[] = [];

function formatCount(value: number | null | undefined): string {
  return value === null || value === undefined ? "Unavailable" : value.toLocaleString();
}

function formatCost(value: number | null | undefined): string {
  return value === null || value === undefined || !Number.isFinite(value) ? "Unavailable" : `$${value.toFixed(4)}`;
}

function formatDuration(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "Unavailable";
  if (value >= 1000) return `${(value / 1000).toFixed(1)}s`;
  return `${Math.round(value)}ms`;
}

function formatDate(value: string | undefined): string {
  if (!value) return "Date unavailable";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return "Date unavailable";
  return parsed.toLocaleString([], {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

function statusForJob(job: JobTelemetry): string {
  return String(job.status || job.summary?.job_status || "unknown").toLowerCase();
}

function statusLabel(status: string | null | undefined): string {
  const normalized = String(status || "unknown").replace(/[_-]+/g, " ");
  return normalized.charAt(0).toUpperCase() + normalized.slice(1);
}

function statusClass(status: string | null | undefined): string {
  const normalized = String(status || "unknown").toLowerCase();
  if (["completed", "complete", "succeeded", "success", "passed"].includes(normalized)) {
    return "status-chip status-chip--success";
  }
  if (["failed", "failure", "error", "cancelled", "canceled"].includes(normalized)) {
    return "status-chip status-chip--warning";
  }
  return "status-chip status-chip--neutral";
}

function isSuccessful(status: string): boolean {
  return ["completed", "complete", "succeeded", "success", "passed"].includes(status);
}

function isTerminal(status: string): boolean {
  return [
    "completed",
    "complete",
    "succeeded",
    "success",
    "passed",
    "failed",
    "failure",
    "error",
    "cancelled",
    "canceled",
  ].includes(status);
}

function successRate(jobs: JobTelemetry[]): { value: number | null; successful: number; known: number } {
  const outcomes = jobs.map(statusForJob).filter(isTerminal);
  const successful = outcomes.filter(isSuccessful).length;
  return {
    value: outcomes.length > 0 ? Math.round((successful / outcomes.length) * 100) : null,
    successful,
    known: outcomes.length,
  };
}

function jobKind(job: JobTelemetry): string {
  return job.job_kind || job.voice_mode || "production";
}

function existingGenerationAccessHeaders(): HeadersInit {
  if (typeof window === "undefined") return {};
  const token = window.sessionStorage.getItem("fyf-generation-access")?.trim();
  return token ? { "X-FYF-Access-Token": token } : {};
}

function StatusChip({ status }: { status: string | null | undefined }) {
  return <span className={statusClass(status)}>{statusLabel(status)}</span>;
}

function EmptyState({ children }: { children: string }) {
  return <div className="telemetry-empty">{children}</div>;
}

export default function TelemetryPage() {
  const [runtime, setRuntime] = useState<RuntimeInfo>(STATIC_RUNTIME_FALLBACK);
  const [runtimeSource, setRuntimeSource] = useState<"api" | "fallback">("fallback");
  const [summary, setSummary] = useState<TelemetrySummaryWithDelivery | null>(null);
  const [selectedJobId, setSelectedJobId] = useState("");
  const [jobDetails, setJobDetails] = useState<JobTelemetryResponse | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [lastSynced, setLastSynced] = useState("");
  const [loadError, setLoadError] = useState<string | null>(null);
  const [autoRefresh, setAutoRefresh] = useState(false);
  const [searchTerm, setSearchTerm] = useState("");

  const [selectedQueryId, setSelectedQueryId] = useState<QueryId>(PRESET_QUERIES[0].queryId);
  const [queryResult, setQueryResult] = useState<QueryResult | null>(null);
  const [queryLoading, setQueryLoading] = useState(false);
  const [queryError, setQueryError] = useState<string | null>(null);

  const [officerQuestion, setOfficerQuestion] = useState("");
  const [officerAnswer, setOfficerAnswer] = useState<string | null>(null);
  const [officerToolUsed, setOfficerToolUsed] = useState(false);
  const [officerBusy, setOfficerBusy] = useState(false);
  const [officerError, setOfficerError] = useState<string | null>(null);

  const loadData = useCallback(async () => {
    setIsLoading(true);
    setLoadError(null);
    try {
      const [runtimeRes, telemetryRes] = await Promise.all([
        fetch(`${API_URL}/api/runtime`).catch(() => null),
        fetch(`${API_URL}/api/telemetry`).catch(() => null),
      ]);

      if (runtimeRes?.ok) {
        setRuntime(await runtimeRes.json());
        setRuntimeSource("api");
      }

      let nextJobId = selectedJobId;
      if (telemetryRes?.ok) {
        const telemetryData = (await telemetryRes.json()) as TelemetrySummaryWithDelivery;
        setSummary(telemetryData);
        const availableJobs = Array.isArray(telemetryData.jobs) ? telemetryData.jobs : [];
        if (!nextJobId || !availableJobs.some(job => job.job_id === nextJobId)) {
          nextJobId = availableJobs[0]?.job_id || "";
          if (nextJobId) setSelectedJobId(nextJobId);
        }
      } else {
        setLoadError("Telemetry is unavailable right now. No local or ClickHouse record was changed.");
      }

      if (nextJobId) {
        const jobRes = await fetch(`${API_URL}/api/jobs/${nextJobId}/telemetry`).catch(() => null);
        if (jobRes?.ok) {
          setJobDetails((await jobRes.json()) as JobTelemetryResponse);
        } else {
          setJobDetails(null);
        }
      } else {
        setJobDetails(null);
      }
      setLastSynced(new Date().toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }));
    } catch (error) {
      setLoadError(error instanceof Error ? error.message : "Telemetry is unavailable right now.");
    } finally {
      setIsLoading(false);
    }
  }, [selectedJobId]);

  useEffect(() => {
    const timer = window.setTimeout(() => void loadData(), 0);
    return () => window.clearTimeout(timer);
  }, [loadData]);

  useEffect(() => {
    if (!autoRefresh) return;
    const timer = window.setInterval(() => void loadData(), 15000);
    return () => window.clearInterval(timer);
  }, [autoRefresh, loadData]);

  const jobs = summary?.jobs ?? EMPTY_JOBS;
  const filteredJobs = useMemo(() => {
    const needle = searchTerm.trim().toLowerCase();
    if (!needle) return jobs;
    return jobs.filter(job => {
      const haystack = [job.job_id, job.title, job.job_kind, job.voice_mode, statusForJob(job)]
        .filter(Boolean)
        .join(" ")
        .toLowerCase();
      return haystack.includes(needle);
    });
  }, [jobs, searchTerm]);

  const selectedJob = jobs.find(job => job.job_id === selectedJobId) || jobs[0] || null;
  const selectedSummary = jobDetails?.job.summary;
  const selectedCalls = jobDetails?.job.calls || [];
  const selectedScenes = jobDetails?.scenes || [];
  const outcomes = successRate(jobs);
  const sourceStatusKnown = Boolean(
    summary && (
      summary.cloud?.connected !== undefined ||
      summary.delivery?.cloud_connected !== undefined ||
      summary.source !== undefined
    ),
  );
  const cloudConnected = summary?.cloud?.connected === true || summary?.delivery?.cloud_connected === true || summary?.source === "clickhouse";
  const sourceLabel = !summary
    ? "Waiting for telemetry"
    : cloudConnected
      ? "ClickHouse connected"
      : sourceStatusKnown
        ? "Local mirror only"
        : "Source status unavailable";
  const sourceDescription = cloudConnected
    ? "The latest summary is connected to ClickHouse."
    : !summary
      ? "Waiting for the first telemetry response."
      : sourceStatusKnown
        ? "ClickHouse is not connected; this view is showing the recorded local mirror."
        : "The API did not report whether this summary is local or remote.";

  const timelineEvents = [
    ...selectedCalls.map(call => ({
      id: `call-${call.call_id}`,
      label: call.stage,
      detail: call.operation,
      duration: call.duration_ms,
      status: call.status,
    })),
    ...selectedScenes.map(scene => ({
      id: `scene-${scene.scene_id}`,
      label: scene.scene_id,
      detail: scene.treatment_type,
      duration: scene.render_time_ms,
      status: "recorded",
    })),
  ];
  const maxTimelineDuration = Math.max(...timelineEvents.map(event => event.duration || 0), 1);

  async function runClickHouseQuery(queryId: QueryId = selectedQueryId) {
    if (queryLoading) return;
    setQueryLoading(true);
    setQueryError(null);
    try {
      const response = await fetch(`${API_URL}/api/clickhouse/query`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          ...existingGenerationAccessHeaders(),
        },
        body: JSON.stringify({ query_id: queryId }),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(typeof data.detail === "string" ? data.detail : "Query execution failed");
      }
      setQueryResult(data as QueryResult);
    } catch (error) {
      setQueryError(error instanceof Error ? error.message : "Query execution failed");
    } finally {
      setQueryLoading(false);
    }
  }

  async function askDataOfficer(event: FormEvent) {
    event.preventDefault();
    const question = officerQuestion.trim();
    if (!question || officerBusy) return;
    setOfficerBusy(true);
    setOfficerError(null);
    setOfficerAnswer(null);
    setOfficerToolUsed(false);
    try {
      const response = await fetch(`${API_URL}/api/insights`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          ...existingGenerationAccessHeaders(),
        },
        body: JSON.stringify({ question }),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(typeof data.detail === "string" ? data.detail : "Data Officer unavailable");
      }
      setOfficerAnswer(String(data.answer ?? ""));
      setOfficerToolUsed(Boolean(data.tool_used));
    } catch (error) {
      setOfficerError(error instanceof Error ? error.message : "Request failed");
    } finally {
      setOfficerBusy(false);
    }
  }

  return (
    <div className="app-shell telemetry-page">
      <StudioHeader runtime={runtime} runtimeSource={runtimeSource} />

      <main className="telemetry-main">
        <header className="telemetry-hero">
          <div className="telemetry-hero__copy">
            <h1>Generation telemetry</h1>
            <p>
              A clear record of production activity, cost signals, and render evidence. Every value below comes from telemetry that was actually recorded.
            </p>
          </div>

          <div className="telemetry-hero__controls">
            <button type="button" className="telemetry-button telemetry-button--secondary" onClick={() => void loadData()} disabled={isLoading}>
              {isLoading ? "Refreshing…" : "Refresh data"}
            </button>
            <button
              type="button"
              className={`telemetry-button ${autoRefresh ? "telemetry-button--primary" : "telemetry-button--secondary"}`}
              onClick={() => setAutoRefresh(value => !value)}
              aria-pressed={autoRefresh}
            >
              Auto-sync: {autoRefresh ? "ON (15s)" : "OFF"}
            </button>
            <div className="telemetry-sync" data-testid="telemetry-source" aria-live="polite">
              <span className={`telemetry-sync__dot${cloudConnected ? " telemetry-sync__dot--connected" : ""}`} aria-hidden="true" />
              <span className="telemetry-sync__copy">
                <strong>ClickHouse sync</strong>
                <span>{sourceLabel}</span>
              </span>
            </div>
          </div>
        </header>

        <section className="telemetry-section telemetry-overview" aria-labelledby="overview-title">
          <div className="telemetry-section-heading">
            <div>
              <h2 id="overview-title">Overview</h2>
              <p>Start with the facts we can verify now, then choose a production to inspect.</p>
            </div>
            <div className="telemetry-section-heading__meta">
              <span>{sourceDescription}</span>
              <span>{lastSynced ? `Checked ${lastSynced}` : "Not checked yet"}</span>
            </div>
          </div>

          {loadError && <p className="telemetry-alert" role="alert">{loadError}</p>}

          <div className="telemetry-metric-grid">
            <article className="telemetry-metric" data-testid="metric-total-generations">
              <p>Total generations</p>
              <strong>{formatCount(summary?.total_jobs)}</strong>
              <span>Recorded production rows</span>
            </article>
            <article className="telemetry-metric" data-testid="metric-known-cost">
              <p>Known cost</p>
              <strong>{formatCost(summary?.total_cost_usd)}</strong>
              <span>{summary?.total_cost_usd === null || summary?.total_cost_usd === undefined ? "No priced records available" : "Sum of recorded priced jobs"}</span>
            </article>
            <article className="telemetry-metric" data-testid="metric-success-rate">
              <p>Success rate</p>
              <strong>{outcomes.value === null ? "Unavailable" : `${outcomes.value}%`}</strong>
              <span>{outcomes.known ? `${outcomes.successful} of ${outcomes.known} recorded outcomes` : "No recorded outcomes"}</span>
            </article>
            <article className="telemetry-metric">
              <p>Recorded tokens</p>
              <strong>{formatCount(summary?.total_tokens_used)}</strong>
              <span>{summary?.total_tokens_used === null || summary?.total_tokens_used === undefined ? "Token total unavailable" : "Sum of recorded token totals"}</span>
            </article>
          </div>

          <dl className="telemetry-sync-facts">
            <div>
              <dt>Cloud status</dt>
              <dd>{cloudConnected ? "Connected" : summary ? "Not connected" : "Unavailable"}</dd>
            </div>
            <div>
              <dt>Local outbox</dt>
              <dd>{summary?.delivery?.pending === null || summary?.delivery?.pending === undefined ? "Unavailable" : `${summary.delivery.pending} pending`}</dd>
            </div>
            <div>
              <dt>Ingestion lag</dt>
              <dd>{summary?.ingestion_lag_seconds === null || summary?.ingestion_lag_seconds === undefined ? "Unavailable" : `${summary.ingestion_lag_seconds}s`}</dd>
            </div>
          </dl>
        </section>

        <section className="telemetry-section telemetry-productions" aria-labelledby="productions-title">
          <div className="telemetry-section-heading">
            <div>
              <h2 id="productions-title">Productions</h2>
              <p>Search a real production record, then keep its summary beside the list.</p>
            </div>
            <span className="telemetry-count">{filteredJobs.length} shown · {jobs.length} recorded</span>
          </div>

          <div className="telemetry-productions__layout">
            <div className="production-browser">
              <div className="production-browser__toolbar">
                <label htmlFor="production-search">Search productions</label>
                <input
                  id="production-search"
                  type="search"
                  role="searchbox"
                  aria-label="Search productions"
                  value={searchTerm}
                  onChange={event => setSearchTerm(event.target.value)}
                  placeholder="Title, job ID, or status"
                />
              </div>

              <div className="production-list" role="list" aria-label="Recorded productions">
                {filteredJobs.map(job => {
                  const selected = selectedJob?.job_id === job.job_id;
                  return (
                    <button
                      key={job.job_id}
                      type="button"
                      className={`production-row${selected ? " production-row--selected" : ""}`}
                      onClick={() => {
                        setSelectedJobId(job.job_id);
                        setJobDetails(null);
                      }}
                      aria-pressed={selected}
                    >
                      <span className="production-row__main">
                        <strong>{job.title || "Untitled production"}</strong>
                        <span>{job.job_id} · {jobKind(job)}</span>
                      </span>
                      <span className="production-row__meta">
                        <StatusChip status={statusForJob(job)} />
                        <span>{formatDate(job.created_at)}</span>
                      </span>
                    </button>
                  );
                })}
                {!jobs.length && <EmptyState>No productions recorded yet.</EmptyState>}
                {jobs.length > 0 && !filteredJobs.length && <EmptyState>No productions match that search.</EmptyState>}
              </div>
            </div>

            <aside className="selected-production" data-testid="selected-production" aria-labelledby="selected-production-title">
              {selectedJob ? (
                <>
                  <div className="selected-production__topline">
                    <span>Selected production</span>
                    <StatusChip status={statusForJob(selectedJob)} />
                  </div>
                  <h3 id="selected-production-title">{selectedJob.title || "Untitled production"}</h3>
                  <p className="selected-production__id">{selectedJob.job_id} · {jobKind(selectedJob)}</p>
                  <dl className="selected-production__facts">
                    <div>
                      <dt>Known cost</dt>
                      <dd>{formatCost(selectedSummary?.estimated_cost_usd ?? selectedJob.cost_usd)}</dd>
                    </div>
                    <div>
                      <dt>Duration</dt>
                      <dd>{selectedJob.duration_sec === null || selectedJob.duration_sec === undefined || !Number.isFinite(selectedJob.duration_sec) ? "Unavailable" : `${selectedJob.duration_sec.toFixed(1)}s`}</dd>
                    </div>
                    <div>
                      <dt>Tokens</dt>
                      <dd>{formatCount(selectedSummary?.total_tokens ?? selectedJob.total_tokens_used)}</dd>
                    </div>
                    <div>
                      <dt>Created</dt>
                      <dd>{formatDate(selectedJob.created_at)}</dd>
                    </div>
                  </dl>
                  <p className="selected-production__hint">Details below update when this production is selected.</p>
                </>
              ) : (
                <EmptyState>Select a production to see its recorded summary.</EmptyState>
              )}
            </aside>
          </div>
        </section>

        <section className="telemetry-section telemetry-details" aria-labelledby="details-title">
          <div className="telemetry-section-heading">
            <div>
              <h2 id="details-title">Details</h2>
              <p>Inspect the selected production’s timing, retries, provider calls, and scene evidence.</p>
            </div>
            {selectedJob && <span className="telemetry-count">{selectedJob.job_id}</span>}
          </div>

          {selectedJob ? (
            <div className="telemetry-detail-grid">
              <section className="detail-panel detail-panel--timeline" aria-labelledby="timeline-title">
                <div className="detail-panel__heading">
                  <div>
                    <h3 id="timeline-title">Performance timeline</h3>
                    <p>Recorded call and scene durations, shown in the order available.</p>
                  </div>
                  <span>{formatDuration(selectedJob.total_render_time_ms)} total render</span>
                </div>
                {timelineEvents.length ? (
                  <ol className="telemetry-timeline">
                    {timelineEvents.map(event => (
                      <li key={event.id}>
                        <div className="telemetry-timeline__label">
                          <strong>{event.label}</strong>
                          <span>{event.detail}</span>
                        </div>
                        <div className="telemetry-timeline__track" aria-label={`${event.label} ${formatDuration(event.duration)}`}>
                          <span style={{ width: `${Math.max(16, Math.round(((event.duration || 0) / maxTimelineDuration) * 100))}%` }} />
                        </div>
                        <div className="telemetry-timeline__meta">
                          <StatusChip status={event.status} />
                          <time>{formatDuration(event.duration)}</time>
                        </div>
                      </li>
                    ))}
                  </ol>
                ) : (
                  <EmptyState>No timing records for this production.</EmptyState>
                )}
              </section>

              <section className="detail-panel" aria-labelledby="calls-title">
                <div className="detail-panel__heading">
                  <div>
                    <h3 id="calls-title">Provider calls</h3>
                    <p>SDK calls recorded for the selected production.</p>
                  </div>
                  <span>{formatCount(selectedSummary?.total_calls)} recorded</span>
                </div>
                {selectedCalls.length ? (
                  <ul className="call-list">
                    {selectedCalls.map(call => (
                      <li key={call.call_id}>
                        <div>
                          <strong>{call.stage}</strong>
                          <span>{call.operation} · attempt {call.attempt}</span>
                        </div>
                        <div className="call-list__model">{call.model || "Model unavailable"}</div>
                        <div className="call-list__duration">
                          <StatusChip status={call.status} />
                          <time>{formatDuration(call.duration_ms)}</time>
                        </div>
                      </li>
                    ))}
                  </ul>
                ) : (
                  <EmptyState>No provider calls recorded for this production.</EmptyState>
                )}
              </section>

              <section className="detail-panel" aria-labelledby="health-title">
                <div className="detail-panel__heading">
                  <div>
                    <h3 id="health-title">Cache and retries</h3>
                    <p>Signals recorded by the provider and pipeline.</p>
                  </div>
                </div>
                <dl className="detail-fact-grid">
                  <div>
                    <dt>Retries</dt>
                    <dd>{formatCount(selectedSummary?.retry_calls)}</dd>
                  </div>
                  <div>
                    <dt>Failed calls</dt>
                    <dd>{formatCount(selectedSummary?.failed_calls)}</dd>
                  </div>
                  <div>
                    <dt>Cached input</dt>
                    <dd>{formatCount(selectedSummary?.total_cached_input_tokens)}</dd>
                  </div>
                  <div>
                    <dt>Token record</dt>
                    <dd>{selectedSummary?.token_status || "Unavailable"}</dd>
                  </div>
                </dl>
              </section>

              <section className="detail-panel detail-panel--evidence" aria-labelledby="evidence-title">
                <div className="detail-panel__heading">
                  <div>
                    <h3 id="evidence-title">Scene evidence</h3>
                    <p>Scene-level render latency and evidence counts from the selected record.</p>
                  </div>
                  <span>{selectedScenes.length} scenes</span>
                </div>
                {selectedScenes.length ? (
                  <ul className="evidence-list">
                    {selectedScenes.map(scene => (
                      <li key={scene.scene_id}>
                        <div>
                          <strong>{scene.scene_id}</strong>
                          <span>{scene.treatment_type || "Treatment unavailable"}</span>
                        </div>
                        <div>
                          <span>{scene.evidence_claim_count} claims</span>
                          <span>{formatDuration(scene.render_time_ms)} render · {formatDuration(scene.vertex_latency_ms)} Vertex</span>
                        </div>
                      </li>
                    ))}
                  </ul>
                ) : (
                  <EmptyState>No scene evidence recorded for this production.</EmptyState>
                )}
              </section>
            </div>
          ) : (
            <EmptyState>Select a production above to load its detail record.</EmptyState>
          )}
        </section>

        <details className="telemetry-advanced" data-testid="advanced-telemetry">
          <summary>
            <span>Advanced telemetry</span>
            <span>Bounded queries, Data Officer, and privacy facts</span>
          </summary>
          <div className="telemetry-advanced__content">
            <section className="advanced-panel" aria-labelledby="query-console-title">
              <div className="detail-panel__heading">
                <div>
                  <h3 id="query-console-title">ClickHouse query console</h3>
                  <p>Run a server-owned read-only preset. SQL text is never accepted in this interface.</p>
                </div>
                <span>ClickHouse only</span>
              </div>

              <div className="query-toolbar">
                <label htmlFor="clickhouse-query-selector">Preset</label>
                <select id="clickhouse-query-selector" value={selectedQueryId} onChange={event => setSelectedQueryId(event.target.value as QueryId)}>
                  {PRESET_QUERIES.map(preset => <option key={preset.queryId} value={preset.queryId}>{preset.label}</option>)}
                </select>
                <button id="run-query-button" type="button" className="telemetry-button telemetry-button--primary" onClick={() => void runClickHouseQuery()} disabled={queryLoading}>
                  {queryLoading ? "Executing…" : "Run query"}
                </button>
              </div>

              <div className="query-presets" aria-label="Approved ClickHouse query presets">
                {PRESET_QUERIES.map(preset => (
                  <button
                    key={preset.queryId}
                    type="button"
                    className={`query-preset${selectedQueryId === preset.queryId ? " query-preset--selected" : ""}`}
                    onClick={() => {
                      setSelectedQueryId(preset.queryId);
                      void runClickHouseQuery(preset.queryId);
                    }}
                  >
                    {preset.label}
                  </button>
                ))}
              </div>

              {queryError && <p className="telemetry-alert" role="alert">{queryError}</p>}
              {queryResult && (
                <div className="query-result">
                  <div className="query-result__meta">
                    <span>Rows <strong>{queryResult.row_count}</strong></span>
                    <span>Latency <strong>{queryResult.duration_ms}ms</strong></span>
                    <span>Source <strong>{queryResult.source}</strong></span>
                    <span>Availability <strong>{queryResult.availability || "Unavailable"}</strong></span>
                  </div>
                  <div className="query-result__table-wrap">
                    <table>
                      <thead><tr>{queryResult.columns.map(column => <th key={column}>{column}</th>)}</tr></thead>
                      <tbody>
                        {queryResult.rows.map((row, rowIndex) => (
                          <tr key={rowIndex}>{row.map((cell, cellIndex) => <td key={cellIndex}>{cell === null ? "null" : String(cell)}</td>)}</tr>
                        ))}
                        {!queryResult.rows.length && <tr><td colSpan={queryResult.columns.length || 1}>Query returned 0 rows.</td></tr>}
                      </tbody>
                    </table>
                  </div>
                </div>
              )}
            </section>

            <section className="advanced-panel" aria-labelledby="data-officer-title">
              <div className="detail-panel__heading">
                <div>
                  <h3 id="data-officer-title">Ask the Data Officer</h3>
                  <p>Ask a plain-language question about recorded production telemetry.</p>
                </div>
                <span>ClickHouse Cloud</span>
              </div>
              <form className="officer-form" onSubmit={askDataOfficer}>
                <label htmlFor="officer-question">Question</label>
                <div>
                  <input
                    id="officer-question"
                    type="text"
                    value={officerQuestion}
                    onChange={event => setOfficerQuestion(event.target.value)}
                    placeholder="e.g. How many video jobs succeeded this week?"
                    maxLength={500}
                  />
                  <button type="submit" className="telemetry-button telemetry-button--primary" disabled={officerBusy || !officerQuestion.trim()}>
                    {officerBusy ? "Querying…" : "Ask"}
                  </button>
                </div>
              </form>
              {officerError && <p className="telemetry-alert" role="alert">{officerError}</p>}
              {officerAnswer !== null && (
                <div className="officer-answer">
                  <p>{officerAnswer}</p>
                  <span>{officerToolUsed ? "Answered from a live ClickHouse query" : "Answered without tool use"}</span>
                </div>
              )}
            </section>

            <section className="advanced-panel advanced-panel--boundary" aria-labelledby="boundary-title">
              <div className="detail-panel__heading">
                <div>
                  <h3 id="boundary-title">Telemetry boundary</h3>
                  <p>What this ledger records, and what it deliberately leaves out.</p>
                </div>
              </div>
              <ul className="boundary-list">
                <li>Prompts excluded from telemetry</li>
                <li>Response text excluded from telemetry</li>
                <li>Credentials excluded from telemetry</li>
                <li>Cost confidence remains labeled when pricing is partial or unavailable</li>
              </ul>
            </section>
          </div>
        </details>
      </main>
    </div>
  );
}
