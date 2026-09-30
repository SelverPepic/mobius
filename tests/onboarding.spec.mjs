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

test('an app is installed only after its access is shown in the Apps slide', async ({ page }) => {
  let previews = 0
  let installs = 0
  await page.route(/\/api\/apps\/$/, async route => {
    const response = await route.fetch()
    const apps = (await response.json()).filter(app => app.slug !== 'notes')
    await route.fulfill({ response, body: JSON.stringify(apps) })
  })
  await page.route(/\/api\/apps\/\d+\/source\/file\?path=catalog\.json$/, route => route.fulfill({ status: 503 }))
  await page.route(/\/api\/proxy\?/, route => {
    const source = new URL(route.request().url()).searchParams.get('url')
    if (source?.endsWith('/catalog.json')) return route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({ schema: 1, apps: [{ id: 'notes', name: 'Notes', description: 'Write notes.', collection: 'Everyday', manifest_url: 'https://example.com/notes/mobius.json', raw_base: 'https://example.com/notes/' }] }),
    })
    if (source?.endsWith('/notes/mobius.json')) return route.fulfill({ status: 200, contentType: 'application/json', body: '{"id":"notes"}' })
    return route.continue()
  })
  await page.route(/\/api\/apps\/preview$/, route => {
    previews += 1
    if (previews === 1) return route.fulfill({ status: 503, contentType: 'application/json', body: '{"detail":"App source unavailable"}' })
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({
      manifest: { id: 'notes' }, capability_digest: 'reviewed-notes-access',
      capability_contract: { agent: { skills: [] }, data: { github_access: true }, runtime: {} },
      installed_contract: null,
    }) })
  })
  await page.route(/\/api\/apps\/install$/, route => {
    installs += 1
    expect(route.request().postDataJSON().reviewed_capability_digest).toBe(installs === 1 ? 'reviewed-notes-access' : 'changed-notes-access')
    if (installs === 1) return route.fulfill({ status: 409, contentType: 'application/json', body: JSON.stringify({ detail: {
      code: 'capability_changed', manifest: { id: 'notes' }, capability_digest: 'changed-notes-access',
      capability_contract: { agent: { skills: [] }, data: { shared_memory: 'read' }, runtime: {} },
    } }) })
    return route.fulfill({ status: 201, contentType: 'application/json', body: '{"id":42}' })
  })

  const { guide } = await openGuide(page)
  for (let step = 0; step < 3; step += 1) await guide.getByRole('button', { name: 'Next' }).click()
  await expect(guide.getByRole('heading', { name: 'Explore apps' })).toBeVisible()
  await guide.getByRole('button', { name: 'Install Notes' }).click()
  const access = guide.getByRole('region', { name: 'Notes requested access' })
  await expect(access.getByRole('alert')).toContainText('App source unavailable')
  expect(installs).toBe(0)
  await access.getByRole('button', { name: 'Retry check' }).click()
  await expect(access).toContainText('GitHub data')
  await expect(guide.getByRole('button', { name: 'Cancel Notes' })).toBeFocused()
  expect(installs).toBe(0)
  await access.getByRole('button', { name: 'Install app' }).click()
  await expect(access.getByRole('alert')).toContainText('Nothing was installed')
  await expect(access).toContainText('Shared memory')
  expect(installs).toBe(1)
  await access.getByRole('button', { name: 'Install app' }).click()
  await expect(guide.getByRole('button', { name: 'Installed Notes' })).toBeDisabled()
  await expect(guide.getByRole('button', { name: 'Installed Notes' })).toBeFocused()
  expect(installs).toBe(2)
})
