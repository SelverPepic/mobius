import { after, test } from 'node:test'
import assert from 'node:assert/strict'
import { createElement as h } from 'react'
import { renderToStaticMarkup as render } from 'react-dom/server'
import { createServer } from 'vite'
import { goalContinuationHandoff } from '../chatHandoffPresentation.js'

const vite = await createServer({ appType: 'custom', logLevel: 'error', server: { middlewareMode: true, hmr: false, ws: false }, ssr: { noExternal: ['@openai/apps-sdk-ui'] } })
after(() => vite.close())
const { default: CompactHandoff } = await vite.ssrLoadModule('/src/components/ChatView/CompactHandoff.jsx')
const { default: ProgressRail } = await vite.ssrLoadModule('/src/components/ChatView/ProgressRail.jsx')
const { default: GoalHistoryCard } = await vite.ssrLoadModule('/src/components/ChatView/GoalHistoryCard.jsx')
const { WaitCard } = await vite.ssrLoadModule('/src/components/ChatView/WaitingChip.jsx')
const goal = { id: 'goal-a', revision: 4, objective: 'Verify release', status: 'paused', pause_reason: 'deferred', hold_reason: 'Paid verification was deferred.', handoff: { kind: 'none' } }

test('a held Goal uses the existing expandable panel with one continuation action', () => {
  const handoff = goalContinuationHandoff(goal)
  const html = render(h(ProgressRail, { items: [{ key: 'goal', label: 'Goal · On hold · 1/2',
    expandable: true, details: h('p', null, handoff.description), actionLabel: handoff.actionLabel,
  }], onActionItem: () => {} }))
  assert.match(html, /chat__progress-rail/)
  assert.match(html, /Goal · On hold/)
  assert.match(html, /Continue this work/)
  assert.match(html, /aria-expanded="false"/)
  assert.doesNotMatch(html, /chat__handoff|role="alert"/)
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

test('Goal panel preserves disabled actions and shows exact continuation failures', () => {
  const item = { key: 'goal', label: 'Goal · On hold', expandable: true,
    actionLabel: 'Continue this work', actionDisabled: true, actionError: 'This Goal changed.' }
  const html = render(h(ProgressRail, { items: [item], onActionItem: () => {} }))
  assert.match(html, /disabled=""/)
  assert.match(html, /role="alert"/)
  assert.match(html, /This Goal changed/)
})

test('completed outcome remains a transcript receipt after leaving the current progress rail', () => {
  const html = render(h(GoalHistoryCard, { summary: { id: 'goal-a', objective: 'Verify release', status: 'completed', task_totals: { completed: 3 }, result: { summary: 'Verified in staging.' } } }))
  assert.match(html, /Verify release/)
  assert.match(html, /Completed/)
  assert.doesNotMatch(html, /Continue this work/)
})


test('all finished Goals are read-only records with an expandable plan', () => {
  for (const status of ['completed', 'cannot_complete', 'cancelled']) {
    const summary = { id: 'goal-a', objective: 'Verify release', status,
      plan: { tasks: [{ id: 'check', title: 'Check release', status: 'completed' }] } }
    const html = render(h(GoalHistoryCard, { summary }))
    assert.match(html, /Verify release/)
    assert.match(html, /View plan/)
    assert.match(html, /Check release/)
    assert.doesNotMatch(html, /<button|Clear|Abandon|Continue this work/)
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
