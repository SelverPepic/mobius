import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  createNestedScrollHandoff,
} from '../scroll/nestedScrollHandoff.js'

function fixture({
  nestedTop = 120,
  nestedHeight = 300,
  nestedClient = 100,
  onHandoff = null,
} = {}) {
  const nested = {
    scrollTop: nestedTop,
    scrollHeight: nestedHeight,
    clientHeight: nestedClient,
    closest: selector => selector === '[data-chat-scroll-region], .chat__scroll'
      ? nested
      : null,
  }
  const child = { closest: nested.closest }
  const outer = {
    scrollTop: 400,
    contains: node => node === nested,
  }
  const handlers = createNestedScrollHandoff(outer, {
    readStyle: () => ({ lineHeight: '20px', fontSize: '15px' }),
    onHandoff,
  })
  return { child, handlers, nested, outer }
}

function wheel(target, properties = {}) {
  let prevented = false
  return {
    target,
    deltaY: 0,
    deltaMode: 0,
    cancelable: true,
    preventDefault: () => { prevented = true },
    prevented: () => prevented,
    ...properties,
  }
}

test('wheel burst stays in its reader; a new edge gesture transfers only residual motion', () => {
  const ownership = []
  const { child, handlers, nested, outer } = fixture({
    onHandoff: input => ownership.push({ input, outerTop: outer.scrollTop }),
  })
  const within = wheel(child, { deltaY: 40 })
  handlers.onWheel(within)
  assert.equal(nested.scrollTop, 120)
  assert.equal(outer.scrollTop, 400)
  assert.equal(within.prevented(), false)

  nested.scrollTop = 190
  const crossing = wheel(child, { deltaY: 25, timeStamp: 1000 })
  handlers.onWheel(crossing)
  assert.equal(nested.scrollTop, 200, 'nested surface consumes its remaining range')
  assert.equal(outer.scrollTop, 400, 'the first crossing cannot spill into chat')
  assert.equal(crossing.prevented(), true)

  handlers.onWheel(wheel(child, { deltaY: 30, timeStamp: 1050 }))
  assert.equal(outer.scrollTop, 400, 'momentum from the same burst stays contained')
  handlers.onWheel(wheel(child, { deltaY: 18, timeStamp: 1130 }))
  assert.equal(outer.scrollTop, 400, 'continued fast wheel motion stays contained')
  const deliberate = wheel(child, { deltaY: 15, timeStamp: 1280 })
  handlers.onWheel(deliberate)
  assert.equal(outer.scrollTop, 415, 'a fresh edge gesture reaches the transcript')
  assert.equal(deliberate.prevented(), true)
  assert.deepEqual(ownership, [{
    input: { delta: 15, type: 'wheel' },
    outerTop: 400,
  }], 'reader ownership is claimed before the outer scroll mutates')
  handlers.dispose()
})

test('line and page wheel handoff uses CSS pixels after a deliberate new gesture', () => {
  const { child, handlers, nested, outer } = fixture({ nestedTop: 200 })
  handlers.onWheel(wheel(child, { deltaY: 1, deltaMode: 1, timeStamp: 1000 }))
  assert.equal(outer.scrollTop, 400)
  const lines = wheel(child, { deltaY: 3, deltaMode: 1, timeStamp: 1160 })
  handlers.onWheel(lines)
  assert.equal(outer.scrollTop, 460, 'three computed 20px lines stay in CSS pixels')
  assert.equal(lines.prevented(), true)

  outer.scrollTop = 400
  nested.scrollTop = 0
  handlers.onWheel(wheel(child, { deltaY: -1, deltaMode: 2, timeStamp: 1500 }))
  assert.equal(outer.scrollTop, 400)
  const page = wheel(child, { deltaY: -1, deltaMode: 2, timeStamp: 1660 })
  handlers.onWheel(page)
  assert.equal(outer.scrollTop, 300, 'one page is the nested 100px client height')
  assert.equal(page.prevented(), true)
  handlers.dispose()
})

test('delegation keeps each marked nested region independent', () => {
  const { child, handlers, nested, outer } = fixture({ nestedTop: 200 })
  handlers.onWheel(wheel(child, { deltaY: 30, timeStamp: 1000 }))
  const event = wheel(child, { deltaY: 30, timeStamp: 1160 })
  handlers.onWheel(event)
  assert.equal(outer.scrollTop, 430)
  assert.equal(event.prevented(), true)

  const second = {
    scrollTop: 200, scrollHeight: 300, clientHeight: 100,
    closest: selector => selector === '[data-chat-scroll-region], .chat__scroll'
      ? second
      : null,
  }
  outer.contains = node => node === nested || node === second
  handlers.onWheel(wheel(second, { deltaY: 30, timeStamp: 1320 }))
  assert.equal(outer.scrollTop, 430, 'a different reader cannot inherit the first reader’s handoff')

  const unrelated = { closest: () => null }
  const outside = wheel(unrelated, { deltaY: 30 })
  handlers.onWheel(outside)
  assert.equal(outer.scrollTop, 430)
  assert.equal(outside.prevented(), false)
  nested.scrollTop = 0
  handlers.dispose()
})

test('reaching the edge exactly arms the next gesture without handing off the first', () => {
  const { child, handlers, nested, outer } = fixture({ nestedTop: 190 })
  const exact = wheel(child, { deltaY: 10, timeStamp: 1000 })
  handlers.onWheel(exact)
  assert.equal(exact.prevented(), false, 'native scrolling consumes the whole delta')
  nested.scrollTop = 200 // browser applies the native movement after the event
  handlers.onWheel(wheel(child, { deltaY: 20, timeStamp: 1160 }))
  assert.equal(outer.scrollTop, 420)
  handlers.dispose()
})

test('ctrl-wheel zoom and scrollable room retain native browser behavior', () => {
  const { child, handlers, nested, outer } = fixture({ nestedTop: 200 })
  const zoom = wheel(child, { deltaY: 50, ctrlKey: true })
  handlers.onWheel(zoom)
  assert.equal(outer.scrollTop, 400)
  assert.equal(zoom.prevented(), false)

  nested.scrollTop = 80
  const native = wheel(child, { deltaY: -30 })
  handlers.onWheel(native)
  assert.equal(outer.scrollTop, 400)
  assert.equal(native.prevented(), false)
  handlers.dispose()
})

test('non-cancelable edge momentum stays contained but a fresh gesture can hand off', () => {
  const { child, handlers, outer } = fixture({ nestedTop: 200 })
  const event = wheel(child, { deltaY: 30, timeStamp: 1000, cancelable: false })
  assert.equal(handlers.onWheel(event), true)
  assert.equal(event.prevented(), false)
  assert.equal(outer.scrollTop, 400)
  handlers.onWheel(wheel(child, { deltaY: 20, timeStamp: 1160, cancelable: false }))
  assert.equal(outer.scrollTop, 420)
  handlers.dispose()
})

test('touch handoff follows one contact and cleanup removes the full lifecycle', () => {
  const ownership = []
  const { child, handlers, nested, outer } = fixture({
    nestedTop: 200,
    onHandoff: input => ownership.push({ input, outerTop: outer.scrollTop }),
  })
  handlers.onTouchStart({
    target: child,
    touches: [{ identifier: 7, clientY: 100 }],
  })
  let prevented = false
  handlers.onTouchMove({
    touches: [{ identifier: 7, clientY: 70 }],
    preventDefault: () => { prevented = true },
  })
  assert.equal(outer.scrollTop, 430)
  assert.equal(prevented, true)
  assert.deepEqual(ownership, [{
    input: { delta: 30, type: 'touchmove' },
    outerTop: 400,
  }], 'reader ownership is claimed with direction before scroll mutation')

  handlers.onTouchEnd({ touches: [] })
  handlers.dispose()
  handlers.onWheel(wheel(child, { deltaY: 30 }))
  assert.equal(outer.scrollTop, 430, 'disposed controller cannot mutate scrolling')
  assert.equal(nested.scrollTop, 200)
})
