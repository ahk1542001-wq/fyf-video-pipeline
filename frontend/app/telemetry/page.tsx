"use client";

import { useEffect, useState } from "react";
import StudioHeader from "../../components/studio-header";
import {
  API_URL,
  STATIC_RUNTIME_FALLBACK,
  type RuntimeInfo,
  type TelemetrySummary,
  type JobTelemetry,
  type SceneTelemetry,
} from "../../lib/video-ui";

type JobTelemetryResponse = {
  job: JobTelemetry;
  scenes: SceneTelemetry[];
};

function formatCount(value: number | null | undefined): string {
  return value === null || value === undefined ? "—" : value.toLocaleString();
}

function formatCost(value: number | null | undefined): string {
  return value === null || value === undefined ? "Unpriced" : `$${value.toFixed(4)}`;
}

function statusTone(status: string | null | undefined): string {
  if (status === "completed" || status === "exact" || status === "succeeded") {
    return "text-[#16856B] bg-[#16856B]/10 border-[#16856B]/20";
  }
  if (status === "failed" || status === "unpriced") {
    return "text-[#B45309] bg-[#B45309]/10 border-[#B45309]/20";
  }
  return "text-[#30382C]/70 bg-[#30382C]/5 border-[#30382C]/10";
}

export default function TelemetryPage() {
  const [runtime, setRuntime] = useState<RuntimeInfo>(STATIC_RUNTIME_FALLBACK);
  const [runtimeSource, setRuntimeSource] = useState<"api" | "fallback">("fallback");
  const [summary, setSummary] = useState<TelemetrySummary | null>(null);
  const [selectedJobId, setSelectedJobId] = useState<string>("");
  const [jobDetails, setJobDetails] = useState<JobTelemetryResponse | null>(null);
  const [isLoading, setIsLoading] = useState<boolean>(true);

  useEffect(() => {
    async function loadData() {
      setIsLoading(true);
      try {
        // 1. Fetch runtime
        const runtimeRes = await fetch(`${API_URL}/api/runtime`).catch(() => null);
        if (runtimeRes && runtimeRes.ok) {
          const rData = await runtimeRes.json();
          setRuntime(rData);
          setRuntimeSource("api");
        }

        // 2. Fetch telemetry overview
        const telRes = await fetch(`${API_URL}/api/telemetry`).catch(() => null);
        if (telRes && telRes.ok) {
          const tData = await telRes.json();
          setSummary(tData);
          if (!selectedJobId && Array.isArray(tData.jobs) && tData.jobs.length > 0) {
            setSelectedJobId(tData.jobs[0].job_id);
          }
        }

        // 3. Fetch the selected job details after the overview identifies a real job.
        if (selectedJobId) {
          const jobRes = await fetch(`${API_URL}/api/jobs/${selectedJobId}/telemetry`).catch(() => null);
          if (jobRes && jobRes.ok) {
            const jData = await jobRes.json();
            setJobDetails(jData);
          }
        }
      } catch (err) {
        console.error("Telemetry load failed:", err);
      } finally {
        setIsLoading(false);
      }
    }
    loadData();
  }, [selectedJobId]);

  return (
    <div className="min-h-screen bg-[#F4F0E6] text-[#30382C]">
      <StudioHeader runtime={runtime} runtimeSource={runtimeSource} />

      <main className="max-w-6xl mx-auto px-6 py-8">
        {/* Header Title */}
        <div className="flex flex-wrap items-center justify-between gap-4 mb-8 pb-6 border-b border-[#30382C]/15">
          <div>
            <div className="flex flex-wrap items-center gap-3 mb-2">
              <h1 className="text-2xl font-black tracking-tight">⚡ Generation telemetry</h1>
              <span className="bg-[#16856B]/15 text-[#16856B] text-xs font-bold px-2.5 py-1 rounded-md border border-[#16856B]/30">
                In-app cloud view
              </span>
            </div>
            <p className="text-sm opacity-80 max-w-2xl">
              A factual ledger of Vertex calls, retries, tokens, Gemini TTS usage, cost status, and render evidence. Prompts and keys stay out of the record.
            </p>
          </div>

          <div className="flex items-center gap-3">
            {isLoading && <span className="text-xs font-semibold opacity-60" aria-live="polite">Syncing…</span>}
            <span className="inline-flex items-center gap-2 bg-[#FFFFFF] px-3.5 py-1.5 rounded-lg border border-[#30382C]/15 text-xs font-semibold shadow-xs max-w-full">
              <span className="w-2 h-2 rounded-full bg-[#16856B] animate-pulse" />
              <span className="truncate">Primary: {runtime.script_model}</span>
            </span>
          </div>
        </div>

        {/* Top KPI Cards */}
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4 mb-8">
          <div className="bg-[#FFFFFF] p-5 rounded-xl border border-[#30382C]/15 shadow-xs">
            <div className="text-xs font-semibold uppercase tracking-wider opacity-70 mb-1">Jobs observed</div>
            <div className="text-3xl font-black text-[#16856B]">{formatCount(summary?.total_jobs)}</div>
            <div className="text-xs opacity-75 mt-1">Local job records available to this UI</div>
          </div>

          <div className="bg-[#FFFFFF] p-5 rounded-xl border border-[#30382C]/15 shadow-xs">
            <div className="text-xs font-semibold uppercase tracking-wider opacity-70 mb-1">Selected job cost</div>
            <div className="text-3xl font-black text-[#30382C]">{formatCost(jobDetails?.job.summary?.estimated_cost_usd)}</div>
            <div className={`inline-flex text-xs font-semibold mt-1 px-2 py-0.5 rounded border ${statusTone(jobDetails?.job.summary?.cost_status)}`}>
              {jobDetails?.job.summary?.cost_status || "Awaiting job"}
            </div>
          </div>

          <div className="bg-[#FFFFFF] p-5 rounded-xl border border-[#30382C]/15 shadow-xs">
            <div className="text-xs font-semibold uppercase tracking-wider opacity-70 mb-1">Provider calls</div>
            <div className="text-3xl font-black text-[#30382C]">{formatCount(jobDetails?.job.summary?.total_calls)}</div>
            <div className="text-xs opacity-75 mt-1">Actual SDK requests in selected job</div>
          </div>

          <div className="bg-[#FFFFFF] p-5 rounded-xl border border-[#30382C]/15 shadow-xs">
            <div className="text-xs font-semibold uppercase tracking-wider opacity-70 mb-1">Token status</div>
            <div className="text-3xl font-black text-[#16856B]">{jobDetails?.job.summary?.token_status || "—"}</div>
            <div className="text-xs opacity-75 mt-1">{formatCount(jobDetails?.job.summary?.total_tokens)} total tokens</div>
          </div>
        </div>

        {/* Job Selector & Details */}
        <div className="bg-[#FFFFFF] rounded-xl border border-[#30382C]/15 p-6 mb-8 shadow-xs">
          <div className="flex flex-wrap items-center justify-between gap-4 mb-6 pb-4 border-b border-[#30382C]/10">
            <div>
              <h2 className="text-lg font-bold text-[#30382C]">Job ledger</h2>
              <p className="text-xs opacity-75">Choose a real job to inspect provider calls, usage, cost confidence, and scene evidence.</p>
            </div>

            <div className="flex flex-wrap items-center justify-end gap-2 max-w-2xl">
              {(summary?.jobs || []).slice(0, 6).map(job => {
                const selected = selectedJobId === job.job_id;
                return (
                  <button
                    key={job.job_id}
                    type="button"
                    onClick={() => setSelectedJobId(job.job_id)}
                    className={`px-3 py-1.5 text-xs font-bold rounded-md transition border ${
                      selected
                        ? "bg-[#16856B] text-white border-[#16856B] shadow-xs"
                        : "bg-[#F4F0E6] text-[#30382C] border-[#30382C]/10 hover:bg-[#30382C]/10"
                    }`}
                  >
                    {job.job_id} · {job.job_kind || job.voice_mode || "job"}
                  </button>
                );
              })}
              {!summary?.jobs?.length && <span className="text-xs opacity-60">No telemetry jobs yet</span>}
            </div>
          </div>

          {jobDetails?.job.summary && (
            <div className="grid grid-cols-2 md:grid-cols-4 gap-3 mb-6">
              {[
                ["Status", jobDetails.job.summary.job_status || "unknown"],
                ["Input tokens", formatCount(jobDetails.job.summary.total_input_tokens)],
                ["Output tokens", formatCount(jobDetails.job.summary.total_output_tokens)],
                ["Retries / failures", `${jobDetails.job.summary.retry_calls} / ${jobDetails.job.summary.failed_calls}`],
              ].map(([label, value]) => (
                <div key={label} className="rounded-lg bg-[#F4F0E6]/60 border border-[#30382C]/10 px-3 py-3">
                  <div className="text-[11px] uppercase tracking-wide opacity-60">{label}</div>
                  <div className="mt-1 text-sm font-bold text-[#30382C]">{value}</div>
                </div>
              ))}
            </div>
          )}

          <div className="mb-6">
            <div className="flex items-center justify-between mb-3 text-xs font-semibold opacity-75">
              <span>Vertex / Gemini TTS call ledger</span>
              <span>{formatCount(jobDetails?.job.calls?.length)} calls recorded</span>
            </div>
            <div className="space-y-2 max-h-80 overflow-y-auto pr-2">
              {(jobDetails?.job.calls || []).slice().reverse().map(call => (
                <div key={call.call_id} className="grid grid-cols-1 md:grid-cols-[1.1fr_1.6fr_auto] gap-2 items-center p-3 rounded-lg border border-[#30382C]/10 bg-[#F4F0E6]/40 text-xs">
                  <div>
                    <div className="flex items-center gap-2">
                      <strong className="text-[#16856B]">{call.stage}</strong>
                      <span className={`px-2 py-0.5 rounded border ${statusTone(call.status)}`}>{call.status}</span>
                    </div>
                    <div className="font-mono text-[11px] opacity-70 mt-1">{call.operation} · attempt {call.attempt}</div>
                  </div>
                  <div className="min-w-0">
                    <div className="font-mono truncate" title={call.model || "provider operation"}>{call.model || "provider operation"}</div>
                    <div className="opacity-70 mt-1">
                      {call.usage.total_tokens === null || call.usage.total_tokens === undefined
                        ? "Token metadata unavailable"
                        : `${call.usage.total_tokens.toLocaleString()} tokens`}
                      {call.input_characters ? ` · ${call.input_characters.toLocaleString()} chars` : ""}
                    </div>
                  </div>
                  <div className="font-mono text-right text-[11px] opacity-75">{call.duration_ms.toFixed(0)}ms</div>
                </div>
              ))}
              {!jobDetails?.job.calls?.length && <div className="rounded-lg border border-dashed border-[#30382C]/20 px-4 py-6 text-center text-xs opacity-60">This job has no detailed provider ledger yet.</div>}
            </div>
          </div>

          {/* Scene Latency Waterfall Chart */}
          <div className="mb-6">
            <div className="flex items-center justify-between mb-3 text-xs font-semibold opacity-75">
              <span>Scene ID & Treatment Grammar</span>
              <span>Render Latency (ms) & Vertex Latency (ms)</span>
            </div>

            <div className="space-y-2 max-h-96 overflow-y-auto pr-2">
              {(jobDetails?.scenes || []).map((scene, idx) => {
                const renderWidth = Math.min(100, Math.max(15, (scene.render_time_ms / 8000) * 100));
                const vertexWidth = Math.min(100, Math.max(10, (scene.vertex_latency_ms / 3000) * 100));

                return (
                  <div
                    key={scene.scene_id || idx}
                    className="p-3 bg-[#F4F0E6]/50 rounded-lg border border-[#30382C]/10 flex flex-col gap-2"
                  >
                    <div className="flex items-center justify-between text-xs">
                      <div className="flex items-center gap-2">
                        <strong className="font-mono text-[#16856B]">{scene.scene_id}</strong>
                        <span className="bg-[#FFFFFF] px-2 py-0.5 rounded text-[11px] font-medium border border-[#30382C]/15">
                          {scene.treatment_type || "3D Diorama"}
                        </span>
                        <span className="text-[11px] opacity-70">Claims: {scene.evidence_claim_count}</span>
                      </div>
                      <div className="font-mono text-[11px] text-[#30382C]">
                        Render: <strong>{scene.render_time_ms}ms</strong> | Vertex: <strong>{scene.vertex_latency_ms}ms</strong>
                      </div>
                    </div>

                    {/* Progress visual bars */}
                    <div className="w-full bg-[#30382C]/10 h-2 rounded-full overflow-hidden flex gap-1">
                      <div
                        className="bg-[#16856B] h-full rounded-full transition-all"
                        style={{ width: `${renderWidth}%` }}
                        title={`Remotion Render: ${scene.render_time_ms}ms`}
                      />
                      <div
                        className="bg-[#D97706] h-full rounded-full opacity-75 transition-all"
                        style={{ width: `${vertexWidth}%` }}
                        title={`Vertex AI Latency: ${scene.vertex_latency_ms}ms`}
                      />
                    </div>
                  </div>
                );
              })}
            </div>
          </div>
        </div>

        {/* Privacy and partner boundary */}
        <section className="bg-[#FFFFFF] rounded-xl border border-[#30382C]/15 p-6 shadow-xs">
          <div className="flex items-center justify-between mb-4 pb-3 border-b border-[#30382C]/10">
            <h3 className="text-md font-bold text-[#30382C]">🛡️ Telemetry boundary</h3>
            <span className="text-xs bg-[#16856B]/15 text-[#16856B] font-bold px-2.5 py-0.5 rounded-full">
              In-app canonical
            </span>
          </div>

          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3 text-xs">
            {[
              "Prompts excluded from telemetry",
              "Response text excluded from telemetry",
              "Credentials excluded from telemetry",
              "Replit host uses secret-store configuration",
              "External sinks remain opt-in",
              "Cost confidence is labeled exact / partial / unpriced",
            ].map((check, idx) => (
              <div
                key={idx}
                className="flex items-center gap-2 p-2.5 rounded-lg bg-[#F0FDF4] border border-[#16856B]/20 text-[#16856B]"
              >
                <span className="text-sm font-bold">✓</span>
                <span className="font-mono text-[11px] text-[#30382C]">{check}</span>
              </div>
            ))}
          </div>
        </section>
      </main>
    </div>
  );
}
