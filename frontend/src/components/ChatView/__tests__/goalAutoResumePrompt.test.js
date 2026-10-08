import assert from 'node:assert/strict'
import test from 'node:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'

import GoalAutoResumePrompt, {
  isGoalAutoResumeDismissed,
  rememberGoalAutoResumeDismissed,
  shouldOfferGoalAutoResume,
} from '../GoalAutoResumePrompt.jsx'

test('offers the policy only for actionable top-level Goals that have it disabled', () => {
  assert.equal(shouldOfferGoalAutoResume({
    goalStatus: 'active', autoResumeEnabled: false, policyKnown: true,
  }), true)
  assert.equal(shouldOfferGoalAutoResume({
    goalStatus: 'paused', autoResumeEnabled: false, policyKnown: true,
  }), true)
  assert.equal(shouldOfferGoalAutoResume({
    goalStatus: 'completed', autoResumeEnabled: false,
  }), false)
  assert.equal(shouldOfferGoalAutoResume({
    goalStatus: 'active', autoResumeEnabled: true,
  }), false)
  assert.equal(shouldOfferGoalAutoResume({
    embedded: true, goalStatus: 'active', autoResumeEnabled: false, policyKnown: true,
  }), false)
})

test('waits for the chat policy to load before offering it', () => {
  assert.equal(shouldOfferGoalAutoResume({
    goalStatus: 'active', autoResumeEnabled: false,
  }), false)
  assert.equal(shouldOfferGoalAutoResume({
    goalStatus: 'active', autoResumeEnabled: false, policyKnown: false,
  }), false)
})

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

function memoryStorage() {
  const values = new Map()
  return {
    getItem: (key) => (values.has(key) ? values.get(key) : null),
    setItem: (key, value) => { values.set(key, String(value)) },
  }
}

test('a stored "Not now" suppresses the prompt for that chat and Goal only', () => {
  const storage = memoryStorage()
  rememberGoalAutoResumeDismissed('chat-1', 'goal-1', storage)

  assert.equal(isGoalAutoResumeDismissed('chat-1', 'goal-1', storage), true)
  assert.equal(renderToStaticMarkup(createElement(GoalAutoResumePrompt, {
    chatId: 'chat-1', goalKey: 'goal-1', storage, onEnable() {},
  })), '')
  assert.match(renderToStaticMarkup(createElement(GoalAutoResumePrompt, {
    chatId: 'chat-1', goalKey: 'goal-2', storage, onEnable() {},
  })), /Continue after resets/)
  assert.match(renderToStaticMarkup(createElement(GoalAutoResumePrompt, {
    chatId: 'chat-2', goalKey: 'goal-1', storage, onEnable() {},
  })), /Continue after resets/)
})

test('blocked storage keeps the prompt usable', () => {
  const storage = {
    getItem() { throw new Error('blocked') },
    setItem() { throw new Error('blocked') },
  }
  rememberGoalAutoResumeDismissed('chat-1', 'goal-1', storage)
  assert.equal(isGoalAutoResumeDismissed('chat-1', 'goal-1', storage), false)
  assert.match(renderToStaticMarkup(createElement(GoalAutoResumePrompt, {
    chatId: 'chat-1', goalKey: 'goal-1', storage, onEnable() {},
  })), /Continue after resets/)
})
