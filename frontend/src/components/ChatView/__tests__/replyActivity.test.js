/* Continuous reply activity uses selected live data without losing source identities. */
import test from 'node:test'
import assert from 'node:assert/strict'
import { presentAssistantActivity, assistantReplyGroups } from '../assistantReplies.js'
import { mergeProjectedActivity, foldPeerActivity } from '../peerTimeline.js'
import { mergeAdjacentCompactActivityEntries, insertPositionedActivity } from '../activityPosition.js'
const tool = (id, status = 'done') => ({ type: 'tool', tool: 'Bash', tool_use_id: id, status })
const row = (n, blocks, extra = {}) => ({ key: `key-${n}`, index: n,
  notes: [], message: { role: 'assistant', id: `run:assistant:${n + 1}`, blocks, content: '', ...extra } })

test('selected live tools extend the original stretch and leave the active source anchor intact', () => {
  const before = [row(0, [tool('a'), tool('b')]), row(1, [tool('c', 'running')])]
  const snapshot = JSON.stringify(before)
  const { rows } = presentAssistantActivity(before, { activeIndex: 1 })
  assert.deepEqual(rows[0].message.blocks.map(b => b.tool_use_id), ['a', 'b', 'c'])
  assert.equal(rows[0].message.blocks.at(-1).reply_activity_live, true)
  assert.equal(rows[1].key, 'key-1')
  assert.equal(rows[1].message.hidden, undefined)
  assert.deepEqual(rows[1].message.blocks, [])
  assert.equal(rows[1].message.content, '')
  assert.equal(JSON.stringify(before), snapshot)
})

test('prose, question and outcome metadata keep their real boundaries and live ownership', () => {
  for (const boundary of [{ type: 'text', content: 'Explanation' }, { type: 'question', question_id: 'q' }]) {
    const { rows } = presentAssistantActivity([row(0, [tool('a')]), row(1, [tool('b'), boundary])], { activeIndex: 1 })
    assert.equal(rows[0].message.blocks.some(b => b.reply_activity_live), false)
    assert.deepEqual(rows[1].message.blocks.map(b => b.type), [boundary.type])
  }
  for (const extra of [{ continuation_reason: 'resume' }, { wait_summaries: [{ id: 'w' }] }]) {
    const { rows } = presentAssistantActivity([row(0, [tool('a')]), row(1, [tool('b')], extra)], { activeIndex: 1 })
    assert.equal(rows[0].message.blocks.length, 1)
  }
})

test('moved timeline notes retain original coordinates including the active tail boundary', () => {
  const before = [row(0, [tool('a')]), row(1, [tool('b')])]
  const note = { id: 'peer', display_position: { assistant_message_id: before[1].message.id, block_index: 1 } }
  const { positions } = presentAssistantActivity(before, { activeIndex: 1, positions: new Map([[before[1].message.id, [note]]]) })
  assert.equal(positions.get(before[0].message.id)[0].display_position.source_message_id, before[1].message.id)
  assert.equal(positions.has(before[1].message.id), false)
})

test('a compact saved stretch keeps its mount key and detail range while raw live tools append', () => {
  const saved = { type: 'activity', activity_id: 'saved-activity', message_index: 4, start: 0, end: 8,
    entries: [{ idx: 0, item: tool('a') }], tool_count: 8 }
  const entries = [saved, { ...tool('b', 'running'), reply_activity_live: true }].map((item, idx) => ({ item, idx }))
  const combined = mergeAdjacentCompactActivityEntries(entries)[0].item
  assert.equal(combined.activity_id, saved.activity_id)
  assert.equal(combined.reply_activity_live, true)
  assert.deepEqual(combined.detail_segments[0].detail_ref, { message_index: 4, start: 0, end: 8 })
  const grown = mergeAdjacentCompactActivityEntries([...entries, { idx: 2, item: tool('c') }])[0].item
  assert.equal(grown.activity_id, saved.activity_id)
})

test('Goal-root helper delivery connects the same physical reply, never a different run or foreign peer', () => {
  const before = row(0, [tool('a')]).message
  const after = row(1, [tool('b')]).message
  const helper = { role: 'user', hidden: true, steered: true, kind: 'delegation_result', source_work_id: 'logical-goal' }
  assert.equal(assistantReplyGroups([before, helper, after]).get(0).rows.length, 2)
  assert.equal(assistantReplyGroups([before, { ...helper, kind: 'peer_message' }, after]).get(0).rows.length, 1)
  assert.equal(assistantReplyGroups([before, helper, { ...after, id: 'different:assistant:2' }]).get(0).rows.length, 1)
})

test('an undelivered wait outcome stays after the continuing activity, not before it', () => {
  const before = [row(0, [tool('a')]), row(1, [tool('b')], { wait_summaries: [{ status: 'met', delivery_pending: true }] })]
  const { rows } = presentAssistantActivity(before, { activeIndex: 1 })
  assert.equal(rows[0].message.blocks.length, 2)
  assert.equal(rows[1].message.wait_summaries[0].delivery_pending, true)
})

test('projected helper and peer boundaries survive the chosen live source once, in order', () => {
  const old = tool('a')
  const live = { ...old, output: 'newer output' }
  const helper = { type: 'helper_result', id: 'helper', activityId: 'helper:one' }
  const peer = { type: 'tool', tool: 'PeerMessage', tool_use_id: 'peer-one' }
  const projected = [peer, old, helper]
  assert.deepEqual(mergeProjectedActivity([live], projected, [old]), [peer, live, helper])
  assert.deepEqual(mergeProjectedActivity([peer, live, helper], projected, [old]), [peer, live, helper])
  const folded = foldPeerActivity([row(0, [old]).message], {
    slots: new Map([[1, [helper]]]), tools: new Map(), positions: new Map(),
  }, 'chat')
  assert.equal(folded.slots.size, 0)
  assert.deepEqual(mergeProjectedActivity([live], folded.messages[0].blocks, [old]), [live, helper])
})

test('prepended mail never shifts the recorded boundary of another note in a folded source', () => {
  const a = row(0, [tool('a')]), b = row(1, [tool('b'), tool('c')])
  const notes = [{ id: 'inside', display_position: { assistant_message_id: b.message.id, block_index: 1 }, sender_name: 'Inside' }]
  const projected = foldPeerActivity([a.message, b.message], {
    slots: new Map([[1, [{ id: 'before', sender_name: 'Before' }]]]), tools: new Map(), positions: new Map([[b.message.id, notes]]),
  }, 'chat')
  const { rows, positions } = presentAssistantActivity([a, { ...b, message: projected.messages[1] }], { activeIndex: 1, positions: projected.positions })
  const entries = rows[0].message.blocks.map((item, idx) => ({ item, idx }))
  const shown = insertPositionedActivity(entries, positions.get(a.message.id), a.message.blocks, 'chat')
  assert.deepEqual(shown.map(e => e.item.tool_use_id), ['a', 'peer-before', 'b', 'peer-inside', 'c'])
})

test('the first legacy thinking disclosure keeps its ordinal when compact activity grows', () => {
  const thinking = { idx: 0, item: { type: 'thinking', content: 'Inspecting' } }
  const saved = { type: 'activity', activity_id: 'saved', entries: [thinking], message_index: 1, start: 0, end: 1 }
  const combined = mergeAdjacentCompactActivityEntries([{ idx: 0, item: saved }, { idx: 1, item: tool('b') }])[0].item
  assert.equal(combined.entries[0].idx, thinking.idx)
})

test('projected helper boundaries survive saved prose with stamped source coordinates', () => {
  const raw = [tool('a'), { type: 'text', content: 'Still checking.' }, tool('b')]
  const helper = { type: 'helper_result', id: 'helper', activityId: 'helper:one' }
  const live = [...raw, tool('c', 'running')]
  const projected = foldPeerActivity([row(0, raw).message], {
    slots: new Map([[1, [helper]]]), tools: new Map(), positions: new Map(),
  }, 'chat').messages[0].blocks
  assert.deepEqual(mergeProjectedActivity(live, projected, raw), [...live, helper])
})
