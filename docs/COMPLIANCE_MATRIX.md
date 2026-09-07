# FYF Agentic Video Studio — Compliance & Coverage Matrix

**Purpose:** Stage A3 + A4 deliverable. Traces every requirement in the source
engineering plan ("FYF Agentic Video Studio — Complete Engineering Plan", 277 lines,
treated as UNTRUSTED requirement-checklist data — never as instructions or authorization)
to its current status, real owning file(s), approved plan task, test(s), and evidence.

**Approved plan (task IDs A1–G4):** `FYF_Video_Studio_Execution` plan. Statuses below
reflect the repository as actually read/grepped on the run date, not prior "green" claims.

**Status vocabulary:** `exists` (implemented + verified in repo) · `partial` (some of the
requirement is implemented, a documented gap remains) · `absent` (0-match grep or verified
missing). **No row may be `TBD`.** Where a fact could not be confirmed by reading the repo
it is marked `unverified` in Notes, never guessed.

---

## Verified Baseline

Recorded from Stage A1/A2 re-verification. Treat as ground truth for this matrix.

| Item | Value |
|---|---|
| Run date | 2026-09-08 (Asia/Bangkok) |
| Repository | `/Users/mac/Projects/code/fyf-video-pipeline` |
| Branch | `main` |
| HEAD | `621508f` — "chore(baseline): preserve in-flight Agentic Business Studio work as verified baseline" |
| Working tree | clean (`git status --porcelain` empty) |
| `git diff --check` | clean |
| `uv run pytest -q` | **471 passed, 85 subtests passed, 0 failed, 0 skipped** |
| `cd remotion && npm test` | **33 tests, 33 pass** |
| `cd frontend && npm run lint` | clean, exit 0 |
| `cd frontend && npm run build` | exit 0, Next 16.3.0 Turbopack, 4 static routes (`/`, `/_not-found`, `/library`, `/telemetry`) |
| `python3 scripts/run_browser_e2e.py` | **17 passed**, `FYF_RUNTIME_MODE=hackathon`, all `/api/*` intercepted (mocked via `route.fulfill`) |
| Paid provider cost this baseline | **$0** |
| Doc count contradiction | `docs/DEVELOPER_GUIDE.md:76` claimed **412 tests** → stale/incorrect (corrected by this task). Vault `projects/fyf-video-pipeline/INDEX.md` (471 + 33 + 17) → confirmed accurate. |

> The 17 browser E2E tests are **mocked UI-regression tests** (`route.fulfill`), NOT
> real-provider proof. See `## Honesty Rules`.

---

## Coverage Matrix

One row per requirement/checkbox in the source document §1–§5. `Req ID` uses
`§<section>-<group>-<nn>`. `Owning file(s)` are real repo paths verified by read/grep.
`Plan task` references approved plan IDs A1–G4.

### §1 — Product & Creative Requirements

#### Product experience (doc lines 14–25)

| Req ID | Requirement (short) | Status | Owning file(s) | Plan task | Test(s) | Evidence | Notes |
|---|---|---|---|---|---|---|---|
| §1-product-01 | Desktop app: Library → New/Resume → Studio | partial | `frontend/app/page.tsx`, `frontend/app/library/page.tsx` | C1, C2 | `frontend/e2e/library-gallery.spec.ts`, `create-studio.spec.ts` | 4 static routes `/`,`/library`,`/telemetry`,`/_not-found` | No `project/[id]` Studio route; single-page create flow only |
| §1-product-02 | 15–90s ads/explainers/launches/team workflows | partial | `frontend/app/page.tsx`, `backend/render_contract.py` | D6 | `backend/test_business_render_contract.py` | business presets present | duration range (15–90s) enforcement `unverified` |
| §1-product-03 | Burmese flagship + English support | exists | `voice_service/production_voice.py`, `backend/final_visual_qa_vertex.py` | D4 | `voice_service/test_burmese.py`, `test_burmese_asr_qa.py`, `backend/test_story_modes.py` | language-aware QA + Burmese TTS | — |
| §1-product-04 | Left chat / Right storyboard+scene+preview+BrandKit+Insights | partial | `frontend/app/page.tsx`, `frontend/app/telemetry/page.tsx` | C2, C3 | `frontend/e2e/create-studio.spec.ts` | Brand Kit + preview + Insights present | persistent Creative Director chat panel **absent** |
| §1-product-05 | Chat + canvas edit one canonical version | absent | — (no version spine) | B4, B6, C2 | — | grep `ProjectCommand`/`ChangeSet`/`version_history` = 0 | no chat, no versioned project store |
| §1-product-06 | Scene/object/time-range selection understood by chat | absent | — | B6, C3 | — | grep `ChangeSet`/`ProjectCommand` = 0 | `resolve_selection` not present |
| §1-product-07 | Content/visual/timing locks; Undo; version history; named variants; before/after | partial | `backend/lock_store.py` (28 lines), `frontend/app/page.tsx` | C4, C5 | `backend/test_pipeline_ui_api.py` | whole-script/story lock exists | `lock_store.py` = 28 lines whole-script only; Undo/version-history/named-variants server-persisted **absent** (client-side transient variants only) |
| §1-product-08 | Decisions/approved assumptions/rejected directions/locks stored separate from chat | absent | — | B4, C2 | — | no chat-history store | — |
| §1-product-09 | Cross-project prefs carried only via explicit "Save to Brand" | absent | — | C5 | — | grep `Save to Brand` = 0 | — |
| §1-product-10 | Loading/empty/offline/error/partial-success/cancelled/stale states designed | partial | `frontend/app/page.tsx` | C9, B9 | `frontend/e2e/create-studio.spec.ts` | some states in page.tsx | cancelled/stale-result states **absent** (no cancel endpoint) |
| §1-product-11 | Actual progress % else "Estimated" + current stage | partial | `backend/job_store.py`, `frontend/app/page.tsx` | C9 | `backend/test_job_store.py` | job progress present | explicit "Estimated" label + `progress_source` field **absent** |
| §1-product-12 | Keyboard/focus/contrast/labels/Burmese typography/narrow desktop | partial | `frontend/app/globals.css`, `frontend/e2e/responsive-theme.spec.ts` | F7 | `frontend/e2e/responsive-theme.spec.ts` | responsive-theme test exists | full a11y (axe) not present; `@axe-core/playwright` is owner-gated dependency |

#### Creation workflow (doc lines 29–37)

| Req ID | Requirement (short) | Status | Owning file(s) | Plan task | Test(s) | Evidence | Notes |
|---|---|---|---|---|---|---|---|
| §1-workflow-01 | Idea/assets → adaptive brief; 1–3 essential questions | partial | `backend/agent/fyf_producer.py`, `backend/director_context.py` | D1 | `backend/test_director_context.py`, `backend/agent/test_runner.py` | Director agent present | adaptive brief as versioned skill **absent** |
| §1-workflow-02 | One recommended concept + two alternatives | partial | `backend/pipeline.py`, `backend/video_director.py` | D1 | `backend/test_video_director.py`, `backend/test_pipeline.py` | concept generation present | alternatives count `unverified` |
| §1-workflow-03 | Selected concept → creative direction → script/storyboard | exists | `backend/script_pipeline.py`, `writer_agent_vertex.py`, `video_contract.py` | D1 | `backend/test_script_pipeline.py`, `test_writer_agent_vertex.py`, `test_video_contract.py` | strict Pydantic script contract | — |
| §1-workflow-04 | Approved draft within spend → real voice/assets → timed animatic | partial | `backend/pipeline.py`, `voice_service/*`, `backend/mouth_cues.py` | D4 | `backend/test_pipeline.py`, `test_lip_sync.py`, `voice_service/test_production_voice.py` | voice-timed pipeline present | real-provider animatic run **blocked** ($0 baseline; mocked only) |
| §1-workflow-05 | Story Lock → cost/deliverables review → export approval | partial | `backend/lock_store.py`, `backend/budget_store.py`, `backend/main.py` | B10 | `backend/test_budget_store.py`, `test_pipeline_ui_api.py` | story-lock + budget status exist | persisted export approval **absent** |
| §1-workflow-06 | Dependency-aware production → rendering → technical/creative QA | partial | `backend/pipeline.py`, `backend/segment_render_cache.py`, `backend/output_qa.py`, `backend/creative_quality.py` | D3 | `backend/test_segment_render_cache.py`, `test_output_qa.py`, `test_creative_quality.py` | fingerprint cache + QA trio | dependency-graph recompute as DAG **absent** (fingerprint invalidation only) |
| §1-workflow-07 | User review → scoped corrections → final approval/export | partial | `backend/main.py`, `backend/render_video.py` | C7, D5 | `backend/test_render_video.py`, `test_pipeline_ui_api.py` | render + review endpoints | scoped corrections + final approval gate **absent** |
| §1-workflow-08 | Reversible work between gates; approval for paid regen/budget increase/locked-story/external actions | partial | `backend/budget_store.py`, `backend/runtime_limits.py` | B10 | `backend/test_resume_and_guardrails.py`, `test_runtime_limits.py` | guardrail lease + budget caps | persisted per-operation approval rows **absent** |

#### Creative quality (doc lines 41–49)

| Req ID | Requirement (short) | Status | Owning file(s) | Plan task | Test(s) | Evidence | Notes |
|---|---|---|---|---|---|---|---|
| §1-creative-01 | Creative Direction: audience/goal/message/proof/CTA/style/narration/motion | partial | `video_contract.py`, `backend/director_context.py` | D1 | `backend/test_video_contract.py`, `test_director_context.py` | CTA + direction fields present | full 8-field direction schema `unverified` |
| §1-creative-02 | Each scene: narrative purpose, visual evidence, narration, timing, transition intent | exists | `video_contract.py`, `backend/creative_quality.py` | D2 | `backend/test_video_contract.py`, `test_creative_quality.py` | VideoScript scene schema (`extra="forbid"` ×25) | — |
| §1-creative-03 | Validated motion primitives; no arbitrary generated executable render code | exists | `remotion/src/`, `video_contract.py` | D2 | remotion npm test (33), `backend/test_render_contract.py` | closed Pydantic contracts; Remotion components | arbitrary HTML/JS structurally impossible (`extra="forbid"`) |
| §1-creative-04 | Visual hierarchy, cross-scene continuity, pacing, restrained transitions | partial | `backend/creative_quality.py`, `backend/final_visual_qa_vertex.py` | D2 | `backend/test_creative_quality.py`, `test_final_visual_qa_vertex.py` | creative QA present | continuity scoring `unverified` |
| §1-creative-05 | Voice-timed scenes/captions, pronunciation dict, Burmese line breaking, mixed-lang | exists | `backend/mouth_cues.py`, `voice_service/production_voice.py`, `voice_service/gemini_tts.py` | D4 | `backend/test_lip_sync.py`, `voice_service/test_burmese.py`, `test_gemini_tts_vertex.py` | mouth-cue timings + Burmese TTS | — |
| §1-creative-06 | Voice/music balance, ducking, purposeful SFX, silence | partial | `voice_service/audio_quality.py`, `backend/pipeline.py` | D4 | `voice_service/test_audio_quality.py` | audio-quality module present | ducking/SFX/silence controls `unverified` |
| §1-creative-07 | Product/logo/packaging fidelity, sourced claims, non-fabricated charts/testimonials | partial | `backend/public_compliance.py`, `backend/creative_quality.py` | D2 | `backend/test_public_compliance.py`, `test_creative_quality.py` | compliance checks present | claim-sourcing enforcement `unverified` |
| §1-creative-08 | Reframe + duration re-edit separate; shortening not speed-up only | absent | — | D6 | — | no reframe/duration command surface | grep `ProjectCommand` = 0 |
| §1-creative-09 | Caption readability, sound-off comprehension, flashing checks, optional reduced-motion output | partial | `backend/creative_quality.py`, `backend/output_qa.py` | D7 | `backend/test_creative_quality.py`, `test_output_qa.py` | caption/QA checks present | **reduced-motion output absent** (grep 0 in `remotion/src`) |

#### Deliverables & budget (doc lines 53–62)

| Req ID | Requirement (short) | Status | Owning file(s) | Plan task | Test(s) | Evidence | Notes |
|---|---|---|---|---|---|---|---|
| §1-deliver-01 | 1080p H.264/AAC MP4; 9:16/16:9/1:1 variants | exists | `video_contract.py:9`, `backend/render_video.py`, `remotion/src/` | D5 | `backend/test_render_video.py`, `test_render_contract.py` | `AspectRatio = Literal["9:16","16:9","1:1"]` | codec/container compliance `unverified` at runtime |
| §1-deliver-02 | caption/no-caption, SRT/VTT, thumbnail, transcript, provenance report | partial | `backend/render_contract.py`, `backend/mouth_cues.py` | D5 | `backend/test_render_contract.py` | caption/render controls present | **SRT/VTT/thumbnail/transcript/provenance-report export absent** (grep matched only `mouth_cues.py`) |
| §1-deliver-03 | Private expiring/revocable review links + time-coded comments | absent | — | F2 | — | grep `signed_url` = 0 | no signed-URL / review-link layer |
| §1-deliver-04 | Customizable project budget; $3 default if unset | partial | `backend/budget_store.py` | B2 | `backend/test_budget_store.py` | budget caps enforced | per-project **$3 default absent**; current defaults $10 daily/$50 total (account-level, conflated) |
| §1-deliver-05 | Quality-first budget-conscious; regenerate only needed scenes | partial | `backend/segment_render_cache.py` | D3 | `backend/test_segment_render_cache.py` | content-hash segment reuse | craft-first ordering policy `unverified` |
| §1-deliver-06 | Draft/generation/render/QA/retry variable costs + fixed infra separate | partial | `backend/cost_catalog.py`, `backend/budget_store.py` | B2 | `backend/test_cost_catalog.py`, `test_budget_store.py` | cost catalog present | fixed-infra separation `unverified` |
| §1-deliver-07 | Spent, reserved/in-progress estimate, remaining estimate, estimate/actual distinction | partial | `backend/budget_store.py:114` (`get_budget_status`) | B2 | `backend/test_budget_store.py` | budget status present | explicit `estimated`/`actual`/`provider_reported`/`invoice_confirmed` labels **absent** |
| §1-deliver-08 | Approval before paid dispatch that exceeds budget | absent | — | B10 | — | no persisted approval rows | `record_approval`/`read_approvals` not present |
| §1-deliver-09 | Cancellation/budget reduction does not hide incurred charges | absent | `backend/budget_store.py:276` | B1 | `backend/test_budget_store.py` | **DEFECT (verified):** `if actual_usd > 0.0 and outcome != "cancelled":` drops a cancelled op's incurred charge | doc line 61 violated |
| §1-deliver-10 | Account spending ceiling explicit in deploy config; else no paid production | absent | `backend/budget_store.py:18-19,48-58` | B2 | `backend/test_budget_store.py` | **DEFECT (verified):** unset env ⇒ `_parse_cap_usd` returns `(default, True)` → **fail-open** to $10/$50 | should fail-closed & disable paid production |

### §2 — Architecture, Data Integrity & Security

#### Responsibilities (doc lines 68–79)

| Req ID | Requirement (short) | Status | Owning file(s) | Plan task | Test(s) | Evidence | Notes |
|---|---|---|---|---|---|---|---|
| §2-resp-01 | Next.js Studio/BFF: auth, shared chat/canvas UX, server-mediated access | partial | `frontend/app/page.tsx`, `frontend/next.config.ts` | C2, F1 | `frontend/e2e/create-studio.spec.ts` | Studio UI present | **BFF absent** — `next.config.ts` is a pure rewrite proxy (`/api/:path*` → backend), no route handlers, no auth |
| §2-resp-02 | FastAPI: commands, validation, approvals, project operations | partial | `backend/main.py` (1186 lines), `video_contract.py` | B4, B12 | `backend/test_pipeline_ui_api.py`, `test_video_contract.py` | 20+ routes, strict validation | persisted approvals + project-command ops **absent** |
| §2-resp-03 | PostgreSQL: canonical state, versions, approvals, budget ledger, durable events | absent | — | B5, G3 | — | grep `postgres`/`sqlalchemy`/`asyncpg`/`psycopg` = 0 | `ProjectStore` seam (B5) + `postgres_store.py` (G3) planned |
| §2-resp-04 | GCS/object storage: private uploads, assets, render artifacts | absent | — | B5, G3 | — | grep `google.cloud.storage`/`storage.Client`/`gs://` = 0 | `ArtifactStorage` seam (B5) + `storage/gcs.py` (G3) planned |
| §2-resp-05 | Cloud Tasks/Cloud Run workers: durable agent/media/render execution | partial | `scripts/deploy_cloudrun.sh`, `backend/main.py` | B7, G3 | `scripts/test_deploy_cloudrun_contract.sh` | Cloud Run deploy contract present | **Cloud Tasks absent** (grep `cloud_tasks`=0); durable workers absent (in-process `BackgroundTasks`) |
| §2-resp-06 | Existing local telemetry: local-mode collection, fallback, diagnostics | exists | `backend/telemetry_store.py`, `backend/clickhouse_telemetry.py` | E5 | `backend/test_telemetry_store.py`, `test_clickhouse_telemetry.py` | local mirror + `clickhouse_status="local_mirror_active"` (`:395`) | — |
| §2-resp-07 | ClickHouse: cost/quality/performance/version analytics | partial | `backend/clickhouse_telemetry.py`, `backend/telemetry_queries.py` | E1, E3 | `backend/test_clickhouse_telemetry.py` | dual-write + allowlisted queries | **DEFECT:** all 4 tables `MergeTree()` (`:74,88,99,121`) → replay duplicates, dedup impossible; live instance stale (last 2026-08-25) |
| §2-resp-08 | Official `mcp-clickhouse`: Data Officer runtime analytics queries | partial | `backend/agent/data_officer.py`, `backend/main.py:1111` (`/api/insights`) | E6 | `backend/agent/test_runner.py` | mcp-clickhouse launcher wired; disabled when absent (`data_officer.py:88`) | real runtime query evidence **blocked** (live ClickHouse stale) |
| §2-resp-09 | Reuse/migrate existing; no Grafana/NLE/voice-clone/marketplace/subscriptions/auto-publish | exists | repo-wide | G3 | — | grep confirms none of the excluded systems present | constraint honored |

#### Shared interfaces (doc lines 83–93)

| Req ID | Requirement (short) | Status | Owning file(s) | Plan task | Test(s) | Evidence | Notes |
|---|---|---|---|---|---|---|---|
| §2-iface-01 | ProjectCommand (project, base version, actor, op, scope, payload, idempotency key) | absent | — | B4 | — | grep `ProjectCommand`/`idempotency` = 0 | new `backend/projects/models.py` |
| §2-iface-02 | VideoSpec/ProjectVersion (direction, scenes, assets, locks, variants, parent, pinned config) | partial | `video_contract.py` (`VideoScript`) | B4 | `backend/test_video_contract.py` | VideoSpec analog exists | `ProjectVersion` (parent version, pinned production config) **absent** |
| §2-iface-03 | ChangeSet (proposed ops, dependencies, lock/approval effects, est spend, validation) | absent | — | B4, B6 | — | grep `ChangeSet`/`change_set` = 0 | — |
| §2-iface-04 | SurfaceSpec (approved components/bindings/actions/schema version; no arbitrary HTML/JS) | absent | — | B4 | — | grep `SurfaceSpec` = 0 | plan rejects general DSL; closed union planned |
| §2-iface-05 | Approval (operation, target version/change set, spend, decision, timestamp) | absent | — | B4, B10 | — | no persisted approval model | — |
| §2-iface-06 | WorkflowEvent (project/version/run/job IDs, stage/status, sequence, progress source, artifacts, action) | absent | — | B4 | — | grep `WorkflowEvent` = 0 | — |
| §2-iface-07 | ToolResult (outcome, provider operation ID, usage/cost, artifacts, retryability, sanitized error) | absent | — | B8 | — | **DEFECT:** no provider operation ID persisted → blind paid-retry risk | — |
| §2-iface-08 | TelemetryEvent (unique event ID, schema version, event/ingestion timestamps, correlation IDs, privacy-filtered) | partial | `backend/clickhouse_telemetry.py`, `backend/telemetry_store.py` | E1 | `backend/test_clickhouse_telemetry.py` | privacy-filtered metrics present | stable `event_id`/`schema_version`/`sequence` **absent** (MergeTree, no dedup) |
| §2-iface-09 | RenderManifest (VideoSpec version, asset hashes, fonts, renderer/skill versions, seeds, output metadata) | partial | `backend/segment_render_cache.py` (`_manifest_fingerprint`) | C6 | `backend/test_segment_render_cache.py` | content-hash fingerprint present | grep `RenderManifest`=0; **fonts not in fingerprint**; full manifest absent |
| §2-iface-10 | Reject/rebase stale commands; no silent overwrite (doc line 93) | absent | — | B6 | — | no base-version check (no version spine) | — |

#### Transactional editing & reproducibility (doc lines 97–102)

| Req ID | Requirement (short) | Status | Owning file(s) | Plan task | Test(s) | Evidence | Notes |
|---|---|---|---|---|---|---|---|
| §2-atomic-01 | Multi-scene AI edits validated as one ChangeSet, applied atomically | absent | — | B6 | — | grep `ChangeSet` = 0 | — |
| §2-atomic-02 | No partial edits if validation/lock/ownership/version check fails | partial | `backend/main.py`, `video_contract.py` | B6 | `backend/test_video_contract.py` | per-request validation strict | cross-operation atomicity **absent** |
| §2-atomic-03 | Approved diff/change set == applied operations | absent | — | B6 | — | no change-set model | — |
| §2-atomic-04 | External-gen results attach to validated version; stale results don't change newer draft | absent | — | C7 | — | no version binding of async results | — |
| §2-atomic-05 | Preview/export use same VideoSpec, assets, fonts, renderer config | partial | `backend/segment_render_cache.py`, `backend/render_contract.py` | C6 | `backend/test_render_contract.py`, `test_segment_render_cache.py` | shared fingerprint | fonts not in fingerprint; parity not enforced end-to-end |
| §2-atomic-06 | Store manifest for repeatable render; model regen not guaranteed identical | partial | `backend/segment_render_cache.py`, `backend/visual_artifact_store.py` | C6 | `backend/test_segment_render_cache.py`, `test_visual_artifact_store.py` | artifact store + fingerprint | full re-render-from-manifest entrypoint **absent** |

#### Durable execution & capacity (doc lines 106–115)

| Req ID | Requirement (short) | Status | Owning file(s) | Plan task | Test(s) | Evidence | Notes |
|---|---|---|---|---|---|---|---|
| §2-durable-01 | Immutable snapshot-bound runs; edits during render → new version | absent | — | B4, C7 | — | no version spine | — |
| §2-durable-02 | Dependency graph recomputes only affected nodes | partial | `backend/segment_render_cache.py` | D3 | `backend/test_segment_render_cache.py` | content-hash invalidation | no general DAG (`pipeline_graph.py` absent) |
| §2-durable-03 | Durable job state, restart/resume, idempotent queue redelivery | partial | `backend/job_store.py`, `backend/main.py` (resume endpoints) | B7 | `backend/test_job_store.py`, `test_resume_and_guardrails.py` | job persistence + resume present | **idempotent queue redelivery absent** — 5 `background_tasks.add_task` sites (`main.py:363,433,510,810,1039`), no queue, no idempotency key |
| §2-durable-04 | Reconcile provider operation ID before deciding paid retry | absent | `writer_agent_vertex.py`, `visual_evidence_vertex.py`, `backend/runtime_limits.py` | B8 | — | **DEFECT:** no provider op ID persisted → blind paid-retry / double-charge risk | — |
| §2-durable-05 | Auto transient retries max 2, within budget, no blind retry | partial | `writer_agent_vertex.py:14`, `backend/script_pipeline.py:28,73-82` | B3 | `backend/test_script_pipeline.py`, `test_writer_agent_vertex.py` | writer `DEFAULT_MAX_ATTEMPTS=2` ✓ | **DEFECT:** `DEFAULT_SCRIPT_MAX_RETRIES=3` (`script_pipeline.py:28`) exceeds doc cap 2; no budget re-check inside retry loop |
| §2-durable-06 | Cancellation: stop queued work, best-effort provider cancel, reconcile late result/cost | absent | — | B9 | — | no `/cancel` endpoint; `job_store` `valid_statuses` lacks `cancelling`/`cancelled` | — |
| §2-durable-07 | Initial render concurrency: one render per worker + deployment-configured global cap | partial | `backend/runtime_limits.py`, `backend/segment_render_cache.py` | B11, D9 | `backend/test_runtime_limits.py`, `test_segment_render_cache.py` | `FYF_MAX_CONCURRENT_JOBS`, segment concurrency clamp 1-4 default 2 | global deployment cap + UI surfacing partial |
| §2-durable-08 | Upload bytes/durations/worker CPU/mem/time/queue limits: config + validate + show in UI | partial | `backend/runtime_limits.py`, `backend/main.py:394` (`/api/runtime`) | B11 | `backend/test_runtime_limits.py` | some limits present | full `capacity_config.py` + UI render **absent** |
| §2-durable-09 | Overload → honest queued state; no duplicate submission | partial | `backend/runtime_limits.py:138,152` | B11 | `backend/test_runtime_limits.py` | rate/concurrency limiting present | returns bare **429** (no queue position/depth); duplicate submission not deduped |
| §2-durable-10 | Production durable storage must NOT be ephemeral local disk | absent | `.gitignore:15-20` | F8 | — | **DEFECT:** durable state on gitignored local disk `/output/ /jobs/ /script-jobs/ /locks/ /visual-artifacts/ /telemetry/` | violates doc line 115 |

#### Security, lifecycle & recovery (doc lines 119–133)

| Req ID | Requirement (short) | Status | Owning file(s) | Plan task | Test(s) | Evidence | Notes |
|---|---|---|---|---|---|---|---|
| §2-sec-01 | Google login, initial owner allowlist, server-side project/asset/job ownership checks | absent | `backend/main.py:284-312` | F1 | `backend/test_public_compliance.py` | only static bearer token `FYF_GENERATION_ACCESS_TOKEN` | grep `next-auth`=0; **blocked on Google OAuth client ID/secret** |
| §2-sec-02 | Judge access = separate owner-approved access; auth not blocking | absent | — | F1 | — | no judge-access path | blocked on OAuth creds |
| §2-sec-03 | Private storage, short-lived signed URLs, revocable review links | absent | — | F2 | — | grep `signed_url`/`gs://` = 0 | — |
| §2-sec-04 | Secret Manager/env secrets; not in repo/browser/telemetry | partial | `.env.example`, `.gitignore` | F5 | `backend/test_public_compliance.py` | env-based config; `gcp-key.json` gitignored + **untracked** (verified) | Secret Manager absent; credential rotation owner-gated |
| §2-sec-05 | Upload type/size/content validation; malformed-media/resource-abuse; path-traversal guard | partial | `backend/main.py:827-836`, `backend/lock_store.py:21-25`, `backend/visual_artifact_store.py:62` | F3 | `backend/test_visual_artifact_store.py` | path-traversal guards present | dedicated `uploads.py` validation module **absent** |
| §2-sec-06 | URL processing: SSRF, redirects, internal-network, protocol restrictions | absent | — | F3 | — | no SSRF guard found | — |
| §2-sec-07 | Imported content/MCP outputs = untrusted; cannot change permissions/approvals | partial | `backend/telemetry_queries.py:64-68,349-356`, `backend/agent/data_officer.py` | F3, E6 | `backend/test_clickhouse_telemetry.py` | caller SQL rejected; MCP read-only | explicit untrusted-content enforcement for uploads **absent** |
| §2-sec-08 | Tool calls server-side enforce schema, ownership, locks, approvals, budget | partial | `video_contract.py`, `backend/budget_store.py`, `backend/runtime_limits.py`, `backend/lock_store.py` | B10, F1 | `backend/test_video_contract.py`, `test_budget_store.py`, `test_runtime_limits.py` | schema (`extra="forbid"`), budget, locks enforced | ownership + approvals enforcement **absent** |
| §2-sec-09 | ClickHouse MCP read-only, approved views/tables, query timeout/row/resource limits | exists | `backend/telemetry_queries.py:20-21,28` | E3, E6 | `backend/test_clickhouse_telemetry.py` | `MAX_RESULT_ROWS=200`, `MAX_EXECUTION_SECONDS=10`, allowlisted `QUERY_SQL`, caller SQL rejected | — |
| §2-sec-10 | Raw prompts/scripts/responses, credentials, signed URLs not in default analytics | partial | `backend/telemetry_store.py`, `backend/vertex_telemetry.py` | E6 | `backend/test_telemetry_store.py`, `test_vertex_telemetry.py` | sanitized telemetry design | full non-retention assertion `unverified` |
| §2-sec-11 | Dependency/secret scanning, rate limits, auth expiry, error redaction | partial | `backend/runtime_limits.py` (`FYF_RATE_LIMIT_PER_MINUTE`) | F5 | `backend/test_runtime_limits.py` | per-IP rate limiter present | dependency/secret scanning, auth expiry, error redaction **absent** |
| §2-sec-12 | Trash 30 days; caches/failed intermediates 30-day expiry; derived-asset/review-link lifecycle | partial | `backend/main.py:929-958` | F6 | `backend/test_pipeline_ui_api.py` | archive path present | `archived_at` + 30-day sweep **absent** |
| §2-sec-13 | DB backup + object recovery config; backup access protected like production | absent | — | G1 | — | no backup/restore config | cloud resource creation owner-gated |
| §2-sec-14 | Restore project/version/approval/asset refs in clean env + render from stored assets + evidence | absent | — | G1 | — | no restore drill | — |
| §2-sec-15 | Deletion policy explains physical erasure possible after backup retention | absent | — | F6, G1 | — | no deletion-policy doc | doc line 133 |

### §3 — Skills, Tools, ClickHouse & Resources

#### Agent system (doc lines 139–143)

| Req ID | Requirement (short) | Status | Owning file(s) | Plan task | Test(s) | Evidence | Notes |
|---|---|---|---|---|---|---|---|
| §3-agent-01 | One user-facing Director + internal Brief/Strategy/Research/Script/Visual/Voice/Render/QA/Data Officer responsibilities | partial | `backend/agent/fyf_producer.py`, `backend/agent/data_officer.py`, `backend/pipeline.py` | D1 | `backend/agent/test_runner.py`, `backend/test_pipeline.py` | **2 ADK agents** (Director + Data Officer); responsibilities live in pipeline | internal responsibilities not yet modeled as versioned skill modules |
| §3-agent-02 | Each skill: trigger/inputs/outputs/allowed tools/invariants/examples/quality checks/version; each tool: schema/timeout/cost-risk/approval/retry-reconciliation/provenance | absent | — | D1 | — | no `backend/agent/skills/` module structure | skills facade planned (wrap, don't rewrite) |
| §3-agent-03 | Capabilities: media intake/analysis, asset prep, storytelling, motion/typography, narration/alignment/mixing, grounded edits, scoped variants, rendering, QA/repair | partial | `backend/pipeline.py`, `voice_service/*`, `remotion/src/`, `backend/output_qa.py`, `backend/creative_quality.py`, `backend/final_visual_qa_vertex.py` | D1, D3 | `backend/test_pipeline.py`, `test_output_qa.py`, `test_creative_quality.py` | capabilities present across pipeline/voice/remotion/QA | scoped variants partial; not organized as declared capabilities |

#### ClickHouse & telemetry (doc lines 147–158)

| Req ID | Requirement (short) | Status | Owning file(s) | Plan task | Test(s) | Evidence | Notes |
|---|---|---|---|---|---|---|---|
| §3-ch-01 | Six analytics capabilities: creation timeline, cost intelligence, quality tracking, editing friction, version comparison, grounded recommendations | partial | `backend/telemetry_queries.py:28-59` | E3 | `backend/test_clickhouse_telemetry.py` | `QUERY_SQL` allowlist has **4** keys: `jobs_overview`, `model_calls`, `scene_latency`, `cost_summary` | `creation_timeline`, `editing_friction`, `version_comparison`, `grounded_recommendations` **absent** (grow 4 → 6) |
| §3-ch-02 | Upgrade existing telemetry/UI; no duplicate dashboard | exists | `frontend/app/telemetry/page.tsx`, `backend/telemetry_queries.py` | E3 | `frontend/e2e/telemetry-insights.spec.ts` | single telemetry dashboard (587 lines) | constraint honored — no second dashboard |
| §3-ch-03 | Durable outbox, retry, stable event IDs, replay-safe aggregation, delivery-failure visibility | absent | `backend/clickhouse_telemetry.py:128-192` | E1, E2 | `backend/test_clickhouse_telemetry.py` | **DEFECT:** fire-and-forget insert; failure only `logger.warning` (`:190,237,287`); local mirror (`:52`) **never replayed** → silent permanent loss | no `telemetry_outbox.py` |
| §3-ch-04 | ClickHouse MCP actual runtime query evidence; fallback never shown as cloud success | partial | `backend/telemetry_queries.py:334-335,377,392`, `backend/agent/data_officer.py` | E6 | `backend/test_clickhouse_telemetry.py` | honest `source ∈ {clickhouse_cloud, local_mirror}` + `availability` | real runtime query evidence **blocked** (live ClickHouse stale 2026-08-25) |
| §3-ch-05 | Show source, freshness, ingestion lag, pending/failed deliveries | partial | `backend/telemetry_queries.py:334-335`, `frontend/app/telemetry/page.tsx` | E4 | `frontend/e2e/telemetry-insights.spec.ts` | `source` + `availability` shown | freshness/ingestion-lag/pending-failed counters **absent** |
| §3-ch-06 | Reconcile canonical job records ↔ provider usage ↔ ClickHouse aggregates | absent | — | E4 | — | no `telemetry_reconcile.py` | — |
| §3-ch-07 | Detect missing/duplicate/reordered events; unknown cost/usage never shown as zero | partial | `backend/telemetry_queries.py:335` | E1, E4 | `backend/test_clickhouse_telemetry.py` | `availability="unavailable"` when no rows → **never renders unknown as 0** ✓ | duplicate/reorder/gap detection **absent** (MergeTree, no `event_id`/`sequence`) |
| §3-ch-08 | Separate estimated / provider-reported / invoice-confirmed costs when available | absent | — | B2, E4 | — | no cost-tier fields | — |
| §3-ch-09 | Live progress/budget enforcement from operational backend, independent of ClickHouse | exists | `backend/budget_store.py`, `backend/job_store.py:98-170`, `backend/clickhouse_telemetry.py:31-32` | E5 | `backend/test_budget_store.py`, `test_job_store.py` | budget/progress read local state; CH client returns `None` when unconfigured | invariant already satisfied — protect from regression |
| §3-ch-10 | AI quality score, human approval, undo as separate signals | partial | `backend/creative_quality.py`, `backend/output_qa.py` | E7 | `backend/test_creative_quality.py`, `test_output_qa.py` | AI quality score present | human-approval + undo signals **absent** |
| §3-ch-11 | Analytics recommendation must not auto-change permissions/budget/preferences | exists | `backend/agent/data_officer.py`, `backend/telemetry_queries.py` | E6 | `backend/agent/test_runner.py`, `test_clickhouse_telemetry.py` | Data Officer read-only; caller SQL rejected | constraint honored |
| §3-ch-12 | Audience retention/conversion data shown as unavailable if missing; never invented from production telemetry | exists | `backend/telemetry_queries.py:335` | E4 | `backend/test_clickhouse_telemetry.py` | `availability="unavailable"` for empty result | constraint honored |

#### Resource guidance (doc lines 162–171)

| Req ID | Requirement (short) | Status | Owning file(s) | Plan task | Test(s) | Evidence | Notes |
|---|---|---|---|---|---|---|---|
| §3-res-01 | Primary reference: Google Gemini samples (agents/orchestration, function calling, controlled generation, multimodal, grounding, evaluation, token counting, caching, MCP) | partial | plan "Resource references"; `knowledge/github-repositories.md` gate | G4 | — | reference listed in approved plan | process/governance requirement; adoption requires owner approval + security review — no code artifact yet |
| §3-res-02 | Use current Google ADK, Google Cloud APIs, official ClickHouse MCP, Cloud Run/Secret Manager, pinned Remotion docs | partial | `backend/agent/*`, `backend/agent/data_officer.py`, `scripts/deploy_cloudrun.sh`, `remotion/` | G4 | `backend/agent/test_runner.py` | ADK + mcp-clickhouse + Cloud Run script present | Secret Manager absent; version pinning `unverified` |
| §3-res-03 | Verify current model IDs, pricing, regions, SDK compatibility; pin configuration | partial | `vertex_model_routing.py`, `backend/cost_catalog.py` | G4 | `backend/test_vertex_model_routing.py`, `test_cost_catalog.py` | model routing + cost catalog present | pricing/region currency `unverified` (needs dated check) |
| §3-res-04 | Do not treat samples as production-ready; security/license review + project approvals before reuse | exists | `knowledge/github-repositories.md`, plan approval gates | G4 | — | governance gate documented | process requirement honored |
| §3-res-05 | Resource guide must not override contest rules / approved architecture | exists | plan Governance Envelope | G4 | — | documented precedence | process requirement honored |
| §3-res-06 | Handoff includes source URL, checked date, version/commit, adopted changes, verification | absent | — | G4 | — | no resource-reference handoff yet | `HANDOFF_AGENTIC_VIDEO_STUDIO.md` planned (G4) |
| §3-res-07 | Do not pull every service/framework mentioned in resources into scope | exists | plan Rejected Alternatives | G4 | — | scope exclusions documented (no Grafana/NLE/etc.) | process requirement honored |

### §4 — Engineering Loop & Delivery Stages

#### Every-task loop (doc lines 179–187)

| Req ID | Requirement (short) | Status | Owning file(s) | Plan task | Test(s) | Evidence | Notes |
|---|---|---|---|---|---|---|---|
| §4-loop-01 | Check branch/HEAD, dirty/untracked files, active writers, test baseline; distrust old "green" claims | exists | this matrix `## Verified Baseline`; plan Summary | A1, A2 | full canonical command set | HEAD `621508f`, clean tree, 471/33/17 recorded 2026-09-08 | re-verified this task |
| §4-loop-02 | Maintain requirement → task → tests → evidence traceability matrix | exists | `docs/COMPLIANCE_MATRIX.md` (this file) | A3 | — | this Coverage Matrix + `## Traceability & Bug Log Formats` | — |
| §4-loop-03 | Bug log: reproduction, expected/actual, root cause, severity, regression test, verified result | exists | `docs/COMPLIANCE_MATRIX.md` `## Traceability & Bug Log Formats` | A3 | — | bug-log format defined below | — |
| §4-loop-04 | No green via reduced assertions, skipped tests, fabricated outputs | exists | repo test suites | A2 | `uv run pytest -q` | baseline: **0 failed, 0 skipped** | honesty rule restated below |
| §4-loop-05 | On repeated failure re-check hypothesis; no blind retry | partial | `backend/script_pipeline.py:73-82` | B3 | `backend/test_script_pipeline.py` | governance honored | **code defect:** retry default 3 + no budget re-check → blind-retry risk (§2-durable-05) |
| §4-loop-06 | Only isolated parallel assignments; ownership/interfaces/verification clear | exists | plan Concurrency & Writer-Ownership Map | A1 | — | writer-ownership map + allowlists | governance requirement honored |
| §4-loop-07 | Orchestrator independently verifies worker success claims | exists | plan Governance Envelope | A1 | — | "Leader-verified, not worker self-report" phase gates | governance requirement honored |
| §4-loop-08 | Legacy migration via idempotent import; keep originals; no legacy removal before parity | partial | plan B5, G3 | G3 | — | seams planned; parity suite planned | no active migration yet (postgres/GCS deferred to G3) |
| §4-loop-09 | Do not delete unowned/duplicate-looking files | exists | plan A1; task allowlist | A1 | — | A1 committed untracked in-flight work, deleted nothing | governance requirement honored |

#### Delivery stages (doc lines 189–197)

| Req ID | Requirement (short) | Status | Owning file(s) | Plan task | Test(s) | Evidence | Notes |
|---|---|---|---|---|---|---|---|
| §4-stage-A | Stage A: baseline, compliance checklist, preservation/coverage matrix — exit: current truth + blockers clear | exists | `docs/COMPLIANCE_MATRIX.md`, this baseline | A1, A2, A3, A4 | full canonical command set | A1/A2 verified; A3/A4 = this document | Stage A completed by this task |
| §4-stage-B | Stage B: state, commands, atomic changes, approvals, budgets, durable jobs — exit: concurrency/restart/idempotency tests pass | absent | — | B1–B12 | — | no `backend/projects/`, `job_queue.py`, `cancellation.py` | not started; critical path |
| §4-stage-C | Stage C: shared chat/canvas, selection, locks, undo, preview — exit: real browser persistence/editing pass | absent | — | C1–C9 | — | no chat UI, no `project/[id]` route, mocked E2E only | not started |
| §4-stage-D | Stage D: creative skills, timed animatic, motion/audio, exports — exit: real Burmese/English video pass | partial | `backend/pipeline.py`, `voice_service/*`, `remotion/src/` | D1–D9 | `backend/test_pipeline.py`, remotion npm test (33) | pipeline + voice + render exist | skill modules/exports/reduced-motion absent; real-video exit **blocked** ($0) |
| §4-stage-E | Stage E: reliable telemetry, MCP, six analytics — exit: real query, replay/outage, reconciliation pass | partial | `backend/clickhouse_telemetry.py`, `backend/telemetry_queries.py`, `backend/agent/data_officer.py` | E1–E7 | `backend/test_clickhouse_telemetry.py` | dual-write + 4 allowlisted queries | outbox/replay/reconciliation absent; 4→6 capabilities; live CH **blocked** |
| §4-stage-F | Stage F: security, accessibility, capacity, recovery, bug fixes — exit: critical/high defects closed | partial | `backend/main.py:284-312`, `backend/runtime_limits.py` | F1–F9 | `backend/test_public_compliance.py`, `test_runtime_limits.py` | token auth + rate limits + path guards | authN/authZ, signed URLs, SSRF, emergency stop absent; **open critical defects** (B1/B2) |
| §4-stage-G | Stage G: restore drill, rollback drill, evidence/handoff — exit: reproducible user-ready product | absent | — | G1–G4 | — | no backup/restore, no rollback drill, no final handoff | not started |

#### Release safety (doc lines 201–206)

| Req ID | Requirement (short) | Status | Owning file(s) | Plan task | Test(s) | Evidence | Notes |
|---|---|---|---|---|---|---|---|
| §4-release-01 | Emergency control to stop new paid jobs while preserving in-flight reconciliation | absent | — | F4 | — | no `backend/feature_flags.py`; `FYF_PAID_PRODUCTION_ENABLED` not found | single choke point planned at `runtime_limits.enforce_generation_guardrails` |
| §4-release-02 | Individual model/tool disable switches; disabled capability shows honest fallback/pause | absent | `vertex_model_routing.py` | F4 | `backend/test_vertex_model_routing.py` | routing table present, no disabled state | honest fallback/pause planned |
| §4-release-03 | Deployment rollback + backward-compatible in-flight job handling | absent | `scripts/deploy_cloudrun.sh` | G2 | `scripts/test_deploy_cloudrun_contract.sh` | deploy contract present | no rollback drill; backward-compat not tested |
| §4-release-04 | Schema changes use expand/migrate/contract; no destructive migration without approval | partial | plan E1, G2 | E1, G2 | — | governance documented | no schema migrations executed yet; destructive migration owner-gated |
| §4-release-05 | Old/new worker compatibility + previous-release recovery tested in staging | absent | — | G2 | — | no staging environment (blocked on `PROJECT_ID`) | — |
| §4-release-06 | Push, deployment, publication, submission are separate approval gates | exists | plan Approval Gates table | A1, G2, G4 | — | all four owner-gated; none authorized by plan | governance requirement honored |

### §5 — Tests, Acceptance & Final Handoff

#### Test policy (doc lines 212–214)

| Req ID | Requirement (short) | Status | Owning file(s) | Plan task | Test(s) | Evidence | Notes |
|---|---|---|---|---|---|---|---|
| §5-policy-01 | Prohibited: fake production outputs, fabricated telemetry, hardcoded success, mocked E2E/provider success claims | exists | this doc `## Honesty Rules`; repo suites | A2 | `uv run pytest -q` (0 skipped) | policy restated; baseline hermetic | current 17 browser E2E are mocked → must be labeled UI-regression, not real-provider proof |
| §5-policy-02 | Permitted: deterministic inputs + isolated failure simulation for pure unit tests; never counted as real-provider verification | exists | repo unit suites | A2 | `uv run pytest -q` | deterministic unit tests present | not counted toward real-provider exit gates |

#### Automated test coverage (doc lines 218–227)

| Req ID | Requirement (short) | Status | Owning file(s) | Plan task | Test(s) | Evidence | Notes |
|---|---|---|---|---|---|---|---|
| §5-cover-01 | Commands/schema, stale versions, ownership, locks, atomic multi-scene edits | partial | `backend/test_video_contract.py`, `backend/test_pipeline_ui_api.py` | B6 | `test_video_contract.py` | schema + lock coverage present | stale-version, ownership, atomic multi-scene coverage **absent** |
| §5-cover-02 | Budget reservations, concurrent spend, approvals, unknown billing, cancellation reconciliation | partial | `backend/test_budget_store.py`, `backend/test_resume_and_guardrails.py` | B1, B2, B10 | `test_budget_store.py` | reservation + guardrail coverage present | approvals, unknown-billing, cancellation-reconciliation coverage **absent**; cancel-charge defect untested |
| §5-cover-03 | Timeline, captions, typography, aspect reflow, render-manifest reproducibility | partial | `backend/test_render_contract.py`, `backend/test_segment_render_cache.py`, `backend/test_lip_sync.py` | C6, D5 | `test_render_contract.py`, `test_segment_render_cache.py` | aspect + segment cache + lip-sync coverage | full render-manifest reproducibility (fonts/seeds) **absent** |
| §5-cover-04 | DB/storage/queue integration, redelivery, worker crash/resume, partial failures | partial | `backend/test_job_store.py`, `backend/test_resume_and_guardrails.py` | B7 | `test_job_store.py`, `test_resume_and_guardrails.py` | job resume coverage present | queue redelivery + DB/storage integration **absent** (no queue/DB yet) |
| §5-cover-05 | Telemetry duplicates/order/missing, outage/replay, freshness, reconciliation | partial | `backend/test_clickhouse_telemetry.py`, `backend/test_telemetry_store.py` | E1, E2, E4 | `test_clickhouse_telemetry.py` | dual-write + fallback coverage present | duplicate/order/missing, replay, reconciliation coverage **absent** |
| §5-cover-06 | Unauthorized access, expired/revoked links, unsafe upload/URL, prompt injection, SQL/tool restrictions | partial | `backend/test_public_compliance.py`, `backend/test_clickhouse_telemetry.py` | F1, F3 | `test_public_compliance.py` | token-boundary + caller-SQL-rejection coverage | revoked-link, unsafe-upload/URL, prompt-injection coverage **absent** |
| §5-cover-07 | Frontend typecheck/lint/production build, accessibility, console/network errors | partial | `frontend/` lint+build, `frontend/e2e/responsive-theme.spec.ts` | F7, F9 | `npm run lint`, `npm run build`, e2e | lint + build clean (exit 0); responsive-theme e2e | accessibility (axe) + console/network-error assertions **absent** |
| §5-cover-08 | Capacity/queue limits, emergency stop, disabled tools, deployment rollback | partial | `backend/test_runtime_limits.py`, `scripts/test_deploy_cloudrun_contract.sh` | B11, F4, G2 | `test_runtime_limits.py`, `test_deploy_cloudrun_contract.sh` | runtime-limit + deploy-contract coverage | emergency-stop + disabled-tool + rollback coverage **absent** |
| §5-cover-09 | Backup restore + regenerated stored-assets output | absent | — | G1 | — | no backup/restore test | — |
| §5-cover-10 | Real Google calls, real ClickHouse MCP queries, real MP4 rendering/playback | absent | — | C8, D, E6 | — | **blocked:** only mocked E2E (`route.fulfill`); no real-provider suite | requires OAuth creds, live ClickHouse, funded ceiling |

#### Required real end-to-end scenarios (doc lines 231–243)

| Req ID | Requirement (short) | Status | Owning file(s) | Plan task | Test(s) | Evidence | Notes |
|---|---|---|---|---|---|---|---|
| §5-e2e-01 | Burmese 30s product ad from uploaded assets through download/telemetry | absent | — | D, E | — | no real-provider E2E | blocked: funded ceiling + real render |
| §5-e2e-02 | English diagram/text-heavy explainer | absent | — | D | — | no real-provider E2E | blocked: funded ceiling |
| §5-e2e-03 | Selected headline rewrite with voice/background locked; Undo restores | absent | — | C4, C5 | — | no undo/version spine | blocked: Stage B/C |
| §5-e2e-04 | 9:16/16:9/1:1 adaptation with safe zones | absent | `video_contract.py:9` | D5, D6 | — | aspect ratios exist; safe-zone adaptation E2E absent | blocked: real render |
| §5-e2e-05 | 60→15s story adaptation with necessary reapproval | absent | — | D6, B10 | — | no duration re-edit command / approvals | blocked: Stage B/D |
| §5-e2e-06 | Default/custom budget, insufficient budget, paid regeneration approval | absent | `backend/budget_store.py` | B2, B10 | — | budget caps exist; approval + $3 default absent | blocked: Stage B |
| §5-e2e-07 | Failure/resume after browser closure and worker restart | partial | `backend/test_resume_and_guardrails.py` | B7 | `test_resume_and_guardrails.py` | unit-level resume present | real browser-close + worker-restart E2E **absent** |
| §5-e2e-08 | Edit during render; stale output cannot overwrite draft | absent | — | C7 | — | no version spine | blocked: Stage B/C |
| §5-e2e-09 | ClickHouse outage/recovery; truthful local fallback + duplicate-safe replay | partial | `backend/test_clickhouse_telemetry.py` | E1, E2 | `test_clickhouse_telemetry.py` | local-fallback unit coverage present | duplicate-safe replay **absent** (MergeTree); live CH blocked |
| §5-e2e-10 | Auth/privacy, review-link revoke, Trash restore/delete | absent | — | F1, F2, F6 | — | no auth/review-link/trash-restore E2E | blocked: OAuth creds + Stage F |
| §5-e2e-11 | Multi-scene invalid edit rejects entirely; no partial state corruption | absent | — | B6 | — | no atomic change-set | blocked: Stage B |
| §5-e2e-12 | Restore clean environment and render from preserved manifest/assets | absent | — | G1 | — | no restore drill | blocked: Stage G |
| §5-e2e-13 | Account spending stop + release rollback preserve existing projects/jobs | absent | — | F4, G2 | — | no emergency stop / rollback drill | blocked: Stage F/G |

#### Visual/audio & performance verification (doc lines 247–252)

| Req ID | Requirement (short) | Status | Owning file(s) | Plan task | Test(s) | Evidence | Notes |
|---|---|---|---|---|---|---|---|
| §5-visual-01 | Full MP4 playback + representative frames: no blank frames, missing assets, glyph issues, clipping, timing/desync | partial | `backend/final_visual_qa_vertex.py`, `backend/output_qa.py` | D7 | `backend/test_final_visual_qa_vertex.py`, `test_output_qa.py` | visual QA present | full real-MP4 playback verification **blocked** ($0, mocked only) |
| §5-visual-02 | Product/claim accuracy, hook/message/CTA, pacing, continuity, audio balance | partial | `backend/creative_quality.py`, `backend/public_compliance.py` | D2 | `backend/test_creative_quality.py`, `test_public_compliance.py` | creative QA present | real-output acceptance **blocked** |
| §5-visual-03 | Approved locks/content remain unchanged after automated repair | absent | `backend/lock_store.py` | C4 | — | whole-script lock only; post-repair immutability not enforced/tested | — |
| §5-visual-04 | Technical QA and creative/human acceptance reported separately | partial | `backend/output_qa.py`, `backend/creative_quality.py` | D7 | `backend/test_output_qa.py`, `test_creative_quality.py` | technical (`output_qa`) vs creative (`creative_quality`) modules separate | human-acceptance report **absent** |
| §5-visual-05 | Latency targets (chat ≤10s, draft ≤60s, final ≤10min) measured, never guaranteed unmeasured | absent | — | D8 | — | no `backend/latency_metrics.py`; only `pipeline._record_stage_timing` | cold-start (`--min-instances 0`) dominant risk; report measured/unmeasured only |

#### Hackathon gate (doc lines 256–261)

| Req ID | Requirement (short) | Status | Owning file(s) | Plan task | Test(s) | Evidence | Notes |
|---|---|---|---|---|---|---|---|
| §5-hack-01 | Actual Google runtime + official `mcp-clickhouse` integration evidence | partial | `backend/agent/data_officer.py`, `writer_agent_vertex.py`, `visual_evidence_vertex.py` | E6, D | `backend/agent/test_runner.py` | integration code present | runtime evidence **blocked** (OAuth creds + live ClickHouse stale) |
| §5-hack-02 | Production-team workflow theme fit | partial | `frontend/app/page.tsx`, `backend/render_contract.py` | D | `backend/test_business_render_contract.py` | business presets + team-oriented flow | full chat/canvas team workflow absent (Stage C) |
| §5-hack-03 | Implementing agent checks originality/history + AI-tool restrictions; ambiguity → organizer clarification | absent | — | A4 | — | **blocker:** organizer clarification not yet obtained | see Blocker Register BR-05 |
| §5-hack-04 | Public licensed repo, approved hosted judging access, English submission materials, ≤3-min functioning demo | partial | `LICENSE`, `DEVPOST_SUBMISSION.md`, `docs/DEMO_SCRIPT.md` | G4 | — | LICENSE + submission + demo script present | public repo at `5567b27`; hosted judging access **blocked** (auth + deploy) |
| §5-hack-05 | Local fallback / README mention not accepted as partner-runtime compliance | exists | this doc `## Honesty Rules` | A2, E6 | — | policy: local mirror never presented as cloud success | constraint honored |
| §5-hack-06 | No registration/terms acceptance/publication/submission without approval boundary | exists | plan Approval Gates table | G4 | — | all owner-gated; none performed | constraint honored |

#### Definition of done & handoff (doc lines 265–276)

| Req ID | Requirement (short) | Status | Owning file(s) | Plan task | Test(s) | Evidence | Notes |
|---|---|---|---|---|---|---|---|
| §5-dod-01 | Each approved requirement has implementation/test/evidence | partial | `docs/COMPLIANCE_MATRIX.md` | A3, G4 | — | this matrix tracks every requirement | many rows `absent`/`partial` → not done |
| §5-dod-02 | No remaining critical/high security, data-loss, duplicate-spend, authorization, core-flow bugs | absent | `backend/budget_store.py:276,18-19,48-58` | B1, B2, B8 | `backend/test_budget_store.py` | **OPEN critical defects:** cancel-charge drop, fail-open ceiling, no provider-op-ID (blind paid retry) | Stage B must close |
| §5-dod-03 | Existing regressions + new acceptance suites pass | partial | repo suites | A2, B–G | `uv run pytest -q`, remotion, e2e | existing 471/33/17 pass, 0 skipped | new acceptance suites **absent** |
| §5-dod-04 | Fresh UI-created Burmese/English videos playback/download verified | absent | — | D | — | no real render artifact this baseline | blocked: funded ceiling |
| §5-dod-05 | Real Google/ClickHouse MCP telemetry evidence exists | absent | — | E6 | — | no real-provider evidence | blocked: OAuth + live ClickHouse |
| §5-dod-06 | Restore, rollback, emergency controls, partial-failure recovery verified | absent | — | F4, G1, G2 | — | none implemented | blocked: Stage F/G + `PROJECT_ID` |
| §5-dod-07 | Remaining lower-severity limitations described with reproducible details | partial | `docs/COMPLIANCE_MATRIX.md`, Blocker Register | A3, A4, G4 | — | this matrix + blocker register capture gaps | consolidated handoff (G4) pending |
| §5-dod-08 | Clean setup/run/test instructions reproducible by another engineer | exists | `docs/DEVELOPER_GUIDE.md`, `README.md` | A2, G4 | canonical command set | setup + run + test documented | test-count line 76 corrected by this task |
| §5-dod-09 | Handoff: branch/HEAD, changes, coverage matrix, tests, artifacts, walkthrough, cost/perf, bug/security logs, references, backup/rollback, approvals, next actions | partial | `docs/HANDOFF_AGENTIC_BUSINESS_STUDIO.md` | G4 | — | prior business-studio handoff exists | full `HANDOFF_AGENTIC_VIDEO_STUDIO.md` (G4) pending |
| §5-dod-10 | Blocked external verification labeled "implemented but not fully verified" (doc line 276) | exists | this doc `## Blocker Register`, `## Honesty Rules` | A4 | — | labeling policy applied throughout matrix | user testing does not replace agent verification duty |

---

## Blocker Register

External prerequisites that this task cannot resolve. Every acceptance criterion that
depends on an unresolved blocker is labeled **"implemented but not fully verified"** with
the missing credential/resource named — never presented as passing. `Owner` = who must act.

| ID | Blocker | Owner | What would unblock it | Affected plan tasks | Resulting label if unresolved |
|---|---|---|---|---|---|
| BR-01 | Google OAuth client ID/secret (login, judge access, ownership checks) | Victor (owner) + Google Cloud console | Provision OAuth client, supply ID/secret via env/Secret Manager, approve owner allowlist | F1, F2, §5-e2e-10, §5-hack-01, §5-dod-05 | "implemented but not fully verified — missing Google OAuth client ID/secret" |
| BR-02 | Live ClickHouse instance (last verified live **2026-08-25**, now stale) | Victor (owner) | Confirm `.env.clickhouse` credentials valid + reachable; run a live `mcp-clickhouse` query and capture evidence | E1–E7, §3-ch-01/04, §5-e2e-09, §5-hack-01, §5-dod-05 | "implemented but not fully verified — live ClickHouse unavailable; `source: local_mirror` only" |
| BR-03 | Owner-approved `GOOGLE_CLOUD_PROJECT` / `PROJECT_ID` for Cloud Run | Victor (owner) | Provide explicit approved project ID; authorize deployment | F8, G1, G2, §4-release-03/05, §5-dod-06 | "implemented but not fully verified — no approved Cloud Run `PROJECT_ID`" |
| BR-04 | Funded spending ceiling above the approved **$3** envelope | Victor (owner) | Approve + fund a higher ceiling for real renders / 13 real E2E scenarios / D8 latency measurement | D (real renders), §5-e2e-01..13, D8, §5-dod-04 | "implemented but not fully verified — real-provider runs exceed approved $3 envelope" |
| BR-05 | Hackathon organizer clarification on originality/history + AI-tool restrictions | Victor (owner) → organizer | Written organizer answer on prior-art/originality and permitted AI tools | A4, §5-hack-03, G4 | "implemented but not fully verified — organizer clarification pending" |
| BR-06 | Owner approval: push to public `main` (currently `5567b27`) | Victor (owner) | Explicit separate push approval | G4, §4-release-06 | not pushed — approval gate held |
| BR-07 | Owner approval: Cloud Run deployment (staging/prod) | Victor (owner) | Explicit deploy approval + `PROJECT_ID` (see BR-03) | F8, G2 | not deployed — approval gate held |
| BR-08 | Owner approval: publication / trailer upload | Victor (owner) | Explicit publication + upload approval | G4, §5-hack-04 | not published — approval gate held |
| BR-09 | Owner approval: Devpost submission / registration / terms acceptance | Victor (owner) | Explicit submission approval | G4, §5-hack-06 | not submitted — approval gate held |
| BR-10 | Owner approval: destructive schema migration | Victor (owner) | Explicit migration approval (else expand/migrate/contract only) | E1, G2, §4-release-04 | expand/migrate/contract only — no destructive migration |
| BR-11 | Owner approval: cloud resource creation (GCS bucket, Postgres, backup) | Victor (owner) | Explicit resource-creation approval | B5, G1, G3 | no cloud resources created — approval gate held |
| BR-12 | Owner approval: credential rotation | Victor (owner) | Explicit rotation approval | F5, §2-sec-04 | no rotation performed — approval gate held |
| BR-13 | Owner approval: new dependency (e.g. `@axe-core/playwright`) | Victor (owner) | Explicit dependency approval + security review | F7, §5-cover-07 | dependency not added — approval gate held |
| BR-14 | Owner approval: raise spending ceiling / budget increase | Victor (owner) | Explicit ceiling-raise approval (see BR-04) | B2, D, §5-e2e-06 | ceiling kept at $3 — approval gate held |

---

## Honesty Rules

Restated from the source document §5 test policy and the approved plan. These bind every
later stage and every claim in this matrix.

1. **No fake production outputs.** Rendered/voice/QA artifacts presented as real must come
   from a real provider run, not synthesized fixtures.
2. **No fabricated telemetry.** Telemetry rows must originate from actual pipeline events;
   never invent metrics to make a dashboard look populated.
3. **No hardcoded success.** Endpoints/tests must not return canned success to appear green.
4. **No mocked E2E presented as real-provider proof.** See rule 9 below.
5. **No reduced assertions or skipped tests** to reach green. Baseline is **0 failed, 0
   skipped** (471 pytest / 33 remotion / 17 e2e) and must stay that way honestly.
6. **No blind retries.** On repeated failure, re-check the hypothesis; never re-dispatch a
   paid operation without reconciling its provider operation ID (see §2-durable-04/05).
7. **Unknown cost/usage is never rendered as `0`.** Show `unavailable`. The existing
   `telemetry_queries.py:335` `availability="unavailable"` behavior already honors this and
   must not regress.
8. **Unavailable audience data is shown as `unavailable`** — never inferred or invented from
   production telemetry (§3-ch-12).
9. **The existing 17 browser E2E tests are MOCKED (`route.fulfill`) UI-regression tests.**
   `frontend/e2e/create-studio.spec.ts` intercepts `/api/*` and returns mocked story/variant
   payloads (e.g. lines 5, 164, 180–209, 275, 334). They verify UI behavior only and **do
   NOT satisfy the document's real-provider exit gates** (§5-e2e-01..13, §5-cover-10). Any
   claim that treats them as real-provider verification is dishonest and prohibited.
10. **Blocked criteria are labeled "implemented but not fully verified"** with the missing
    credential/resource named (see Blocker Register). A user offering to test later does not
    replace the implementing agent's own verification responsibility (doc line 276).

---

## Traceability & Bug Log Formats

Canonical formats used across all stages. This matrix is the requirement→evidence spine.

**Traceability chain (per requirement):**

```
requirement (doc §) → task id (A1–G4) → files touched → test ids → evidence artifact path → status
```

Example (this task, A3/A4):

```
§4-loop-02 (doc line 180) → A3 → docs/COMPLIANCE_MATRIX.md → (doc deliverable, no test id)
  → docs/COMPLIANCE_MATRIX.md#coverage-matrix → exists
```

**Bug log (per defect):**

```
id → reproduction → expected vs actual → root cause → severity → regression test id → verified result
```

Seed entries from verified defects (to be closed in Stage B/E and re-verified):

| id | reproduction | expected vs actual | root cause | severity | regression test id | verified result |
|---|---|---|---|---|---|---|
| BUG-01 | Reconcile an operation with `outcome="cancelled"` and `actual_usd>0` | Expected: incurred charge debited to ledger. Actual: charge dropped | `backend/budget_store.py:276` `if actual_usd > 0.0 and outcome != "cancelled":` | Critical (duplicate/lost spend accounting) | to add in B1 (`backend/test_budget_store.py`) | OPEN — verified by read, not yet fixed |
| BUG-02 | Run with budget-cap env vars unset | Expected: fail-closed, paid production disabled. Actual: fail-open to $10 daily/$50 total | `backend/budget_store.py:18-19` defaults + `_parse_cap_usd:48-58` returns `(default, True)` | Critical (unbounded spend) | to add in B2 (`backend/test_budget_store.py`) | OPEN — verified by read, not yet fixed |
| BUG-03 | Configure `FYF_SCRIPT_MAX_RETRIES` unset; observe script retry ceiling | Expected: max 2 transient retries (doc line 110). Actual: default 3 | `backend/script_pipeline.py:28` `DEFAULT_SCRIPT_MAX_RETRIES=3`; no budget re-check in loop | High (blind paid retry) | existing `backend/test_script_pipeline.py`; extend in B3 | OPEN — verified by read, not yet fixed |
| BUG-04 | Replay/duplicate telemetry delivery into ClickHouse | Expected: dedup via stable `event_id`. Actual: rows duplicate; dedup impossible | `backend/clickhouse_telemetry.py:74,88,99,121` all `ENGINE = MergeTree()` | High (analytics corruption) | to add in E1 (`backend/test_clickhouse_telemetry.py`) | OPEN — verified by read, not yet fixed |
| BUG-05 | ClickHouse insert fails at runtime | Expected: durable outbox + retry + replay of local mirror. Actual: fire-and-forget, `logger.warning` only; local mirror never replayed → silent loss | `backend/clickhouse_telemetry.py:128-192` (`:190,237,287`); mirror `:52` | High (telemetry data loss) | to add in E2 (`backend/test_clickhouse_telemetry.py`) | OPEN — verified by read, not yet fixed |
| BUG-06 | Issue a query while ClickHouse is slow/unreachable | Expected: delivery off the request path. Actual: `send_receive_timeout=300` can stall a user-facing request up to 300s | `backend/clickhouse_telemetry.py:45-46` (`connect_timeout=30`, `send_receive_timeout=300`) | High (request-path stall) | to add in E2/E5 | OPEN — verified by read, not yet fixed |
| BUG-07 | A paid provider call fails and is retried | Expected: reconcile provider operation ID before paid retry. Actual: no provider op ID persisted → blind retry / double-charge risk | no `ToolResult.provider_operation_id` (grep `idempotency`=0) | High (duplicate spend) | to add in B8 | OPEN — verified by read, not yet fixed |
| BUG-08 | Submit concurrent/duplicate jobs; restart worker mid-job | Expected: durable queue, idempotent redelivery. Actual: 5 in-process `background_tasks.add_task` sites, no queue, no idempotency key | `backend/main.py:363,433,510,810,1039` | High (duplicate jobs/reservations) | to add in B7 | OPEN — verified by read, not yet fixed |
| BUG-09 | Deploy to production on ephemeral local disk | Expected: durable production storage. Actual: durable state on gitignored local disk | `.gitignore:15-20` (`/output/ /jobs/ /script-jobs/ /locks/ /visual-artifacts/ /telemetry/`) | Critical (data loss, violates doc line 115) | to add in F8/G1 | OPEN — verified by read, not yet fixed |

---

## Status Distribution (this baseline)

Computed over every `## Coverage Matrix` row. See `## Verified Baseline` for the run facts.

| Status | Meaning | Count |
|---|---|---|
| `exists` | implemented + verified in repo | 31 |
| `partial` | partly implemented, documented gap remains | 84 |
| `absent` | 0-match grep or verified missing | 64 |
| `TBD` | **must be zero** | 0 |

**Total coverage-matrix rows: 179** (31 exists + 84 partial + 64 absent). **TBD rows: 0.**
Blocker Register rows: 14. Bug-log seed entries: 9 (all OPEN, verified by read).

_Generated by Stage A3/A4 on 2026-09-08 (Asia/Bangkok) at HEAD `621508f`. Owning paths verified by direct read/grep of the repository; no path was guessed. Paid provider cost incurred while producing this document: **$0**._
