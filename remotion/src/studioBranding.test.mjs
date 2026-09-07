import test from "node:test";
import assert from "node:assert/strict";
import { studioBranding } from "./studioBranding.ts";

test("preserves the legacy FYF branding when no custom studio is selected", () => {
  assert.deepEqual(studioBranding(), {
    name: "FYF",
    tagline: "Understand AI. Build Real Systems.",
  });
  assert.deepEqual(studioBranding("FYF Studio"), {
    name: "FYF Studio",
    tagline: "Understand AI. Build Real Systems.",
  });
});

test("uses the selected studio branding in the production composition", () => {
  assert.deepEqual(studioBranding("Cinema Lab"), {
    name: "Cinema Lab",
    tagline: "Cinema Lab • Agentic Cinema",
  });
});
