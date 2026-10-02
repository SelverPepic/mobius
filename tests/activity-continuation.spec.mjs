/** Internal helper deliveries never split one continuous live activity stretch. */
import { test } from '@playwright/test'
import { attachCleanup, createTaggedChat } from './_chatTracker.mjs'
import { checkActivityContinuation } from './_activityContinuationFixture.mjs'
const BASE = process.env.MOBIUS_URL || 'http://localhost:8001'
test.use({ serviceWorkers: 'block' })
attachCleanup()
for (const compact of [false, true]) {
  test(`continuous ${compact ? 'compact' : 'raw'} activity survives hidden cuts and saved handoff`, async ({ page }) => {
    await page.goto(BASE, { waitUntil: 'domcontentloaded' })
    const chat = await createTaggedChat(page, 'activity-continuity')
    await checkActivityContinuation(page, chat, BASE, { compact })
  })
}
