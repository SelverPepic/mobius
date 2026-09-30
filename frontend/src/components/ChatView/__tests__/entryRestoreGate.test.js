import { test } from 'node:test'
import assert from 'node:assert/strict'

import { entryRestoreDecision } from '../scroll/restore.js'

// Behavioral coverage for the entry (restore) gate. Kept in its own file with
// NO source reads so the structural-test ratchet does not count these
// behavioral cases as implementation-text assertions.
//
// The gate converts the neutral INITIAL scroll mode into a concrete reading
// coordinate on chat (re)activation. `applyMode` treats INITIAL as a no-op, so
// a chat left in INITIAL sits at scrollTop 0 — the physical top. A restore pass
// that runs before the transcript rows have painted must therefore WAIT, not
// commit INITIAL, or the reader is stranded at the top with no re-resolution
// (the reported "keep being taken to the top of a chat").

// A scroll element in the pre-paint window: no `.chat__msg[data-key]` rows yet.
function unpaintedScrollEl() {
  return {
    scrollTop: 0,
    scrollHeight: 2000,
    clientHeight: 800,
    querySelector: (sel) => (sel === '.spacer-dynamic' ? { offsetHeight: 0 } : null),
    querySelectorAll: () => [],
  }
}

// A single painted message row with browser-like geometry, mirroring the
// fixture shape the sibling scroll suite uses for anchor arithmetic.
function partedRow(key, top, partHeights) {
  const row = {
    offsetTop: top,
    offsetHeight: partHeights.reduce((sum, h) => sum + h, 0),
    dataset: { key },
  }
  let cursor = top
  row.children = partHeights.map((height) => {
    const child = { offsetTop: cursor, offsetHeight: height }
    cursor += height
    return child
  })
  return row
}

function paintedScrollEl(row, { scrollTop = 0, clientHeight = 900, spacer = 0 } = {}) {
  const scrollHeight = row.offsetTop + row.offsetHeight + spacer
  const scrollEl = {
    scrollTop,
    clientHeight,
    scrollHeight,
    getBoundingClientRect: () => ({ top: 0 }),
    querySelector(selector) {
      if (selector === '.spacer-dynamic') return { offsetHeight: spacer }
      return selector.includes(row.dataset.key) ? row : null
    },
    querySelectorAll(selector) {
      return selector === '.chat__msg[data-key]' ? [row] : []
    },
  }
  const attachRect = (node) => {
    node.getBoundingClientRect = () => ({ top: node.offsetTop - scrollEl.scrollTop })
    node.children?.forEach(attachRect)
  }
  attachRect(row)
  return scrollEl
}

for (const phase of ['cached', 'stream-catchup', 'ready', 'cache-validating']) {
  test(`entryRestoreDecision waits (never commits INITIAL) with no painted rows: ${phase}`, () => {
    const decision = entryRestoreDecision({
      mode: { kind: 'INITIAL' },
      saved: { kind: 'ANCHOR_AT', key: 'm-1', offset: 40, at: 1 },
      messages: [{ id: 'm-1' }],
      scrollEl: unpaintedScrollEl(),
      phase,
    })
    assert.equal(decision.action, 'wait',
      'committing here would reveal the chat at scrollTop 0 with no re-resolution')
    assert.equal(decision.savedPresent, true,
      'the unresolved-but-present flag keeps persistence from clearing the saved spot')
  })
}

test('entryRestoreDecision commits the validated tail once rows exist (cached)', () => {
  const scrollEl = paintedScrollEl(partedRow('m-1', 0, [700]))
  const decision = entryRestoreDecision({
    mode: { kind: 'INITIAL' },
    saved: undefined,
    messages: [],
    scrollEl,
    phase: 'cached',
  })
  assert.equal(decision.action, 'commit')
  assert.notEqual(decision.mode.kind, 'INITIAL')
  assert.equal(decision.resolved, false,
    'a manufactured tail fallback is not an explicit reader location')
})

test('entryRestoreDecision commits an explicit saved anchor once it resolves (ready)', () => {
  const scrollEl = paintedScrollEl(partedRow('m-1', 0, [700]))
  const decision = entryRestoreDecision({
    mode: { kind: 'INITIAL' },
    saved: { kind: 'ANCHOR_AT', key: 'm-1', offset: 40, at: 1 },
    messages: [{ id: 'm-1' }],
    scrollEl,
    phase: 'ready',
  })
  assert.equal(decision.action, 'commit')
  assert.equal(decision.mode.key, 'm-1')
  assert.equal(decision.resolved, true)
})

test('entryRestoreDecision commits an explicit saved anchor while running catch-up continues', () => {
  const scrollEl = paintedScrollEl(partedRow('m-1', 0, [700]))
  const decision = entryRestoreDecision({
    mode: { kind: 'INITIAL' },
    saved: { kind: 'ANCHOR_AT', key: 'm-1', offset: 40, at: 1 },
    messages: [{ id: 'm-1' }],
    scrollEl,
    phase: 'stream-catchup',
  })
  assert.equal(decision.action, 'commit')
  assert.equal(decision.mode.key, 'm-1')
  assert.equal(decision.resolved, true)
})

test('entryRestoreDecision holds cache-validating until an authoritative coordinate resolves', () => {
  const scrollEl = paintedScrollEl(partedRow('m-1', 0, [700]))
  // Rows exist, but there is no saved location — only a manufactured tail — so
  // the cache-validating window keeps waiting rather than revealing on it.
  const decision = entryRestoreDecision({
    mode: { kind: 'INITIAL' },
    saved: undefined,
    messages: [],
    scrollEl,
    phase: 'cache-validating',
  })
  assert.equal(decision.action, 'wait')
})

test('entryRestoreDecision is idle outside the restore window', () => {
  const scrollEl = paintedScrollEl(partedRow('m-1', 0, [700]))
  assert.equal(entryRestoreDecision({
    mode: { kind: 'INITIAL' }, saved: undefined, messages: [], scrollEl, phase: 'history',
  }).action, 'idle', 'history blocks restore')
  assert.equal(entryRestoreDecision({
    mode: { kind: 'FOLLOW_BOTTOM' }, saved: undefined, messages: [], scrollEl, phase: 'ready',
  }).action, 'idle', 'an already-restored mode is not re-resolved')
})

for (const phase of ['cached', 'cache-validating', 'stream-catchup', 'ready']) {
  test(`restored-open detail holds coordinate validation until its layout settles (${phase})`, () => {
    const row = partedRow('detail-row', 0, [800])
    const scrollEl = paintedScrollEl(row)
    const query = scrollEl.querySelector.bind(scrollEl)
    let pending = true
    scrollEl.querySelector = selector => selector === '[data-reading-layout-pending="true"]'
      ? (pending ? {} : null) : query(selector)
    const input = {
      mode: { kind: 'INITIAL' },
      saved: { kind: 'ANCHOR_AT', key: 'detail-row', part: [1], offset: 20 },
      messages: [], scrollEl, phase,
    }
    assert.equal(entryRestoreDecision(input).action, 'wait', 'missing part is temporary, not retired')
    const detail = { offsetTop: 800, offsetHeight: 400, getBoundingClientRect: () => ({ top: 800, bottom: 1200, height: 400 }) }
    row.children.push(detail)
    pending = false
    const restored = entryRestoreDecision(input)
    assert.equal(restored.action, 'commit')
    assert.equal(restored.resolved, true)
    assert.deepEqual(restored.mode.part, [1])
  })
}

test('terminal detail failure permits the existing fallback, rather than an endless wait', () => {
  const scrollEl = paintedScrollEl(partedRow('detail-row', 0, [800]))
  const decision = entryRestoreDecision({
    mode: { kind: 'INITIAL' },
    saved: { kind: 'ANCHOR_AT', key: 'detail-row', part: [8], offset: 0 },
    messages: [], scrollEl, phase: 'ready',
  })
  assert.equal(decision.action, 'commit')
  assert.equal(decision.resolved, false)
  assert.equal(decision.savedPresent, true, 'failed retrieval does not authorize deleting saved position')
})
