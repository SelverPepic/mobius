import assert from 'node:assert/strict'
import test from 'node:test'
import { installFrameShellShortcuts } from '../frameShellShortcuts.js'

function nestedDocument() {
  const listeners = new Map()
  const posts = []
  const parent = { postMessage(message, origin) { posts.push({ message, origin }) } }
  const win = {
    parent,
    addEventListener(type, callback) { listeners.set(type, callback) },
    removeEventListener(type) { listeners.delete(type) },
    document: {
      addEventListener(type, callback, capture) {
        assert.equal(capture, true, 'captures before the page’s own handlers')
        listeners.set(type, callback)
      },
      removeEventListener(type) { listeners.delete(type) },
    },
  }
  const uninstall = installFrameShellShortcuts(win)
  return {
    parent,
    posts,
    listeners,
    uninstall,
    advertise(source, shortcuts) {
      listeners.get('message')({ source, data: { type: 'moebius:frame-shortcuts', shortcuts } })
    },
    key(overrides = {}) {
      const event = {
        key: 'k', metaKey: true, prevented: false, stopped: false,
        preventDefault() { this.prevented = true },
        stopImmediatePropagation() { this.stopped = true },
        ...overrides,
      }
      listeners.get('keydown')(event)
      assert.equal(event.stopped, event.prevented)
      return event.prevented
    },
  }
}

const search = [{ actionId: 'search.open', binding: { key: 'k', mod: true } }]

test('a nested document asks its parent for the shell chords and forwards only those', () => {
  const doc = nestedDocument()
  assert.deepEqual(doc.posts.map(post => post.message), [{ type: 'moebius:frame-shortcuts-request' }])
  assert.equal(doc.key(), false, 'nothing is captured before the parent advertises')

  doc.advertise({}, search)
  assert.equal(doc.key(), false, 'only the parent can advertise chords')

  doc.advertise(doc.parent, search)
  assert.equal(doc.key(), true)
  assert.deepEqual(doc.posts.at(-1).message, { type: 'moebius:shell-shortcut', actionId: 'search.open' })
  assert.equal(doc.key({ metaKey: false, ctrlKey: true }), true, 'Ctrl is the same modifier')

  for (const other of [{ key: 'c' }, { metaKey: false }, { shiftKey: true }, { isComposing: true }, { repeat: true }]) {
    assert.equal(doc.key(other), false, 'every other key stays with the document')
  }

  doc.advertise(doc.parent, [])
  assert.equal(doc.key(), false, 'an app that opted out advertises nothing')
  doc.uninstall()
  assert.equal(doc.listeners.size, 0)
})

test('a top-level document has no parent to forward to', () => {
  const win = { addEventListener() { assert.fail('must not listen') } }
  win.parent = win
  installFrameShellShortcuts(win)()
})
