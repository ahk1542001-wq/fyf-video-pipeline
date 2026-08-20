import test from "node:test";
import assert from "node:assert/strict";
import {
  CANONICAL_TREATMENTS,
  routeTreatment,
  resolveTreatment,
  resolveVisualGrammar,
  comparisonTransformGroups,
  objectActionItems,
  selectActiveTreatment,
  shouldShowOverlayLabel,
  visibleTreatmentLabels,
  treatmentEvidenceLabels,
  treatmentLabelFontSize,
  visibleDataValue,
} from "./treatmentRouting.ts";

const canonicalTreatments = [
  "story_scene",
  "object_action",
  "ui_proof",
  "editorial_data",
  "comparison_transform",
  "motion_diagram",
  "kinetic_type",
  "mascot_performance",
];

test("defines all eight canonical treatment values", () => {
  assert.deepEqual(CANONICAL_TREATMENTS, canonicalTreatments);
});

test("shows overlay labels only for legacy scenes and caption-mode treatments", () => {
  assert.equal(shouldShowOverlayLabel(), true);
  assert.equal(shouldShowOverlayLabel(null), true);
  assert.equal(shouldShowOverlayLabel({treatment_type: "story_scene", text_mode: "caption"}), true);
  assert.equal(shouldShowOverlayLabel({treatment_type: "story_scene", text_mode: "label"}), false);
  assert.equal(shouldShowOverlayLabel({treatment_type: "kinetic_type", text_mode: "caption"}), true);
  assert.equal(shouldShowOverlayLabel({treatment_type: "mascot_performance", text_mode: "caption"}), true);
  assert.equal(shouldShowOverlayLabel({treatment_type: "ui_proof", text_mode: "caption"}), true);
  assert.equal(shouldShowOverlayLabel({treatment_type: "ui_proof", text_mode: "label"}), false);
});

test("routes each treatment to its treatment-aware visual grammar", () => {
  const routed = canonicalTreatments.map((treatment) => routeTreatment({treatment}));

  assert.deepEqual(routed.map((result) => result.treatment), canonicalTreatments);
  assert.equal(new Set(routed.map((result) => result.grammar)).size, canonicalTreatments.length);
});

test("uses the legacy renderer when treatment metadata is absent", () => {
  assert.deepEqual(resolveTreatment({}), {treatment: null, grammar: "legacy"});
  assert.deepEqual(resolveTreatment({treatment: null}), {treatment: null, grammar: "legacy"});
});

test("non-diagram treatments never alias to centered-card or motion_diagram", () => {
  for (const treatment of canonicalTreatments.filter((value) => value !== "motion_diagram")) {
    const result = routeTreatment({treatment});
    assert.notEqual(result.grammar, "centered-card");
    assert.notEqual(result.grammar, "motion_diagram");
  }
});

test("motion graphics render through their planned non-diagram treatment grammar", () => {
  assert.equal(resolveVisualGrammar({
    media_type: "motion_graphic",
    motion_spec: {layout: "relationship"},
    treatment: {treatment_type: "ui_proof"},
  }), "interface-proof");
  assert.equal(resolveVisualGrammar({
    media_type: "motion_graphic",
    motion_spec: {layout: "sequence"},
    treatment: {treatment_type: "object_action"},
  }), "observable-object-action");
});

test("unplanned motion graphics and explicit diagrams use the diagram grammar", () => {
  assert.equal(resolveVisualGrammar({
    media_type: "motion_graphic",
    motion_spec: {layout: "relationship"},
  }), "motion-diagram");
  assert.equal(resolveVisualGrammar({
    media_type: "motion_graphic",
    motion_spec: {layout: "relationship"},
    treatment: {treatment_type: "motion_diagram"},
  }), "motion-diagram");
});

test("preserves Burmese labels byte-for-byte through routing", () => {
  const labels = ["လူသားကြီးကြပ်မှု", "အကျိုးကျေးဇူးနှင့် ဘေးကင်းမှု"];
  const result = routeTreatment({
    treatment: "editorial_data",
    labels,
  });

  assert.deepEqual(result.labels, labels);
  assert.equal(Buffer.from(result.labels.join("\u0000"), "utf8").toString("hex"), Buffer.from(labels.join("\u0000"), "utf8").toString("hex"));
});

test("multi-party comparison keeps the focal label and every verified branch", () => {
  assert.deepEqual(comparisonTransformGroups({
    labels: ["AI ဆုံးဖြတ်ချက်", "ဖန်တီးသူ", "အသုံးပြုသူ", "AI ကိုယ်တိုင်"],
    values: ["တာဝန် မသေချာ", "?", "?", "?"],
    layout: "directional_branch",
  }), {
    focalLabel: "AI ဆုံးဖြတ်ချက်",
    items: [
      {label: "ဖန်တီးသူ", value: "?"},
      {label: "အသုံးပြုသူ", value: "?"},
      {label: "AI ကိုယ်တိုင်", value: "?"},
    ],
  });
});

test("multi-party comparison keeps every verified item regardless of planner layout", () => {
  assert.deepEqual(comparisonTransformGroups({
    labels: ["AI အမှား", "ဖန်တီးသူ", "အသုံးပြုသူ", "AI ကိုယ်တိုင်"],
    values: ["တာဝန်ခံရမည့်သူ", "မသေချာပါ", "မသေချာပါ", "မသေချာပါ"],
    layout: "sequence",
  }), {
    focalLabel: "AI အမှား",
    items: [
      {label: "ဖန်တီးသူ", value: "မသေချာပါ"},
      {label: "အသုံးပြုသူ", value: "မသေချာပါ"},
      {label: "AI ကိုယ်တိုင်", value: "မသေချာပါ"},
    ],
  });
});

test("object-action sequence keeps every verified step", () => {
  assert.deepEqual(objectActionItems([
    "မှားယွင်းသော ဒေတာ",
    "AI စနစ်",
    "မှားယွင်းသော ဆုံးဖြတ်ချက်",
  ]), [
    "မှားယွင်းသော ဒေတာ",
    "AI စနစ်",
    "မှားယွင်းသော ဆုံးဖြတ်ချက်",
  ]);
});

test("never exposes director metadata as production-visible text", () => {
  const treatment = {
    treatment_type: "object_action",
    focal_object: "ENGLISH FOCAL OBJECT",
    action: "ENGLISH ACTION",
    change: "ENGLISH CHANGE",
    director_reason: "ENGLISH INTERNAL REASON",
  };

  assert.deepEqual(visibleTreatmentLabels(treatment, ["လူသားစစ်ဆေးမှု", "ယုံကြည်မှု"]), [
    "လူသားစစ်ဆေးမှု",
    "ယုံကြည်မှု",
  ]);
  assert.deepEqual(visibleTreatmentLabels(treatment, []), []);
});

test("every treatment grammar receives only verified production labels", () => {
  const verified = ["လက်တွေ့အခြေအနေ", "AI အကြံပြုချက်", "လူသားအတည်ပြုချက်"];
  for (const treatment_type of canonicalTreatments) {
    assert.deepEqual(treatmentEvidenceLabels({
      treatment: {
        treatment_type,
        focal_object: "DO NOT SHOW",
        action: "DO NOT SHOW",
        change: "DO NOT SHOW",
        director_reason: "DO NOT SHOW",
      },
      motion_spec: {labels: verified},
    }), verified);
  }
});

test("shrinks long Burmese evidence labels instead of overflowing their object", () => {
  assert.equal(treatmentLabelFontSize("AI စနစ်", 36), 36);
  assert.ok(treatmentLabelFontSize("အက်ပ်လိုက် သော သုံးမဟုတ် မှားယွင်းသော လေ့ကျင့်ရေး အချက်အလက်", 36) <= 24);
});

test("editorial metric column only exposes compact numeric evidence", () => {
  assert.equal(visibleDataValue("12"), "12");
  assert.equal(visibleDataValue("၂၅%"), "၂၅%");
  assert.equal(visibleDataValue("လူသားစစ်ဆေးမှုနှင့် အတည်ပြုချက်"), "");
});

test("selects the active planned treatment by hold_fraction boundaries", () => {
  const shots = [
    {shot_id: "intro", hold_fraction: 0.25, treatment: {treatment_type: "story_scene"}},
    {shot_id: "mascot", hold_fraction: 0.5, treatment: {treatment_type: "mascot_performance"}},
    {shot_id: "proof", hold_fraction: 0.25, treatment: {treatment_type: "ui_proof"}},
  ];

  assert.equal(selectActiveTreatment(shots, 0, 100), shots[0].treatment);
  assert.equal(selectActiveTreatment(shots, 24, 100), shots[0].treatment);
  assert.equal(selectActiveTreatment(shots, 25, 100), shots[1].treatment);
  assert.equal(selectActiveTreatment(shots, 74, 100), shots[1].treatment);
  assert.equal(selectActiveTreatment(shots, 75, 100), shots[2].treatment);
});

test("returns null when there are no planned shots", () => {
  assert.equal(selectActiveTreatment([], 0, 100), null);
});

test("selects mascot_performance only during its own shot, not StoryLabel beat index", () => {
  const shots = [
    {shot_id: "context", hold_fraction: 0.2, treatment: {treatment_type: "story_scene"}},
    {shot_id: "mascot", hold_fraction: 0.3, treatment: {treatment_type: "mascot_performance"}},
    {shot_id: "context-2", hold_fraction: 0.5, treatment: {treatment_type: "story_scene"}},
  ];

  assert.notEqual(selectActiveTreatment(shots, 10, 100)?.treatment_type, "mascot_performance");
  assert.equal(selectActiveTreatment(shots, 20, 100)?.treatment_type, "mascot_performance");
  assert.notEqual(selectActiveTreatment(shots, 50, 100)?.treatment_type, "mascot_performance");
});

test("rejects an unknown treatment when selecting a planned shot", () => {
  assert.throws(
    () => selectActiveTreatment([{hold_fraction: 1, treatment: {treatment_type: "not_a_treatment"}}], 0, 100),
    /Unknown treatment: not_a_treatment/,
  );
});
