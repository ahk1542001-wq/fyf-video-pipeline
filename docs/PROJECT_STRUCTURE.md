# Project Structure

This document maps the repository so new engineers (and agents) can navigate without archaeology. For the reasoning behind major choices, see [docs/decisions/](decisions/).

```
fyf-video-pipeline/
├── backend/                    # FastAPI application + pipeline orchestration
│   ├── main.py                 #   HTTP API: routes under /api/*, auth guards
│   ├── pipeline.py             #   Video generation stages: visuals → voice → render → QA
│   ├── script_pipeline.py      #   Script generation entry (ADK producer or legacy path)
│   ├── runtime_limits.py       #   Concurrency guard, daily budget cap, lease guardrails
│   ├── render_video.py         #   Remotion invocation, timeouts, browser staging
│   ├── segment_render_cache.py #   Segmented rendering with per-segment checkpoints
│   ├── telemetry_store.py      #   Privacy-safe local telemetry + ClickHouse dual-write
│   ├── clickhouse_telemetry.py #   ClickHouse Cloud client, schema init, insert helpers
│   ├── budget_store.py         #   Cost ledger and daily caps
│   └── agent/                  #   Google ADK layer
│       ├── fyf_producer.py     #     Producer agent definition (story generation)
│       ├── runner.py           #     ADK Runner execution wrapper
│       ├── tools.py            #     Producer tool functions
│       └── data_officer.py     #     Telemetry Q&A agent via mcp-clickhouse MCP server
├── frontend/                   # Next.js app (Create Studio, Library, Telemetry UI)
│   └── next.config.ts          #   Standalone output; /api/* rewritten to FastAPI
├── remotion/                   # Remotion composition (VisualSystemV3Full)
│   ├── src/                    #   React video components
│   └── node_modules/.bin/remotion  # Render CLI used by the backend
├── scripts/
│   ├── deploy_cloudrun.sh      # One-shot Cloud Build + Cloud Run deploy
│   └── start_cloudrun.sh       # Container entrypoint (uvicorn + next server)
├── docs/
│   └── decisions/              # Architecture Decision Records (ADRs)
├── tests/                      # Backend and contract test suites
├── voice_service/              # Voice synthesis service modules
├── output/                     # Runtime artifacts (jobs, locks, telemetry) — gitignored
├── Dockerfile                  # Multi-stage image for Google Cloud Run
├── requirements.txt            # Locked Python dependencies
├── LICENSE                     # Open-source license (hackathon requirement)
└── AGENTS.md                   # Working rules for AI agents contributing here
```

## Key flows

1. **Script generation:** `POST /api/generate-script` → `script_pipeline` → ADK `fyf_producer` agent → story lock (`lock_id`).
2. **Video generation:** `POST /api/generate-video` with a lock → visuals (Vertex) → voice (Gemini TTS) → segmented Remotion render → deterministic QA → MP4 in Library.
3. **Telemetry:** every stage writes sanitized metrics locally; a best-effort sink mirrors job rows to ClickHouse Cloud.
4. **Data Officer:** `POST /api/insights` runs an ADK agent whose tools come from the official `mcp-clickhouse` MCP server — natural-language answers over the warehouse.
