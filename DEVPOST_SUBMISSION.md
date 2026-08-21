# FYF Agentic Cinema — Devpost Submission Draft

**Track:** Replit Partner Track
**Product:** FYF Video Pipeline — an evidence-led Burmese video production agent

> This is a factual submission draft, not a claim that the Replit track is
> complete. The Replit deployment URL remains a release gate until the free
> quota resets and a public `replit.app` or `replit.dev` URL is verified.

## Project summary

FYF turns a Burmese topic or draft into a reviewable vertical video. The agent
keeps factual claims, visual evidence, narration, render checkpoints, and final
quality gates explicit so a human can inspect the result before sharing it.

## What the working product does

1. Accepts a topic or draft in the Create Studio.
2. Uses Gemini on Google Cloud Vertex AI to produce a structured Burmese script
   and immutable story lock.
3. Plans and verifies visual evidence for each scene, with deterministic
   fallbacks when a transient provider response cannot be trusted.
4. Generates Burmese narration with Gemini TTS in hackathon mode.
5. Renders a 1080x1920 vertical MP4 with Remotion and runs deterministic,
   creative, and final rendered-meaning QA.
6. Shows approved results in Library and exposes a privacy-safe in-app
   telemetry ledger for calls, retries, tokens, TTS, latency, and cost
   confidence.

## Google Cloud / Gemini runtime evidence

- Python runtime uses the `google-genai` SDK and Vertex/Gemini configuration.
- Hackathon mode routes text/storyboard work through Gemini 3.7 Flash and
  narration through Gemini TTS.
- The local verified job ledger records real provider calls and QA results;
  prompts, response text, and credentials are excluded from telemetry.
- The repository includes the locked dependency graph, setup instructions,
  backend tests, frontend checks, and Remotion checks.

## Replit partner-track evidence

- Replit Agent was used during development of this project.
- `.replit`, `replit.nix`, and `run_replit.sh` define the deployment boundary.
- `run_replit.sh` uses the locked Python 3.11 environment, builds/starts the
  Next.js production server, and routes same-origin `/api/*` and `/health`
  requests to FastAPI.
- **Hosted URL:** pending quota reset and a successful public deployment.
- Do not submit this draft until the hosted URL loads the Create Studio and
  the deployment is verified from a clean browser session.

## Partner-track scope decision

The selected partner track is **Replit only**. The local `/telemetry` page is an
operator feature; it is not being claimed as a second partner track.

## Demo and source gates

- **Demo video:** use the public English or English-subtitled walkthrough,
  no longer than three minutes. `[PENDING: public YouTube/Vimeo URL]`
- **Source repository:** public Git repository with an open-source license,
  source, assets, and run instructions. `[PENDING: final exact repository URL]`
- **Hosted project URL:** `[PENDING: verified replit.app or replit.dev URL]`
- **Submission:** choose Replit as the single partner track.

## Verification snapshot

- Backend regression suite: 320 tests passed.
- Voice adapter/audio QA suite: 23 tests passed.
- Remotion suite: 27 tests passed; Remotion TypeScript check passed.
- Frontend ESLint, TypeScript, and production build passed.
- Local production entrypoint passed `/`, `/health`, `/api/runtime`, and
  `/api/telemetry` checks.
- In-app browser verified Create, Library, Telemetry, and a completed-job
  telemetry selection with zero recorded retries/failures.

## Post-hackathon boundary

After judging, the local/private product can run in product mode with the
Replit deployment boundary and hackathon-only partner documentation removed
or disabled. That change is deliberately separate from this submission draft.
