/* A message's media_dimensions map describes its saved text. The chat often
 * shows newer text with that map (live stream, stream promotion, joined steer
 * replay), so only an explicit null from the server means "unreadable"; a path
 * the map does not mention keeps the default frame, and known sizes stay. */
import { after, test } from 'node:test'
import assert from 'node:assert/strict'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createServer } from 'vite'
import { promoteAssistantStream } from '../streamPromotion.js'
import { assistantReplyGroups } from '../assistantReplies.js'

const shellLocation = { origin: 'http://localhost', href: 'http://localhost/shell/' }
globalThis.window = { location: shellLocation, innerHeight: 800 }
globalThis.location = shellLocation

// Server rendering has no DOM for DOMPurify. The image hrefs here are plain
// paths, so an identity sanitizer exercises the same URL checks.
const domPurifyStub = {
  name: 'dompurify-ssr-stub',
  enforce: 'pre',
  resolveId: id => (id === 'dompurify' ? '\0dompurify-stub' : null),
  load: id => (id === '\0dompurify-stub'
    ? 'export default { sanitize: value => String(value) }'
    : null),
}

const vite = await createServer({
  appType: 'custom',
  logLevel: 'error',
  server: { middlewareMode: true, hmr: false, ws: false },
  ssr: { noExternal: ['@openai/apps-sdk-ui', 'dompurify'] },
  plugins: [domPurifyStub],
})
const { default: MsgContent } = await vite.ssrLoadModule(
  '/src/components/ChatView/MsgContent.jsx',
)
const { default: AssistantReply } = await vite.ssrLoadModule(
  '/src/components/ChatView/AssistantReply.jsx',
)
const { imageDimensionsForHref, imageUnreadableForHref } = await vite.ssrLoadModule(
  '/src/components/ChatView/markdown/imageDims.js',
)

after(() => vite.close())

const chatId = 'chat-media-dims'
const A = `/api/chats/${chatId}/media/a.png`
const B = `/api/chats/${chatId}/media/b.png`
const BROKEN = `/api/chats/${chatId}/media/broken.png`
const sizeA = { width: 640, height: 480 }
const oldText = `First ![a](${A})`
const newText = `${oldText}\n\nThen ![b](${B})`

const assistant = (content, extras = {}) => ({
  role: 'assistant', id: 'run', content, blocks: [{ type: 'text', content }], ...extras,
})

function frames(html) {
  return {
    errors: (html.match(/md-image-error/g) || []).length,
    frames: (html.match(/class="md-image-frame"/g) || []).length,
    sizedA: html.includes('--md-image-ratio:640 / 480'),
  }
}

function renderMessage(msg) {
  return renderToStaticMarkup(createElement(MsgContent, {
    msg, chatId, isLastMsg: true, isStreaming: false,
  }))
}

test('only an explicit null marks an image unreadable', () => {
  const map = { [A]: sizeA, [BROKEN]: null }
  assert.deepEqual(imageDimensionsForHref(`${A}?preview=true`, map), sizeA)
  assert.equal(imageUnreadableForHref(BROKEN, map), true)
  assert.equal(imageUnreadableForHref(B, map), false, 'missing means unknown')
  assert.equal(imageUnreadableForHref(A, map), false)
  assert.equal(imageUnreadableForHref(B, undefined), false)

  const html = renderMessage(assistant(
    `![a](${A}) ![b](${B}) ![broken](${BROKEN})`,
    { media_dimensions: map },
  ))
  assert.deepEqual(frames(html), { errors: 1, frames: 2, sizedA: true })
})

test('(a) stream promotion keeps known sizes and frames newer images', () => {
  const partial = assistant(oldText, { media_dimensions: { [A]: sizeA } })
  const [promoted] = promoteAssistantStream([partial], {
    items: [{ type: 'text', content: newText }],
    assistantMessageId: 'run',
  })
  assert.equal(promoted.content, newText)
  assert.deepEqual(frames(renderMessage(promoted)), { errors: 0, frames: 2, sizedA: true })
})

test('(b) a live reply over a sized partial frames newly streamed images', () => {
  const partial = assistant(oldText, { media_dimensions: { [A]: sizeA } })
  const html = renderToStaticMarkup(createElement(AssistantReply, {
    replyGroup: { rows: [{ message: partial, key: 'run', anchorKey: 'run', notes: [] }] },
    activeMirrorMsg: partial,
    activitySourceBlocks: partial.blocks,
    useDbActivePayload: false,
    hasLivePayload: true,
    streamItems: [{ type: 'text', content: newText }],
    chatId,
    isStreaming: true,
  }))
  assert.deepEqual(frames(html), { errors: 0, frames: 2, sizedA: true })
})

test('(c) a joined steer replay frames images from the later row', () => {
  const first = assistant(oldText, { media_dimensions: { [A]: sizeA } })
  const carrier = { role: 'user', hidden: true, steered: true, source_work_id: 'run', kind: 'peer_message' }
  const second = assistant(newText, {
    id: 'run:assistant:1',
    media_dimensions: { [A]: sizeA, [B]: { width: 300, height: 600 } },
  })
  const group = assistantReplyGroups([first, carrier, second]).get(0)
  assert.equal(group.rows.length, 2)
  const html = renderToStaticMarkup(createElement(AssistantReply, {
    replyGroup: group,
    activeRowIndex: 1,
    activeMirrorMsg: second,
    activitySourceBlocks: second.blocks,
    useDbActivePayload: true,
    hasLivePayload: false,
    chatId,
    isStreaming: false,
  }))
  // The first row now displays the joined text with its own smaller map.
  assert.deepEqual(frames(html), { errors: 0, frames: 2, sizedA: true })
})
