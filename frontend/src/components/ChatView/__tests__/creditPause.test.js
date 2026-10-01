/* Credit exhaustion is an informational, manually continued chat pause. */
import { after, test } from 'node:test'
import assert from 'node:assert/strict'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createServer } from 'vite'
import { isCreditPause, isResumableError, ownsRecoveryAction } from '../recoveryCard.js'
import { supersedeResumedPauseBlocks } from '../chatRuntimeState.js'
import { streamItemsToAssistantPayload } from '../streamPromotion.js'

const vite = await createServer({
  appType: 'custom', logLevel: 'error',
  server: { middlewareMode: true, hmr: false, ws: false },
  ssr: { noExternal: ['@openai/apps-sdk-ui'] },
})
const { default: ErrorCard, errorCardViewModel } = await vite.ssrLoadModule('/src/components/ChatView/ErrorCard.jsx')
const { default: MsgContent } = await vite.ssrLoadModule('/src/components/ChatView/MsgContent.jsx')
after(() => vite.close())

const creditBlock = { type: 'error', message: 'Your workspace is out of credits. Add credits to continue.' }
const message = { role: 'assistant', content: '', blocks: [creditBlock] }

test('the exact workspace-credit rejection becomes a calm pause without rewriting history', () => {
  const original = structuredClone(creditBlock)
  assert.equal(isCreditPause(creditBlock), true)
  assert.equal(isResumableError(creditBlock), true)
  const vm = errorCardViewModel(creditBlock)
  assert.equal(vm.benign, true)
  assert.equal(vm.parked, false)
  assert.equal(vm.label, 'Credits needed')
  const html = renderToStaticMarkup(createElement(ErrorCard, { block: creditBlock }))
  assert.match(html, /chat__text--parked/)
  assert.match(html, /Your progress is saved/)
  assert.match(html, /Add credits to your workspace or choose another provider, then Continue/)
  assert.doesNotMatch(html, /role="alert"|>Error<|automatically|retry check|Rate limit/)
  assert.deepEqual(creditBlock, original)
})

test('live stream and persisted credit blocks have identical informative bodies', () => {
  const payload = streamItemsToAssistantPayload([{ ...creditBlock, seq: 1 }])
  assert.equal(errorCardViewModel(payload.blocks[0]).credits, true)
  const render = block => renderToStaticMarkup(createElement(ErrorCard, { block }))
  assert.equal(render(payload.blocks[0]), render(creditBlock))
})

test('only the actionable transcript tail offers Continue, never automatic paid recovery', () => {
  const render = props => renderToStaticMarkup(createElement(MsgContent, {
    msg: message, isLastMsg: true, onResume() {},
    autoResumeAvailable: true, autoResumeEnabled: true, onAutoResumeChange() {},
    ...props,
  }))
  const html = render({})
  assert.match(html, />Continue<\/button>/)
  assert.doesNotMatch(html, /auto-continue|Try now|>Resume<\/button>/)
  assert.doesNotMatch(render({ isLastMsg: false }), />Continue<\/button>/)
  assert.doesNotMatch(render({ onResume: undefined }), />Continue<\/button>/)
  const ownership = { block: creditBlock, entryIndex: 0, lastEntryIndex: 0, isLastMessage: true, canResume: true }
  assert.equal(ownsRecoveryAction(ownership), true)
  assert.equal(ownsRecoveryAction({ ...ownership, questionOwnsTurn: true }), false)
  assert.equal(ownsRecoveryAction({ ...ownership, lastEntryIndex: 1 }), false)
})

test('credit continuation keeps existing pending and unavailable button states', () => {
  for (const [resumeState, label] of [[{ pending: true }, 'Resuming…'], [{ unavailable: true }, 'Reconnecting…']]) {
    const html = renderToStaticMarkup(createElement(MsgContent, {
      msg: message, isLastMsg: true, onResume() {}, resumeState,
    }))
    assert.match(html, new RegExp(`>${label}<\\/button>`))
    assert.match(html, /disabled=""/)
  }
})

test('accepted continuation supersedes the old credit pause only in the render projection', () => {
  const messages = [message, { role: 'user', kind: 'continuation', content: '' }]
  const projected = supersedeResumedPauseBlocks(messages)
  assert.equal(projected[0].hidden, true)
  assert.deepEqual(projected[0].blocks, [])
  assert.deepEqual(messages[0].blocks, [creditBlock])
  assert.equal(supersedeResumedPauseBlocks([message])[0], message)
})

test('unrelated payment failures remain errors rather than invitations to retry', () => {
  for (const block of [
    { type: 'error', message: 'Payment authorization failed.' },
    { type: 'error', message: '402 Payment Required' },
    { type: 'error', message: 'Could not load credits.' },
    { type: 'text', message: creditBlock.message },
  ]) {
    assert.equal(isCreditPause(block), false)
    assert.equal(isResumableError(block), false)
    assert.equal(errorCardViewModel(block).benign, false)
  }
})
