import { expect, test } from '@playwright/test';

// This exercises the real browser surface without replacing its native bridge.
// Connected capture/provider/file acceptance must run in the installed Tauri app.
test('browser clearly disables native journeys and never calls a direct backend', async ({ page }) => {
  const backendRequests: string[] = [];
  page.on('request', (request) => { if (request.url().includes('/api/v1/')) backendRequests.push(request.url()); });
  await page.goto('/');
  await expect(page.getByRole('heading', { name: 'Meeting library' })).toBeVisible();
  await expect(page.getByText('Native desktop features are unavailable in this browser.')).toBeVisible();
  await page.getByRole('button', { name: 'New meeting', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: 'New meeting' });
  await dialog.getByLabel('Meeting name').fill('Browser is not a recorder');
  await expect(dialog.getByRole('button', { name: 'Create meeting' })).toBeDisabled();
  await page.keyboard.press('Escape');
  await expect(dialog).toBeHidden();
  await page.getByRole('main').getByRole('button', { name: 'Import audio' }).click();
  await page.getByLabel('Allow private temporary decoded audio').check();
  await expect(page.getByRole('button', { name: 'Choose file and import' })).toBeDisabled();
  await page.keyboard.press('Escape');
  await page.getByRole('navigation', { name: 'Main navigation' }).getByRole('link', { name: 'Settings', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Settings', exact: true })).toBeVisible();
  await page.getByRole('tab', { name: 'Window', exact: true }).click();
  await expect(page.getByText('Desktop connection unavailable', { exact: true })).toBeVisible();
  await page.keyboard.press('Control+k');
  await expect(page.getByRole('heading', { name: 'Search & Ask' })).toBeVisible();
  await expect(page.getByLabel('Query', { exact: true })).toBeFocused();
  await page.getByLabel('Query', { exact: true }).fill('? typed question should not open help');
  await expect(page.getByRole('dialog')).toHaveCount(0);
  expect(backendRequests).toEqual([]);
});

test('keyboard navigation and modal focus restoration remain usable', async ({ page }) => {
  await page.goto('/');
  await page.keyboard.press('?');
  await expect(page.getByRole('dialog', { name: 'Keyboard shortcuts' })).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await page.keyboard.press('Control+,');
  await expect(page.getByRole('heading', { name: 'Settings', exact: true })).toBeVisible();
  await page.getByRole('tab', { name: 'Privacy & backup', exact: true }).focus();
  await page.keyboard.press('ArrowRight');
  await expect(page.getByRole('tab', { name: 'Window', exact: true })).toBeFocused();
});

test('model archive and semantic indexing stay explicit and unavailable without native host', async ({ page }) => {
  await page.goto('/settings');
  await page.getByRole('tab', { name: 'Models / runtimes', exact: true }).click();
  await page.getByRole('button', { name: 'Import user-supplied model archive…' }).click();
  const archive = page.getByRole('dialog', { name: 'Import unreviewed model weights' });
  await expect(archive.getByRole('button', { name: 'Choose archive and import model data' })).toBeDisabled();
  await archive.getByLabel('I understand this archive is user-supplied and unreviewed').check();
  await archive.getByLabel('I reviewed the actual upstream model license').check();
  await expect(archive.getByRole('button', { name: 'Choose archive and import model data' })).toBeDisabled();
  await page.keyboard.press('Escape');
  await page.keyboard.press('Control+k');
  await page.getByRole('button', { name: 'Scope & index' }).click();
  await page.getByRole('button', { name: 'Build / rebuild semantic index…' }).click();
  const index = page.getByRole('dialog', { name: 'Explicit semantic index rebuild' });
  await expect(index.getByText('Embedding scope: ALL active meetings in this workspace.', { exact: false })).toBeVisible();
  await expect(index.getByRole('button', { name: 'Queue real semantic rebuild' })).toBeDisabled();
});

test('P27 keyboard-only: navigation, dialog focus containment and focus restoration', async ({ page }) => {
  await page.goto('/');
  await expect(page.getByRole('heading', { name: 'Meeting library' })).toBeVisible();
  const nav = page.getByRole('navigation', { name: 'Main navigation' });

  // Every primary destination is a real link reachable and activatable without a pointer.
  const settings = nav.getByRole('link', { name: 'Settings', exact: true });
  await settings.focus();
  await expect(settings).toBeFocused();
  await page.keyboard.press('Enter');
  await expect(page.getByRole('heading', { name: 'Settings', exact: true })).toBeVisible();

  // Open the dialog from the keyboard, keep Tab inside it, and restore focus to the trigger on Escape.
  const trigger = page.getByRole('button', { name: 'New meeting', exact: true });
  await trigger.focus();
  await page.keyboard.press('Enter');
  const dialog = page.getByRole('dialog', { name: 'New meeting' });
  await expect(dialog).toBeVisible();
  for (let index = 0; index < 25; index += 1) {
    await page.keyboard.press('Tab');
    expect(await dialog.evaluate((node) => node.contains(document.activeElement))).toBe(true);
  }
  for (let index = 0; index < 25; index += 1) {
    await page.keyboard.press('Shift+Tab');
    expect(await dialog.evaluate((node) => node.contains(document.activeElement))).toBe(true);
  }
  await page.keyboard.press('Escape');
  await expect(dialog).toBeHidden();
  await expect(trigger).toBeFocused();

  // Settings tabs follow the roving-tabindex arrow-key pattern, wrapping at both ends.
  const tabs = page.getByRole('tab');
  const first = tabs.first();
  await first.focus();
  await page.keyboard.press('ArrowLeft');
  await expect(tabs.last()).toBeFocused();
  await page.keyboard.press('ArrowRight');
  await expect(first).toBeFocused();
  await page.keyboard.press('End');
  await expect(tabs.last()).toBeFocused();
  await page.keyboard.press('Home');
  await expect(first).toBeFocused();

  // Every interactive control exposes an accessible name.
  const unnamed = await page.evaluate(() => Array.from(document.querySelectorAll('button, a[href], input, select, textarea, [role="tab"]'))
    .filter((node) => !(node as HTMLElement).hidden && (node as HTMLElement).offsetParent !== null)
    .filter((node) => !(node.getAttribute('aria-label') || node.getAttribute('aria-labelledby') || (node as HTMLElement).innerText?.trim() || (node as HTMLInputElement).labels?.length || node.getAttribute('title')))
    .map((node) => node.outerHTML.slice(0, 120)));
  expect(unnamed).toEqual([]);
});
