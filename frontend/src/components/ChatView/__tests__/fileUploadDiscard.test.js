import test from 'node:test'
import assert from 'node:assert/strict'
import { renderHook } from '../hooks/__tests__/react-hook-shim.mjs'
import useFileUpload from '../useFileUpload.js'
import { persistComposerDraft, readComposerDraft } from '../composerDraft.js'

function setup(t, initialFiles = []) {
  const calls = []
  t.mock.method(globalThis, 'fetch', (url, options) => {
    if (options.method === 'DELETE') {
      calls.push({ url, options })
      return Promise.resolve({ ok: true })
    }
    return new Promise(resolve => calls.push({ url, options, resolve }))
  })
  const hook = renderHook(() => useFileUpload({ chatId: 'chat', initialFiles }))
  return { hook, calls }
}
const record = (name, discard_token = 'receipt') => ({ name, discard_token, size: 3, mime_type: 'text/plain', status: 'done' })
const uploadedFile = () => new File(['abc'], 'local.txt', { type: 'text/plain' })

test('discard excludes committed names, cannot delete legacy owner files, and is idempotent', t => {
  const { hook, calls } = setup(t, [record('unused.txt'), record('accepted.txt'), record('legacy.txt', undefined)])
  // Explicitly model an older draft with no server receipt.
  hook.result.current.restoreFiles([record('unused.txt'), record('accepted.txt'), { name: 'legacy.txt', status: 'done' }])
  hook.result.current.discardFiles({ exceptNames: ['accepted.txt'] })
  hook.result.current.discardFiles()
  assert.equal(calls.length, 1)
  assert.match(calls[0].url, /unused.txt\?only_if_unused=true&discard_token=receipt$/)
  assert.deepEqual(hook.result.current.files, [])
  hook.unmount()
})

for (const action of ['remove', 'discard']) {
  test(`${action} during upload discards late success using server metadata`, async t => {
    const { hook, calls } = setup(t)
    const pending = hook.result.current.addFiles([uploadedFile()])
    if (action === 'remove') hook.result.current.removeFile(hook.result.current.files[0].id)
    else hook.result.current.discardFiles()
    assert.equal(calls.length, 1)
    calls[0].resolve({ ok: true, json: async () => [record('server_1.txt', 'fresh')] })
    await pending
    assert.deepEqual(hook.result.current.files, [])
    assert.match(calls[1].url, /server_1.txt\?only_if_unused=true&discard_token=fresh$/)
    hook.unmount()
  })
}

test('navigation/unmount preserves completed drafts but discards orphaned late success', async t => {
  const { hook, calls } = setup(t, [record('draft.txt')])
  const pending = hook.result.current.addFiles([uploadedFile()])
  hook.unmount()
  calls[0].resolve({ ok: true, json: async () => [record('server.txt')] })
  await pending
  assert.equal(calls.length, 2)
  assert.match(calls[1].url, /server.txt\?only_if_unused=true&discard_token=receipt$/)
  assert.ok(!calls.some(call => call.url.includes('draft.txt')))
})

test('server metadata replaces browser guesses', async t => {
  const { hook, calls } = setup(t)
  const pending = hook.result.current.addFiles([uploadedFile()])
  calls[0].resolve({ ok: true, json: async () => [record('server.txt')] })
  await pending
  assert.equal(hook.result.current.files[0].name, 'server.txt')
  assert.equal(hook.result.current.files[0].discard_token, 'receipt')
  hook.unmount()
})

test('discard preserves a late success explicitly included in accepted names', async t => {
  const { hook, calls } = setup(t)
  const pending = hook.result.current.addFiles([uploadedFile()])
  hook.result.current.discardFiles({ exceptNames: ['server.txt'] })
  calls[0].resolve({ ok: true, json: async () => [record('server.txt')] })
  await pending
  assert.equal(calls.length, 1)
  assert.deepEqual(hook.result.current.files, [])
  hook.unmount()
})

test('removing an uploaded attachment after draft restoration keeps its deletion receipt', async t => {
  const values = new Map()
  const storage = {
    getItem: key => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
    removeItem: key => values.delete(key),
  }
  const { hook, calls } = setup(t)
  const pending = hook.result.current.addFiles([uploadedFile()])
  calls[0].resolve({ ok: true, json: async () => [record('server.txt', 'fresh')] })
  await pending
  persistComposerDraft('chat', '', hook.result.current.files, storage)
  hook.unmount()

  // Restoration is persisted again on mount; both directions must retain it.
  const restored = readComposerDraft('chat', storage)
  persistComposerDraft('chat', restored.input, restored.attachments, storage)
  const second = readComposerDraft('chat', storage)
  const remounted = renderHook(() => useFileUpload({ chatId: 'chat', initialFiles: second.attachments }))
  try {
    remounted.result.current.removeFile(remounted.result.current.files[0].id)
    assert.deepEqual(remounted.result.current.files, [])
    assert.equal(calls.length, 2)
    assert.match(calls[1].url, /server.txt\?only_if_unused=true&discard_token=fresh$/)
  } finally {
    remounted.unmount()
  }
})
