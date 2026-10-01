import { after, test } from 'node:test'
import assert from 'node:assert/strict'
import { createElement as h } from 'react'
import { renderToStaticMarkup as render } from 'react-dom/server'
import { createServer } from 'vite'
import { goalContinuationHandoff } from '../chatHandoffPresentation.js'

const vite = await createServer({ appType: 'custom', logLevel: 'error', server: { middlewareMode: true, hmr: false, ws: false }, ssr: { noExternal: ['@openai/apps-sdk-ui'] } })
after(() => vite.close())
const { default: CompactHandoff } = await vite.ssrLoadModule('/src/components/ChatView/CompactHandoff.jsx')
const { default: GoalHandoff } = await vite.ssrLoadModule('/src/components/ChatView/GoalHandoff.jsx')
const { default: GoalHistoryCard } = await vite.ssrLoadModule('/src/components/ChatView/GoalHistoryCard.jsx')
const { RetainedGoalContext } = await vite.ssrLoadModule('/src/components/ChatView/retainedGoalContext.js')
const { WaitCard } = await vite.ssrLoadModule('/src/components/ChatView/WaitingChip.jsx')
const goal = { id: 'goal-a', revision: 4, objective: 'Verify release', status: 'paused', pause_reason: 'deferred', hold_reason: 'Paid verification was deferred.', handoff: { kind: 'none' } }

test('compact hold keeps the reason and Continue visible while detail starts closed', () => {
  const html = render(h(GoalHandoff, { goal, handoff: goalContinuationHandoff(goal), resumeState: {}, onContinue: () => {} }))
  assert.match(html, /On hold/)
  assert.match(html, /Paid verification was deferred/)
  assert.match(html, /Continue this work/)
  assert.match(html, /No answer needed now/)
  assert.match(html, /aria-expanded="false"/)
  assert.doesNotMatch(html, /<dl|role="alert"/)
})

test('details explain action scope without changing the action', () => {
  const html = render(h(CompactHandoff, { stateLabel: 'On hold', ariaLabel: 'Goal hold', expanded: true,
    rows: [{ label: 'Scope', value: 'Continuing does not approve a declined action.' }],
    action: { label: 'Continue this work', onClick: () => {} },
  }))
  assert.match(html, /aria-expanded="true"/)
  assert.match(html, /Continuing does not approve/)
  assert.match(html, /Continue this work/)
})

test('pending, unavailable and failed exact continuation remain visible and honest', () => {
  for (const resumeState of [{ pending: true }, { unavailable: true }]) {
    const html = render(h(GoalHandoff, { goal, handoff: goalContinuationHandoff(goal), resumeState }))
    assert.match(html, /disabled=""/)
  }
  const html = render(h(GoalHandoff, { goal, handoff: goalContinuationHandoff(goal), resumeState: { error: 'This Goal changed. Review its latest state.' } }))
  assert.match(html, /role="alert"/)
  assert.match(html, /This Goal changed/)
})

test('completed outcome remains a transcript receipt after leaving the current progress rail', () => {
  const html = render(h(GoalHistoryCard, { summary: { id: 'goal-a', objective: 'Verify release', status: 'completed', task_totals: { completed: 3 }, result: { summary: 'Verified in staging.' } } }))
  assert.match(html, /Verify release/)
  assert.match(html, /Completed/)
  assert.doesNotMatch(html, /Continue this work/)
})


test('only the exact retained terminal receipt exposes Clear, never a historical sibling', () => {
  for (const status of ['completed', 'cannot_complete', 'cancelled']) {
    const summary = { id: 'goal-a', objective: 'Verify release', status }
    const retained = { id: 'goal-a', onClear: () => {} }
    const html = render(h(RetainedGoalContext.Provider, { value: retained }, h(GoalHistoryCard, { summary })))
    assert.match(html, /Clear retained Goal/)
    assert.doesNotMatch(html, /Confirm clear Goal/)
    const other = render(h(RetainedGoalContext.Provider, { value: { ...retained, id: 'goal-b' } }, h(GoalHistoryCard, { summary })))
    assert.doesNotMatch(other, /Clear retained Goal/)
  }
})

test('collapsed cancellable Wait exposes Stop while blocked delivery reveals its existing recovery', () => {
  const wait = { id: 'wait-a', kind: 'timer', description: 'Deployment ready' }
  const props = { expanded: false, onToggle: () => {}, onCancel: () => {}, onRevealRecovery: () => {} }
  const html = render(h(WaitCard, { ...props, wait }))
  assert.match(html, /Stop waiting/)
  assert.doesNotMatch(html, /<dl/)
  for (const resume_blocker of ['manual_resume', 'resume_failed', 'restart']) {
    const blocked = render(h(WaitCard, { ...props, wait: { ...wait, delivery_pending: true, resume_blocker } }))
    assert.match(blocked, /View recovery/)
    assert.doesNotMatch(blocked, /Stop waiting/)
  }
  const activation = render(h(WaitCard, { ...props, wait: { ...wait, kind: 'platform_activation' } }))
  assert.doesNotMatch(activation, /Stop waiting/)
})
