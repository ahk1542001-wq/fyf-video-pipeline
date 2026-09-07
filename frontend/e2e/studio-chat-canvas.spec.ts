import { test, expect, type Page, type APIRequestContext } from '@playwright/test';

// ---------------------------------------------------------------------------
// Stage C exit gate - REAL browser persistence/editing pass.
//
// This spec runs the real Next.js frontend against the real FastAPI backend and
// the real FileProjectStore (temp FYF_PROJECTS_ROOT set by run_browser_e2e.py).
// There is NO route.fulfill mocking of the project/command APIs anywhere in this
// file: every create / command / chat / undo / variant / lock call is a real HTTP
// round-trip that really persists to disk. Provider generation stays stubbed via
// FYF_RUNTIME_MODE=hackathon and no render is ever dispatched, so paid cost is $0.
//
// The existing 17 mocked specs remain as honestly-labelled UI-regression tests;
// this NEW spec proves the persistence/editing semantics end to end.
// ---------------------------------------------------------------------------

const ORIGINAL_S1 = 'မူလ ပထမ စာတန်း';
const ORIGINAL_S2 = 'မူလ ဒုတိယ စာတန်း';

function baseScript() {
  return {
    title: 'Studio Persistence E2E',
    language: 'my-MM',
    segments: [
      {
        id: 's1',
        text: ORIGINAL_S1,
        visual_action: 'whiteboard intro',
        scene_type: 'whiteboard',
        mascot_action: 'present',
        emotion: 'neutral',
        emphasis: [],
      },
      {
        id: 's2',
        text: ORIGINAL_S2,
        visual_action: 'demo walkthrough',
        scene_type: 'demo',
        mascot_action: 'explain',
        emotion: 'warm',
        emphasis: [],
      },
    ],
  };
}

async function createProject(request: APIRequestContext): Promise<string> {
  const response = await request.post('/api/projects', {
    data: { script: baseScript(), actor: 'e2e-operator' },
  });
  const text = await response.text();
  expect(response.status(), `create project failed: ${text}`).toBe(201);
  const body = JSON.parse(text) as { success: boolean; project_id: string };
  expect(body.success).toBe(true);
  expect(body.project_id).toMatch(/^[0-9a-f]{8}$/);
  return body.project_id;
}

async function expectReady(page: Page, head: string) {
  await expect(page.getByTestId('studio-status')).toHaveText('ready', { timeout: 20_000 });
  await expect(page.getByTestId('head-version')).toHaveText(head, { timeout: 20_000 });
}

async function canvasEditScene(page: Page, sceneId: string, text: string) {
  await page.getByTestId(`scene-select-${sceneId}`).click();
  const input = page.getByTestId('scene-text-input');
  await input.fill(text);
  await page.getByTestId('scene-apply').click();
}

test.describe('Studio chat + canvas (real backend persistence)', () => {
  test.setTimeout(120_000);

  test('canvas and chat edit ONE canonical version and it persists across reload', async ({
    page,
    request,
  }) => {
    const projectId = await createProject(request);
    await page.goto(`/project/${projectId}`);
    await expectReady(page, 'v1');

    // Burmese narration renders (typography does not regress).
    await expect(page.getByTestId('scene-text-s1')).toContainText('မူလ');

    // 1) EDIT VIA CANVAS -> a real command appends v2.
    await canvasEditScene(page, 's1', 'Canvas edited narration');
    await expectReady(page, 'v2');
    await expect(page.getByTestId('scene-text-s1')).toHaveText('Canvas edited narration');

    // 2) THE SAME CHANGE IS VISIBLE IN CHAT CONTEXT (chat shares the canonical
    //    head; s1 is still selected so the chat context shows the new text).
    await expect(page.getByTestId('chat-context-value')).toContainText('Canvas edited narration');

    // 3) EDIT VIA CHAT (select s2 first so the command scope is s2) -> v3.
    await page.getByTestId('scene-select-s2').click();
    await page.getByTestId('chat-input').fill('rewrite the narration as "Chat edited line"');
    await page.getByTestId('chat-send').click();
    await expectReady(page, 'v3');

    // 4) THE CHAT EDIT IS VISIBLE ON CANVAS.
    await expect(page.getByTestId('scene-text-s2')).toHaveText('Chat edited line');

    // 5) RELOAD -> persistence (real FileProjectStore on disk).
    await page.reload();
    await expectReady(page, 'v3');
    await expect(page.getByTestId('scene-text-s1')).toHaveText('Canvas edited narration');
    await expect(page.getByTestId('scene-text-s2')).toHaveText('Chat edited line');
    await expect(page.getByTestId('version-count')).toHaveText('3');
  });

  test('undo restores prior content as a NEW version and keeps history', async ({
    page,
    request,
  }) => {
    const projectId = await createProject(request);
    await page.goto(`/project/${projectId}`);
    await expectReady(page, 'v1');

    await canvasEditScene(page, 's1', 'Edited then undone');
    await expectReady(page, 'v2');
    await expect(page.getByTestId('scene-text-s1')).toHaveText('Edited then undone');

    // Undo to v1 appends v3 restoring v1 content; nothing is deleted.
    await page.getByTestId('undo-1').click();
    await expectReady(page, 'v3');
    await expect(page.getByTestId('scene-text-s1')).toContainText('မူလ');
    await expect(page.getByTestId('version-count')).toHaveText('3');
    await expect(page.getByTestId('studio-notice-message')).toContainText('Nothing was deleted');
  });

  test('a granular content lock blocks a canvas edit with an honest reason', async ({
    page,
    request,
  }) => {
    const projectId = await createProject(request);
    await page.goto(`/project/${projectId}`);
    await expectReady(page, 'v1');

    // Lock the content scope (persisted in the granular lock registry). The
    // checkbox is server-controlled (checked reflects the persisted registry), so
    // click it and then wait for the REAL round-trip to land rather than asserting
    // an immediate DOM state that React correctly reverts until the lock persists.
    await page.getByTestId('lock-toggle-content').click();
    await expect(page.getByTestId('locked-scopes')).toContainText('content', { timeout: 15_000 });
    await expect(page.getByTestId('lock-toggle-content')).toBeChecked();

    // Attempt a canvas narration edit -> refused by the granular lock_checker.
    await page.getByTestId('scene-select-s1').click();
    await page.getByTestId('scene-text-input').fill('This must be blocked');
    await page.getByTestId('scene-apply').click();

    // Honest refusal: the head does NOT advance and the reason names the scope.
    await expect(page.getByTestId('studio-notice')).toHaveAttribute('data-kind', 'locked', {
      timeout: 15_000,
    });
    await expect(page.getByTestId('studio-notice-message')).toContainText('content');
    await expect(page.getByTestId('head-version')).toHaveText('v1');
    await expect(page.getByTestId('scene-text-s1')).toContainText('မူလ');
  });

  test('a named variant survives reload and before/after renders two versions', async ({
    page,
    request,
  }) => {
    const projectId = await createProject(request);
    await page.goto(`/project/${projectId}`);
    await expectReady(page, 'v1');

    // v2: a real canvas edit so the two compared versions genuinely differ.
    await canvasEditScene(page, 's1', 'Variant narration');
    await expectReady(page, 'v2');

    // v3: promote a NAMED variant to a server-persisted version.
    await page.getByTestId('variant-name-input').fill('Golden Cut');
    await page.getByTestId('variant-create').click();
    await expectReady(page, 'v3');
    await expect(page.getByTestId('variant-name-3')).toHaveText('Golden Cut');

    // Reload -> the named variant is still there (this is what persistent means).
    await page.reload();
    await expectReady(page, 'v3');
    await expect(page.getByTestId('variant-name-3')).toHaveText('Golden Cut');

    // Before/after renders two immutable versions side by side.
    await page.getByTestId('compare-before-select').selectOption('1');
    await page.getByTestId('compare-after-select').selectOption('3');
    await page.getByTestId('compare-toggle').click();
    await expect(page.getByTestId('compare-before-version')).toHaveText('v1', { timeout: 15_000 });
    await expect(page.getByTestId('compare-after-version')).toHaveText('v3', { timeout: 15_000 });
    await expect(page.getByTestId('before-s1')).toContainText('မူလ');
    await expect(page.getByTestId('after-s1')).toContainText('Variant narration');
  });
});
