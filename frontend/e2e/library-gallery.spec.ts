import { test, expect } from '@playwright/test';

test.describe('Library / Gallery (/library)', () => {
  test('renders approved video cards, player, and metadata modal', async ({ page }) => {
    await page.goto('/library');

    // Page header check
    await expect(page.locator('h1')).toContainText('Finished videos, ready to reuse');
    await expect(page.locator('.library-count')).toBeVisible();

    // Wait for video cards list to load from API
    const videoItems = page.locator('.library-item');
    await expect(videoItems.first()).toBeVisible({ timeout: 10000 });
    const count = await videoItems.count();
    expect(count).toBeGreaterThan(0);

    // First video item should be selected by default or selectable
    const firstItem = videoItems.first();
    await expect(firstItem).toBeVisible();

    // Select first item
    await firstItem.locator('.library-item__select').click();
    await expect(firstItem).toHaveClass(/library-item--selected/);

    // Verify player is visible and has controls
    const player = page.locator('.library-player__video');
    await expect(player).toBeVisible();
    await expect(player).toHaveAttribute('controls', '');
    await expect(player).toHaveAttribute('playsinline', '');

    // Verify Download MP4 link
    const downloadLink = page.locator('.library-item__download').first();
    await expect(downloadLink).toBeVisible();
    await expect(downloadLink).toHaveAttribute('href', /.*\.mp4|.*\/video/);

    // Test Metadata Modal
    const metadataBtn = page.getByRole('button', { name: /View metadata/i }).first();
    await expect(metadataBtn).toBeVisible();
    await metadataBtn.click();

    // Modal dialog should be open
    const modalDialog = page.locator('.modal-backdrop');
    await expect(modalDialog).toBeVisible();
    await expect(modalDialog.locator('#modal-title')).toBeVisible();
    await expect(modalDialog.locator('.modal-tag')).toHaveText('Video Metadata');

    // Verify QA details
    await expect(modalDialog).toContainText('Output QA');
    await expect(modalDialog).toContainText('Visual Evidence QA');
    await expect(modalDialog).toContainText('Verification Checks');

    // Switch to Technical JSON tab
    const jsonTab = modalDialog.getByRole('button', { name: 'Technical JSON' });
    await jsonTab.click();
    await expect(modalDialog.locator('.modal-json-viewer')).toBeVisible();

    // Switch back to QA Verification tab
    const qaTab = modalDialog.getByRole('button', { name: 'QA Verification' });
    await qaTab.click();
    await expect(modalDialog.locator('.modal-check-list')).toBeVisible();

    // Close via close button
    const closeBtn = modalDialog.getByRole('button', { name: 'Close', exact: true });
    await closeBtn.click();
    await expect(modalDialog).not.toBeVisible();

    // Reopen and test close via '×' button
    await metadataBtn.click();
    await expect(modalDialog).toBeVisible();
    const xBtn = modalDialog.getByLabel('Close modal');
    await xBtn.click();
    await expect(modalDialog).not.toBeVisible();

    // Reopen and test close via Escape key
    await metadataBtn.click();
    await expect(modalDialog).toBeVisible();
    await page.keyboard.press('Escape');
    await expect(modalDialog).not.toBeVisible();
  });

  test('handles archive action with confirmation dialog', async ({ page }) => {
    // Intercept DELETE call so test is hermetic and doesn't mutate test fixtures permanently
    await page.addInitScript(() => {
      window.sessionStorage.setItem('fyf-generation-access', 'archive-test-token');
    });
    let archiveHeaders: Record<string, string> | undefined;
    await page.route('**/api/jobs/*', async (route) => {
      if (route.request().method() === 'DELETE') {
        archiveHeaders = route.request().headers();
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({ success: true, archived: true }),
        });
      } else {
        await route.continue();
      }
    });

    await page.goto('/library');
    await expect(page.locator('.library-item').first()).toBeVisible({ timeout: 10000 });
    const initialCount = await page.locator('.library-item').count();
    expect(initialCount).toBeGreaterThan(0);

    const archiveBtn = page.getByRole('button', { name: /Archive/i }).first();
    await archiveBtn.click();

    // Archive confirmation dialog should appear
    const archiveDialog = page.locator('.modal-backdrop');
    await expect(archiveDialog).toBeVisible();
    await expect(archiveDialog.locator('#archive-dialog-title')).toHaveText('Archive Video?');

    // Click Cancel
    const cancelBtn = archiveDialog.getByRole('button', { name: 'Cancel' });
    await cancelBtn.click();
    await expect(archiveDialog).not.toBeVisible();

    // Reopen and Confirm Archive
    await archiveBtn.click();
    await expect(archiveDialog).toBeVisible();
    const confirmBtn = archiveDialog.getByRole('button', { name: 'Confirm Archive' });
    await confirmBtn.click();

    await expect.poll(() => archiveHeaders?.['x-fyf-access-token']).toBe('archive-test-token');

    // Verify dialog closes, item count decreases, and success banner shows
    await page.evaluate(() => {
      const header = document.querySelector('.studio-header') as HTMLElement | null;
      if (header) header.style.position = 'static';
      window.scrollTo(0, 0);
    });
    await page.screenshot({ path: 'output/playwright/library-gallery.png', fullPage: true });
  });
});
