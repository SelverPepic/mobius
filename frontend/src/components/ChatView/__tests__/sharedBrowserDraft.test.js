/* Shared draft text is tab/grant scoped and never touches owner durable drafts. */
import test from 'node:test'
import assert from 'node:assert/strict'
import 'fake-indexeddb/auto'
import { createStore, get, set } from 'idb-keyval'
import { setActiveSharedBrowserGrantId } from '../../../lib/sharedBrowserWorkspace.js'

function storageStub() {
  const values = new Map()
  return {
    get length() { return values.size },
    key: index => [...values.keys()][index] ?? null,
    getItem: key => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, String(value)),
    removeItem: key => values.delete(key),
  }
}

test('guest composer reload uses its own tab storage, not owner IndexedDB', async () => {
  globalThis.location = { pathname: '/shell/shared' }
  globalThis.sessionStorage = storageStub()
  setActiveSharedBrowserGrantId('grant-A')
  const ownerStore = createStore('mobius-owner-drafts', 'drafts-v1')
  await set('chat-1', 'owner draft', ownerStore)
  const drafts = await import('../composerDraft.js')
  drafts.persistComposerDraft('chat-1', 'guest draft')
  drafts._clearComposerDraftMemoryForTests()
  assert.equal((await drafts.readComposerDraftAsync('chat-1')).input, 'guest draft')
  assert.equal(await get('chat-1', ownerStore), 'owner draft')
  setActiveSharedBrowserGrantId('grant-B')
  drafts._clearComposerDraftMemoryForTests()
  assert.equal((await drafts.readComposerDraftAsync('chat-1')).input, '')
  assert.equal(await get('chat-1', ownerStore), 'owner draft')
})
