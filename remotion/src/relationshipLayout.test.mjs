import test from "node:test";
import assert from "node:assert/strict";
import {relationshipPresentation, visibleMotionValues} from "./relationshipLayout.ts";

test("relationship presentation keeps every relation value visible without a workflow arrow", () => {
  const result = relationshipPresentation({
    labels: ["AI", "လူသား"],
    values: ["ဉာဏ်ပညာ", "ကျင့်ဝတ်", "ဆင်ခြင်တုံတရား", "အစားထိုး၍မရပါ"],
    relation_mode: "non_replacement",
  });

  assert.deepEqual(result.nodes, ["AI", "လူသား"]);
  assert.deepEqual(result.relations, ["ဉာဏ်ပညာ", "ကျင့်ဝတ်", "ဆင်ခြင်တုံတရား", "အစားထိုး၍မရပါ"]);
  assert.equal(result.connector, "↮");
});

test("legacy and positive relationships stay directional", () => {
  assert.equal(relationshipPresentation({labels: ["အကြောင်း", "အကျိုး"], values: [], relation_mode: "directional"}).connector, "→");
  assert.equal(relationshipPresentation({labels: ["အကြောင်း", "အကျိုး"], values: []}).connector, "→");
});

test("bidirectional relationships use a two-way connector", () => {
  assert.equal(relationshipPresentation({labels: ["လူ", "AI"], values: [], relation_mode: "bidirectional"}).connector, "↔");
});

test("multi-step relationships retain every locked node in visible order", () => {
  const result = relationshipPresentation({
    labels: ["အကြောင်းရင်း", "အမှား", "ထိခိုက်မှု"],
    values: [],
    relation_mode: "directional",
  });
  assert.deepEqual(result.nodes, ["အကြောင်းရင်း", "အမှား", "ထိခိုက်မှု"]);
});

test("removes duplicate and unapproved English value pills", () => {
  assert.deepEqual(
    visibleMotionValues(
      ["ဘက်လိုက်သော AI", "မမျှတသော ရလဒ်များ"],
      ["ဘက်လိုက်သော AI", "မမျှတသော ရလဒ်များ", "Freedom & Future", "AI"],
    ),
    ["", "", "", "AI"],
  );
});
