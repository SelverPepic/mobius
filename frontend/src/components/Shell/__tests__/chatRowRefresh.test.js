import test from 'node:test'
import assert from 'node:assert/strict'
import { createChatRowRefresh } from '../chatRowRefresh.js'

function harness({ fullReadInFlight = () => false } = {}) {
  const timers = []
  const reads = []
  const applied = []
  let refreshedAll = 0
  let fullReads = 0
  const refresh = createChatRowRefresh({
    readRows: ids => new Promise((resolve, reject) => {
      reads.push({ ids, resolve, reject })
    }),
    applyRows: (ids, rows) => applied.push({ ids, rows }),
    refreshAll: async () => { refreshedAll += 1 },
    fullReadInFlight,
    fullReadsStarted: () => fullReads,
    schedule: fn => { timers.push(fn); return timers.length },
    unschedule: () => {},
  })
  const settle = () => new Promise(resolve => setTimeout(resolve, 0))
  return {
    refresh, timers, reads, applied, settle,
    fireTimer: () => timers.shift()(),
    startFullRead: () => { fullReads += 1 },
    get refreshedAll() { return refreshedAll },
  }
}

test('a burst of run events becomes one scoped read of those rows', async () => {
  const h = harness()
  h.refresh.request('a')
  h.refresh.request('b')
  h.refresh.request('a')
  assert.equal(h.timers.length, 1)
  h.fireTimer()
  assert.deepEqual(h.reads.map(read => read.ids), [['a', 'b']])
  h.reads[0].resolve([{ id: 'a' }])
  await h.settle()
  assert.deepEqual(h.applied, [{ ids: ['a', 'b'], rows: [{ id: 'a' }] }])
  assert.equal(h.refreshedAll, 0)
})

test('events during a scoped read wait for exactly one follow-up read', async () => {
  const h = harness()
  h.refresh.request('a')
  h.fireTimer()
  h.refresh.request('a')
  h.refresh.request('c')
  assert.equal(h.timers.length, 0, 'no second read may overlap the first')
  h.reads[0].resolve([{ id: 'a', running: true }])
  await h.settle()
  assert.equal(h.timers.length, 1)
  h.fireTimer()
  assert.deepEqual(h.reads.map(read => read.ids), [['a'], ['a', 'c']])
})

test('a complete read already in flight turns the batch into a fresh complete read', async () => {
  const h = harness({ fullReadInFlight: () => true })
  h.refresh.request('a')
  h.fireTimer()
  await h.settle()
  assert.equal(h.reads.length, 0)
  assert.equal(h.refreshedAll, 1)
})

test('a complete read started during the scoped read supersedes its answer', async () => {
  const h = harness()
  h.refresh.request('a')
  h.fireTimer()
  h.startFullRead()
  h.reads[0].resolve([{ id: 'a', running: true }])
  await h.settle()
  assert.deepEqual(h.applied, [])
  assert.equal(h.refreshedAll, 0)
})

test('a failed scoped read, such as one over the id bound, becomes a complete read', async () => {
  const h = harness()
  h.refresh.request('a')
  h.fireTimer()
  h.reads[0].reject(new Error('422'))
  await h.settle()
  assert.deepEqual(h.applied, [])
  assert.equal(h.refreshedAll, 1)
})
