/* The instance's GitHub account row in the Settings Accounts card: connect with a device code, add private-repo access, or disconnect. */
import { useCallback, useEffect, useRef, useState } from 'react'
import ProviderRow from '../ProviderAuth/ProviderRow.jsx'
import StatusDot from '../ui/StatusDot.jsx'
import {
  cancelGithubSignIn,
  disconnectGithub,
  fetchGithubStatus,
  hasFullPrAccess,
  hasPrivateRepoAccess,
  startGithubSignIn,
  waitForGithubSignIn,
} from '../../lib/githubConnection.js'

function SignInCode({ attempt, retrying, onCancel }) {
  const [copied, setCopied] = useState(false)
  const copy = () => navigator.clipboard?.writeText(attempt.userCode).then(() => setCopied(true), () => {})
  return (
    <div className="codex-auth">
      <p className="pa__muted">Copy this one-time code, then open GitHub and paste it to continue.</p>
      <div className="codex-auth__device">
        <span className="codex-auth__code-copy">
          <code className="codex-auth__code" aria-label="GitHub device code" onClick={copy}>{attempt.userCode}</code>
          <button type="button" className="pa__btn pa__btn--sm codex-auth__copy-btn" onClick={copy}>
            {copied ? 'Copied' : 'Copy code'}
          </button>
        </span>
      </div>
      <div className="codex-auth__pending-actions">
        <p className="pa__muted codex-auth__waiting" role="status" aria-live="polite">
          {retrying ? 'GitHub is not responding. Retrying…' : 'Waiting for sign-in to complete…'}
        </p>
        <a className="pa__btn pa__btn--sm" href={attempt.verificationUri} target="_blank" rel="noopener noreferrer">Open GitHub</a>
        <button type="button" className="pa__btn pa__btn--sm" onClick={onCancel}>Cancel</button>
      </div>
    </div>
  )
}

export default function GithubConnection({ active = true, focusRef, attention = false }) {
  const [conn, setConn] = useState({ state: 'checking' })
  const [expanded, setExpanded] = useState(false)
  // null, 'starting', or the attempt being waited on.
  const [signIn, setSignIn] = useState(null)
  const [retrying, setRetrying] = useState(false)
  const [message, setMessage] = useState('')
  const [includePrivate, setIncludePrivate] = useState(false)
  const [confirmDisconnect, setConfirmDisconnect] = useState(false)
  const [busy, setBusy] = useState(false)
  const waitRef = useRef(null)

  const refresh = useCallback(async () => {
    const next = await fetchGithubStatus()
    setConn(next)
    return next
  }, [])

  useEffect(() => () => waitRef.current?.abort(), [])

  const waitFor = useCallback(async (attempt) => {
    waitRef.current?.abort()
    const controller = new AbortController()
    waitRef.current = controller
    setSignIn(attempt)
    setExpanded(true)
    const result = await waitForGithubSignIn(attempt.attemptId, { signal: controller.signal, onRetrying: setRetrying })
    if (controller.signal.aborted) return
    waitRef.current = null
    setSignIn(null)
    setRetrying(false)
    if (result.status === 'complete') setIncludePrivate(false)
    else setMessage(result.message || '')
    await refresh()
  }, [refresh])

  const start = useCallback(async (privateRepos) => {
    setMessage('')
    setSignIn('starting')
    try {
      await waitFor(await startGithubSignIn({ privateRepos }))
    } catch (error) {
      setSignIn(null)
      setMessage(error.message)
    }
  }, [waitFor])

  // Resume only when Settings opens, not when a local wait finishes or is cancelled.
  useEffect(() => {
    if (!active) return
    let disposed = false
    void refresh().then(next => {
      if (!disposed && next.attempt && !waitRef.current) void waitFor(next.attempt)
    })
    return () => { disposed = true }
  }, [active, refresh, waitFor])

  const cancel = useCallback(async () => {
    const attemptId = signIn?.attemptId
    waitRef.current?.abort()
    waitRef.current = null
    setSignIn(null)
    setRetrying(false)
    if (attemptId) await cancelGithubSignIn(attemptId).catch(() => {})
    await refresh()
  }, [signIn, refresh])

  const disconnect = useCallback(async () => {
    setBusy(true)
    setMessage('')
    try {
      await disconnectGithub()
    } catch (error) {
      setMessage(error.message)
    }
    // Disconnect is idempotent; the fresh status is the truth either way.
    await refresh()
    setBusy(false)
    setConfirmDisconnect(false)
  }, [refresh])

  const connected = conn.state === 'connected'
  const privateAccess = connected && hasPrivateRepoAccess(conn.scopes)
  const needsReconnect = connected && !hasFullPrAccess(conn.scopes)

  const statusNode = {
    checking: <StatusDot color="--muted">Checking…</StatusDot>,
    unknown: <StatusDot color="--danger">Status unavailable</StatusDot>,
    disconnected: undefined,
    connected: needsReconnect
      ? <StatusDot color="--danger">Reconnect needed</StatusDot>
      : <StatusDot color="--green">{conn.login}{privateAccess ? ' · private repos' : ''}</StatusDot>,
  }[conn.state]

  const note = message ? <p className="pa__error" role="status">{message}</p> : null
  const button = (label, onClick, extra = '') => (
    <button type="button" className={`pa__btn pa__btn--sm${extra}`} disabled={busy} onClick={onClick}>{label}</button>
  )
  let panel
  if (signIn === 'starting') {
    panel = <p className="pa__muted" role="status">Starting GitHub sign-in…</p>
  } else if (signIn) {
    panel = <SignInCode attempt={signIn} retrying={retrying} onCancel={cancel} />
  } else if (conn.state === 'unknown') {
    panel = (
      <div className="provider-connection">
        <p className="pa__muted">{conn.message}</p>
        <div className="provider-connection__actions">{button('Check again', refresh)}</div>
      </div>
    )
  } else if (!connected) {
    panel = conn.signInAvailable ? (
      <div className="provider-connection">
        <p className="pa__muted">Used to send changes, open pull requests, and follow reviews as you.</p>
        <label className="settings-github__check">
          <input type="checkbox" checked={includePrivate} onChange={event => setIncludePrivate(event.target.checked)} />
          Include private repositories
        </label>
        <div className="provider-connection__actions">
          <button type="button" className="pa__btn" onClick={() => start(includePrivate)}>Connect GitHub</button>
        </div>
        {note}
      </div>
    ) : <p className="pa__muted">GitHub sign-in is not configured for this Möbius instance.</p>
  } else if (confirmDisconnect) {
    panel = (
      <div className="provider-connection">
        <p className="pa__muted">Disconnect GitHub? Saved drafts and review history stay in your apps.</p>
        <div className="provider-connection__actions">
          {button('Cancel', () => setConfirmDisconnect(false))}
          {button(busy ? 'Disconnecting…' : 'Disconnect', disconnect, ' provider-connection__disconnect')}
        </div>
        {note}
      </div>
    )
  } else {
    panel = (
      <div className="provider-connection">
        {needsReconnect ? (
          <p className="pa__muted">This older connection lacks access Möbius now needs to send changes. Disconnect, then connect again.</p>
        ) : null}
        <div className="provider-connection__actions">
          {!needsReconnect && !privateAccess ? button('Add private repositories', () => start(true)) : null}
          {button('Disconnect…', () => setConfirmDisconnect(true))}
        </div>
        {note}
      </div>
    )
  }

  // A row inside the Settings Accounts card; the wrapper is the focus target
  // apps reach with moebius:open-settings section 'github'.
  return (
    <div
      className={`settings-github${attention ? ' settings-setup-target' : ''}`}
      id="settings-github"
      ref={focusRef}
      tabIndex={-1}
    >
      <ProviderRow
        name="GitHub"
        connected={connected}
        statusNode={statusNode}
        actionLabel={connected ? 'Manage' : 'Connect'}
        disabled={conn.state === 'checking'}
        expanded={expanded}
        onToggleExpand={() => {
          // Closing never cancels a sign-in; the server keeps it and it resumes.
          setConfirmDisconnect(false)
          setExpanded(value => !value)
        }}
      >
        {panel}
      </ProviderRow>
    </div>
  )
}
