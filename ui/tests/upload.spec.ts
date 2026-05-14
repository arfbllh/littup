import { test, expect } from '@playwright/test';
import path from 'path';

test.describe('Upload flow', () => {
  test('navigates to documents page on root visit', async ({ page }) => {
    await page.goto('/');
    await expect(page).toHaveURL('/documents');
  });

  test('shows upload dropzone when Upload button is clicked', async ({ page }) => {
    await page.goto('/documents');
    await page.getByRole('button', { name: /upload documents/i }).click();
    await expect(page.locator('text=Drag PDF files here')).toBeVisible();
  });

  test('upload page has dropzone and back link', async ({ page }) => {
    await page.goto('/documents/upload');
    await expect(page.locator('text=Drag PDF files here')).toBeVisible();
    await expect(page.locator('text=Back to documents list')).toBeVisible();
  });

  test('documents table renders after API load', async ({ page }) => {
    await page.route('/api/documents*', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          items: [
            {
              document_id: 'doc-test-001',
              filename: 'test-contract.pdf',
              status: 'ready',
              page_count: 12,
              size_bytes: 204800,
              created_at: new Date().toISOString(),
            },
          ],
          next_offset: null,
        }),
      });
    });

    await page.goto('/documents');
    await expect(page.locator('text=test-contract.pdf')).toBeVisible();
    await expect(page.locator('text=ready')).toBeVisible();
  });

  test('shows error state when API is unavailable', async ({ page }) => {
    await page.route('/api/documents*', async (route) => {
      await route.fulfill({ status: 503, body: 'Service Unavailable' });
    });
    await page.goto('/documents');
    await expect(page.locator('text=Failed to load documents')).toBeVisible();
  });
});
