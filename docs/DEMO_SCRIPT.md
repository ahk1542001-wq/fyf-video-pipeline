# Demo Video Script: Agentic Business Video Studio

**Target length**: 2:30-2:50

**Screen**: 1920x1080 (16:9), browser at a legible zoom

**Recording gate**: Run this script only after root verifies the current UI, request contract, fresh render, and telemetry path. Use an owner-approved, non-sensitive business brief. Never use mock evidence, seeded demo numbers, a cached/API-only result, or a static audit badge.

If a target control is not verified, omit that claim from the recording. If the selected job or ClickHouse has no data, use the unavailable wording in Beat 4; do not fill the gap with an estimate.

## Beat 1 - Studio Presets & Multi-Format Brand Kit (0:00-0:22)

**Screen:** Create Studio at `http://localhost:3001/`, showing the two-column workbench.

**Voiceover:**

> "Welcome to FYF Agentic Business Video Studio, created for the Google Cloud Agentic Cinema Hackathon. Our studio turns a simple brief into reviewable production video across Burmese flagship explainers, high-converting social ads, and product launches with instant aspect ratio switching."

**Action:** Show Quick Business Presets (`Brand Explainer (Flagship)`, `High-Converting Social Ad`, `Product Launch`), aspect ratios (`16:9`, `1:1`, `9:16`), and brand kit parameters.

## Beat 2 - Chat + Canvas Studio & Granular Scene Locks (0:22-0:48)

**Screen:** Interactive Canvas Studio at `http://localhost:3001/project/{id}`.

**Voiceover:**

> "In our new Chat plus Canvas Studio, operators collaborate with an AI Creative Director. The living canvas organizes scenes into an interactive storyboard with granular scene controls and locks, enabling surgical script revisions, version history, and safe human-in-the-loop proposals."

**Action:** Navigate to the Project Studio, select Scene 1 on the storyboard canvas to reveal scene controls, focus the note to the Creative Director in the Chat Panel, and demonstrate granular scene locks and linear version history.

## Beat 3 - Approved Video Library & Remotion Rendering (0:48-0:70)

**Screen:** Video Library at `http://localhost:3001/library`.

**Voiceover:**

> "In the Approved Video Library, let's inspect our completed flagship production. The Remotion engine renders frame-accurate motion graphics, synchronized character lip-sync, and multi-layer visual evidence while preserving Burmese typography."

**Action:** Select the flagship production explainer video and play full playback showing mascot lip-sync and dynamic visual evidence.

## Beat 4 - 20-Gate Automated QA Inspector (0:70-0:84)

**Screen:** Video Metadata QA Inspector Modal.

**Voiceover:**

> "Every production must pass twenty automated quality gates before release. Operators can inspect the verification checklist confirming Output QA, Visual Evidence QA, audio headroom, and phonetic lip-sync alignment."

**Action:** Open the Metadata Modal, hover over the verification check pills showing green checkmarks across deterministic Output QA, Visual Evidence QA, and audio/lip-sync constraints.

## Beat 5 - ClickHouse Cloud Telemetry & MCP Data Officer (0:84-1:15)

**Screen:** Telemetry dashboard at `http://localhost:3001/telemetry`.

**Voiceover:**

> "Finally, every production run dual-writes sanitized telemetry to ClickHouse Cloud. In our Telemetry dashboard, operators audit render duration, tokens, and cost, while querying live warehouse data through our Google ADK Data Officer via official ClickHouse MCP."

**Action:** Display ClickHouse Cloud telemetry ledger, execute preset warehouse query, and highlight live Q&A with the Data Officer agent.

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
