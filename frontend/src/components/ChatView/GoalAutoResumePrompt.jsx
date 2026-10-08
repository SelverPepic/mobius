/* GoalAutoResumePrompt makes the existing server-owned limit continuation
   policy discoverable before a long Goal reaches a provider limit. */

import { useState } from 'react'

export function shouldOfferGoalAutoResume({
  embedded = false,
  goalStatus = '',
  autoResumeEnabled = false,
  policyKnown = false,
}) {
  // Until chat info loads, autoResumeEnabled reads false even when the
  // policy is on, so wait for it rather than flash a prompt for a setting
  // that is already enabled.
  return policyKnown
    && !embedded
    && ['active', 'paused'].includes(goalStatus)
    && !autoResumeEnabled
}

const DISMISSED_PREFIX = 'mobius:goal-auto-resume-dismissed:'

function defaultStorage() {
  try {
    return globalThis.localStorage || null
  } catch {
    return null
  }
}

function dismissedStorageKey(chatId, goalKey) {
  return `${DISMISSED_PREFIX}${chatId || ''}:${goalKey}`
}

/** "Not now" is remembered per chat and Goal so a remount does not re-ask. */
export function isGoalAutoResumeDismissed(chatId, goalKey, storage = defaultStorage()) {
  if (!goalKey || !storage) return false
  try {
    return storage.getItem(dismissedStorageKey(chatId, goalKey)) === '1'
  } catch {
    return false
  }
}

export function rememberGoalAutoResumeDismissed(chatId, goalKey, storage = defaultStorage()) {
  if (!goalKey || !storage) return
  try {
    storage.setItem(dismissedStorageKey(chatId, goalKey), '1')
  } catch { /* storage blocked or full: dismissal stays session-only */ }
}

export default function GoalAutoResumePrompt({
  chatId = '',
  goalKey,
  saving = false,
  error = '',
  onEnable,
  storage = defaultStorage(),
}) {
  const [dismissedGoalKey, setDismissedGoalKey] = useState(null)

  if (!goalKey || !onEnable) return null
  if (dismissedGoalKey === goalKey) return null
  if (isGoalAutoResumeDismissed(chatId, goalKey, storage)) return null

  const dismiss = () => {
    rememberGoalAutoResumeDismissed(chatId, goalKey, storage)
    setDismissedGoalKey(goalKey)
  }

  return (
    <aside className="chat__goal-auto-resume" aria-label="Goal continuation">
      <div className="chat__goal-auto-resume-copy">
        <strong>Keep this Goal moving after a usage limit?</strong>
        <span>
          Möbius can continue at the provider&apos;s reported reset—even while
          this device sleeps. Manual stops stay stopped.
        </span>
      </div>
      <div className="chat__goal-auto-resume-actions">
        <button
          type="button"
          className="chat__goal-auto-resume-enable"
          onPointerDown={(event) => event.preventDefault()}
          onClick={() => onEnable(true)}
          disabled={saving}
        >
          {saving ? 'Enabling…' : 'Continue after resets'}
        </button>
        <button
          type="button"
          className="chat__goal-auto-resume-later"
          onPointerDown={(event) => event.preventDefault()}
          onClick={dismiss}
          disabled={saving}
        >
          Not now
        </button>
      </div>
      {error && <span className="chat__goal-auto-resume-error" role="alert">{error}</span>}
    </aside>
  )
}
