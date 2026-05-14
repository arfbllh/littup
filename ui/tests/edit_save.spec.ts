import { test, expect } from '@playwright/test';

const MOCK_DRAFT = {
  draft_id: 'draft-edit-001',
  template_id: 'demand-letter',
  template_version: 1,
  prompt_fingerprint: 'abcdef12',
  status: 'complete',
  fields: { plaintiff: 'Original Plaintiff' },
  sections: [
    {
      name: 'introduction',
      text: 'Original section text here.',
      citations: [],
      groundedness: 0.8,
    },
  ],
  groundedness_score: 0.8,
  generated_at: new Date().toISOString(),
  cost_usd: 0.002,
  edit_count: 0,
  error: null,
};

test.describe('Edit mode and save', () => {
  test.beforeEach(async ({ page }) => {
    await page.route('/api/drafts/draft-edit-001', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(MOCK_DRAFT),
      });
    });
    await page.route('/api/edits', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ ok: true }),
      });
    });
  });

  test('Edit button toggles edit mode', async ({ page }) => {
    await page.goto('/drafts/draft-edit-001');
    await expect(page.locator('text=Original Plaintiff')).toBeVisible();

    await page.getByRole('button', { name: 'Edit' }).click();

    // Fields should become inputs in edit mode
    await expect(page.locator('input[value="Original Plaintiff"]')).toBeVisible();
  });

  test('Cancel reverts edit mode without saving', async ({ page }) => {
    await page.goto('/drafts/draft-edit-001');
    await page.getByRole('button', { name: 'Edit' }).click();

    const input = page.locator('input[value="Original Plaintiff"]');
    await input.fill('Modified Plaintiff');

    await page.getByRole('button', { name: 'Cancel' }).click();

    // Should be back to review mode showing original
    await expect(page.locator('text=Original Plaintiff')).toBeVisible();
  });

  test('Save edits calls /api/edits', async ({ page }) => {
    let editCallCount = 0;
    let editBody: Record<string, unknown> | null = null;

    await page.route('/api/edits', async (route) => {
      editCallCount++;
      editBody = JSON.parse(route.request().postData() ?? '{}') as Record<string, unknown>;
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ ok: true }),
      });
    });

    await page.goto('/drafts/draft-edit-001');
    await page.getByRole('button', { name: 'Edit' }).click();

    // Modify the plaintiff field
    const input = page.locator('input[value="Original Plaintiff"]');
    await input.fill('Modified Plaintiff');

    await page.getByRole('button', { name: 'Save edits' }).click();

    // Wait for the save to complete
    await expect(page.locator('input[value="Modified Plaintiff"]')).not.toBeVisible({ timeout: 5000 });

    expect(editCallCount).toBeGreaterThan(0);
    expect(editBody?.field_name).toBe('plaintiff');
    expect(editBody?.edited_text).toBe('Modified Plaintiff');
  });

  test('section textarea is editable in edit mode', async ({ page }) => {
    await page.goto('/drafts/draft-edit-001');
    await page.getByRole('button', { name: 'Edit' }).click();

    const textarea = page.locator('textarea').first();
    await expect(textarea).toBeVisible();
    await textarea.fill('New section content');
    await expect(textarea).toHaveValue('New section content');
  });
});
