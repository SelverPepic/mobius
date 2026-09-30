/* Unread rows stay visible and actionable without opening the bell clearing them. */
import assert from 'node:assert/strict'
import test from 'node:test'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import NotificationsView from '../NotificationsView.jsx'
import { notificationQueries } from '../../../hooks/queries.js'

test('new and earlier rows are distinguished and can be marked read without deleting', () => {
  const queryClient = new QueryClient()
  queryClient.setQueryData(notificationQueries.list.key, {
    pages: [[
      { id: 'old', source_type: 'agent', title: 'Older item', sent_at: '2026-09-29T12:00:00Z', read_at: '2026-09-29T13:00:00Z' },
      { id: 'new', source_type: 'agent', title: 'Fresh item', sent_at: '2026-09-30T10:00:00Z', read_at: null },
    ]],
    pageParams: [null],
  })
  const html = renderToStaticMarkup(React.createElement(
    QueryClientProvider, { client: queryClient },
    React.createElement(NotificationsView, {
      active: true, unreadCount: 1, onMarkRead() {}, onMarkAllRead() {}, onDismiss() {},
    }),
  ))
  assert.match(html, /Mark all as read/)
  assert.match(html, /New[\s\S]*Fresh item[\s\S]*Mark as read[\s\S]*Earlier[\s\S]*Older item/)
  assert.match(html, /notifications__row-item--unread/)
  assert.equal((html.match(/Mark as read/g) || []).length, 1)
})
