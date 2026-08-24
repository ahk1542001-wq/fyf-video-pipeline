# ADR-003: Per-segment lock metadata via forced function calling

- **Status:** Accepted (2026-08-24)
- **Context stage:** Hackathon production hardening on Cloud Run
- **Supersedes:** the responseJsonSchema variant of the per-segment lock path

## Context

The exact-lock stage turns an approved narration into strict, schema-valid
visual metadata. To keep Vertex latency bounded for long scripts we split the
call per segment (FYF_LOCK_METADATA_MODE=per_segment), sending one request
per segment with the CompactVisualPlanSegment schema.

Production runs on Cloud Run then failed in two different ways:

1. google.genai ServerError 504 DEADLINE_EXCEEDED after hanging for the full
   server deadline (~165s), locally and on Cloud Run.
2. Terminal contract-validation failures on CompactVisualPlanSegment.

Isolated probes with a tiny prompt and the same schema succeeded, so neither
prompt size nor the schema alone explained it.

## Investigation

We captured the exact failing request (system instruction 2538 chars,
segment schema 5610 chars with $defs, thinking MEDIUM) and replayed it while
varying one variable at a time. No single variable reproduced the failure:
the identical request passed minutes after it had failed.

An alternating burst comparison (same payload, three rounds back to back)
produced the decisive pattern:

| Round | responseJsonSchema | ToolConfig ANY forced call |
| ----- | ------------------ | -------------------------- |
| 1 | OK 13.3s | OK 49.0s |
| 2 | FAIL 429 RESOURCE_EXHAUSTED | OK 94.9s |
| 3 | FAIL 504 after 177s hang | OK 15.0s |

Vertex's constrained-decoding endpoint degraded under burst load from this
project, while forced function calling carrying the same schema as tool
parameters stayed healthy.

## Decision

- The per-segment lock path emits metadata through a required
  emit_lock_metadata function call: tools=[...] plus ToolConfig with
  FunctionCallingConfigMode.ANY and allowed_function_names=["emit_lock_metadata"].
- The Pydantic schema is dereferenced ($defs/$ref inlined) because
  FunctionDeclaration accepts only the OpenAPI subset.
- Response handling prefers the function-call args dict and falls back to
  parsing JSON text, so both response shapes stay valid.
- The per-segment branch now also populates metadata_by_id (a latent crash:
  every successful per-segment run died at the merge assert) and applies the
  same guards as the combined path: segment ID set equality and canonical
  Fact Agent claims reconciliation.
- Thinking level stays MEDIUM; combined mode stays unchanged for tests and
  small scripts.

## Consequences

- Bursty multi-segment productions no longer trip the unstable
  constrained-decoding path.
- Failures now surface as real errors (empty responses raise instead of
  returning silently), consistent with the fail-fast retry policy.
- If Vertex stabilizes responseJsonSchema, the combined path can migrate
  later; no action needed before the hackathon deadline.

## Evidence

- Burst log: /tmp/burst_out.txt (session of 2026-08-24, asia-southeast1 + global)
- Isolation matrix: /tmp/matrix_out.txt
- Commit af7952b "fix(vertex): per-segment lock emits via forced tool call"
