/* Shell feedback behaves as notification history for the current session. */
import assert from 'node:assert/strict'
import test from 'node:test'
import { renderHook } from '../../ChatView/hooks/__tests__/react-hook-shim.mjs'
import useSessionNotices from '../../NotificationBell/useSessionNotices.js'
import { mergeNotificationRows } from '../../NotificationsView/notificationsModel.js'

test('repeated feedback keeps distinct rows and arrivals without replacing an earlier message', () => {
  const { result } = renderHook(useSessionNotices)
  result.current.addNotice('Chat archived')
  result.current.addNotice('Chat archived')
  result.current.addNotice('You are offline.', { variant: 'error' })
  assert.equal(result.current.rows.length, 3)
  assert.equal(new Set(result.current.rows.map(row => row.id)).size, 3)
  assert.equal(result.current.newCount, 3)
  assert.equal(result.current.unreadCount, 3)
  assert.equal(result.current.rows[0].variant, 'error')
  assert.equal(result.current.announcement.title, 'You are offline.')
})

test('opening acknowledges arrivals without marking them read; open-panel arrivals are already seen', () => {
  const { result, rerender } = renderHook(useSessionNotices)
  result.current.addNotice('Chat archived')
  rerender(true)
  result.current.addNotice('Chat restored')
  assert.equal(result.current.newCount, 0)
  assert.equal(result.current.unreadCount, 2)
  result.current.markRead(result.current.rows[0].id)
  assert.equal(result.current.unreadCount, 1)
  rerender(false)
  result.current.addNotice('Another notice')
  assert.equal(result.current.newCount, 1)
  result.current.markRead()
  assert.equal(result.current.unreadCount, 0)
  assert.equal(result.current.newCount, 0)
})

test('Undo is one action per row, survives clear history, and completes only after success', async () => {
  const { result } = renderHook(useSessionNotices)
  let finish
  let calls = 0
  const pending = new Promise(resolve => { finish = resolve })
  result.current.addNotice('Chat archived', { action: {
    label: 'Undo', onAction: () => { calls += 1; return pending },
  } })
  const id = result.current.rows[0].id
  result.current.addNotice('Another notice')
  result.current.clearAll()
  assert.deepEqual(result.current.rows.map(row => row.id), [id])
  const first = result.current.runAction(id)
  await result.current.runAction(id)
  assert.equal(calls, 1)
  assert.equal(result.current.rows[0].actionWorking, true)
  assert.ok(result.current.rows[0].sessionAction)
  finish(true)
  await first
  assert.equal(result.current.rows[0].sessionAction, null)
  assert.equal(result.current.rows[0].actionStatus, 'Undone')
  result.current.clearAll()
  assert.equal(result.current.rows.length, 0)
})

test('failed Undo is visible and retryable rather than being reported as completed', async () => {
  const { result } = renderHook(useSessionNotices)
  let success = false
  result.current.addNotice('Chat archived', { action: { label: 'Undo', onAction: () => success } })
  const id = result.current.rows[0].id
  await result.current.runAction(id)
  assert.match(result.current.rows[0].actionError, /Couldn’t complete/)
  assert.ok(result.current.rows[0].sessionAction)
  success = true
  await result.current.runAction(id)
  assert.equal(result.current.rows[0].actionError, null)
  assert.equal(result.current.rows[0].actionStatus, 'Undone')
})

test('a newer archive action disables only the earlier Undo for the same chat', async () => {
  const { result } = renderHook(useSessionNotices)
  let calls = 0
  const options = noticeKey => ({ noticeKey, action: { label: 'Undo', onAction: () => { calls += 1 } } })
  result.current.addNotice('Chat archived', options('archive:a'))
  const firstId = result.current.rows[0].id
  result.current.addNotice('Chat archived', options('archive:b'))
  result.current.addNotice('Chat restored', options('archive:a'))
  const old = result.current.rows.find(row => row.id === firstId)
  assert.equal(old.sessionAction, null)
  assert.equal(old.actionStatus, 'Superseded by a newer action')
  assert.ok(result.current.rows[1].sessionAction, 'another chat keeps its Undo')
  await result.current.runAction(firstId)
  assert.equal(calls, 0)
})

test('offline feedback can be dismissed without a request; history merges chronologically without mutation', () => {
  const { result } = renderHook(useSessionNotices)
  result.current.addNotice('You are offline.')
  const id = result.current.rows[0].id
  const history = [{ id: 'stored', title: 'Stored notification', sent_at: '2020-01-01T00:00:00Z' }]
  const rows = mergeNotificationRows(history, result.current.rows)
  assert.deepEqual(rows.map(row => row.id), [id, 'stored'])
  assert.equal(history.length, 1)
  result.current.dismiss(id)
  assert.equal(result.current.rows.length, 0)
})

test('notice delivery stays stable across panel changes and initially open panels acknowledge arrivals', () => {
  const { result, rerender } = renderHook(useSessionNotices, true)
  const deliver = result.current.addNotice
  deliver('Chat archived')
  assert.equal(result.current.newCount, 0)
  assert.equal(result.current.unreadCount, 1)
  rerender(false)
  assert.equal(result.current.addNotice, deliver)
  deliver('Chat restored')
  assert.equal(result.current.newCount, 1)
  rerender(true)
  assert.equal(result.current.addNotice, deliver)
  assert.equal(result.current.newCount, 0)
  assert.equal(result.current.unreadCount, 2)
})

test('dismissing a pending Undo never resurrects its row when the action settles', async () => {
  const { result } = renderHook(useSessionNotices)
  let finish
  const pending = new Promise(resolve => { finish = resolve })
  result.current.addNotice('Chat archived', { action: { label: 'Undo', onAction: () => pending } })
  const id = result.current.rows[0].id
  const action = result.current.runAction(id)
  result.current.dismiss(id)
  finish(true)
  await action
  assert.equal(result.current.rows.length, 0)
})
