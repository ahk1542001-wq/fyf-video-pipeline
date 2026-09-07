import { test, expect } from '@playwright/test';

test.describe('Responsive Layout & QA Polish', () => {
  test('checks mobile viewport responsiveness without horizontal overflow', async ({ page }) => {
    await page.setViewportSize({ width: 375, height: 667 });

    for (const path of ['/', '/library', '/telemetry']) {
      await page.goto(path);
      await page.waitForLoadState('domcontentloaded');

      // Verify page width does not cause horizontal scroll
      const scrollWidth = await page.evaluate(() => document.documentElement.scrollWidth);
      const clientWidth = await page.evaluate(() => document.documentElement.clientWidth);
      expect(scrollWidth).toBeLessThanOrEqual(clientWidth + 2); // 2px margin for subpixel rendering
    }
  });

  test('checks tablet viewport responsiveness', async ({ page }) => {
    await page.setViewportSize({ width: 768, height: 1024 });

    for (const path of ['/', '/library', '/telemetry']) {
      await page.goto(path);
      await page.waitForLoadState('domcontentloaded');

      const scrollWidth = await page.evaluate(() => document.documentElement.scrollWidth);
      const clientWidth = await page.evaluate(() => document.documentElement.clientWidth);
      expect(scrollWidth).toBeLessThanOrEqual(clientWidth + 2);
    }
  });

  test('verifies brand theme colors and contrast', async ({ page }) => {
    await page.goto('/');

    const brandMark = page.locator('.studio-brand__mark');
    await expect(brandMark).toBeVisible();

    // Check that brand mark has dark olive color
    const brandColor = await brandMark.evaluate((el) => window.getComputedStyle(el).color);
    expect(brandColor).toMatch(/rgb\(48,\s*56,\s*44\)/); // #30382C

    // Check background color of app shell
    const bgColor = await page.locator('.app-shell').evaluate((el) => window.getComputedStyle(el).backgroundColor);
    expect(bgColor).toMatch(/rgb\(244,\s*240,\s*230\)/); // #F4F0E6
  });

  test('ensures zero unhandled console errors across all pages', async ({ page }) => {
    const consoleErrors: string[] = [];
    await page.route('**/api/runtime', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          runtime_mode: 'product',
          allowed_voice_providers: ['gemini'],
          script_model: 'test-script-model',
          fallback_model: 'test-fallback-model',
          generation_available: false,
          generation_access_required: false,
          generation_status: 'disabled',
          generation_message: 'Disabled for this read-only UI check.',
        }),
      });
    });
    await page.route('**/api/video-styles', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ styles: [] }),
      });
    });
    await page.route('**/api/jobs/recent', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify([]),
      });
    });
    await page.route('**/api/telemetry', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          total_jobs: 0,
          total_tokens_used: 0,
          total_cost_usd: 0,
          avg_render_time_sec: 0,
          jobs: [],
          budget_status: 'no production jobs recorded',
        }),
      });
    });
    page.on('console', (msg) => {
      if (msg.type() === 'error') {
        const text = msg.text();
        // Ignore expected network aborts or dev favicon 404s if any
        if (!text.includes('AbortError') && !text.includes('favicon')) {
          consoleErrors.push(text);
        }
      }
    });

    await page.goto('/');
    await page.goto('/library');
    await page.goto('/telemetry');

    expect(consoleErrors).toEqual([]);
  });
});
