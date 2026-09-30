import test from 'node:test'
import assert from 'node:assert/strict'
import { accessRows } from '../../components/Walkthrough/accessReview.js'

test('guide review explains the access in a live app contract', () => {
  const rows = accessRows({
    agent: { system_prompt: { file: 'pages.md' }, skills: ['pages.md'] },
    data: { chat_logs: { effective: 'summary' }, github_access: true, cross_app_access: 'read' },
    background: { mode: 'scheduled' },
    runtime: { 'device.storage': { title: 'Remember data on this device', description: 'Keeps app data in this browser.' } },
  })
  assert.deepEqual(rows.map(row => row.title), [
    'Agent chats', 'Agent skills', 'Chat history', 'Other apps’ data', 'GitHub data',
    'Background work', 'Remember data on this device',
  ])
  assert.match(rows.find(row => row.title === 'Chat history').detail, /redacted/)
  assert.match(rows.find(row => row.title === 'Background work').detail, /schedule/)
})

test('guide review discloses unfamiliar data grants instead of hiding them', () => {
  const rows = accessRows({ data: { future_permission: true, github_connect: false }, future_access: { enabled: true } })
  assert.equal(rows.length, 2)
  assert.equal(rows[0].title, 'Future permission')
  assert.match(rows[0].detail, /App Store/)
  assert.equal(rows[1].title, 'Future access')
})

test('guide review recognizes no special access', () => {
  assert.deepEqual(accessRows({ agent: { skills: [] }, data: { github_access: false, chat_logs: { effective: 'none' } }, runtime: {} }), [])
})
