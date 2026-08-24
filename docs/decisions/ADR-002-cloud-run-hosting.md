# ADR-002: Migrate hosting from Replit to Google Cloud Run

## Status
Accepted

## Date
2026-08-24

## Context
The pipeline was initially hosted on Replit Autoscale (2 vCPU / 4 GiB). Production smoke testing surfaced two independent platform-level failures:

1. **Vertex AI throttling of shared egress IPs** - identical Gemini calls succeeded from a residential IP in 3.6s while receiving `429 RESOURCE_EXHAUSTED` from the deployment, repeatedly across multiple hours and attempts.
2. **Chromium could not launch inside CloudRun containers** - Remotion's browser failed with `Timed out after 25000 ms while trying to connect to the browser` and Remotion's `Detected differing memory amounts` guard (cgroup 3207 MB vs node 2534 MB). Segmented rendering reduced per-process memory but still required a working browser.

Additionally, every republish wipes runtime state (locks, jobs, checkpoints), which made multi-stage generation chains fragile during iteration.

## Decision
Host on **Google Cloud Run** (gen2 execution environment) using a single container that runs FastAPI (internal :8000) behind a Next.js standalone server (:8080) which rewrites same-origin `/api/*`. Build via Cloud Build with `scripts/deploy_cloudrun.sh`; secrets served through Secret Manager; ClickHouse access credentials injected as secret-backed env vars.

## Alternatives Considered

### Stay on Replit
- Pros: Already configured; one-click publishes.
- Cons: IP-based provider throttling and browser launch failures are platform properties we cannot change; runtime state loss on every publish.
- Rejected: Directly caused repeated production failures documented in monitoring logs.

### Local-only serving (Mac + tunnel)
- Pros: Everything already works locally; zero marginal cost; Vertex responds in ~4s.
- Cons: Requires the development machine to stay awake for judging; not a credible production story for judges.
- Deferred: Remains the fallback demo path if cloud issues arise, and the post-hackathon business mode.

### Other PaaS (Railway/Fly/Render)
- Pros: Similar convenience to Replit with different IP pools.
- Cons: Same class of shared-egress risk; another vendor to learn for no architectural gain.
- Rejected: Google-to-Google traffic avoids third-party egress throttling entirely.

### VPS (Hetzner/DO)
- Pros: Dedicated IP, full control.
- Cons: Manual DevOps (hardening, TLS, restarts) with no time budget before the deadline.
- Rejected for now; revisit if long-term hosting costs matter after judging.

## Consequences
- Segmented rendering plus a pre-downloaded headless browser (`remotion browser ensure` at build time) are baked into the image; renders run with per-segment checkpoints.
- Scale-to-zero keeps idle cost near $0; the entire project can be deleted after judging with no lingering infrastructure.
- The local-first workflow remains fully functional and is the canonical development mode; the container is a packaging of that same stack.
