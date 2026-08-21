# FYF Public Hackathon Compliance Baseline

**Date:** 2026-08-21
**Base Commit:** `4990bf4` (Branch: `codex/replit-ready-20260821`)
**New Target Branch:** `codex/hackathon-compliance`

## Baseline Verification Record

- **Backend Pytest:** 343 passed, 0 failures (3 warnings, 84 subtests passed).
- **Frontend Build & Lint:**
  - `eslint`: 0 errors / 0 warnings.
  - `next build` (Next.js 16.3.0 Turbopack): Compiled cleanly; static pages generated (6/6 routes).
- **Remotion Suite:**
  - Node test suite: 27/27 passed.
  - TypeScript `tsc --noEmit`: Clean, 0 errors.

## Prohibited Runtime & Partner Artifacts Present at Baseline (To Be Remediated)

1. Optional dependencies in `pyproject.toml` / `uv.lock`: `partner-voices` (`kaggle`), `operator-tools` (`colab-mcp`, `mcp-server-colab-exec`).
2. Voice service legacy references: `voice_service/kaggle_runner.py`, `VoxCPM2` routing logic in `voice_service/voice_generator.py`.
3. Backend legacy references: `backend/clickhouse_telemetry.py` and remote ClickHouse adapters.
4. UI & Docs claims mentioning non-Google providers or unverified hosted states.

## Target Cleanup Objectives

- Public repository tree contains strictly Gemini/Vertex AI, Gemini TTS, and Google ADK agent execution.
- Rate, concurrency, and global budget protection implemented.
- Resumable retry/resume states instead of raw dead-end failures.
- Privacy-safe local telemetry metrics only.
