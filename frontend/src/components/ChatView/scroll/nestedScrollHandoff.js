import { nestedScrollRange } from './policy.js'

const PIXEL_DELTA = 0
const LINE_DELTA = 1
const PAGE_DELTA = 2
const WHEEL_GESTURE_IDLE_MS = 280

function lineHeightInPixels(element, readStyle) {
  const style = readStyle?.(element)
  const lineHeight = Number.parseFloat(style?.lineHeight)
  if (Number.isFinite(lineHeight) && lineHeight > 0) return lineHeight
  const fontSize = Number.parseFloat(style?.fontSize)
  return Number.isFinite(fontSize) && fontSize > 0 ? fontSize * 1.2 : 16
}

/** WheelEvent deltas use CSS pixels, lines, or pages. Keep the result in the
 * same CSS-pixel coordinate space as scrollTop; device scale and page zoom do
 * not belong in this conversion. */
export function wheelDeltaPixels(event, nested, readStyle) {
  const mode = event?.deltaMode
  const delta = Number(event?.deltaY)
  if (!Number.isFinite(delta)) return 0
  if (mode === LINE_DELTA) {
    return delta * lineHeightInPixels(nested, readStyle)
  }
  if (mode === PAGE_DELTA) {
    const pageHeight = Number(nested?.clientHeight)
    return Number.isFinite(pageHeight) && pageHeight > 0
      ? delta * pageHeight
      : 0
  }
  return mode === PIXEL_DELTA || mode == null
    ? delta
    : 0
}

/** Consume the reader's remaining range and return only the excess. Native
 * scrolling remains untouched until a delta crosses an edge. */
function consumeNestedEdge(range, delta) {
  if (!range || !Number.isFinite(delta) || delta === 0) return null
  const available = delta > 0
    ? Math.max(0, range.maxScrollTop - range.scrollTop)
    : Math.max(0, range.scrollTop)
  if (Math.abs(delta) <= available) return null

  const consumed = Math.sign(delta) * available
  range.nested.scrollTop += consumed
  return delta - consumed
}

function preventNativeScroll(event) {
  if (event?.cancelable !== false) event?.preventDefault?.()
}

function touchWithIdentifier(touches, identifier) {
  for (let index = 0; index < (touches?.length || 0); index += 1) {
    const point = touches[index]
    if (point?.identifier === identifier) return point
  }
  return null
}

/** Install one delegated handoff for every capped transcript surface marked
 * with data-chat-scroll-region. A wheel burst stays inside its reader even
 * when it reaches an edge; a new gesture at that edge may enter the chat. */
export function createNestedScrollHandoff(scrollEl, {
  readStyle = typeof globalThis.getComputedStyle === 'function'
    ? globalThis.getComputedStyle.bind(globalThis)
    : null,
  onHandoff = null,
} = {}) {
  let active = true
  let touch = null
  let wheel = { nested: null, lastTime: -Infinity, edge: 0, transferring: false }

  const onWheel = (event) => {
    if (!active) return false
    // Pinch-to-zoom is exposed as ctrl+wheel by browsers. It is not scroll
    // intent and must retain its native behavior.
    if (event?.defaultPrevented || event?.ctrlKey) return false
    const range = nestedScrollRange(event?.target, scrollEl)
    if (!range) return false
    const delta = wheelDeltaPixels(event, range.nested, readStyle)
    const direction = Math.sign(delta)
    if (!direction) return false
    const available = direction > 0
      ? Math.max(0, range.maxScrollTop - range.scrollTop)
      : Math.max(0, range.scrollTop)
    const time = Number.isFinite(event?.timeStamp) ? event.timeStamp : performance.now()
    const crossesEdge = Math.abs(delta) > available
    const canTransfer = crossesEdge && wheel.nested === range.nested
      && wheel.edge === direction
      && (wheel.transferring || time - wheel.lastTime > WHEEL_GESTURE_IDLE_MS)
    wheel = {
      nested: range.nested,
      lastTime: time,
      edge: Math.abs(delta) >= available ? direction : 0,
      transferring: canTransfer,
    }
    if (!crossesEdge) return false
    // Native containment still owns a non-cancelable wheel event. Do not let
    // the outer input policy mistake it for a chat gesture.
    if (event?.cancelable === false) return true
    const residual = consumeNestedEdge(range, delta)
    if (residual == null) return false
    if (canTransfer) {
      onHandoff?.({ delta, type: 'wheel' })
      scrollEl.scrollTop += residual
    }
    preventNativeScroll(event)
    return true
  }

  const onTouchStart = (event) => {
    if (!active) return false
    if (event?.touches?.length !== 1) {
      touch = null
      return false
    }
    const point = event.touches[0]
    const range = nestedScrollRange(event.target, scrollEl)
    touch = range && Number.isFinite(point?.clientY)
      ? { identifier: point.identifier, y: point.clientY, target: event.target }
      : null
    return touch != null
  }

  const onTouchMove = (event) => {
    if (!active) return false
    if (!touch || event?.defaultPrevented || event?.touches?.length !== 1) {
      if (event?.touches?.length !== 1) touch = null
      return false
    }
    const point = touchWithIdentifier(event.touches, touch.identifier)
    if (!Number.isFinite(point?.clientY)) return false
    const delta = touch.y - point.clientY
    touch.y = point.clientY
    const range = nestedScrollRange(touch.target, scrollEl)
    const residual = consumeNestedEdge(range, delta)
    if (residual == null) return false
    onHandoff?.({ delta, type: 'touchmove' })
    scrollEl.scrollTop += residual
    preventNativeScroll(event)
    return true
  }

  const onTouchEnd = (event) => {
    if (!active) return
    if (!touch) return
    if (!touchWithIdentifier(event?.touches, touch.identifier)) touch = null
  }

  return {
    onWheel,
    onTouchStart,
    onTouchMove,
    onTouchEnd,
    dispose() {
      active = false
      touch = null
      wheel = { nested: null, lastTime: -Infinity, edge: 0, transferring: false }
    },
  }
}
