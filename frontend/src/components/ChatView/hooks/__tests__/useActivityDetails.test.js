import test from 'node:test'
import assert from 'node:assert/strict'
import useActivityDetails from '../useActivityDetails.js'
import { renderHook } from './react-hook-shim.mjs'

const flush = async () => { for (let i = 0; i < 12; i++) await Promise.resolve() }
const response = entries => ({ ok: true, json: async () => ({ entries }) })
function reader() {
  const calls = []
  const request = (url, options) => new Promise((resolve, reject) => {
    calls.push({ url, options, resolve, reject })
  })
  return { calls, request }
}

test('equivalent range plans survive rendering churn without canceling reads', async () => {
  const { calls, request } = reader()
  let ready = 0
  const props = { urls: ['/chats/a/activity-detail?start=0'], requested: true, request }
  const hook = renderHook(useActivityDetails, { ...props, onReady: () => { ready = 1 } })
  for (let i = 0; i < 40; i++) {
    hook.rerender({ ...props, urls: [...props.urls], onReady: () => { ready = 2 } })
  }
  assert.equal(calls.length, 1)
  assert.equal(calls[0].options.signal.aborted, false)
  calls[0].resolve(response([{ idx: 0, item: { type: 'tool' } }]))
  await flush()
  assert.equal(ready, 2, 'settling uses the current interaction, not a stale callback')
  assert.equal(hook.result.current.entries[0][0].idx, 0)
  hook.rerender({ ...props, requested: false })
  hook.rerender(props)
  assert.equal(calls.length, 1, 'a completed range is reused when reopened')
  hook.unmount()
})

test('closed detail stays network-free and a canceled open can be reopened', async () => {
  const { calls, request } = reader()
  const props = { urls: ['/range'], requested: false, request }
  const hook = renderHook(useActivityDetails, props)
  assert.equal(calls.length, 0)
  hook.rerender({ ...props, requested: true })
  hook.rerender(props)
  assert.equal(calls[0].options.signal.aborted, true)
  calls[0].resolve(response(['late']))
  await flush()
  assert.equal(hook.result.current.entries, null)
  hook.rerender({ ...props, requested: true })
  assert.equal(calls.length, 2)
  hook.unmount()
  assert.equal(calls[1].options.signal.aborted, true)
})

test('changed chat or range rejects stale results, even with identical array lengths', async () => {
  const { calls, request } = reader()
  const props = { urls: ['/chats/a/range'], requested: true, request }
  const hook = renderHook(useActivityDetails, props)
  hook.rerender({ ...props, urls: ['/chats/b/range'] })
  assert.equal(calls[0].options.signal.aborted, true)
  calls[0].resolve(response(['old']))
  await flush()
  assert.equal(hook.result.current.entries, null)
  calls[1].resolve(response(['new']))
  await flush()
  assert.deepEqual(hook.result.current.entries, [['new']])
  hook.rerender({ ...props, urls: ['/chats/b/another-range'] })
  assert.equal(hook.result.current.entries, null, 'no one-frame ready state from old range')
  hook.unmount()
})

test('composite plans retain raw slot order and inline slots without extra requests', async () => {
  const { calls, request } = reader()
  const hook = renderHook(useActivityDetails, {
    urls: ['/first', null, '/last'], requested: true, request,
  })
  assert.equal(calls.length, 2)
  calls[1].resolve(response(['last']))
  await flush()
  assert.equal(hook.result.current.entries, null, 'a partial timeline is never ready')
  calls[0].resolve(response(['first']))
  await flush()
  assert.deepEqual(hook.result.current.entries, [['first'], null, ['last']])
  hook.unmount()
})

test('terminal errors release layout readiness and retry is explicit, not render-driven', async () => {
  const { calls, request } = reader()
  let ready = 0
  const props = { urls: ['/range'], requested: true, request, onReady: () => { ready++ } }
  const hook = renderHook(useActivityDetails, props)
  calls[0].reject(Object.assign(new Error('timed out'), { name: 'TimeoutError' }))
  await flush()
  assert.equal(hook.result.current.error, true)
  assert.equal(ready, 1)
  hook.rerender({ ...props, urls: ['/range'] })
  assert.equal(calls.length, 1)
  hook.rerender({ ...props, attempt: 1 })
  assert.equal(hook.result.current.error, false)
  assert.equal(calls.length, 2)
  calls[1].resolve(response([]))
  await flush()
  assert.deepEqual(hook.result.current.entries, [[]])
  assert.equal(ready, 2)
  hook.unmount()
})

// Use the real wrapper: headers resolve immediately but body consumption
// remains bound to the exact signal fetch received, like a browser response.
function stalledBodyFetch(captures, abortName) {
  return async (_url, { signal }) => {
    const capture = { signal, started: false, aborted: false }
    captures.push(capture)
    return {
      ok: true,
      json: () => new Promise((_resolve, reject) => {
        capture.started = true
        signal.addEventListener('abort', () => {
          capture.aborted = true
          reject(abortName ? new DOMException('aborted', abortName) : signal.reason)
        }, { once: true })
      }),
    }
  }
}

for (const abortName of [undefined, 'AbortError']) {
  test(`the complete detail body read is bounded after headers (${abortName || 'signal reason'})`, async t => {
    t.mock.timers.enable({ apis: ['setTimeout'] })
    const originalFetch = globalThis.fetch
    t.after(() => { globalThis.fetch = originalFetch })
    const captures = []
    globalThis.fetch = stalledBodyFetch(captures, abortName)
    let ready = 0
    const props = { urls: ['/chats/stalled/activity-detail'], requested: true, onReady: () => { ready++ } }
    const hook = renderHook(useActivityDetails, props)
    t.after(() => hook.unmount())
    await flush()
    assert.equal(captures[0].started, true, 'the wrapper already returned headers')
    t.mock.timers.tick(10_000)
    hook.rerender({ ...props, urls: [...props.urls] })
    t.mock.timers.tick(4_999)
    await flush()
    assert.equal(hook.result.current.error, false)
    t.mock.timers.tick(1)
    await flush()
    assert.equal(captures[0].aborted, true)
    assert.equal(hook.result.current.error, true, 'a deadline is terminal even if fetch names it AbortError')
    assert.equal(ready, 1)
    assert.equal(captures.length, 1, 'render churn never restarts the deadline or request')
  })
}

for (const boundary of ['close', 'unmount']) {
  test(`${boundary} cancels body consumption after headers without revealing stale detail`, async t => {
    t.mock.timers.enable({ apis: ['setTimeout'] })
    const originalFetch = globalThis.fetch
    t.after(() => { globalThis.fetch = originalFetch })
    const captures = []
    globalThis.fetch = stalledBodyFetch(captures)
    let ready = 0
    const props = { urls: ['/chats/stalled/activity-detail'], requested: true, onReady: () => { ready++ } }
    const hook = renderHook(useActivityDetails, props)
    t.after(() => hook.unmount())
    await flush()
    if (boundary === 'close') hook.rerender({ ...props, requested: false })
    else hook.unmount()
    await flush()
    assert.equal(captures[0].aborted, true)
    assert.equal(ready, 0, 'lifecycle cancellation cannot reveal or create an error state')
    assert.equal(hook.result.current.error, false)
    t.mock.timers.tick(15_000)
    await flush()
    assert.equal(ready, 0)
  })
}
