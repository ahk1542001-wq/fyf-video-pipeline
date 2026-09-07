# Demo Video Script: Agentic Business Video Studio

**Target length**: 2:30-2:50

**Screen**: 1920x1080 (16:9), browser at a legible zoom

**Recording gate**: Run this script only after root verifies the current UI, request contract, fresh render, and telemetry path. Use an owner-approved, non-sensitive business brief. Never use mock evidence, seeded demo numbers, a cached/API-only result, or a static audit badge.

If a target control is not verified, omit that claim from the recording. If the selected job or ClickHouse has no data, use the unavailable wording in Beat 4; do not fill the gap with an estimate.

## Beat 1 - The business problem (0:00-0:25)

**Screen:** Create Studio at `http://localhost:3001/`, showing the verified two-column workbench.

**Voiceover:**

> "Business teams need clear explainers, social ads, product launches, and brand videos from the same brief. Agentic Business Video Studio keeps that work in one reviewable workspace: choose a business preset, shape the brand output, approve the story, and render the finished video. The original Burmese FYF explainer remains Preset #1 and the default."

**Action:** Show the Quick Business Presets in order:

1. `Brand Explainer (Flagship)` (the Burmese FYF default)
2. `High-Converting Social Ad`
3. `Product Launch Hype (Magnific)`

Keep these labels scoped to the business product and the target event.

## Beat 2 - From brief to approved story (0:25-0:55)

**Screen:** Left-side brief and story controls, then the story options and approval state.

**Action:** Paste the owner-approved business brief. If no approved brief is available, stop the take rather than inventing a product, claim, or source. Use the visible story-generation control, review the returned options, and approve the selected narration/story lock.

**Voiceover:**

> "One Google ADK Producer Agent coordinates research, drafting, story-quality review, and visual-shot planning. Voice generation, storyboard work, Remotion rendering, quality checks, and telemetry are pipeline stages, tools, or deterministic responsibilities, not separate agents. I review the options and approve the narration lock before rendering."

## Beat 3 - Brand controls and multi-format output (0:55-1:35)

**Screen:** Verified brand kit on the left and MP4 preview on the right.

**Action:** Select a verified business preset, set the owner-approved CTA, toggle the requested options, and choose one format. Show the controls only if root has verified that they reach the render request:

- `cta_text`
- `retention_progress_bar`
- `animated_lower_thirds`
- `aspect_ratio` (`9:16`, `16:9`, or `1:1`)

Queue the video from the approved lock and show the real progress states. Do not claim a control is rendered when the current run did not carry it through.

**Voiceover:**

> "The brand kit is explicit: CTA text, retention progress, animated lower thirds, and aspect ratio. I can choose vertical, widescreen, or square output without opening a timeline editor. The workspace stays focused on the brief, the approved story, and the finished cut."

## Beat 4 - Evidence-backed audit (1:35-2:25)

**Screen:** The completed MP4 in the right preview, then `/telemetry` with the same job selected.

**Action:**

1. Wait for the actual render and QA status. Play the fresh MP4 for several seconds.
2. Point to the audit area and read only values present for the selected job, such as actual duration, token status/count, cost status/value, and aspect ratio. Never read a memorized or hard-coded metric.
3. Open `http://localhost:3001/telemetry`, refresh the ledger, and select the same job ID.
4. Ask the Data Officer:

   `How many video jobs are recorded, how many succeeded, and what did they cost in total?`

5. In the ClickHouse query console, choose one of the supported query presets and run it through the allowlisted `POST /api/clickhouse/query` endpoint. The endpoint accepts a `query_id`, not caller SQL. Show the returned `source`, `availability`, and rows alongside the Data Officer answer.
6. Say "answered from live ClickHouse" only when the allowlisted response reports `source: "clickhouse_cloud"`, `availability: "available"`, and relevant rows that match the answer.

**Voiceover when live data is available:**

> "The audit view is reading this selected run's recorded evidence, not a sample value. The Data Officer is a separate ADK agent using the official ClickHouse MCP path. The allowlisted query endpoint reports `source: clickhouse_cloud` and `availability: available`, and these rows match the answer."

**Voiceover when the endpoint uses the local fallback:**

> "The allowlisted query endpoint reports `source: local_mirror`. This is local real telemetry fallback for this environment, not ClickHouse Cloud proof, so I am not making a live-cloud claim."

**Voiceover when data is unavailable, times out, or returns no row:**

> "The allowlisted query is unavailable or returned no rows for this run, so no live-cloud metric is claimed. The product shows the unavailable or retryable state instead of inventing a number."

Do not record a green live-cloud claim unless the `source: "clickhouse_cloud"` and `availability: "available"` conditions are visible. If the source is `local_mirror`, label it **local real telemetry fallback** and do not describe it as ClickHouse Cloud evidence. A loaded page, a Data Officer answer without the allowlisted query result, or a green-looking badge alone is insufficient evidence.

## Beat 5 - Closing (2:25-2:50)

**Screen:** Approved video preview or a simple stack summary showing the business product name.

**Voiceover:**

> "Agentic Business Video Studio makes business video production reviewable from brief to approved MP4, with explicit brand controls and evidence-backed telemetry. It is powered by Google Cloud Run, Vertex AI, Gemini, Google ADK, Remotion, and ClickHouse. We are presenting it for Agentic Cinema: The Blockbuster Hackathon."

## Recording checklist

- [ ] Root verified the business-first labels, two-column layout, no-timeline interaction, and old Burmese Preset #1/default.
- [ ] Root verified request propagation for `cta_text`, `retention_progress_bar`, `animated_lower_thirds`, and `aspect_ratio`.
- [ ] The take uses an owner-approved, non-sensitive business brief and a fresh UI-created MP4.
- [ ] The selected aspect ratio, CTA, lower thirds, retention bar, playback, and QA state match the actual run.
- [ ] Audit values are read from the selected job; no mock, static, or hard-coded metrics appear on screen.
- [ ] The Data Officer answer is paired with an allowlisted query response reporting `source: "clickhouse_cloud"` and `availability: "available"`, or the local-fallback/unavailable wording is used.
- [ ] No credentials, environment values, `.env` contents, or secret-store output appear in the recording.
- [ ] Any commit, push, deployment, public video upload, and Devpost submission has its own explicit owner approval. The canonical deploy command, if approved, is `bash scripts/deploy_cloudrun.sh`.
