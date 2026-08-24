# FYF Agentic Cinema — Devpost Submission Draft

**Track:** ClickHouse Partner Track
**Product:** FYF Video Pipeline — an evidence-led Burmese video production agent that audits itself in ClickHouse

## Project summary

FYF turns a Burmese topic or draft into a reviewable vertical video. The agent
keeps factual claims, visual evidence, narration, render checkpoints, and final
quality gates explicit so a human can inspect the result before sharing it.
Every production run writes sanitized job, QA, scene, and model-call telemetry
to ClickHouse Cloud, and a built-in "Data Officer" agent answers questions about
that data at runtime through the official mcp-clickhouse MCP server.

## What the working product does

1. Accepts a topic or draft in the Create Studio.
2. Employs a Google ADK Agent (`fyf_producer`) with specialized tools for topic research, segment drafting, quality audit, and shot planning.
3. Uses Gemini on Google Cloud Vertex AI to produce a structured Burmese script and immutable story lock.
4. Provides controlled dynamic video styles (`fyf_explainer`, `cinematic_continuity`, `evidence_story`).
5. Plans and verifies visual evidence for each scene, with deterministic fallbacks when a transient provider response cannot be trusted.
6. Enforces rate limits, concurrency guardrails, and daily budget caps to protect against quota exhaustion.
7. Supports automatic and operator-driven resumable recovery (`/api/jobs/{job_id}/resume`).
8. Generates Burmese narration with Gemini TTS in hackathon mode.
9. Renders a 1080x1920 vertical MP4 with Remotion (segmented strategy with per-segment checkpoints on constrained containers) and runs deterministic, creative, and final rendered-meaning QA.
10. Shows approved results in Library and exposes a privacy-safe in-app telemetry ledger for calls, retries, tokens, TTS, latency, and cost confidence.
11. **Dual-writes every job's sanitized telemetry into ClickHouse Cloud** (`video_pipeline_jobs`, `video_qa_records`, `video_scene_telemetry`, `video_vertex_calls`) via an optional sink in the telemetry store.
12. Ships the **FYF Data Officer** — an ADK agent whose tools come from the official **mcp-clickhouse** MCP server — exposed at `POST /api/insights`: ask "how many jobs passed QA this week?" and get an answer grounded in live warehouse data.

## Google Cloud / Gemini runtime evidence

- Python runtime uses the official `google-genai` SDK and Google ADK (`google-adk`).
- Hackathon mode routes text/storyboard work through Gemini 3.7 Flash and narration through Gemini TTS.
- The verified job ledger records real provider calls and QA results; prompts, response text, and credentials are excluded from telemetry.
- Deployment target: Google Cloud Run (Dockerfile + one-shot `scripts/deploy_cloudrun.sh`; Next.js standalone rewrites same-origin `/api/*` to FastAPI).

## ClickHouse partner-track evidence

- Official MCP server: `mcp-clickhouse` is launched as a stdio MCP server and wired into the agent via ADK `MCPToolset` (`backend/agent/data_officer.py`); imported and called at runtime by `POST /api/insights` in `backend/main.py`.
- Runtime cluster: ClickHouse Cloud service on GCP `asia-southeast1` (secure HTTPS 8443), schema auto-provisioned by `backend/clickhouse_telemetry.py`.
- Real writes: `backend/telemetry_store.py` dual-writes sanitized job telemetry on every completed generation; verified end-to-end against the live cluster (row-level SELECT round-trip).
- Verified conversations (production, 2026-08-24/25): the Data Officer executed real ClickHouse SELECTs through MCP and answered "How many video jobs are recorded in total, and how many succeeded?" from live warehouse data (`tool_used: true`).
- In-app UI: the Telemetry page ships an **Ask the Data Officer** panel — judges can ask their own questions and see answers badged "✓ answered from live ClickHouse query".

## Shipped production evidence (2026-08-25)

- **Two complete end-to-end productions on Google Cloud Run** (revisions 00017-lt6 / 00018-cz6):
  - `e49aa2d5` — 32.8s vertical MP4 (2.19 MB), deterministic QA + creative QA + final rendered-meaning QA all passed; downloaded and ffprobe-verified.
  - `838803f2` — driven entirely through the browser UI (Create form → script → lock → render → Library Download), 34.7s MP4 verified.
- **Cost honesty:** in-app ledger recorded $0.0083 provider cost for job `e49aa2d5` across 15 Vertex/TTS calls, 0 retries, 0 failures.
- **Resilience finding (documented in ADR-003):** burst testing showed Vertex responseJsonSchema constrained decoding failing under load while ToolConfig ANY forced function calling stayed healthy; the per-segment lock stage uses forced function calling with the identical schema.
- Public repo: https://github.com/ahk1542001-wq/fyf-video-pipeline (MIT).

## Submission checklist

- [x] Hosted project URL: https://fyf-pipeline-605161166139.asia-southeast1.run.app
- [x] Public GitHub repo with OSS license (MIT) — secrets audit clean
- [ ] <=3-minute English demo video (shot list: docs/DEMO_SCRIPT.md; assets ready: two cloud-rendered MP4s, Telemetry ledger, Data Officer Q&A)
- [ ] Devpost form under ClickHouse track

Deadline: September 9, 2026, 2:00 PM PT.
