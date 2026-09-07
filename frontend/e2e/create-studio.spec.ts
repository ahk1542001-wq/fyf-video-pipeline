import { test, expect, type Page } from '@playwright/test';

async function mockReadyRuntime(page: Page) {
  await page.route('**/api/runtime', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        runtime_mode: 'product',
        allowed_voice_providers: ['gemini'],
        script_model: 'test-script-model',
        fallback_model: 'test-fallback-model',
        generation_available: true,
        generation_access_required: false,
        generation_status: 'ready',
        generation_message: 'Ready',
      }),
    });
  });
}

test.describe('Create Studio (/ and /create)', () => {
  test('renders Create Studio properly on / and /create routes', async ({ page }) => {
    // 1. Test root route /
    await page.goto('/');
    await expect(page.locator('h1')).toContainText('Turn a draft into a finished video');
    await expect(page.locator('.studio-brand')).toBeVisible();
    await expect(page.locator('.studio-brand__mark')).toHaveText('FYF Agentic Business Studio');

    // Verify navigation links
    const createLink = page.locator('.studio-nav').getByRole('link', { name: 'Create', exact: true });
    const libraryLink = page.locator('.studio-nav').getByRole('link', { name: 'Library', exact: true });
    const telemetryLink = page.locator('.studio-nav').getByRole('link', { name: 'Telemetry', exact: true });
    await expect(createLink).toBeVisible();
    await expect(libraryLink).toBeVisible();
    await expect(telemetryLink).toBeVisible();

    // 2. Test /create route rewrite
    await page.goto('/create');
    await expect(page.locator('h1')).toContainText('Turn a draft into a finished video');
    await expect(createLink).toHaveClass(/studio-nav__link--active/);
  });

  test('interacts with Runtime Disclosure toggle panel', async ({ page }) => {
    await page.goto('/');
    const disclosureBtn = page.locator('.runtime-disclosure__trigger');
    await expect(disclosureBtn).toBeVisible();

    // Open disclosure panel
    await disclosureBtn.click();
    const panel = page.locator('.runtime-disclosure__panel');
    await expect(panel).toBeVisible();
    await expect(panel).toContainText('Primary model');
    await expect(panel).toContainText('Allowed voice modes');

    // Close disclosure panel
    await disclosureBtn.click();
    await expect(panel).not.toBeVisible();
  });

  test('tests topic input, persona, style, and mascot toggles', async ({ page }) => {
    await mockReadyRuntime(page);
    await page.goto('/');

    const topicArea = page.locator('#topic-source');
    const generateBtn = page.getByRole('button', { name: 'Generate script' });
    const polishBtn = page.getByRole('button', { name: /FYF Polish/i });
    const styleSelect = page.locator('#video-style');
    const personaSelect = page.locator('#presenter-persona');
    const mascotToggle = page.locator('#mascot-toggle');

    // Verify initial state: buttons disabled when topic is empty
    await expect(topicArea).toHaveValue('');
    await expect(generateBtn).toBeDisabled();
    await expect(polishBtn).toBeDisabled();
    await expect(page.locator('.action-hint')).toContainText('Enter a topic above to enable script generation');

    // Select style
    await styleSelect.selectOption('evidence_story');
    await expect(styleSelect).toHaveValue('evidence_story');
    await styleSelect.selectOption('cinematic_continuity');
    await expect(styleSelect).toHaveValue('cinematic_continuity');
    await styleSelect.selectOption('fyf_explainer');
    await expect(styleSelect).toHaveValue('fyf_explainer');

    // Select persona
    await personaSelect.selectOption('dynamic_storyteller');
    await expect(personaSelect).toHaveValue('dynamic_storyteller');
    await personaSelect.selectOption('objective_analyst');
    await expect(personaSelect).toHaveValue('objective_analyst');
    await personaSelect.selectOption('clear_educator');
    await expect(personaSelect).toHaveValue('clear_educator');

    // Toggle mascot
    await expect(mascotToggle).toHaveText(/On-screen/);
    await mascotToggle.click();
    await expect(mascotToggle).toHaveText(/Voice only/);
    await mascotToggle.click();
    await expect(mascotToggle).toHaveText(/On-screen/);

    // Enter topic
    await topicArea.fill('AI Video Production Workflow in Myanmar');
    await expect(generateBtn).toBeEnabled();
    await expect(polishBtn).toBeEnabled();
    await expect(page.locator('.action-hint')).not.toBeVisible();

    // Verify preview empty state
    await expect(page.locator('.preview-empty')).toBeVisible();
  });

  test('shows the approved business presets and essential brand kit controls', async ({ page }) => {
    await page.goto('/');

    await expect(page.getByRole('button', { name: 'Brand Explainer (Flagship)', exact: true })).toBeVisible();
    await expect(page.getByRole('button', { name: 'High-Converting Social Ad', exact: true })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Product Launch Hype (Magnific)', exact: true })).toBeVisible();
    await expect(page.getByText('Business studio presets', { exact: true })).toBeVisible();

    const ctaInput = page.locator('#cta-button-text');
    const retentionToggle = page.locator('#retention-progress-bar-toggle');
    const lowerThirdsToggle = page.locator('#animated-lower-thirds-toggle');
    await expect(ctaInput).toHaveValue('');
    await expect(retentionToggle).toBeChecked();
    await expect(lowerThirdsToggle).toBeChecked();

    await page.getByRole('button', { name: 'High-Converting Social Ad', exact: true }).click();
    await expect(page.locator('#studio-name')).toHaveValue('Brand Ad Studio');
    await expect(page.locator('#studio-language')).toHaveValue('en-US');
    await expect(ctaInput).toHaveValue('Shop Now');

    await page.getByRole('button', { name: 'Product Launch Hype (Magnific)', exact: true }).click();
    await expect(page.locator('#studio-name')).toHaveValue('Launch Studio');
    await expect(ctaInput).toHaveValue('Pre-Order Now');

    await page.getByRole('button', { name: 'Brand Explainer (Flagship)', exact: true }).click();
    await expect(page.locator('#studio-name')).toHaveValue('FYF Studio');
    await expect(page.locator('#studio-language')).toHaveValue('my-MM');
    await expect(ctaInput).toHaveValue('');
  });

  test('accepts English story metadata and sends the selected render controls', async ({ page }) => {
    const englishScript = {
      title: 'Brand Ad Story',
      language: 'en-US',
      studio_name: 'Brand Ad Studio',
      genre: 'tech_explainer',
      presenter_mode: 'voiceover_only',
      voice_actor: 'Puck',
      segments: [
        {
          id: 'en-1',
          text: 'Turn your next campaign into a clear customer story.',
          visual_action: 'Product and customer journey diagram',
          scene_type: 'demo',
          mascot_action: 'present',
          emotion: 'confident',
          emphasis: ['customer story'],
        },
      ],
    };
    const queryBodies: Record<string, unknown>[] = [];

    await page.route('**/api/runtime', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          runtime_mode: 'product',
          allowed_voice_providers: ['gemini'],
          script_model: 'test-script-model',
          fallback_model: 'test-fallback-model',
          generation_available: true,
          generation_access_required: false,
          generation_status: 'ready',
          generation_message: 'Ready',
        }),
      });
    });
    await page.route('**/api/story-polish', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ success: true, model_used: 'test-model', variants: [{ name: 'Direct Response', script: englishScript }] }),
      });
    });
    await page.route('**/api/story-lock', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ success: true, lock_id: 'feed1234', data: englishScript }),
      });
    });
    await page.route('**/api/generate-video', async (route) => {
      queryBodies.push(route.request().postDataJSON() as Record<string, unknown>);
      await route.fulfill({
        status: 202,
        contentType: 'application/json',
        body: JSON.stringify({ success: true, job_id: 'a1b2c3d4', status_url: '/api/jobs/a1b2c3d4/status' }),
      });
    });
    await page.route('**/api/jobs/a1b2c3d4/status', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ status: 'completed' }),
      });
    });
    await page.route('**/api/jobs/a1b2c3d4/telemetry', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          job: {
            job_id: 'a1b2c3d4',
            status: 'completed',
            render_duration_ms: 12345,
            summary: {
              total_tokens: 987,
              token_status: 'complete',
              estimated_cost_usd: 0.0456,
              cost_status: 'exact',
            },
          },
          connected_to_cloud: true,
        }),
      });
    });

    await page.goto('/');
    await page.getByRole('button', { name: 'High-Converting Social Ad', exact: true }).click();
    await page.locator('#topic-source').fill('A product launch campaign for small businesses');
    await page.getByRole('button', { name: /FYF Polish/i }).click();
    await expect(page.locator('.story-section')).toBeVisible();
    await expect(page.getByText('English Narration', { exact: true })).toBeVisible();

    await page.getByRole('button', { name: /Approve selected story/i }).click();
    await expect(page.getByRole('button', { name: /Generate locked video/i })).toBeEnabled();

    await page.locator('#cta-button-text').fill('Book a Demo');
    await page.locator('#retention-progress-bar-toggle').uncheck();
    await page.locator('#animated-lower-thirds-toggle').uncheck();
    await page.getByRole('radio', { name: /16:9/ }).click();
    await page.getByRole('button', { name: /Generate locked video/i }).click();

    await expect.poll(() => queryBodies).toHaveLength(1);
    expect(queryBodies[0]).toMatchObject({
      lock_id: 'feed1234',
      language: 'en-US',
      studio_name: 'Brand Ad Studio',
      cta_text: 'Book a Demo',
      retention_progress_bar: false,
      animated_lower_thirds: false,
      aspect_ratio: '16:9',
    });
    const audit = page.locator('#clickhouse-production-audit');
    await expect(audit).toContainText('a1b2c3d4');
    await expect(audit).toContainText('12.3s');
    await expect(audit).toContainText('987');
    await expect(audit).toContainText('$0.0456');
    await expect(audit).toContainText(/Dual-write confirmed/i);
    await expect(audit).not.toContainText('18.4s');
    await expect(audit).not.toContainText('3,420');
    await expect(audit).not.toContainText('$0.021');

    // A completed render must release its controller so an operator can render
    // the same approved lock again with a different format or brand control.
    await page.getByRole('button', { name: /Generate locked video/i }).click();
    await expect.poll(() => queryBodies).toHaveLength(2);
  });

  test('reviews story variants, edits segments, and approves story lock', async ({ page }) => {
    await mockReadyRuntime(page);
    // Intercept story polish endpoint to return deterministic mock variants
    await page.route('**/api/story-polish', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          success: true,
          model_used: 'gemini-3.7-flash',
          variants: [
            {
              name: 'Whiteboard Focus',
              script: {
                title: 'AI Verification Workflow',
                language: 'my-MM',
                segments: [
                  {
                    id: 's1',
                    text: 'ယနေ့ခေတ် AI စနစ်များတွင် စစ်ဆေးမှု အလွန်အရေးကြီးပါသည်။',
                    visual_action: 'Whiteboard diagram showing human verification gate',
                    scene_type: 'whiteboard',
                    mascot_action: 'present',
                    emotion: 'neutral',
                    emphasis: ['AI စနစ်များ'],
                  },
                  {
                    id: 's2',
                    text: 'အမှားအယွင်းမရှိစေရန် လူသားစစ်ဆေးသူက အတည်ပြုရပါမည်။',
                    visual_action: 'Close-up of approval checkmark on tablet',
                    scene_type: 'demo',
                    mascot_action: 'explain',
                    emotion: 'focused',
                    emphasis: ['အတည်ပြု'],
                  },
                ],
              },
            },
            {
              name: 'Evidence Story',
              script: {
                title: 'Data Officer Telemetry',
                language: 'my-MM',
                segments: [
                  {
                    id: 's1',
                    text: 'ClickHouse Cloud တွင် telemetry အချက်အလက်များ သိမ်းဆည်းပါသည်။',
                    visual_action: 'Telemetry chart dashboard',
                    scene_type: 'demo',
                    mascot_action: 'think',
                    emotion: 'focused',
                    emphasis: ['ClickHouse'],
                  },
                ],
              },
            },
          ],
        }),
      });
    });

    // Intercept story lock endpoint
    await page.route('**/api/story-lock', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          success: true,
          lock_id: 'abcd1234',
          data: {
            title: 'AI Verification Workflow (Locked)',
            language: 'my-MM',
            segments: [
              {
                id: 's1',
                text: 'ယနေ့ခေတ် AI စနစ်များတွင် စစ်ဆေးမှု အလွန်အရေးကြီးပါသည်။ (Verified)',
                visual_action: 'Whiteboard diagram showing human verification gate',
                scene_type: 'whiteboard',
                mascot_action: 'approve',
                emotion: 'confident',
                emphasis: ['AI စနစ်များ'],
              },
            ],
          },
        }),
      });
    });

    await page.goto('/');
    const topicArea = page.locator('#topic-source');
    await topicArea.fill('AI Verification in Myanmar');

    const polishBtn = page.getByRole('button', { name: /FYF Polish/i });
    await polishBtn.click();

    // Verify variants displayed
    await expect(page.locator('.story-section')).toBeVisible();
    const variant1 = page.getByRole('button', { name: /Whiteboard Focus/i });
    const variant2 = page.getByRole('button', { name: /Evidence Story/i });
    await expect(variant1).toBeVisible();
    await expect(variant2).toBeVisible();

    // Verify segment cards
    const segmentInput = page.locator('#segment-s1-narration');
    await expect(segmentInput).toBeVisible();
    await segmentInput.fill('ယနေ့ခေတ် AI စနစ်များတွင် စစ်ဆေးမှု အလွန်အရေးကြီးပါသည်။ (Verified)');

    // Test mascot action toggle pills
    const approveMascotBtn = page.locator('.pill-group').first().getByRole('button', { name: 'approve' });
    await approveMascotBtn.click();
    await expect(approveMascotBtn).toHaveClass(/pill-btn--active/);

    // Test emotion toggle pills
    const confidentEmotionBtn = page.locator('.segment-card__controls').first().getByRole('button', { name: 'confident' });
    await confidentEmotionBtn.click();
    await expect(confidentEmotionBtn).toHaveClass(/pill-btn--active/);

    // Switch to variant 2 and back
    await variant2.click();
    await expect(variant2).toHaveAttribute('aria-pressed', 'true');
    await variant1.click();
    await expect(variant1).toHaveAttribute('aria-pressed', 'true');

    // Click Approve and Lock
    const lockBtn = page.getByRole('button', { name: /Approve selected story/i });
    await expect(lockBtn).toBeEnabled();
    await lockBtn.click();

    // Verify video render button is unlocked
    const renderBtn = page.getByRole('button', { name: /Generate locked video/i });
    await expect(renderBtn).toBeVisible();
    await expect(renderBtn).toBeEnabled();

    // Verify technical JSON disclosure
    const techDisclosure = page.locator('.technical-disclosure summary');
    await expect(techDisclosure).toBeVisible();
    await techDisclosure.click();
    await page.evaluate(() => {
      const header = document.querySelector('.studio-header') as HTMLElement | null;
      if (header) header.style.position = 'static';
      window.scrollTo(0, 0);
    });
    await page.screenshot({ path: 'output/playwright/create-studio.png', fullPage: true });
  });
});
