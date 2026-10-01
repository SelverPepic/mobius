// Routine receipts stay inspectable without duplicating meaningful chat cards.
import test from 'node:test'
import assert from 'node:assert/strict'
import { groupActivityRuns } from '../activityGrouping.js'
import { activityCollapsedLabel, activityMemoSig } from '../groupBlocks.js'
import { isQuietBookkeepingTool, isDistinctiveActivityTool } from '../toolActivityLabel.js'
import { suppressedQuestionToolIndices } from '../streamReducers.js'

const tool = (name, extra = {}) => ({ type: 'tool', tool: `mobius_control:${name}`, status: 'done', ...extra })
const entry = item => ({ item })
const question = { type: 'question', question_id: 'q1', questions: [{ question: 'Continue?' }] }
const notification = extra => tool('notify_owner', {
  input: JSON.stringify({ title: 'Möbius needs your answer', body: 'Continue?' }), ...extra,
})

test('routine saves share one disclosure and do not advertise their success', () => {
  const saves = ['checkpoint_chat', 'memory_remember', 'reflection_log_friction'].map(name => entry(tool(name)))
  assert.deepEqual(groupActivityRuns(saves), [{ group: saves }])
  assert.equal(activityCollapsedLabel(saves), 'Activity details')
  assert.equal(activityCollapsedLabel(saves, { live: true }), 'Activity details')
  assert.deepEqual(saves.map(e => e.item.tool), [
    'mobius_control:checkpoint_chat', 'mobius_control:memory_remember', 'mobius_control:reflection_log_friction',
  ])
  const reads = [entry({ type: 'tool', tool: 'Read', status: 'done' }), ...saves]
  assert.equal(activityCollapsedLabel(reads), 'Read a file')
})

test('Memory capture folds into routine activity without losing the saved fact', () => {
  const capture = tool('memory_remember', { app_activity: {
    app_slug: 'memory', activity_id: 'memory-capture', status: 'succeeded',
    label: 'Saved to Memory', detail: 'The saved fact',
  } })
  assert.equal(isQuietBookkeepingTool(capture), true)
  assert.equal(isDistinctiveActivityTool(capture), false)
  const nodes = groupActivityRuns([entry(capture), entry(tool('checkpoint_chat'))])
  assert.equal(nodes.length, 1)
  assert.equal(nodes[0].group[0].item.app_activity.detail, 'The saved fact')
  // Historical script-based captures carry the same app-owned identity.
  assert.equal(isQuietBookkeepingTool({ ...capture, tool: 'Bash' }), true)
})

test('failed saves, warnings and useful resources never become quiet receipts', () => {
  for (const extra of [
    { output_exit_code: 1 }, { status: 'failed' },
    { app_activity: { status: 'failed' } },
    { app_activity: { warning: 'Needs attention' } },
    { app_activity: { resources: [{ label: 'Useful result' }] } },
    { output: JSON.stringify({ isError: true, content: [{ type: 'text', text: 'Save failed' }] }) },
  ]) assert.equal(isQuietBookkeepingTool(tool('memory_remember', extra)), false, JSON.stringify(extra))
  assert.equal(isQuietBookkeepingTool(tool('another_app_write')), false)
  const failed = tool('checkpoint_chat', { output_exit_code: 1 })
  assert.notEqual(activityCollapsedLabel([entry(failed)]), 'Activity details')
  assert.notEqual(activityMemoSig([entry(tool('checkpoint_chat'))]), activityMemoSig([entry(failed)]))
})

test('Memory lookup and read results remain distinctive and visible', () => {
  for (const name of ['memory_search', 'memory_read']) {
    const lookup = tool(name, { app_activity: { app_slug: 'memory', activity_id: 'memory-search', status: 'succeeded' } })
    assert.equal(isQuietBookkeepingTool(lookup), false)
    assert.equal(isDistinctiveActivityTool(lookup), true)
  }
})

test('a Q&A owns its successful notification before or after the card for either provider', () => {
  for (const prefix of ['mobius_control:', 'mcp__mobius_control__']) {
    const notice = { ...notification(), tool: `${prefix}notify_owner` }
    assert.deepEqual([...suppressedQuestionToolIndices([notice, question], 'this-chat')], [0])
    assert.deepEqual([...suppressedQuestionToolIndices([question, notice], 'this-chat')], [1])
    assert.deepEqual([...suppressedQuestionToolIndices([{ ...question, answers: {} }, notice])], [1])
  }
})

test('Q&A dedup reads legacy summaries and accepts an explicit same-chat destination', () => {
  const legacy = notification({ input: 'title=Möbius needs your answer, body=Continue?' })
  const explicit = notification({ input: JSON.stringify({ title: 'Möbius needs your answer', target: '/shell/?chat=this-chat' }) })
  assert.deepEqual([...suppressedQuestionToolIndices([legacy, question])], [0])
  assert.deepEqual([...suppressedQuestionToolIndices([explicit, question], 'this-chat')], [0])
  assert.equal(suppressedQuestionToolIndices([explicit, question], 'other-chat').size, 0)
})

test('missing cards, unrelated notifications and failed or running sends stay inspectable', () => {
  assert.equal(suppressedQuestionToolIndices([notification()]).size, 0)
  for (const extra of [
    { status: 'running' }, { status: 'failed' }, { output_exit_code: 1 },
    { output: JSON.stringify({ isError: true }) },
    { input: '{}' }, { input: '{invalid' },
    { input: JSON.stringify({ title: 'Build finished' }) },
    { input: JSON.stringify({ title: 'Möbius needs your answer', target: '/shell/?app=57' }) },
    { input: JSON.stringify({ title: 'Möbius needs your answer', target: null }) },
    { input: 'title=Möbius needs your answer, body=' + 'a'.repeat(200) },
  ]) assert.equal(suppressedQuestionToolIndices([notification(extra), question], 'this-chat').size, 0, JSON.stringify(extra))
})
