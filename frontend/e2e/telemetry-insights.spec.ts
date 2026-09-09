import { test, expect, type Page } from '@playwright/test';

const runtimeFixture = {
  runtime_mode: 'hackathon',
  allowed_voice_providers: ['gemini'],
  script_model: 'Gemini 3.7 Flash',
  fallback_model: 'Gemini 2.5 Flash',
  generation_available: true,
  generation_access_required: false,
  generation_status: 'ready',
  generation_message: 'Generation ready',
};

const jobsFixture = [
  {
    job_id: 'launch-001',
    title: 'Launch cut',
    job_kind: 'video',
    status: 'completed',
    total_tokens_used: 40000,
    cost_usd: 0.0098,
    total_render_time_ms: 18500,
    qa_passed: 1,
    created_at: '2026-09-08T08:30:00Z',
  },
  {
    job_id: 'research-002',
    title: 'Research explainer',
    job_kind: 'video',
    status: 'failed',
    total_tokens_used: null,
    cost_usd: null,
    total_render_time_ms: null,
    qa_passed: 0,
    created_at: '2026-09-08T07:15:00Z',
  },
  {
    job_id: 'polish-003',
    title: 'Polish pass',
    job_kind: 'video',
    status: 'succeeded',
    total_tokens_used: 12000,
    cost_usd: 0.0034,
    total_render_time_ms: 9200,
    qa_passed: 1,
    created_at: '2026-09-07T17:45:00Z',
  },
];

const detailsByJobId: Record<string, unknown> = {
  'launch-001': {
    job: {
      job_id: 'launch-001',
      title: 'Launch cut',
      job_kind: 'video',
      status: 'completed',
      duration_sec: 18.5,
      total_render_time_ms: 18500,
      created_at: '2026-09-08T08:30:00Z',
      started_at: '2026-09-08T08:30:02Z',
      finished_at: '2026-09-08T08:30:21Z',
      calls: [
        {
          call_id: 'call-1',
          stage: 'script',
          model: 'gemini-3.7-flash',
          operation: 'generate_content',
          attempt: 1,
          billable: true,
          status: 'succeeded',
          duration_ms: 5400,
          usage: {
            input_tokens: 1600,
            output_tokens: 2200,
            total_tokens: 3800,
            cached_input_tokens: 0,
            thoughts_tokens: 0,
          },
        },
        {
          call_id: 'call-2',
          stage: 'voice',
          model: 'gemini-tts',
          operation: 'synthesize',
          attempt: 1,
          billable: true,
          status: 'succeeded',
          duration_ms: 1800,
          usage: {
            input_tokens: null,
            output_tokens: null,
            total_tokens: null,
            cached_input_tokens: null,
            thoughts_tokens: null,
          },
        },
      ],
      summary: {
        total_calls: 2,
        billable_calls: 2,
        operation_poll_calls: 0,
        successful_calls: 2,
        failed_calls: 0,
        retry_calls: 0,
        total_input_tokens: 1600,
        total_output_tokens: 2200,
        total_tokens: 3800,
        total_cached_input_tokens: 0,
        total_thoughts_tokens: 0,
        estimated_cost_usd: 0.0098,
        cost_status: 'partial',
        pricing_version: 'fixture',
        visual_fallbacks: 0,
        job_status: 'completed',
      },
      privacy: {
        prompts_recorded: false,
        response_text_recorded: false,
        credentials_recorded: false,
        raw_provider_errors_recorded: false,
      },
    },
    scenes: [
      {
        job_id: 'launch-001',
        scene_id: 'scene-01',
        treatment_type: 'kinetic type',
        render_time_ms: 7100,
        vertex_latency_ms: 2900,
        evidence_claim_count: 2,
        created_at: '2026-09-08T08:30:15Z',
      },
      {
        job_id: 'launch-001',
        scene_id: 'scene-02',
        treatment_type: 'cut-paper world',
        render_time_ms: 11400,
        vertex_latency_ms: 1800,
        evidence_claim_count: 1,
        created_at: '2026-09-08T08:30:20Z',
      },
    ],
    scene_count: 2,
    qa_records: [
      { gate: 'visual_qa', status: 'passed', evidence: 'scene-01.png' },
    ],
    qa_count: 1,
    connected_to_cloud: false,
  },
  'research-002': {
    job: {
      job_id: 'research-002',
      title: 'Research explainer',
      job_kind: 'video',
      status: 'failed',
      calls: [],
      summary: {
        total_calls: 1,
        billable_calls: 0,
        operation_poll_calls: 0,
        successful_calls: 0,
        failed_calls: 1,
        retry_calls: 1,
        total_input_tokens: 0,
        total_output_tokens: 0,
        total_tokens: 0,
        total_cached_input_tokens: 0,
        total_thoughts_tokens: 0,
        estimated_cost_usd: null,
        cost_status: 'unpriced',
        pricing_version: 'fixture',
        visual_fallbacks: null,
        job_status: 'failed',
      },
    },
    scenes: [],
    scene_count: 0,
    qa_records: [],
    qa_count: 0,
    connected_to_cloud: false,
  },
  'polish-003': {
    job: {
      job_id: 'polish-003',
      title: 'Polish pass',
      job_kind: 'video',
      status: 'succeeded',
      calls: [],
      summary: {
        total_calls: 0,
        billable_calls: 0,
        operation_poll_calls: 0,
        successful_calls: 0,
        failed_calls: 0,
        retry_calls: 0,
        total_input_tokens: 0,
        total_output_tokens: 0,
        total_tokens: 0,
        total_cached_input_tokens: 0,
        total_thoughts_tokens: 0,
        estimated_cost_usd: 0.0034,
        cost_status: 'partial',
        pricing_version: 'fixture',
        visual_fallbacks: 0,
        job_status: 'succeeded',
      },
    },
    scenes: [],
    scene_count: 0,
    qa_records: [],
    qa_count: 0,
    connected_to_cloud: false,
  },
};

async function installTelemetryFixtures(page: Page) {
  await page.addInitScript(() => {
    window.sessionStorage.setItem('fyf-generation-access', 'telemetry-test-token');
  });
  await page.route('**/api/runtime', async route => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(runtimeFixture),
    });
  });
  await page.route('**/api/telemetry', async route => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        total_jobs: 3,
        total_tokens_used: 52000,
        total_cost_usd: 0.0132,
        avg_render_time_sec: 13.9,
        total_vertex_calls: 2,
        jobs: jobsFixture,
        source: 'local_mirror',
        clickhouse_status: 'local_mirror_active',
        cloud: {
          connected: false,
          status: 'unavailable',
          label: 'not_configured',
        },
        delivery: {
          pending: 2,
          failed: 0,
          delivered: 8,
          schema_ready: true,
          cloud_connected: false,
          drain_blocked_reason: null,
          ingestion_lag_seconds: null,
          last_delivered_at: null,
        },
        ingestion_lag_seconds: null,
      }),
    });
  });
  await page.route('**/api/telemetry/reconciliation', async route => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        source: 'local_mirror',
        cloud: { connected: false, status: 'unavailable', label: 'not_configured' },
        local_mirror: { available: true, label: 'local_mirror', event_count: 8 },
        outbox: { pending: 2, failed: 0, delivered: 8, cloud_connected: false },
        freshness: { status: 'unknown' },
      }),
    });
  });
  await page.route('**/api/jobs/*/telemetry', async route => {
    const jobId = new URL(route.request().url()).pathname.split('/').at(-2) || '';
    await route.fulfill({
      status: detailsByJobId[jobId] ? 200 : 404,
      contentType: 'application/json',
      body: JSON.stringify(detailsByJobId[jobId] ?? { detail: 'Job telemetry not found' }),
    });
  });
}

test.describe('Telemetry & Insights / Data Officer (/telemetry and /insights)', () => {
  test('renders the same clean telemetry surface on /telemetry and /insights', async ({ page }) => {
    await installTelemetryFixtures(page);

    await page.goto('/telemetry');
    await expect(page.locator('h1')).toContainText('Generation telemetry');
    await expect(page.locator('.studio-nav__link--active')).toHaveText('Telemetry');
    await expect(page.getByRole('heading', { name: 'Overview' })).toBeVisible();

    await page.goto('/insights');
    await expect(page.locator('h1')).toContainText('Generation telemetry');
    await expect(page.locator('.studio-nav__link--active')).toHaveText('Telemetry');
    await expect(page.getByRole('heading', { name: 'Overview' })).toBeVisible();
  });

  test('shows factual overview, searchable productions, and selected production details', async ({ page }) => {
    await installTelemetryFixtures(page);
    await page.goto('/telemetry');

    await expect(page.getByRole('heading', { name: 'Overview' })).toBeVisible();
    await expect(page.getByText('Total generations')).toBeVisible();
    await expect(page.getByTestId('metric-total-generations').locator('strong')).toHaveText('3');
    await expect(page.getByTestId('metric-known-cost').getByText('Known cost')).toBeVisible();
    await expect(page.getByTestId('metric-known-cost').locator('strong')).toHaveText('$0.0132');
    await expect(page.getByText('Success rate')).toBeVisible();
    await expect(page.getByTestId('metric-success-rate').locator('strong')).toHaveText('67%');
    await expect(page.getByTestId('telemetry-source')).toContainText('Local mirror only');
    await expect(page.getByText('ClickHouse sync')).toBeVisible();

    await expect(page.getByRole('heading', { name: 'Productions' })).toBeVisible();
    const search = page.getByRole('searchbox', { name: 'Search productions' });
    await expect(search).toBeVisible();
    await expect(page.getByRole('button', { name: /Launch cut/i })).toBeVisible();
    await expect(page.getByRole('button', { name: /Research explainer/i })).toBeVisible();

    await search.fill('launch');
    await expect(page.getByRole('button', { name: /Launch cut/i })).toBeVisible();
    await expect(page.getByRole('button', { name: /Research explainer/i })).toBeHidden();

    await search.fill('');
    await page.getByRole('button', { name: /Research explainer/i }).click();
    await expect(page.getByTestId('selected-production')).toContainText('Research explainer');
    await expect(page.getByRole('heading', { name: 'Details' })).toBeVisible();
    await expect(page.getByText('Performance timeline')).toBeVisible();
    await expect(page.getByText('Provider calls', { exact: true })).toBeVisible();
    await expect(page.getByRole('heading', { name: 'Scene evidence' })).toBeVisible();
  });

  test('keeps advanced ClickHouse query controls collapsed and query ids bounded', async ({ page }) => {
    const queryBodies: Record<string, unknown>[] = [];
    const queryHeaders: Record<string, string>[] = [];
    await installTelemetryFixtures(page);
    await page.route('**/api/clickhouse/query', async route => {
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
          source: 'clickhouse',
          availability: 'connected',
        }),
      });
    });
    await page.goto('/telemetry');

    const advanced = page.getByTestId('advanced-telemetry');
    const querySelector = page.locator('#clickhouse-query-selector');
    const runBtn = page.locator('#run-query-button');
    await expect(advanced).toBeVisible();
    await expect(querySelector).toBeHidden();
    await advanced.getByText('Advanced telemetry').click();
    await expect(querySelector).toBeVisible();
    await expect(advanced.locator('textarea')).toHaveCount(0);

    for (const capability of [
      'Creation Timeline',
      'Cost Intelligence',
      'Quality Tracking',
      'Editing Friction',
      'Version Comparison',
      'Grounded Recommendations',
    ]) {
      await expect(advanced.getByRole('button', { name: capability })).toBeVisible();
    }

    await advanced.getByRole('button', { name: 'Jobs Summary' }).click();
    await expect(querySelector).toHaveValue('jobs_overview');
    await expect(advanced.locator('table')).toBeVisible();
    await expect.poll(() => queryBodies).toHaveLength(1);
    expect(queryBodies[0]).toEqual({ query_id: 'jobs_overview' });
    expect(queryHeaders[0]['x-fyf-access-token']).toBe('telemetry-test-token');

    await advanced.getByRole('button', { name: 'Cost Intelligence' }).click();
    await expect(querySelector).toHaveValue('cost_summary');
    await expect.poll(() => queryBodies).toHaveLength(2);
    expect(queryBodies[1]).toEqual({ query_id: 'cost_summary' });
    await runBtn.click();
    await expect.poll(() => queryBodies).toHaveLength(3);
    expect(queryBodies[2]).toEqual({ query_id: 'cost_summary' });
  });

  test('keeps refresh and auto-sync controls explicit', async ({ page }) => {
    await installTelemetryFixtures(page);
    await page.goto('/telemetry');

    const refreshBtn = page.getByRole('button', { name: 'Refresh data' });
    await expect(refreshBtn).toBeVisible();
    await refreshBtn.click();

    const autoSyncBtn = page.getByRole('button', { name: /Auto-sync:/i });
    await expect(autoSyncBtn).toContainText('OFF');
    await autoSyncBtn.click();
    await expect(autoSyncBtn).toContainText('ON (15s)');
    await autoSyncBtn.click();
    await expect(autoSyncBtn).toContainText('OFF');
  });

  test('keeps the telemetry hierarchy usable on a narrow viewport', async ({ page }) => {
    await installTelemetryFixtures(page);
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto('/telemetry');

    await expect(page.getByRole('heading', { name: 'Overview' })).toBeVisible();
    await expect(page.getByRole('searchbox', { name: 'Search productions' })).toBeVisible();
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1);
    expect(overflow).toBe(true);
  });

  test('interacts with Ask the Data Officer inside Advanced telemetry', async ({ page }) => {
    await installTelemetryFixtures(page);
    let officerHeaders: Record<string, string> | undefined;
    await page.route('**/api/insights', async route => {
      officerHeaders = route.request().headers();
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          success: true,
          question: 'How many jobs succeeded?',
          answer: 'Based on the video_pipeline_jobs table in ClickHouse Cloud, 2 production jobs completed successfully.',
          tool_used: true,
        }),
      });
    });

    await page.goto('/telemetry');
    const advanced = page.getByTestId('advanced-telemetry');
    await advanced.getByText('Advanced telemetry').click();
    const input = advanced.getByPlaceholder(/How many video jobs/i);
    const askBtn = advanced.getByRole('button', { name: 'Ask' });
    await expect(input).toBeVisible();
    await expect(askBtn).toBeDisabled();
    await input.fill('How many jobs succeeded?');
    await expect(askBtn).toBeEnabled();
    await askBtn.click();
    await expect(advanced.getByText(/Based on the video_pipeline_jobs table/i)).toBeVisible();
    expect(officerHeaders?.['x-fyf-access-token']).toBe('telemetry-test-token');
  });
});
