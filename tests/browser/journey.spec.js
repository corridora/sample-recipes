import { test, expect } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';
import { execFileSync } from 'node:child_process';

test('search, detail, enrichment, seeded regression, healthy control and recovery', async ({ page, request }) => {
  if (process.env.RUM_BUNDLE_PATH) {
    await page.route('https://rum.corridora.com/corridora-rum.js?*', route => route.fulfill({ path: process.env.RUM_BUNDLE_PATH, contentType: 'application/javascript' }));
  }
  const token = process.env.DEMO_CONTROL_TOKEN || 'local-test-control';
  const headers = { Authorization: `Bearer ${token}` };
  async function phase(phase) {
    const response = await request.post('/api/demo/scenario', { headers, data: { phase, duration_seconds: 120 } });
    expect(response.ok()).toBeTruthy();
  }
  let finalTrace;
  try {
    await phase('healthy');
    await page.goto('/');
    await page.getByLabel('Find a recipe or ingredient').fill('tomato');
    await page.getByRole('button', { name: 'Search recipes' }).click();
    await expect(page.getByTestId('recipe-demo-recipe')).toBeVisible();
    await expect(page.getByTestId('recipe-control-recipe')).toHaveCount(0);
    await page.getByTestId('recipe-demo-recipe').click();
    await expect(page.getByTestId('recipe-title')).toHaveText('Roasted tomato pasta');
    await expect(page.getByTestId('read-mode')).toHaveText('batch');
    await page.getByTestId('enrich').click();
    await expect(page.getByTestId('enrichment-status')).toContainText('6 ingredients', { timeout: 15000 });
    await phase('regression');
    await page.getByTestId('recipe-demo-recipe').click();
    await expect(page.getByTestId('read-mode')).toHaveText('sequential');
    expect(parseInt(await page.getByTestId('duration').textContent())).toBeGreaterThanOrEqual(1450);
    const regressionTrace = await page.getByTestId('trace-id').textContent();
    await page.getByLabel('Find a recipe or ingredient').fill('');
    await page.getByRole('button', { name: 'Search recipes' }).click();
    await page.getByTestId('recipe-control-recipe').click();
    await expect(page.getByTestId('recipe-title')).toHaveText('Garden chickpea salad');
    await expect(page.getByTestId('read-mode')).toHaveText('batch');
    await phase('recover');
    await page.getByTestId('recipe-demo-recipe').click();
    await expect(page.getByTestId('recipe-title')).toHaveText('Roasted tomato pasta');
    await expect(page.getByTestId('read-mode')).toHaveText('batch');
    finalTrace = await page.getByTestId('trace-id').textContent();
    expect(finalTrace).toMatch(/^[a-f0-9]{32}$/);
    await page.screenshot({ path: '.sample/recipe-screenshot.png', fullPage: true });
    const heartbeat = await request.post('/api/demo/heartbeat', { headers, data: { trace_id: regressionTrace, source: 'playwright' } });
    expect(heartbeat.ok()).toBeTruthy();
    console.log(JSON.stringify({ regression_trace_id: regressionTrace, recovery_trace_id: finalTrace }));
    // Let the actual RUM exporter send its batch and page lifecycle telemetry.
    if (process.env.RUM_BUNDLE_PATH && !process.env.BASE_URL) {
      await page.waitForTimeout(6000);
      await page.goto('about:blank');
      await page.waitForTimeout(1000);
      const result = execFileSync(path.resolve('.venv/bin/python'), ['scripts/assert_browser_capture.py'], { encoding: 'utf8' });
      console.log(result);
    }
  } finally { await phase('recover'); }
});
