# Handoff: Agentic Business Video Studio

**Project**: FYF Video Pipeline -> **Agentic Business Video Studio**

**Repository Path**: `/Users/mac/Projects/code/fyf-video-pipeline`

**Vault Context**: `/Users/mac/Documents/Second Brain Test/projects/fyf-video-pipeline`

**Target event**: *Agentic Cinema: The Blockbuster Hackathon* (Devpost, ClickHouse Partner Track)

**Document state**: Business positioning and operator instructions were aligned on 2026-09-07. Root still owns implementation verification, fresh-render evidence, and every external side effect. This docs pass did not query ClickHouse, run a live render, read credentials, commit, push, deploy, upload, or submit.

## 1. Product contract

Agentic Business Video Studio turns an owner-approved business brief into a reviewed video for:

- business explainers;
- social ads and campaign variants;
- product launches; and
- brand explainers for SMEs, e-commerce teams, and solo creators.

The product is business-first. Keep the **Agentic Cinema** name scoped to the target event above or to an event-required label; do not use it as the product positioning.

The original Burmese FYF explainer remains supported as **Preset #1 and the default**. It is the compatibility baseline; the business presets add campaign choices without replacing that workflow.

The workbench is a clean two-column flow:

```text
Left: brief, business preset, and essential brand kit
Right: rendered MP4 preview, format choice, and evidence-backed audit state
```

There is no customer-facing timeline or keyframe editor. Story approval and the small set of brand/render controls are the intended interaction model.

## 2. Target UI and render contract (pending root verification)

The target UI should expose these Quick Business Presets, in this order:

1. `burmese_flagship` - `Brand Explainer (Flagship)`; old Burmese FYF explainer, Preset #1/default.
2. `social_ad` - `High-Converting Social Ad`.
3. `product_launch` - `Product Launch Hype (Magnific)`.

The essential brand kit and preview must map to these exact render-request fields:

| UI control | Request field | Allowed value/meaning |
| --- | --- | --- |
| CTA text | `cta_text` | Owner-entered call-to-action text |
| Retention Progress Bar | `retention_progress_bar` | Boolean enable/disable |
| Animated Lower Thirds | `animated_lower_thirds` | Boolean enable/disable |
| Aspect ratio | `aspect_ratio` | `9:16`, `16:9`, or `1:1` |

The two-column layout, preset labels, old Burmese default, request propagation, and renderer behavior are **target claims** until root verifies them in the current working tree and with a fresh UI-created render. Do not mark them complete from a code diff alone.

## 3. Source baseline confirmed for this handoff

These are source observations, not claims that a live deployment or provider is healthy:

- `frontend/package.json` currently pins Next.js **16.3.0**. The local development command uses port **3001**.
- `scripts/run_browser_e2e.py` starts FastAPI on port **8000** and a Next.js production server on port **3001**.
- `backend/agent/fyf_producer.py` and `backend/agent/runner.py` define the ADK Producer Agent and its orchestration path. Its source tools cover research, drafting, story-quality auditing, and visual-shot planning.
- `backend/agent/data_officer.py` defines the second ADK agent, the Data Officer, wired to the official `mcp-clickhouse` server. It is environment-dependent: without the ClickHouse environment and launcher it must report unavailable, not fabricate an answer.
- The source exposes the relevant workflow endpoints, including `POST /api/generate-script`, `POST /api/story-polish`, `POST /api/story-lock`, `POST /api/generate-video`, job status/video endpoints, telemetry endpoints, `POST /api/insights`, and the server-owned `POST /api/clickhouse/query` allowlist endpoint.
- `POST /api/clickhouse/query` accepts a supported `query_id` rather than caller SQL and reports `source` plus `availability`; the source values are `clickhouse_cloud` and `local_mirror`.
- `scripts/deploy_cloudrun.sh` is the repository's canonical Cloud Run script path. It resolves the canonical deployment target from an explicit `PROJECT_ID`, with `GOOGLE_CLOUD_PROJECT` retained only as a legacy fallback, and uses that resolved `PROJECT_ID` for the image, Cloud Run project, runtime environment, IAM, and health lookup. A deployment still requires an explicit owner-approved `PROJECT_ID`; this docs pass does not authorize it.

There are **two actual ADK agents** in the source: Producer and Data Officer. Storyboard planning, voice generation, Remotion rendering, quality checks, and telemetry are pipeline stages, tools, or deterministic responsibilities. Never present those pipeline responsibilities as separate agents.

No test count, render duration, token total, price, public URL, or ClickHouse success is asserted by this handoff. Root must record current branch/HEAD, changed paths, and actual verification output before calling the target ready.

## 4. Root verification checklist

### UI and request propagation

1. Start the local services on ports 3001 and 8000, then inspect the real browser UI.
2. Confirm the three business presets and the old Burmese default are visible and behave as described.
3. Confirm the UI remains two-column and has no timeline editor.
4. Select each target control and inspect the actual request/job artifact to prove that `cta_text`, `retention_progress_bar`, `animated_lower_thirds`, and `aspect_ratio` reach the backend and renderer.
5. Use an owner-approved, non-sensitive business brief. Do not invent evidence or seed a fake production record.
6. Approve the story lock, render through the UI, wait for QA, play the resulting MP4, and verify the selected aspect ratio and enabled controls in the output.

### Agent and telemetry truth

1. Describe the Producer as one ADK agent coordinating its tools and stages.
2. Describe the Data Officer as a separate ADK agent that can answer only when the official MCP path and ClickHouse are available.
3. For a live ClickHouse claim, run a supported query through the allowlisted `POST /api/clickhouse/query` endpoint and require `source: "clickhouse_cloud"`, `availability: "available"`, and relevant returned rows. Pair that result with the Data Officer answer and the matching record; a loaded page or badge alone is not proof.
4. If the endpoint reports `source: "local_mirror"`, label it **local real telemetry fallback**. It is real local telemetry, but it is not ClickHouse Cloud proof and cannot support a live-cloud claim. If the endpoint is unavailable or returns no rows, say so plainly and show the unavailable/retryable state. Do not substitute a hard-coded or estimated value.

### Commands to run after the implementation is ready

Run from the repository root and preserve the output for the root handoff:

```bash
uv run pytest
(cd remotion && npm test)
python3 scripts/run_browser_e2e.py
(cd frontend && npm run build)
git diff --check
```

This docs workstream did not rerun those suites. The commands above are pending root verification; any pass count belongs in the final handoff only if it comes from the current run.

## 5. Local operator runbook

Install dependencies using the repository's normal setup, then use two terminals:

```bash
# Terminal 1: FastAPI backend
FYF_RUNTIME_MODE=hackathon uv run uvicorn backend.main:app --host 127.0.0.1 --port 8000

# Terminal 2: Next.js frontend (the package script uses port 3001)
cd frontend
npm run dev
```

Open:

- Create Studio: `http://localhost:3001/`
- Video Library: `http://localhost:3001/library`
- Telemetry and Data Officer: `http://localhost:3001/telemetry`

ClickHouse and Vertex configuration must be supplied through the approved host environment or secret store. The relevant names are `CLICKHOUSE_HOST`, `CLICKHOUSE_PORT`, `CLICKHOUSE_USER`, `CLICKHOUSE_PASSWORD`, `CLICKHOUSE_DATABASE`, `CLICKHOUSE_SECURE`, `FYF_VERTEX_API_KEY`, `GOOGLE_APPLICATION_CREDENTIALS`, `GOOGLE_GENAI_USE_VERTEXAI`, `GOOGLE_CLOUD_PROJECT`, `GOOGLE_CLOUD_LOCATION`, `FYF_GENERATION_ACCESS_TOKEN`, `FYF_PUBLIC_DEPLOYMENT`, `FYF_PUBLIC_GENERATION_ENABLED`, `FYF_DAILY_BUDGET_CAP_USD`, `FYF_TOTAL_BUDGET_CAP_USD`, `FYF_RATE_LIMIT_PER_MINUTE`, and `FYF_MAX_CONCURRENT_JOBS`. Keep values out of Markdown, Git, screenshots, terminal output, and chat. Never read or print `.env` or other secret files for this handoff.

## 6. Evidence and demo data policy

The audit panel and demo must use values from the selected job or a real ClickHouse query. Relevant job/telemetry tables include `video_pipeline_jobs`, `video_scene_telemetry`, `video_qa_records`, and `video_vertex_calls`.

Do not write or say fixed values for render time, token count, cost, scene latency, job count, or QA totals. In particular, the demo must not use a static badge or a memorized duration/tokens/cost tuple. If the selected job has no value, display the product's unavailable state and narrate that the value is unavailable for this run.

The Data Officer answer is separate from external query proof. For a ClickHouse Cloud claim, execute a supported query through the allowlisted `POST /api/clickhouse/query` endpoint and require `source: "clickhouse_cloud"`, `availability: "available"`, and relevant rows that match the answer. If the response reports `source: "local_mirror"`, label it **local real telemetry fallback**: it is real local telemetry, not ClickHouse Cloud proof. If the query is unavailable or returns no rows, use the fallback wording in the companion demo script and leave the live-cloud claim unchecked.

## 7. Approval gates and handoff boundaries

- Local source inspection and the verification commands are separate from release approval.
- Commit only after root reviews the complete diff and the owner explicitly approves the commit stage. Stage reviewed paths deliberately; never use a blanket staging command.
- Push is a separate explicit owner gate.
- Public deployment is a separate explicit owner gate. Root must choose and record an explicit owner-approved `PROJECT_ID`; the script uses that value as the canonical build/deploy/runtime target, while `GOOGLE_CLOUD_PROJECT` is legacy fallback only. Only after approval, invoke `bash scripts/deploy_cloudrun.sh` and verify the resulting health endpoint and deployment configuration. Do not treat the command as permission to deploy.
- Recording a fresh MP4, uploading it to a public host, and adding its URL to Devpost are separate explicit owner gates. Demo readiness never implies upload or submission permission.
- This handoff and its companion demo script are documentation only; they do not authorize code changes, credential access, provider calls, deployment, upload, or submission.

## 8. Ready-to-handoff definition

Root may call this workstream ready only when the current implementation has evidence for all of the following:

- business-first labels and positioning, with the old Burmese explainer still Preset #1/default;
- two-column UI with no timeline editor;
- all four target render fields wired and verified;
- a fresh browser-created MP4 that plays and passes the repository's render/QA gates;
- actual audit values from that run, or a visible unavailable state with no invented values;
- a live Data Officer/ClickHouse answer only when a real query and matching record are captured;
- an explicit owner-approved `PROJECT_ID` used as the canonical build/deploy/runtime target;
- current test/build output and a clean documentation diff; and
- explicit owner decisions for commit, push, deploy, recording upload, and Devpost submission.
