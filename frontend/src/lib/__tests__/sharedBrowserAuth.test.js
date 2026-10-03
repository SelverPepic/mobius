/* Guest bearer renewal never reaches the owner's token or persisted cache. */
import test from 'node:test'
import assert from 'node:assert/strict'

function lockManager() {
  const tails = new Map()
  return {
    request(name, _options, operation) {
      const previous = tails.get(name) || Promise.resolve()
      const result = previous.then(operation, operation)
      tails.set(name, result.catch(() => {}))
      return result
    },
  }
}
const navigatorForTests = globalThis.navigator || {}
Object.defineProperty(globalThis, 'navigator', { configurable: true, value: navigatorForTests })
Object.defineProperty(navigatorForTests, 'locks', {
  configurable: true, writable: true, value: lockManager(),
})

test('redeem, one 401 renewal, and failed renewal stay memory-only', async () => {
  const owner = new Map([['token', 'owner-bearer']])
  const ownerAccesses = []
  globalThis.localStorage = {
    getItem: key => { ownerAccesses.push(['read', key]); return owner.get(key) ?? null },
    setItem: (key, value) => { ownerAccesses.push(['write', key]); owner.set(key, value) },
    removeItem: key => { ownerAccesses.push(['delete', key]); owner.delete(key) },
  }
  globalThis.window = { dispatchEvent() {} }
  globalThis.location = { pathname: '/shell/shared' }
  const calls = []
  let protectedCalls = 0
  let failRenewal = false
  globalThis.fetch = async (url, options) => {
    calls.push({ url, options })
    if (url.endsWith('/browser-access/session/redeem')) {
      assert.equal(options.credentials, 'same-origin')
      assert.deepEqual(JSON.parse(options.body), { invite: 'one-use-secret' })
      assert.equal(options.headers.Authorization, undefined)
      return new Response(JSON.stringify({ access_token: 'guest-1', token_type: 'bearer', grant: { id: 'g1', label: 'Friend' }, expires_in: 900 }), { status: 200 })
    }
    if (url.endsWith('/browser-access/session')) {
      assert.equal(options.headers.Authorization, undefined)
      if (failRenewal) return new Response('', { status: 401 })
      return new Response(JSON.stringify({ access_token: 'guest-2', token_type: 'bearer', grant: { id: 'g1', label: 'Friend' }, expires_in: 900 }), { status: 200 })
    }
    if (url.endsWith('/api/chats?shared_browser=1')) {
      protectedCalls += 1
      assert.equal(options.headers.Authorization, `Bearer guest-${protectedCalls}`)
      return new Response('', { status: protectedCalls === 1 ? 401 : 200 })
    }
    assert.fail(`unexpected URL ${url}`)
  }
  const client = await import('../../api/client.js')
  client.beginSharedBrowserAuth()
  await client.redeemSharedBrowserInvite('one-use-secret')
  assert.equal(client.getToken(), 'guest-1')
  assert.equal((await client.apiFetch('/chats')).status, 200)
  assert.equal(client.getToken(), 'guest-2')
  failRenewal = true
  await assert.rejects(client.renewSharedBrowserSession(), /SHARED_ACCESS_ENDED/)
  assert.equal(client.getToken(), null)
  assert.equal(owner.get('token'), 'owner-bearer')
  assert.deepEqual(ownerAccesses, [])
  assert.equal(calls.filter(call => call.url.endsWith('/browser-access/session')).length, 2)
})

test('renewal cannot silently switch an active tab to another grant', async () => {
  globalThis.location = { pathname: '/shell/shared' }
  globalThis.window = { dispatchEvent() {} }
  const owner = new Map([['token', 'owner-bearer']])
  globalThis.localStorage = {
    getItem: key => owner.get(key) ?? null,
    setItem: () => assert.fail('owner write'),
    removeItem: () => assert.fail('owner delete'),
  }
  globalThis.fetch = async url => {
    const grant = url.endsWith('/redeem') ? 'grant-A' : 'grant-B'
    return new Response(JSON.stringify({
      access_token: `token-${grant}`, token_type: 'bearer',
      grant: { id: grant, label: grant }, expires_in: 900,
    }), { status: 200 })
  }
  const client = await import('../../api/client.js')
  client.beginSharedBrowserAuth()
  await client.redeemSharedBrowserInvite('grant-A-invite')
  await assert.rejects(client.renewSharedBrowserSession(), /SHARED_ACCESS_GRANT_CHANGED/)
  assert.equal(client.getToken(), null)
  assert.equal(owner.get('token'), 'owner-bearer')
})

test('leave targets the current grant and reports a cookie mismatch for retry', async () => {
  globalThis.location = { pathname: '/shell/shared' }
  globalThis.window = { dispatchEvent() {} }
  let logoutAttempts = 0
  globalThis.fetch = async (url, options) => {
    if (url.endsWith('/redeem')) return new Response(JSON.stringify({
      access_token: 'grant-A-token', token_type: 'bearer',
      grant: { id: 'grant-A', label: 'A' }, expires_in: 900,
    }), { status: 200 })
    assert.ok(url.endsWith('/session/logout'))
    assert.deepEqual(JSON.parse(options.body), { grant_id: 'grant-A' })
    assert.equal(options.credentials, 'same-origin')
    assert.equal(options.headers.Authorization, undefined)
    logoutAttempts += 1
    return new Response(null, { status: logoutAttempts === 1 ? 409 : 204 })
  }
  const client = await import('../../api/client.js')
  await client.redeemSharedBrowserInvite('another-invite')
  await assert.rejects(client.leaveSharedBrowserSession(), /SHARED_ACCESS_LOGOUT_FAILED/)
  assert.equal(client.getToken(), null)
  assert.equal(await client.leaveSharedBrowserSession(), true)
  assert.equal(logoutAttempts, 2)
})

test('theme reads use one-use cache keys so an older SW cannot replay a revoked session', async () => {
  globalThis.location = { pathname: '/shell/shared' }
  globalThis.window = { dispatchEvent() {} }
  const themeUrls = []
  globalThis.fetch = async url => {
    if (url.endsWith('/session/redeem')) return new Response(JSON.stringify({
      access_token: 'guest-token', token_type: 'bearer',
      grant: { id: 'grant-theme', label: 'Theme' }, expires_in: 900,
    }), { status: 200 })
    if (url.includes('/api/theme?shared_browser=')) {
      themeUrls.push(url)
      return new Response('{}', { status: 200 })
    }
    assert.fail(`unexpected URL ${url}`)
  }
  const client = await import('../../api/client.js')
  await client.redeemSharedBrowserInvite('theme-invite')
  await client.apiFetch('/theme')
  await client.apiFetch('/theme')
  assert.equal(themeUrls.length, 2)
  assert.notEqual(themeUrls[0], themeUrls[1])
})

test('cookie mutations serialize; failed A renewal cannot clear queued B redemption', async () => {
  globalThis.location = { pathname: '/shell/shared' }
  globalThis.window = { dispatchEvent() {} }
  const client = await import('../../api/client.js')
  client.beginSharedBrowserAuth()
  let releaseRenew
  let renewStarted
  const started = new Promise(resolve => { renewStarted = resolve })
  const waiting = new Promise(resolve => { releaseRenew = resolve })
  const calls = []
  globalThis.fetch = async url => {
    calls.push(url)
    if (url.endsWith('/session')) {
      renewStarted()
      await waiting
      return new Response('', { status: 401 })
    }
    return new Response(JSON.stringify({
      access_token: url.endsWith('/redeem') && calls.filter(call => call.endsWith('/redeem')).length === 1 ? 'A' : 'B',
      token_type: 'bearer', grant: { id: calls.filter(call => call.endsWith('/redeem')).length === 1 ? 'A' : 'B' }, expires_in: 900,
    }), { status: 200 })
  }
  await client.redeemSharedBrowserInvite('A')
  const renewal = client.renewSharedBrowserSession()
  await started
  const redeemB = client.redeemSharedBrowserInvite('B')
  assert.equal(calls.filter(call => call.endsWith('/redeem')).length, 1)
  releaseRenew()
  await assert.rejects(renewal, /SHARED_ACCESS_ENDED/)
  await redeemB
  assert.equal(client.getToken(), 'B')
})

test('A mutation and stale success never cross into B authority', async () => {
  globalThis.location = { pathname: '/shell/shared' }
  globalThis.window = { dispatchEvent() {} }
  const client = await import('../../api/client.js')
  client.beginSharedBrowserAuth()
  let releaseA
  let aStarted
  const started = new Promise(resolve => { aStarted = resolve })
  const waiting = new Promise(resolve => { releaseA = resolve })
  let redeems = 0
  const mutations = []
  globalThis.fetch = async (url, options) => {
    if (url.endsWith('/redeem')) {
      redeems += 1
      return new Response(JSON.stringify({ access_token: redeems === 1 ? 'A' : 'B', token_type: 'bearer', grant: { id: redeems === 1 ? 'A' : 'B' }, expires_in: 900 }), { status: 200 })
    }
    if (url.endsWith('/api/chats')) {
      mutations.push(options.headers.Authorization)
      aStarted()
      await waiting
      return new Response('', { status: 401 })
    }
    assert.fail(`unexpected ${url}`)
  }
  await client.redeemSharedBrowserInvite('A')
  const oldMutation = client.apiFetch('/chats', { method: 'POST', body: '{}' })
  await started
  await client.redeemSharedBrowserInvite('B')
  releaseA()
  await assert.rejects(oldMutation, /SHARED_ACCESS_SUPERSEDED/)
  assert.deepEqual(mutations, ['Bearer A'])
  assert.equal(client.getToken(), 'B')
})

test('successful old-grant response is rejected after B redeem', async () => {
  globalThis.location = { pathname: '/shell/shared' }
  globalThis.window = { dispatchEvent() {} }
  const client = await import('../../api/client.js')
  client.beginSharedBrowserAuth()
  let releaseOld
  let oldStarted
  const started = new Promise(resolve => { oldStarted = resolve })
  const waiting = new Promise(resolve => { releaseOld = resolve })
  let redeems = 0
  globalThis.fetch = async url => {
    if (url.endsWith('/redeem')) {
      redeems += 1
      const grant = redeems === 1 ? 'A' : 'B'
      return new Response(JSON.stringify({ access_token: grant, token_type: 'bearer', grant: { id: grant }, expires_in: 900 }), { status: 200 })
    }
    oldStarted()
    await waiting
    return new Response('{}', { status: 200 })
  }
  await client.redeemSharedBrowserInvite('A')
  const oldRead = client.apiFetch('/old')
  await started
  await client.redeemSharedBrowserInvite('B')
  releaseOld()
  await assert.rejects(oldRead, /SHARED_ACCESS_SUPERSEDED/)
  assert.equal(client.getToken(), 'B')
})

test('obsolete successful redeem is logged out before failed next invite can renew it', async () => {
  globalThis.location = { pathname: '/shell/shared' }
  globalThis.window = { dispatchEvent() {} }
  const client = await import('../../api/client.js')
  client.beginSharedBrowserAuth()
  client.clearSharedBrowserSession()
  client.beginSharedBrowserAuth()
  let cookie = null
  let releaseA
  let aStarted
  const started = new Promise(resolve => { aStarted = resolve })
  const waiting = new Promise(resolve => { releaseA = resolve })
  const calls = []
  globalThis.fetch = async (url, options) => {
    if (url.endsWith('/session/redeem')) {
      const invite = JSON.parse(options.body).invite
      calls.push(`redeem:${invite}`)
      if (invite === 'B') return new Response('', { status: 401 })
      aStarted()
      await waiting
      cookie = 'A'
      return new Response(JSON.stringify({ access_token: 'A-token', token_type: 'bearer', grant: { id: 'A' }, expires_in: 900 }), { status: 200 })
    }
    if (url.endsWith('/session/logout')) {
      calls.push('logout:A')
      assert.deepEqual(JSON.parse(options.body), { grant_id: 'A' })
      assert.equal(cookie, 'A')
      cookie = null
      return new Response(null, { status: 204 })
    }
    if (url.endsWith('/session')) {
      calls.push('renew')
      return new Response('', { status: cookie ? 200 : 401 })
    }
    assert.fail(`unexpected URL ${url}`)
  }
  const old = client.redeemSharedBrowserInvite('A')
  const oldRejection = assert.rejects(old, /SHARED_ACCESS_SUPERSEDED/)
  await started
  const next = client.redeemSharedBrowserInvite('B')
  assert.deepEqual(calls, ['redeem:A'])
  releaseA()
  await oldRejection
  await assert.rejects(next, /SHARED_ACCESS_ENDED/)
  assert.deepEqual(calls, ['redeem:A', 'logout:A', 'redeem:B'])
  assert.equal(client.getToken(), null)
  assert.equal(cookie, null)
  await assert.rejects(client.renewSharedBrowserSession(), /SHARED_ACCESS_ENDED/)
  assert.equal(client.getToken(), null)
})

test('another tab holds the origin cookie lock until its response is consumed', async () => {
  globalThis.location = { pathname: '/shell/shared' }
  globalThis.window = { dispatchEvent() {} }
  const client = await import('../../api/client.js')
  client.beginSharedBrowserAuth()
  const locks = lockManager()
  navigatorForTests.locks = locks
  let releaseOtherTab
  let otherTabStarted
  const started = new Promise(resolve => { otherTabStarted = resolve })
  const held = new Promise(resolve => { releaseOtherTab = resolve })
  const external = locks.request('mobius:shared-browser-session-cookie:v1', { mode: 'exclusive' }, async () => {
    otherTabStarted()
    await held
  })
  await started
  const calls = []
  globalThis.fetch = async url => {
    calls.push(url)
    return new Response(JSON.stringify({ access_token: 'cross-tab', token_type: 'bearer', grant: { id: 'cross-tab' }, expires_in: 900 }), { status: 200 })
  }
  const redeem = client.redeemSharedBrowserInvite('cross-tab-invite')
  await Promise.resolve()
  assert.deepEqual(calls, [])
  releaseOtherTab()
  await external
  await redeem
  assert.equal(calls.length, 1)
})

test('missing Web Locks rejects before any cookie request', async () => {
  globalThis.location = { pathname: '/shell/shared' }
  globalThis.window = { dispatchEvent() {} }
  const client = await import('../../api/client.js')
  client.beginSharedBrowserAuth()
  const savedLocks = navigatorForTests.locks
  navigatorForTests.locks = undefined
  let calls = 0
  globalThis.fetch = async () => { calls += 1; assert.fail('cookie request sent without lock') }
  try {
    await assert.rejects(client.redeemSharedBrowserInvite('unsupported'), /SHARED_ACCESS_BROWSER_UNSUPPORTED/)
    await assert.rejects(client.renewSharedBrowserSession(), /SHARED_ACCESS_BROWSER_UNSUPPORTED/)
    assert.equal(calls, 0)
  } finally {
    navigatorForTests.locks = savedLocks
  }
})

test('hash clear invalidates pending redeem even without a newer acceptance', async () => {
  globalThis.location = { pathname: '/shell/shared' }
  globalThis.window = { dispatchEvent() {} }
  const client = await import('../../api/client.js')
  client.beginSharedBrowserAuth()
  client.clearSharedBrowserSession()
  client.beginSharedBrowserAuth()
  let releaseA
  let aStarted
  const started = new Promise(resolve => { aStarted = resolve })
  const waiting = new Promise(resolve => { releaseA = resolve })
  const calls = []
  globalThis.fetch = async (url, options) => {
    if (url.endsWith('/redeem')) {
      calls.push('redeem:A')
      aStarted()
      await waiting
      return new Response(JSON.stringify({ access_token: 'A', token_type: 'bearer', grant: { id: 'A' }, expires_in: 900 }), { status: 200 })
    }
    if (url.endsWith('/logout')) {
      calls.push('logout:A')
      assert.deepEqual(JSON.parse(options.body), { grant_id: 'A' })
      return new Response(null, { status: 204 })
    }
    assert.fail(`unexpected ${url}`)
  }
  const old = client.redeemSharedBrowserInvite('A')
  const rejected = assert.rejects(old, /SHARED_ACCESS_SUPERSEDED/)
  await started
  client.clearSharedBrowserSession() // New #invite arrived; B not accepted yet.
  releaseA()
  await rejected
  assert.deepEqual(calls, ['redeem:A', 'logout:A'])
  assert.equal(client.getToken(), null)
})

test('account finalization uses the same cookie lock and never accepts a superseded account', async () => {
  const client = await import('../../api/client.js')
  client.beginSharedBrowserAuth()
  const locks = lockManager()
  let lockEntries = 0
  navigatorForTests.locks = { request(...args) { lockEntries++; return locks.request(...args) } }
  const calls = []
  let finishAccount
  let started
  const ready = new Promise(resolve => { started = resolve })
  globalThis.fetch = async (url, options) => {
    calls.push(url)
    if (url.endsWith('/session/account/finalize')) {
      assert.equal(options.credentials, 'same-origin')
      assert.deepEqual(JSON.parse(options.body), { pending_id: 'specific-account-flow' })
      assert.equal(options.headers.Authorization, undefined)
      started()
      return new Promise(resolve => { finishAccount = resolve })
    }
    assert.ok(url.endsWith('/session/logout'))
    assert.deepEqual(JSON.parse(options.body), { grant_id: 'account-A' })
    return new Response(null, { status: 204 })
  }
  const pending = client.finalizeSharedBrowserAccount('specific-account-flow')
  assert.equal(client.finalizeSharedBrowserAccount('specific-account-flow'), pending)
  await ready
  client.clearSharedBrowserSession()
  finishAccount(new Response(JSON.stringify({ access_token: 'must-not-install', token_type: 'bearer', grant: { id: 'account-A' }, expires_in: 900 }), { status: 200 }))
  await assert.rejects(pending, /SHARED_ACCESS_SUPERSEDED/)
  assert.equal(lockEntries, 1)
  assert.equal(calls.length, 2)
  assert.equal(client.getToken(), null)
})
