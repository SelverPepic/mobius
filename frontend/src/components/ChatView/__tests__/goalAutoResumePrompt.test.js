import assert from 'node:assert/strict'
import test from 'node:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'

import GoalAutoResumePrompt from '../GoalAutoResumePrompt.jsx'

test('offers a standby-safe provider-reset continuation without promising manual-stop recovery', () => {
  const html = renderToStaticMarkup(createElement(GoalAutoResumePrompt, {
    goalKey: 'goal-1',
    onEnable() {},
  }))

  assert.match(html, /Continue after resets/)
  assert.match(html, /even while this device sleeps/)
  assert.match(html, /Manual stops stay stopped/)
  assert.match(html, /Not now/)
})

test('does not render without an active goal or persistence owner', () => {
  assert.equal(renderToStaticMarkup(createElement(GoalAutoResumePrompt, {
    goalKey: '',
    onEnable() {},
  })), '')
  assert.equal(renderToStaticMarkup(createElement(GoalAutoResumePrompt, {
    goalKey: 'goal-1',
  })), '')
})

test('keeps save failures visible and the action pending', () => {
  const html = renderToStaticMarkup(createElement(GoalAutoResumePrompt, {
    goalKey: 'goal-1',
    saving: true,
    error: 'Could not save',
    onEnable() {},
  }))

  assert.match(html, /Enabling…/)
  assert.match(html, /role="alert"/)
  assert.match(html, /Could not save/)
})
