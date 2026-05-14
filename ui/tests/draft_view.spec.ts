import { test, expect } from '@playwright/test';

const MOCK_DRAFT = {
  draft_id: 'draft-test-001',
  template_id: 'demand-letter',
  template_version: 1,
  prompt_fingerprint: 'abcdef1234567890',
  status: 'complete',
  fields: {
    plaintiff: 'Alice Corp',
    defendant: 'Bob LLC',
    amount: '$50,000',
  },
  sections: [
    {
      name: 'introduction',
      text: 'This letter concerns [chunk:chunk-aaa111] the matter between Alice Corp and Bob LLC.',
      citations: [
        {
          chunk_id: 'chunk-aaa111',
          claim_span_start: 24,
          claim_span_end: 80,
          validation_status: 'supported',
          validation_reason: 'The chunk directly references both parties in the contract.',
        },
      ],
      groundedness: 0.92,
    },
    {
      name: 'demand',
      text: 'We demand payment of $50,000 [chunk:chunk-bbb222] within 30 days.',
      citations: [
        {
          chunk_id: 'chunk-bbb222',
          claim_span_start: 28,
          claim_span_end: 60,
          validation_status: 'partial',
          validation_reason: 'Amount mentioned but payment date not explicitly stated.',
        },
      ],
      groundedness: 0.75,
    },
  ],
  groundedness_score: 0.84,
  generated_at: new Date().toISOString(),
  cost_usd: 0.0042,
  edit_count: 2,
  error: null,
};

test.describe('Draft view', () => {
  test.beforeEach(async ({ page }) => {
    await page.route('/api/drafts/draft-test-001', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(MOCK_DRAFT),
      });
    });
  });

  test('renders draft with fields and sections', async ({ page }) => {
    await page.goto('/drafts/draft-test-001');
    await expect(page.locator('text=Alice Corp')).toBeVisible();
    await expect(page.locator('text=introduction').first()).toBeVisible();
    await expect(page.locator('text=demand').first()).toBeVisible();
  });

  test('shows citation empty state initially', async ({ page }) => {
    await page.goto('/drafts/draft-test-001');
    await expect(page.locator('text=Click any citation chip to see the source span')).toBeVisible();
  });

  test('clicking a citation chip opens the source panel', async ({ page }) => {
    await page.goto('/drafts/draft-test-001');

    // Wait for draft to render
    await expect(page.locator('text=Alice Corp')).toBeVisible();

    // Find and click the citation chip (aaa111 is the last 6 chars)
    const chip = page.locator('text=aaa111').first();
    await expect(chip).toBeVisible();
    await chip.click();

    // Citation panel should appear with source header
    await expect(page.locator('text=Source')).toBeVisible();
  });

  test('groundedness bar renders for non-null score', async ({ page }) => {
    await page.goto('/drafts/draft-test-001');
    await expect(page.locator('text=Groundedness')).toBeVisible();
    await expect(page.locator('text=84%')).toBeVisible();
  });

  test('edit count badge is visible', async ({ page }) => {
    await page.goto('/drafts/draft-test-001');
    await expect(page.locator('text=2 edits applied')).toBeVisible();
  });
});
