/* Resuming sign-in is an activation action, not a side effect of ending a wait. */
import assert from 'node:assert/strict'
import { test } from 'node:test'
import { setTimeout as tick } from 'node:timers/promises'
import { api } from '../../api/client.js'
import GithubConnection from '../../components/SettingsView/GithubConnection.jsx'
import { renderHook } from '../../components/ChatView/hooks/__tests__/react-hook-shim.mjs'

const json = body => new Response(JSON.stringify(body), { status: 200 })
const attempt = { attempt_id: 'a1', user_code: 'TEST-CODE', verification_uri: 'https://github.com/login/device' }

for (const connected of [false, true]) {
  test(`cancel a resumed ${connected ? 'private-access upgrade' : 'sign-in'} without restarting its wait`, async t => {
    const original = api.github
    t.after(() => { api.github = original })
    let polls = 0
    let cancelled = false
    let releaseCancel
    api.github = {
      status: async () => json({ connected, device_flow_available: true, active_attempt: cancelled ? null : attempt }),
      connectPoll: async () => { polls++; return json({ status: 'pending', retry_after: 100 }) },
      connectCancel: async id => {
        assert.equal(id, 'a1')
        await new Promise(resolve => { releaseCancel = resolve })
        cancelled = true
        return json({ status: 'cancelled' })
      },
    }
    const view = renderHook(() => GithubConnection({ active: true }))
    t.after(() => view.unmount())
    await tick(20)
    const panel = view.result.current.props.children.props.children
    assert.equal(panel.props.attempt.attemptId, 'a1')
    assert.equal(polls, 1)
    const cancellation = panel.props.onCancel()
    await tick(20)
    assert.equal(polls, 1, 'cancelling must not resume the cached attempt')
    releaseCancel()
    await cancellation
    await tick(20)
    assert.equal(polls, 1)
    assert.equal(view.result.current.props.children.props.children.props.attempt, undefined)
  })
}
