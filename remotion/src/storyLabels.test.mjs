import test from "node:test";
import assert from "node:assert/strict";
import {nonDuplicateStoryLabels} from "./storyLabels.ts";

test("removes headline text already visible as an evidence node", () => {
  assert.deepEqual(
    nonDuplicateStoryLabels(
      ["လူသားကြီးကြပ်မှု", "အကျိုးကျေးဇူးနှင့် ဘေးကင်းမှု"],
      ["လူသားကြီးကြပ်မှု", "AI အကျိုးကျေးဇူး", "ပြဿနာရှောင်ရှား"],
    ),
    ["အကျိုးကျေးဇူးနှင့် ဘေးကင်းမှု"],
  );
});

test("returns an empty label when every headline duplicates evidence", () => {
  assert.deepEqual(nonDuplicateStoryLabels(["AI"], ["AI"]), [""]);
});

test("removes a headline that repeats an evidence label as a phrase", () => {
  assert.deepEqual(
    nonDuplicateStoryLabels(["လူသား စစ်ဆေးမှု လုပ်ငန်းစဉ်"], ["လူသား စစ်ဆေးမှု"]),
    [""],
  );
});
