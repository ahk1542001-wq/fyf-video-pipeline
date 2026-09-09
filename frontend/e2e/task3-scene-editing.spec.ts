import { test, expect, type APIRequestContext } from "@playwright/test";

function script() {
  return {
    title: "Scene contract E2E",
    language: "my-MM",
    segments: [
      {
        id: "s1",
        text: "one",
        visual_action: "intro",
        scene_type: "whiteboard",
        mascot_action: "present",
        emotion: "neutral",
        emphasis: [],
        caption: "caption one",
        voice: "warm voice",
        duration_seconds: 3,
      },
      {
        id: "s2",
        text: "two",
        visual_action: "walkthrough",
        scene_type: "demo",
        mascot_action: "explain",
        emotion: "warm",
        emphasis: [],
        caption: "caption two",
        voice: "clear voice",
        duration_seconds: 4,
      },
    ],
  };
}

async function createProject(request: APIRequestContext): Promise<string> {
  const response = await request.post("/api/projects", {
    data: { script: script(), actor: "scene-contract-e2e" },
  });
  expect(response.status()).toBe(201);
  return ((await response.json()) as { project_id: string }).project_id;
}

test.describe("scene contracts and exact-head controls", () => {
  test("edits scene fields, locks one scene, plans regeneration, and compares every field", async ({
    page,
    request,
  }) => {
    const projectId = await createProject(request);
    await page.goto(`/project/${projectId}`);
    await expect(page.getByTestId("studio-status")).toHaveText("ready");
    await expect(page.getByTestId("head-version")).toHaveText("v1");

    await page.getByTestId("scene-select-s1").click();
    await expect(page.getByTestId("scene-caption-input")).toHaveValue("caption one");
    await expect(page.getByTestId("scene-voice-input")).toHaveValue("warm voice");
    await expect(page.getByTestId("scene-duration-input")).toHaveValue("3");

    await page.getByTestId("scene-caption-input").fill("updated caption");
    await page.getByTestId("scene-apply-caption").click();
    await expect(page.getByTestId("head-version")).toHaveText("v2");
    await expect(page.getByTestId("scene-caption-s1")).toHaveText("updated caption");

    await page.getByTestId("scene-voice-input").fill("updated voice");
    await page.getByTestId("scene-apply-voice").click();
    await expect(page.getByTestId("head-version")).toHaveText("v3");
    await expect(page.getByTestId("scene-voice-s1")).toHaveText("updated voice");

    await page.getByTestId("scene-duration-input").fill("4.5");
    await page.getByTestId("scene-apply-duration").click();
    await expect(page.getByTestId("head-version")).toHaveText("v4");
    await expect(page.getByTestId("scene-duration-s1")).toHaveText("4.5s");

    await page.getByTestId("scene-lock-toggle-content").click();
    await expect(page.getByTestId("scene-lock-toggle-content")).toBeChecked({ timeout: 15_000 });
    await page.getByTestId("scene-caption-input").fill("must be blocked");
    await page.getByTestId("scene-apply-caption").click();
    await expect(page.getByTestId("studio-notice")).toHaveAttribute("data-kind", "locked");
    await expect(page.getByTestId("head-version")).toHaveText("v4");

    await page.getByTestId("scene-lock-toggle-content").click();
    await expect(page.getByTestId("scene-lock-toggle-content")).not.toBeChecked({ timeout: 15_000 });
    await page.getByTestId("scene-regenerate").click();
    await expect(page.getByTestId("studio-notice-message")).toContainText("exact head v4");
    await expect(page.getByTestId("head-version")).toHaveText("v4");

    await page.getByRole("button", { name: "Story", exact: true }).click();
    await page.getByTestId("compare-before-select").selectOption("1");
    await page.getByTestId("compare-after-select").selectOption("4");
    await page.getByTestId("compare-toggle").click();
    await expect(page.getByTestId("compare-before-version")).toHaveText("v1");
    await expect(page.getByTestId("compare-after-version")).toHaveText("v4");
    await expect(page.getByTestId("before-s1-caption")).toHaveText("caption one");
    await expect(page.getByTestId("after-s1-caption")).toHaveText("updated caption");
    await expect(page.getByTestId("before-s1-voice")).toHaveText("warm voice");
    await expect(page.getByTestId("after-s1-voice")).toHaveText("updated voice");
    await expect(page.getByTestId("before-s1-duration")).toHaveText("3s");
    await expect(page.getByTestId("after-s1-duration")).toHaveText("4.5s");
  });
});
