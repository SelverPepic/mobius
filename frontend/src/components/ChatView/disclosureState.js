import { useRef, useState } from 'react'
import { currentSharedBrowserGrantId, currentSharedBrowserStorage, isSharedBrowserRoute } from '../../lib/sharedBrowserWorkspace.js'


// Disclosure state is screen state, not transcript data. Keep it for the
// browser session so leaving a chat and returning restores the exact activity,
// thought, and tool rows the reader opened without writing presentation state
// into the durable conversation.
const STORAGE_PREFIX = 'chat-disclosures:'
const cache = new Map()

function disclosureStorage() {
  if (isSharedBrowserRoute()) return currentSharedBrowserStorage()
  try { return globalThis.sessionStorage ?? null } catch { return null }
}

function cacheKey(chatId) {
  if (!isSharedBrowserRoute()) return `owner:${chatId}`
  const grant = currentSharedBrowserGrantId()
  return grant ? `shared:${grant}:${chatId}` : null
}

function storageKey(chatId) {
  return `${STORAGE_PREFIX}${chatId}`
}

function readOpenKeys(chatId) {
  const id = String(chatId || '')
  if (!id) return new Set()
  const scopedId = cacheKey(id)
  if (!scopedId) return new Set()
  if (cache.has(scopedId)) return cache.get(scopedId)
  let keys = []
  try {
    const parsed = JSON.parse(disclosureStorage()?.getItem(storageKey(id)) || '[]')
    if (Array.isArray(parsed)) keys = parsed.filter(key => typeof key === 'string')
  } catch {}
  const openKeys = new Set(keys)
  cache.set(scopedId, openKeys)
  return openKeys
}

export function persistDisclosureOpen(chatId, disclosureKey, open) {
  const id = String(chatId || '')
  const key = String(disclosureKey || '')
  if (!id || !key || !cacheKey(id)) return
  const openKeys = readOpenKeys(id)
  if (open) openKeys.add(key)
  else openKeys.delete(key)
  try {
    disclosureStorage()?.setItem(storageKey(id), JSON.stringify([...openKeys]))
  } catch {}
}

export function disclosureIsOpen(chatId, disclosureKey) {
  return readOpenKeys(chatId).has(String(disclosureKey || ''))
}

export function useDisclosureState(chatId, disclosureKey) {
  const [open, setOpenState] = useState(
    () => disclosureIsOpen(chatId, disclosureKey),
  )
  const openRef = useRef(open)
  openRef.current = open
  const setOpen = (next) => {
    const value = typeof next === 'function' ? !!next(openRef.current) : !!next
    openRef.current = value
    persistDisclosureOpen(chatId, disclosureKey, value)
    setOpenState(value)
  }
  return [open, setOpen]
}

export function _resetDisclosureStateForTests() {
  cache.clear()
}
