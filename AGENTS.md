# FYF Video Pipeline — Agent Instructions

## Project Overview
Turns a Burmese topic or draft into a vertical video via Vertex AI stages, Gemini TTS, Remotion assembly, FastAPI + Next.js. Dates: `DEVPOST_SUBMISSION.md`.

## Source-of-Truth Hierarchy
1. Executable source + tests.
2. ADRs in `docs/decisions/`.
3. `docs/PROJECT_STRUCTURE.md`.
4. `README.md`.
5. This file — rules + pointers only.
Conflicts: source/tests win; note them in the handoff.

## Pre-Change Context Gate
Packet before any edit:
```yaml
task_goal:
canonical_sources_read:
existing_implementation:
symbols_to_change:
callers_and_consumers:
relevant_tests:
affected_documentation:
conflicts_or_stale_claims:
planned_files:
new_file_justification:
unknowns:
```
Six searches: rg the symbol/error/feature; filename + aliases; trace the live implementation, never rebuild; map definition→callers→imports→tests→docs; prove new files with an existing-equivalent search; source/tests are current behavior — flag doc contradictions.

## Canonical Pointers
- Local run: `uv sync`; cp .env.example .env; FYF_RUNTIME_MODE=hackathon uvicorn backend.main:app, port 8000.
- Suites: `uv run pytest -q` (`backend`, `voice_service` testpaths); Remotion: `cd remotion && npm test`.
- Routing: `vertex_model_routing.py` ROUTES + `model_for()`; thinking `backend/vertex_thinking.py`.
- Entrypoint: `backend/main.py` (/health … POST /api/insights).
- Contracts `video_contract.py`; writer `writer_agent_vertex.py`.
- Locks `backend/lock_store.py`; jobs `backend/job_store.py`; budgets `backend/budget_store.py` + `backend/runtime_limits.py`.
- Render/QA: `backend/render_video.py` (_run_remotion); `backend/segment_render_cache.py`; QA trio `backend/output_qa.py`, `backend/creative_quality.py`, `backend/final_visual_qa_vertex.py`.
- Telemetry: `backend/telemetry_store.py` dual-write + `backend/clickhouse_telemetry.py` schema.
- Agents: `backend/agent/fyf_producer.py`; `backend/agent/data_officer.py` (mcp-clickhouse).
- Voice: `voice_service/gemini_tts.py`.
- Frontend: `frontend/` (next.config.ts rewrites /api/*).

## Critical Rules
- Brand: Ivory #F4F0E6, Olive #30382C, Viridian #16856B, Sage #A8B7A2; Arial/Noto Sans Myanmar.
- Voice ethics: owner-approved voice clones only; never clone anyone else's voice.
- Secrets: key file gitignored; never commit/print keys; auth via GOOGLE_APPLICATION_CREDENTIALS.
- Model ids: resolve via `model_for()`/ROUTES only; never hardcode.
- Dependencies: `pyproject.toml` + `uv.lock` are canonical; verify an actual consumer before editing `requirements.txt`.
- No push/deploy/publish without owner approval.
