/* Guest bearer renewal never reaches the owner's token or persisted cache. */
import test from 'node:test'
import assert from 'node:assert/strict'

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
