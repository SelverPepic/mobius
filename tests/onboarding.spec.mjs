import { test, expect } from '@playwright/test'

const BASE = process.env.MOBIUS_URL || 'http://localhost:8001'

// Keep the browser's first-run requests inside this isolated test case.
test.use({ serviceWorkers: 'block' })

async function openGuide(page) {
  let completed = false
  let completions = 0

  await page.addInitScript(() => {
    if (sessionStorage.getItem('onboarding-test-started')) return
    localStorage.removeItem('mobius:walkthrough-completed')
    sessionStorage.setItem('onboarding-test-started', '1')
  })
  await page.route(/\/api\/owner\/walkthrough$/, route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ completed, completed_at: completed ? new Date().toISOString() : null }),
  }))
  await page.route(/\/api\/owner\/walkthrough\/complete$/, route => {
    completions += 1
    completed = true
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ completed: true, completed_at: new Date().toISOString() }),
    })
  })
  await page.route(/\/api\/auth\/providers\/status$/, route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: '{}',
  }))

  await page.goto(`${BASE}/shell/`)
  const guide = page.locator('dialog.wt__card')
  await expect(guide).toBeVisible()
  return { guide, completionCount: () => completions }
}

test('a new owner can move through the guide and finish it', async ({ page }) => {
  const { guide, completionCount } = await openGuide(page)
  await expect(guide.getByRole('heading', { name: 'Welcome to Möbius' })).toBeVisible()
  await expect(guide).toHaveJSProperty('open', true)
  await expect(guide).toHaveAttribute('aria-modal', 'true')

  await guide.getByRole('button', { name: 'Next' }).click()
  await expect(guide.getByRole('heading', { name: 'Connect an agent' })).toBeFocused()
  await expect(guide.getByRole('button', { name: 'OpenAI Codex' })).toBeVisible()
  await page.keyboard.press('Tab')
  await expect.poll(() => page.evaluate(() =>
    document.querySelector('dialog.wt__card')?.contains(document.activeElement),
  )).toBe(true)

  await guide.getByRole('button', { name: 'Back' }).click()
  await expect(guide.getByRole('heading', { name: 'Welcome to Möbius' })).toBeVisible()

  for (const title of ['Connect an agent', 'Chat and Projects', 'Explore apps', 'Your settings', 'Choose a handle']) {
    await guide.getByRole('button', { name: 'Next' }).click()
    await expect(guide.getByRole('heading', { name: title })).toBeVisible()
  }

  await guide.getByRole('button', { name: 'Finish guide' }).click()
  await expect(guide).toHaveCount(0)
  await expect.poll(completionCount).toBe(1)

  const refreshedStatus = page.waitForResponse(response =>
    new URL(response.url()).pathname === '/api/owner/walkthrough',
  )
  await page.reload()
  await refreshedStatus
  await expect(page.locator('.shell')).toBeVisible()
  await expect(guide).toHaveCount(0)
})

test('mobile guide content scrolls while its Next action remains usable', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 700 })
  const { guide } = await openGuide(page)
  for (let step = 0; step < 3; step += 1) {
    await guide.getByRole('button', { name: 'Next' }).click()
  }
  await expect(guide.getByRole('heading', { name: 'Explore apps' })).toBeVisible()

  const content = guide.locator('.wt__slide')
  await expect.poll(() => content.evaluate(element => element.scrollHeight > element.clientHeight)).toBe(true)
  await content.evaluate(element => { element.scrollTop = element.scrollHeight })
  await expect(guide.getByRole('button', { name: 'Next' })).toBeInViewport()

  await guide.getByRole('button', { name: 'Next' }).click()
  await expect(guide.getByRole('heading', { name: 'Your settings' })).toBeVisible()
})
