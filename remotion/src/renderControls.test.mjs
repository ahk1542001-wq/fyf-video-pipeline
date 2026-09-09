import test from "node:test";
import assert from "node:assert/strict";
import { aspectRatioDimensions, normalizeRenderControls, resolveReducedMotion } from "./renderControls.ts";

test("maps every supported aspect ratio to its canonical Remotion dimensions", () => {
  assert.deepEqual(aspectRatioDimensions("9:16"), { width: 1080, height: 1920 });
  assert.deepEqual(aspectRatioDimensions("16:9"), { width: 1920, height: 1080 });
  assert.deepEqual(aspectRatioDimensions("1:1"), { width: 1080, height: 1080 });
});

test("normalizes legacy controls without changing Burmese defaults", () => {
  assert.deepEqual(normalizeRenderControls({}), {
    cta_text: "",
    retention_progress_bar: true,
    animated_lower_thirds: true,
    aspect_ratio: "9:16",
  });
});

test("rejects invalid controls before Remotion receives props", () => {
  assert.throws(() => normalizeRenderControls({ cta_text: "  " }), /cta_text/);
  assert.throws(() => normalizeRenderControls({ cta_text: "x".repeat(81) }), /cta_text/);
  assert.throws(() => normalizeRenderControls({ aspect_ratio: "4:3" }), /aspect_ratio/);
  assert.throws(() => normalizeRenderControls({ render_controls: "invalid" }), /render_controls/);
  assert.throws(
    () => normalizeRenderControls({ cta_text: "outer", render_controls: { cta_text: "inner" } }),
    /does not match render_controls/,
  );
});

test("reduced motion is strict and suppresses lower-third animation", () => {
  assert.equal(resolveReducedMotion({reduced_motion: true}), true);
  assert.equal(resolveReducedMotion({}), false);
  assert.throws(() => resolveReducedMotion({reduced_motion: "yes"}), /reduced_motion/);
  assert.equal(
    normalizeRenderControls({reduced_motion: true, animated_lower_thirds: true}).animated_lower_thirds,
    false,
  );
});
