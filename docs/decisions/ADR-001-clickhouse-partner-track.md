# ADR-001: Enter the Agentic Cinema hackathon via the ClickHouse partner track

## Status
Accepted

## Date
2026-08-24

## Context
The Google Cloud Agentic Cinema hackathon requires every submission to enter exactly one partner track: IBM, Grafana Labs, Parallel, ClickHouse, or Replit. The track choice determines the mandatory runtime integration that judges verify (imported and actually called in code), and each track is judged separately.

Our product is an evidence-led Burmese video production pipeline built on Google ADK, Vertex AI (Gemini 3.7 Flash), Gemini TTS, and Remotion. Any chosen integration must not destabilize a working generation pipeline before the September 9 deadline.

## Decision
Enter the **ClickHouse track**.

## Alternatives Considered

### Replit
- Pros: Existing deployment already ran there; prior familiarity.
- Cons: Track requires development via Replit Agent and permanent hosting on replit.app. Production experience showed Vertex AI rate-limits Replit's shared egress IPs (429 RESOURCE_EXHAUSTED while identical calls from a residential IP succeeded in 3.6s), and CloudRun containers failed to launch Chromium for rendering.
- Rejected: Conflicts with the hosting exit decision (ADR-002) and the platform's demonstrated instability for this workload.

### IBM
- Pros: Largest prize visibility.
- Cons: Requires adopting IBM Bob as part of the development process; no existing IBM usage anywhere in the stack.
- Rejected: Late-stage adoption of an unfamiliar toolchain with zero synergy.

### Grafana Labs
- Pros: Official ADK integration example exists; free cloud tier.
- Cons: Hosted Grafana MCP server authenticates via interactive OAuth 2.1 with no service-account option, forcing a self-hosted OSS MCP server for unattended deployments; adds a second always-on system to operate.
- Rejected: Operational overhead outweighed benefit once ClickHouse was viable; monitoring-style story overlaps what our own telemetry UI already provides.

### Parallel
- Pros: Search-grounding integrates naturally into the fact/research stage; official Gemini grounding guide exists.
- Cons: Unknown credit/cost structure; changes factual pipeline behavior late in the cycle, forcing QA re-runs.
- Rejected as primary track; revisit post-hackathon as a product enhancement.

## Consequences
- ClickHouse Cloud service provisioned on GCP asia-southeast1 under trial credits; runtime access via official `mcp-clickhouse` MCP server.
- Telemetry store dual-writes sanitized job metrics so warehouse data accumulates from real production runs.
- The FYF Data Officer agent (ADK + MCPToolset) exposes warehouse Q&A at `POST /api/insights`, satisfying the "imported and actually called" rule with a user-facing feature rather than dead code.
