import test from "node:test";
import assert from "node:assert/strict";
import {existsSync, readFileSync} from "node:fs";

const rootSource = readFileSync(new URL("./Root.tsx", import.meta.url), "utf8");
const fullSource = readFileSync(new URL("./VisualSystemV3Full.tsx", import.meta.url), "utf8");
const retiredPreview = new URL("./VisualSystemV3Preview.tsx", import.meta.url);
const typeModule = await import("./types.ts");

const realRenderInput = {
  title: "Injected production input",
  language: "my-MM",
  fps: 30,
  durationInFrames: 30,
  audioSrc: "voice.wav",
  segments: [
    {
      startFrame: 0,
      endFrame: 30,
      id: "scene-1",
      text: "Injected narration",
      scene_type: "whiteboard",
      mascot_action: "present",
      emotion: "neutral",
      emphasis: [],
    },
  ],
  mouthCues: [],
};

test("production compositions have no sample/default props", () => {
  assert.doesNotMatch(rootSource, /\bsampleInput\b/);
  assert.doesNotMatch(rootSource, /\bdefaultProps\s*=/);
  assert.match(rootSource, /requireExplicitRenderInput/);
  assert.match(fullSource, /requireExplicitRenderInput\(props\)/);
});

test("the retired hardcoded demo preview is absent from source and Root", () => {
  assert.doesNotMatch(rootSource, /VisualSystemV3Preview/);
  assert.equal(existsSync(retiredPreview), false);
});

test("explicit render input validation rejects missing props and accepts real input", () => {
  assert.equal(typeof typeModule.requireExplicitRenderInput, "function");
  assert.throws(
    () => typeModule.requireExplicitRenderInput(undefined),
    /requires explicit injected real render input/i,
  );
  assert.throws(
    () => typeModule.requireExplicitRenderInput({}),
    /requires explicit injected real render input/i,
  );
  assert.deepEqual(typeModule.requireExplicitRenderInput(realRenderInput), realRenderInput);
});
