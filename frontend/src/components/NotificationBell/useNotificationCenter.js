import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../../api/client.js'
import { notificationQueries } from '../../hooks/queries.js'
import useSessionNotices from './useSessionNotices.js'

// Owns the small amount of state behind the shell notification preview. Keeping
// this out of Shell is deliberate: notifications can grow into an app later
// without making navigation, workspace, or pane state aware of that product UI.
export default function useNotificationCenter(queryClient) {
  const [open, setOpen] = useState(false)
  const openRef = useRef(open)
  const rootRef = useRef(null)
  const bellRef = useRef(null)
  openRef.current = open

  const unreadQuery = notificationQueries.unreadCount.useQuery()
  const serverUnreadCount = unreadQuery.data ?? 0
  const newQuery = notificationQueries.newCount.useQuery()
  const session = useSessionNotices()
  const {
    rows: sessionNotices, addNotice, acknowledge: acknowledgeSession,
    markRead: readSession, dismiss: dismissSession, clearAll: clearSession,
    runAction: runNoticeAction,
  } = session
  const unreadCount = serverUnreadCount + session.unreadCount
  const newCount = (newQuery.data ?? 0) + session.newCount

  useEffect(() => { acknowledgeSession(open) }, [acknowledgeSession, open])

  const acknowledgeNew = useCallback(async () => {
    await queryClient.cancelQueries({ queryKey: notificationQueries.newCount.key })
    queryClient.setQueryData(notificationQueries.newCount.key, 0)
    try {
      await api.notifications.seenAll()
    } finally {
      await notificationQueries.newCount.invalidate(queryClient)
    }
  }, [queryClient])

  const markAllRead = useCallback(async () => {
    await api.notifications.readAll()
    readSession()
    await Promise.all([
      notificationQueries.list.invalidate(queryClient),
      notificationQueries.unreadCount.invalidate(queryClient),
      notificationQueries.newCount.invalidate(queryClient),
    ])
  }, [queryClient, readSession])

  const markRead = useCallback(async (notificationId) => {
    if (sessionNotices.some(row => row.id === notificationId)) {
      readSession(notificationId)
      return
    }
    await api.notifications.read(notificationId)
    await Promise.all([
      notificationQueries.list.invalidate(queryClient),
      notificationQueries.unreadCount.invalidate(queryClient),
      notificationQueries.newCount.invalidate(queryClient),
    ])
  }, [queryClient, readSession, sessionNotices])

  const clearAll = useCallback(async () => {
    await api.notifications.clearAll()
    clearSession()
    await Promise.all([
      queryClient.resetQueries({ queryKey: notificationQueries.list.key }),
      notificationQueries.unreadCount.invalidate(queryClient),
      notificationQueries.newCount.invalidate(queryClient),
    ])
  }, [clearSession, queryClient])

  const dismiss = useCallback(async (notificationId) => {
    if (sessionNotices.some(row => row.id === notificationId)) {
      dismissSession(notificationId)
      return
    }
    await api.notifications.dismiss(notificationId)
    await queryClient.resetQueries({ queryKey: notificationQueries.list.key })
    notificationQueries.unreadCount.invalidate(queryClient)
    notificationQueries.newCount.invalidate(queryClient)
  }, [dismissSession, queryClient, sessionNotices])

  useEffect(() => {
    if (!open) return undefined
    const onPointerDown = (event) => {
      if (!rootRef.current?.contains(event.target)) setOpen(false)
    }
    const onKeyDown = (event) => {
      if (event.key !== 'Escape') return
      setOpen(false)
      bellRef.current?.focus()
    }
    document.addEventListener('pointerdown', onPointerDown)
    document.addEventListener('keydown', onKeyDown)
    return () => {
      document.removeEventListener('pointerdown', onPointerDown)
      document.removeEventListener('keydown', onKeyDown)
    }
  }, [open])

  const toggle = useCallback(() => {
    if (!openRef.current) void acknowledgeNew().catch(() => {})
    setOpen(value => !value)
  }, [acknowledgeNew])
  const close = useCallback(() => setOpen(false), [])
  const reconcile = useCallback(() => {
    notificationQueries.unreadCount.invalidate(queryClient)
    notificationQueries.newCount.invalidate(queryClient)
    if (openRef.current) notificationQueries.list.invalidate(queryClient)
  }, [queryClient])
  const onCreated = useCallback(() => {
    notificationQueries.unreadCount.invalidate(queryClient)
    notificationQueries.list.invalidate(queryClient)
    notificationQueries.newCount.invalidate(queryClient)
    if (openRef.current) void acknowledgeNew().catch(() => {})
  }, [acknowledgeNew, queryClient])

  return {
    state: { open, unreadCount, newCount, sessionNotices, announcement: session.announcement },
    actions: { toggle, close, clearAll, dismiss, markRead, markAllRead, reconcile, onCreated, addNotice, runNoticeAction },
    meta: { rootRef, bellRef },
  }
}
