# FYF Video Pipeline — Developer Guide

Everything needed to run, test, deploy, and operate the FYF video factory:
a Burmese-language, fact-gated AI video production pipeline built on Google
ADK + Vertex AI, with ClickHouse-backed telemetry and an MCP "Data Officer".

## Tech stack

| Layer | Choice | Notes |
| --- | --- | --- |
| Backend API | FastAPI (Python 3.11) | `backend/main.py`, uv-managed deps |
| Agent runtime | Google ADK + google-genai | gemini-3.7-flash default route |
| Video/image models | Veo 3.1, Gemini image models | routed via `vertex_model_routing.py` |
| Voice | Gemini TTS (`gemini-2.5-flash-preview-tts`) | selectable in UI |
| Frontend | Next.js 16 (standalone) | rewrites `/api/*` to `FYF_BACKEND_URL` |
| Telemetry store | ClickHouse Cloud (asia-southeast1) | dual-write from `backend/telemetry_store.py` |
| Insights agent | ADK LlmAgent + mcp-clickhouse MCP | `backend/agent/data_officer.py` |
| Hosting | Google Cloud Run (`--no-cpu-throttling`) | single container, dual process |

## Repository layout

See `docs/PROJECT_STRUCTURE.md` for the annotated tree. Key entry points:

- `backend/main.py` — FastAPI app, job endpoints, guardrails
- `writer_agent_vertex.py` — script/lock/story Vertex stages
- `video_contract.py` — Pydantic contracts shared by every stage
- `scripts/deploy_cloudrun.sh` — one-shot infra + build + deploy
- `scripts/start_cloudrun.sh` — container entrypoint (uvicorn + next start)

## Local development

```bash
# 1. Python env (uv)
uv sync                      # creates .venv from pyproject.toml + uv.lock

# 2. Frontend
cd frontend && npm ci

# 3. Credentials (choose one; never commit these files)
cp .env.example .env         # then fill values
# Option A (simplest local): Vertex Express API key
#   FYF_VERTEX_API_KEY=...
# Option B: service account JSON at repo root as gcp-key.json
# Option C: any GOOGLE_APPLICATION_CREDENTIALS path

# 4. ClickHouse telemetry + Data Officer (optional locally)
cp .env.clickhouse.template .env.clickhouse   # if you have a CH instance

# 5. Run both processes
uvicorn backend.main:app --port 8000 --reload   # terminal 1
cd frontend && npm run dev                       # terminal 2 → http://localhost:3001
```

The frontend proxies `/api/*` to `127.0.0.1:8000`, so the UI works against
the local backend with no CORS changes.

## Environment variables

Full reference with defaults lives in `.env.example`. The ones that matter
most in production:

| Variable | Purpose |
| --- | --- |
| `FYF_LOCK_METADATA_MODE` | `per_segment` (prod) splits lock calls per segment; `combined` is the default for small scripts/tests |
| `FYF_VERTEX_THINKING_LOCK` | thinking level for lock stage (LOW/MEDIUM/HIGH) |
| `FYF_VERTEX_CALL_TIMEOUT_SECONDS` | client-side HTTP timeout per call (default 120) |
| `FYF_VERTEX_MAX_ATTEMPTS` | bounded stage retries (default 2, max 3); failures still surface |
| `FYF_GENERATION_ACCESS_TOKEN` | when set, generation requires this token; unset = open demo mode |
| `FYF_DAILY_BUDGET_CAP_USD` / `FYF_TOTAL_BUDGET_CAP_USD` | spend guardrails enforced before job start |
| `FYF_RATE_LIMIT_PER_MINUTE` | per-IP request limiter (default 10/min) |
| `CLICKHOUSE_*` | ClickHouse Cloud connection for telemetry + Data Officer |

## Testing

```bash
uv run pytest -q             # 471 tests + 85 subtests, no network needed
uv run pytest -q -k lock     # focused subset
```

Live-Vertex smoke test of the per-segment lock path:

```bash
GOOGLE_APPLICATION_CREDENTIALS=gcp-key.json \
FYF_LOCK_METADATA_MODE=per_segment python - <<'PY'
from writer_agent_vertex import generate_exact_lock
print(generate_exact_lock({"title": "smoke", "approved_segments": [
    {"id": "s1", "text": "ငွေလွှဲမှုအတည်ပြုမလုပ်ခင် လက်ခံသူအမည်ကို စစ်ဆေးပါ။"}]}))
PY
```

## Deploying to Cloud Run

```bash
bash scripts/deploy_cloudrun.sh
```

One-shot: enables APIs, creates Artifact Registry, uploads secrets from
`.env.clickhouse`, grants the runtime SA secretAccessor, builds with Cloud
Build, deploys with `--no-cpu-throttling` and the production env block.
Idempotent; safe to rerun.

## Operational notes & known failure modes

- **Never run Cloud Run with CPU throttling** for this service: background
  pipeline tasks starve and the backend silently stalls while health stays 200.
  The deploy script already sets `--no-cpu-throttling`.
- **Vertex constrained decoding (responseJsonSchema) can burst-fail** with 429
  or hang-to-504 under load, while ToolConfig ANY forced function calling with
  the same schema stays healthy. The per-segment lock path uses forced function
  calling; see `docs/decisions/ADR-003-forced-function-calling.md`.
- **429 RESOURCE_EXHAUSTED** is transient quota pressure. Stages retry with
  bounded backoff and surface the final error instead of masking it.
- **Ephemeral filesystem**: job artifacts live under `output/` on the instance;
  keep min-instances ≥ 1 during active production runs or mirror state to GCS.

## Security posture

- Only `.env.example` (placeholders) is tracked; `gcp-key.json`, `.env`,
  `.env.clickhouse` are gitignored and absent from git history.
- CORS allows only localhost dev origins; the deployed frontend talks to the
  backend same-origin through Next.js rewrites.
- Generation endpoints are gated by `_enforce_public_generation_access`
  (timing-safe token compare) plus rate limiting and budget caps.
- File serving validates job IDs against a strict pattern; paths are fixed,
  not user-constructed.
- The Data Officer instructs read-only SELECT usage. For defense in depth,
  provision the ClickHouse user with read-only grants.

### Dependency advisories (accepted, tracked)

`pip-audit` reports two transitive advisories that cannot be resolved today
without breaking the runtime:

| Package | Advisory | Why accepted |
| --- | --- | --- |
| `fastmcp` 2.14.7 (via mcp-clickhouse) | PYSEC-2026-2475/2476, fix ≥3.2.0 | Not reachable from the HTTP API: it runs only inside the mcp-clickhouse stdio subprocess. fastmcp ≥3 requires mcp≥2, which conflicts with google-adk's `mcp<2` requirement. Revisit when mcp-clickhouse bumps fastmcp. |
| `diskcache` 5.6.3 (orphan) | PYSEC-2026-2447, no fix yet | No fix published; nothing in this repo imports it and nothing in the lock requires it — inert on disk. |

Re-run the audit after any dependency change:
```bash
uvx pip-audit --skip-editable -l -p .venv
```

## Change-Impact & Context Integrity Protocol

Before changing any file, write a context packet first:

```yaml
task_goal: what changes and why
canonical_sources_read: [files you actually opened]
existing_implementation: what exists today
symbols_to_change: [functions/classes touched]
callers_and_consumers: [importers and callers]
relevant_tests: [targeted suites to rerun]
affected_documentation: [docs describing this behavior]
conflicts_or_stale_claims: [doc vs source contradictions]
planned_files: [exact paths]
new_file_justification: only when adding a file
unknowns: open questions
```

Run these six bounded searches before proposing a change:

1. Search the exact symbol, error, and requested feature with `rg`.
2. Search the filename plus aliases and synonyms used by callers or docs.
3. Locate the existing implementation; extend it instead of rebuilding it.
4. Map definition → callers/imports → tests → docs for every changed interface.
5. Before adding a file, perform an existing-equivalent search and record why reuse is insufficient.
6. When documentation conflicts, source and tests define current behavior; flag and repair the stale doc in the same change.

Focused verification for the lane you touched:

| Change lane | Minimum check |
| --- | --- |
| Backend boot | `FYF_RUNTIME_MODE=hackathon .venv/bin/python -B -m uvicorn backend.main:app --port 8000` |
| Python change | `uv run pytest -q -k <keyword>` |
| Remotion composition | `cd remotion && npm test` |
| Frontend | `cd frontend && npm run build` |

Full gate before declaring done: `uv run pytest -q` must pass offline (suites live under backend/ and voice_service/ per pyproject.toml testpaths; the root tests/ directory is intentionally absent).

Definition of done for documentation changes:

- No unverified status, version, or deadline claims.
- Every factual statement cites its source path (file, plus line where useful).
- Contradictions against executable source/tests are fixed in the doc, not argued away.
- The pre-change packet and six searches are referenced in your handoff.
