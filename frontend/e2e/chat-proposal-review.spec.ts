import { test, expect, type APIRequestContext } from "@playwright/test";

function script() {
  return {
    title: "Chat proposal review",
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
      },
      {
        id: "s2",
        text: "two",
        visual_action: "walkthrough",
        scene_type: "demo",
        mascot_action: "explain",
        emotion: "warm",
        emphasis: [],
      },
    ],
  };
}

async function createProject(request: APIRequestContext): Promise<string> {
  const response = await request.post("/api/projects", {
    data: { script: script(), actor: "proposal-e2e" },
  });
  expect(response.status()).toBe(201);
  return ((await response.json()) as { project_id: string }).project_id;
}

test.describe("Studio chat proposal review", () => {
  test("keeps a chat change pending until Approve and supports Reject", async ({
    page,
    request,
  }) => {
    const projectId = await createProject(request);
    await page.goto(`/project/${projectId}`);
    await expect(page.getByTestId("studio-status")).toHaveText("ready");
    await expect(page.getByTestId("head-version")).toHaveText("v1");

    await page.getByTestId("scene-select-s1").click();
    await page.getByTestId("chat-input").fill('rewrite the narration as "reviewed"');
    await page.getByTestId("chat-send").click();

    const card = page.getByTestId("chat-proposal-card").first();
    await expect(card).toBeVisible();
    await expect(card).toContainText("s1.text");
    await expect(card).toContainText("one");
    await expect(card).toContainText("reviewed");
    await expect(page.getByTestId("head-version")).toHaveText("v1");

    await card.getByTestId("proposal-reject").click();
    await expect(card).toContainText("rejected");
    await expect(page.getByTestId("head-version")).toHaveText("v1");

    await page.getByTestId("chat-input").fill('rewrite the narration as "approved"');
    await page.getByTestId("chat-send").click();
    const approvedCard = page.getByTestId("chat-proposal-card").first();
    await expect(approvedCard).toBeVisible();
    await approvedCard.getByTestId("proposal-approve").click();
    await expect(page.getByTestId("head-version")).toHaveText("v2");
    await expect(page.getByTestId("scene-text-s1")).toHaveText("approved");
  });
});
