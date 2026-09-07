import test from "node:test";
import assert from "node:assert/strict";
import { presenterAllowsMascot } from "./presenterMode.ts";

test("voiceover-only presenter mode never renders a mascot", () => {
  assert.equal(presenterAllowsMascot("voiceover_only"), false);
  assert.equal(presenterAllowsMascot("on_screen"), true);
  assert.equal(presenterAllowsMascot(undefined), true);
});
