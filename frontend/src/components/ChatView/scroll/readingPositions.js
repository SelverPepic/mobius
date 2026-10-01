import { currentSharedBrowserGrantId, isSharedBrowserRoute } from '../../../lib/sharedBrowserWorkspace.js'

/**
 * Durable reading-position storage for chat scroll.
 *
 * This module owns only serialization and bounded retention. It deliberately
 * knows nothing about DOM geometry or live scroll modes: the controller turns
 * its current state into a durable anchor before calling `writeReadingPosition`.
 */

export const READING_POSITION_KEY = 'chat-reading-position'
const READING_POSITION_LIMIT = 300

const ownerPositions = (() => {
  // Shared-browser access must not hydrate owner-local scroll history. Its
  // reading positions are document-memory only, like its bearer token.
  if (isSharedBrowserRoute()) return {}
  try {
    const parsed = JSON.parse(localStorage.getItem(READING_POSITION_KEY) || '{}')
    return (parsed && typeof parsed === 'object') ? parsed : {}
  }
  catch { return {} }
})()

const guestPositions = new Map()
const emptyGuestPositions = Object.freeze({})
function positions() {
  if (!isSharedBrowserRoute()) return ownerPositions
  const grant = currentSharedBrowserGrantId()
  if (!grant) return emptyGuestPositions
  if (!guestPositions.has(grant)) guestPositions.set(grant, {})
  return guestPositions.get(grant)
}

// Logout is a terminal owner-session boundary. React/page lifecycle callbacks
// may still run before reload; disabling writes prevents those late callbacks
// from recreating data that logout just removed.
let writesEnabled = true

function persist() {
  if (!writesEnabled) return
  if (isSharedBrowserRoute()) return
  try {
    const entries = Object.entries(ownerPositions)
    if (entries.length > READING_POSITION_LIMIT) {
      const expired = entries
        .sort((a, b) => (b[1]?.at || 0) - (a[1]?.at || 0))
        .slice(READING_POSITION_LIMIT)
      for (const [chatId] of expired) delete ownerPositions[chatId]
    }
    localStorage.setItem(READING_POSITION_KEY, JSON.stringify(ownerPositions))
  }
  catch { /* best-effort position storage must never break scrolling */ }
}

export function readingPositionFor(chatId) {
  return positions()[String(chatId || '')] || null
}

export function hasReadingPosition(chatId) {
  return Object.hasOwn(positions(), String(chatId || ''))
}

export function writeReadingPosition(chatId, mode) {
  const id = String(chatId || '')
  if (isSharedBrowserRoute() && !currentSharedBrowserGrantId()) return
  const scopedPositions = positions()
  if (!id || !mode || mode.kind === 'INITIAL') {
    if (id) delete scopedPositions[id]
  } else {
    scopedPositions[id] = { ...mode, at: Date.now() }
  }
  persist()
}

export function forgetReadingPosition(chatId) {
  const id = String(chatId || '')
  const scopedPositions = positions()
  if (!(id in scopedPositions)) return false
  delete scopedPositions[id]
  persist()
  return true
}

/** The durable message row an activation needs before reveal. */
export function savedReadingAnchorKey(chatId) {
  const mode = readingPositionFor(chatId)
  return mode?.kind === 'ANCHOR_AT' && typeof mode.key === 'string'
    ? mode.key
    : null
}

/** Nested part paths need committed DOM validation before cache reveal. */
export function savedReadingAnchorHasNestedPart(chatId) {
  const mode = readingPositionFor(chatId)
  return mode?.kind === 'ANCHOR_AT'
    && Array.isArray(mode.part)
    && mode.part.length > 0
}

/** Replace one saved alias before restore consumes it. */
export function remapSavedReadingAnchor(chatId, fromKey, toKey) {
  const id = String(chatId || '')
  const scopedPositions = positions()
  const mode = scopedPositions[id]
  if (mode?.kind !== 'ANCHOR_AT'
      || mode.key !== fromKey
      || typeof toKey !== 'string'
      || !toKey) return false
  scopedPositions[id] = { ...mode, key: toKey, at: Date.now() }
  persist()
  return true
}

export const retireSavedReadingPosition = forgetReadingPosition

export function clearReadingPositions() {
  writesEnabled = false
  for (const key of Object.keys(ownerPositions)) delete ownerPositions[key]
  try { localStorage.removeItem(READING_POSITION_KEY) } catch {}
}
