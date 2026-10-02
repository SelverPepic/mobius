/** Provider-free live cuts exercise the real activity renderer without sending messages. */
import { expect } from '@playwright/test'
import { runtimeSnapshot, testChatAgentSettings, mockDeliveryReady } from './_chatTestPrerequisites.mjs'

export async function checkActivityContinuation(page, chat, base, { compact = false } = {}) {
  const root = `rt-activity-${compact ? 'compact' : 'raw'}`
  const firstId = `${root}:assistant:1`, nextId = `${root}:assistant:2`
  const tool = (id, status = 'done') => ({ type: 'tool', tool: 'Bash', input: `echo ${id}`, output: `${id} output`, tool_use_id: id, status })
  const originals = [tool('inspect'), tool('test')]
  const saved = compact ? [{ type: 'activity', activity_id: 'activity-saved', start: 0, end: 2,
    message_index: 1, tool_count: 2, entries: originals.map((item, idx) => ({ item, idx })) }] : originals
  const carrier = { role: 'user', hidden: true, steered: true, kind: 'delegation_result',
    source_work_id: 'logical-goal-root', cid: 'helper-cut', content: 'Hidden result', ts: 3 }
  let messages = [{ role: 'user', cid: 'activity-request', content: 'Verify continuous activity', ts: 1 },
    { role: 'assistant', id: firstId, content: '', blocks: saved, ts: 2 }]
  let runtime = runtimeSnapshot({ running: true, run_status: 'running', run_id: root,
    active_assistant_message_id: firstId, runtime_revision: 10000000 })
  if (compact) {
    messages.push(carrier)
    runtime.active_assistant_message_id = nextId
  }
  let detailReads = 0
  await mockDeliveryReady(page)
  await page.route(new RegExp(`/api/chats/${chat.id}(?:\\?.*)?$`), route => route.fulfill({ json: {
    ...chat, provider: 'codex', ...testChatAgentSettings(), ...runtime, messages, total: messages.length, offset: 0,
  } }))
  await page.route(`**/api/chats/${chat.id}/runtime`, route => route.fulfill({ json: runtime }))
  await page.route(`**/api/chats/${chat.id}/activity*`, route => {
    if (route.request().url().includes('/activity-detail')) {
      detailReads++
      return route.fulfill({ json: { entries: originals.map((item, idx) => ({ item, idx })) } })
    }
    return route.fulfill({ json: { events: [], next_before: null } })
  })
  await page.route(`**/api/chats/${chat.id}/messages`, route => route.abort())
  await page.addInitScript(({ chatId, firstId, originals }) => {
    const nativeFetch = window.fetch.bind(window)
    window.fetch = (input, options) => {
      const url = typeof input === 'string' ? input : input.url
      if (!url.includes(`/api/chats/${chatId}/stream`)) return nativeFetch(input, options)
      return Promise.resolve(new Response(new ReadableStream({ start(controller) {
        const emit = event => controller.enqueue(new TextEncoder().encode(`data: ${JSON.stringify(event)}\n\n`))
        window.__emitActivityFixture = emit
        emit({ type: 'stream_snapshot', assistant_message_id: firstId, items: originals })
        emit({ type: 'catch_up_done' })
      } }), { headers: { 'Content-Type': 'text/event-stream' } }))
    }
  }, { chatId: chat.id, firstId: compact ? nextId : firstId, originals: compact ? [] : originals })
  await page.goto(`${base}/shell/?chat=${chat.id}`, { waitUntil: 'domcontentloaded' })
  await page.waitForFunction(() => typeof window.__emitActivityFixture === 'function')
  const surface = page.locator('[data-chat-surface="painted"]')
  const headers = surface.locator('.chat__tools > .chat__activity > .chat__activity-header[aria-expanded]')
  await expect(headers).toHaveCount(1)
  if (await headers.getAttribute('aria-expanded') !== 'true') await headers.click()
  await expect(headers).toHaveAttribute('aria-expanded', 'true')
  await headers.evaluate(element => { window.__activityHeader = element })
  if (!compact) messages = [...messages, carrier]
  runtime = { ...runtime, active_assistant_message_id: nextId, runtime_revision: runtime.runtime_revision + 1 }
  if (!compact) await page.evaluate(({ firstId, nextId, originals, carrier }) => window.__emitActivityFixture({
    type: 'steered_into_turn', assistant_message_id: firstId, next_assistant_message_id: nextId,
    sealed_items: originals, items: [], messages: [carrier], ts: carrier.ts,
  }), { firstId, nextId, originals, carrier })
  const current = [tool('continue', 'running')]
  await page.evaluate(current => {
    for (const item of current) window.__emitActivityFixture({ ...item, type: 'tool_start' })
  }, current)
  await expect(headers).toHaveCount(1)
  await expect(headers).toHaveAttribute('aria-expanded', 'true')
  await expect(headers).toHaveAttribute('aria-label', /in progress/)
  await expect(surface.getByText(/echo continue/, { exact: false }).first()).toBeVisible()
  expect(await headers.evaluate(element => element === window.__activityHeader)).toBe(true)
  // A saved partial is older than the selected stream. Returning to saved
  // source must not hide or duplicate the current tool on the way to done.
  const readsBeforeAppend = detailReads
  const finalTools = [tool('continue'), tool('verify')]
  messages = [...messages, { role: 'assistant', id: nextId, content: '', blocks: finalTools, ts: 4 }]
  await page.evaluate(() => {
    window.__emitActivityFixture({ type: 'tool_end', tool_use_id: 'continue' })
    window.__emitActivityFixture({ type: 'tool_start', tool: 'Bash', input: 'echo verify', tool_use_id: 'verify' })
    window.__emitActivityFixture({ type: 'tool_end', tool_use_id: 'verify' })
  })
  await expect(headers).toHaveCount(1)
  await expect(headers).toHaveAttribute('aria-expanded', 'true')
  expect(await headers.evaluate(element => element === window.__activityHeader)).toBe(true)
  expect(detailReads).toBe(readsBeforeAppend)
  await page.evaluate(() => window.__emitActivityFixture({ type: 'text', content: 'Verified, with every step preserved.', text_item_id: 'result' }))
  await expect(surface.getByText('Verified, with every step preserved.', { exact: true })).toBeVisible()
  await expect(headers).not.toHaveAttribute('aria-label', /in progress/)
  expect(detailReads).toBe(readsBeforeAppend)
  messages.at(-1).blocks.push({ type: 'text', content: 'Verified, with every step preserved.' })
  runtime = { ...runtime, running: false, run_status: 'completed', runtime_revision: runtime.runtime_revision + 1 }
  await page.evaluate(() => window.__emitActivityFixture({ type: 'done' }))
  await expect(headers).toHaveCount(1)
  await expect(headers).toHaveAttribute('aria-expanded', 'true')
  await page.reload({ waitUntil: 'domcontentloaded' })
  await expect(headers).toHaveCount(1)
  await expect(headers).not.toHaveAttribute('aria-label', /loading details/)
  if (await headers.getAttribute('aria-expanded') !== 'true') await headers.click()
  await expect(headers).toHaveAttribute('aria-expanded', 'true')
  await expect(surface.getByText(/echo verify/, { exact: false }).first()).toBeVisible()
  return { headers, surface, detailReads }
}
