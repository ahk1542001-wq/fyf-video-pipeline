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
- Verified conversation: the Data Officer answered a live question ("How many jobs are in video_pipeline_jobs?") by executing a ClickHouse query through MCP during development testing.

## Submission checklist

- [ ] Hosted project URL (Google Cloud Run) — pending first cloud deploy
- [ ] Public GitHub repo with OSS license (LICENSE present; final secrets audit before flip)
- [ ] <=3-minute English demo video (Create -> Library -> Telemetry -> /api/insights Q&A)
- [ ] Devpost form under ClickHouse track

Deadline: September 9, 2026, 2:00 PM PT.
