import { test, expect } from '@playwright/test';

test.describe('Telemetry & Insights / Data Officer (/telemetry and /insights)', () => {
  test('renders telemetry dashboard on /telemetry and /insights', async ({ page }) => {
    // 1. Visit /telemetry
    await page.goto('/telemetry');
    await expect(page.locator('h1')).toContainText('Generation telemetry');
    await expect(page.locator('.studio-nav__link--active')).toHaveText('Telemetry');

    // 2. Visit /insights rewrite
    await page.goto('/insights');
    await expect(page.locator('h1')).toContainText('Generation telemetry');
    await expect(page.locator('.studio-nav__link--active')).toHaveText('Telemetry');
  });

  test('verifies KPI cards, job ledger buttons, and latency waterfall chart', async ({ page }) => {
    await page.goto('/telemetry');

    // KPI Cards
    await expect(page.getByText('Jobs observed')).toBeVisible();
    await expect(page.getByText('Selected job cost')).toBeVisible();
    await expect(page.getByText('Provider calls', { exact: true })).toBeVisible();
    await expect(page.getByText('Token status')).toBeVisible();

    // Job Ledger buttons
    const jobButtons = page.locator('.max-w-2xl button');
    const count = await jobButtons.count();
    if (count > 1) {
      // Click second job button
      const secondBtn = jobButtons.nth(1);
      await secondBtn.click();
      await expect(secondBtn).toHaveClass(/bg-\[#16856B\]/);
    }

    // Call ledger
    await expect(page.getByText(/Vertex \/ Gemini TTS call ledger/i)).toBeVisible();

    // Scene Latency Waterfall Chart
    await expect(page.getByText(/Scene ID & Treatment Grammar/i)).toBeVisible();

    // Telemetry boundary items
    await expect(page.getByText('Prompts excluded from telemetry')).toBeVisible();
    await expect(page.getByText('Credentials excluded from telemetry')).toBeVisible();
  });

  test('runs only the approved ClickHouse query presets by query id', async ({ page }) => {
    const queryBodies: Record<string, unknown>[] = [];
    const queryHeaders: Record<string, string>[] = [];
    await page.addInitScript(() => {
      window.sessionStorage.setItem('fyf-generation-access', 'telemetry-test-token');
    });
    await page.route('**/api/clickhouse/query', async (route) => {
      const body = route.request().postDataJSON() as Record<string, unknown>;
      queryBodies.push(body);
      queryHeaders.push(route.request().headers());
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          success: true,
          columns: ['query_id', 'value'],
          rows: [[body.query_id, 'fixture result']],
          row_count: 1,
          duration_ms: 7,
          source: 'fixture',
        }),
      });
    });
    await page.goto('/telemetry');

    const consoleSection = page.locator('section').filter({ hasText: 'ClickHouse query console' });
    await expect(consoleSection).toBeVisible();

    const querySelector = page.locator('#clickhouse-query-selector');
    const runBtn = page.locator('#run-query-button');
    await expect(querySelector).toBeVisible();
    await expect(runBtn).toBeVisible();
    await expect(consoleSection.locator('textarea')).toHaveCount(0);

    // The approved six-capability analytics set (creation timeline, cost intelligence,
    // quality tracking, editing friction, version comparison, grounded recommendations)
    // is rendered as bounded preset pills. These are visibility-only checks and do NOT
    // click, so the sequential /api/clickhouse/query body-count assertions below stay
    // intact. Added to reconcile the intentional capability extension; no existing
    // assertion is removed or weakened.
    for (const capability of [
      'Creation Timeline',
      'Cost Intelligence',
      'Quality Tracking',
      'Editing Friction',
      'Version Comparison',
      'Grounded Recommendations',
    ]) {
      await expect(consoleSection.getByRole('button', { name: capability })).toBeVisible();
    }

    // Each preset is a bounded query id; no arbitrary SQL is accepted by this UI.
    const jobsPreset = consoleSection.getByRole('button', { name: 'Jobs Summary' });
    await jobsPreset.click();
    await expect(querySelector).toHaveValue('jobs_overview');

    const resultsTable = consoleSection.locator('table');
    await expect(resultsTable).toBeVisible({ timeout: 10000 });
    await expect(consoleSection.getByText(/Source:/i)).toBeVisible();
    await expect.poll(() => queryBodies).toHaveLength(1);
    expect(queryBodies[0]).toEqual({ query_id: 'jobs_overview' });
    expect(queryHeaders[0]['x-fyf-access-token']).toBe('telemetry-test-token');

    // 2. Model calls preset
    const modelPreset = consoleSection.getByRole('button', { name: 'Model Usage' });
    await modelPreset.click();
    await expect(querySelector).toHaveValue('model_calls');
    await expect(resultsTable).toBeVisible({ timeout: 10000 });
    await expect.poll(() => queryBodies).toHaveLength(2);
    expect(queryBodies[1]).toEqual({ query_id: 'model_calls' });

    // 3. Scene latency preset
    const scenePreset = consoleSection.getByRole('button', { name: 'Scene Latencies' });
    await scenePreset.click();
    await expect(querySelector).toHaveValue('scene_latency');
    await expect(resultsTable).toBeVisible();
    await expect.poll(() => queryBodies).toHaveLength(3);
    expect(queryBodies[2]).toEqual({ query_id: 'scene_latency' });

    // 4. Cost intelligence preset
    // label renamed Cost Summary -> Cost Intelligence to match the approved six-capability
    // set (cost intelligence); product code (frontend/app/telemetry/page.tsx) is authoritative.
    // The underlying bounded query id remains 'cost_summary'.
    const costPreset = consoleSection.getByRole('button', { name: 'Cost Intelligence' });
    await costPreset.click();
    await expect(querySelector).toHaveValue('cost_summary');
    await expect(resultsTable).toBeVisible({ timeout: 10000 });
    await expect.poll(() => queryBodies).toHaveLength(4);
    expect(queryBodies[3]).toEqual({ query_id: 'cost_summary' });

    // The run button executes the currently selected bounded preset.
    await runBtn.click();
    await expect.poll(() => queryBodies).toHaveLength(5);
    expect(queryBodies[4]).toEqual({ query_id: 'cost_summary' });
  });

  test('tests live refresh and auto-sync toggles', async ({ page }) => {
    await page.goto('/telemetry');

    const refreshBtn = page.getByRole('button', { name: /Refresh Ledger/i });
    await expect(refreshBtn).toBeVisible();
    await refreshBtn.click();

    const autoSyncBtn = page.getByRole('button', { name: /Auto-sync:/i });
    await expect(autoSyncBtn).toContainText('OFF');
    await autoSyncBtn.click();
    await expect(autoSyncBtn).toContainText('ON (15s)');
    await autoSyncBtn.click();
    await expect(autoSyncBtn).toContainText('OFF');
  });

  test('interacts with Ask the Data Officer Q&A chat form', async ({ page }) => {
    await page.addInitScript(() => {
      window.sessionStorage.setItem('fyf-generation-access', 'telemetry-test-token');
    });
    let officerHeaders: Record<string, string> | undefined;
    // Intercept insights API with deterministic mock
    await page.route('**/api/insights', async (route) => {
      officerHeaders = route.request().headers();
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          success: true,
          question: 'How many jobs succeeded?',
          answer: 'Based on the video_pipeline_jobs table in ClickHouse Cloud, 6 production jobs passed all QA gates and completed successfully.',
          tool_used: true,
        }),
      });
    });

    await page.goto('/telemetry');

    const officerSection = page.locator('section').filter({ hasText: 'Ask the Data Officer' });
    await expect(officerSection).toBeVisible();

    const input = officerSection.getByPlaceholder(/How many video jobs/i);
    const askBtn = officerSection.getByRole('button', { name: 'Ask' });

    await expect(input).toBeVisible();
    await expect(askBtn).toBeDisabled();

    await input.fill('How many jobs succeeded?');
    await expect(askBtn).toBeEnabled();
    await askBtn.click();

    await expect(officerSection.getByText(/Based on the video_pipeline_jobs table/i)).toBeVisible();
    expect(officerHeaders?.['x-fyf-access-token']).toBe('telemetry-test-token');
    await page.evaluate(() => {
      const header = document.querySelector('.studio-header') as HTMLElement | null;
      if (header) header.style.position = 'static';
      window.scrollTo(0, 0);
    });
    await page.screenshot({ path: 'output/playwright/telemetry-insights.png', fullPage: true });
  });
});
