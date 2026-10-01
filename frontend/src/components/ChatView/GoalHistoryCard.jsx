/* GoalHistoryCard shows a terminal Goal's outcome at its completion step. */

import { useContext, useState } from 'react'
import { RetainedGoalContext } from './retainedGoalContext.js'
import GoalPlanDetails from './GoalPlanDetails.jsx'
import LifecycleIcon, { LifecycleOutcome } from './LifecycleIcon.jsx'
import { goalHistoryViewModel } from './goalHistory.js'

export default function GoalHistoryCard({ summary }) {
  const retained = useContext(RetainedGoalContext)
  const [clearConfirmed, setClearConfirmed] = useState(false)
  const canClear = retained?.id === summary?.id && Boolean(retained?.onClear)
  const view = goalHistoryViewModel(summary)
  if (!view) return null

  return (
    <aside
      className={`chat__goal-history chat__goal-history--${view.completed ? 'completed' : summary.status === 'cancelled' ? 'cancelled' : 'failed'}`}
      aria-label={view.ariaLabel}
    >
      <LifecycleIcon kind="goal" />
      <div className="chat__goal-history-copy">
        <span className="chat__goal-history-kicker">
          <LifecycleOutcome tone={view.completed ? 'completed' : summary.status === 'cancelled' ? 'stopped' : 'attention'} />{view.kicker}
        </span>
        <strong className="chat__goal-history-objective">{view.objective}</strong>
        {view.reason && <span className="chat__goal-history-reason">{view.reason}</span>}
        {view.metadata && <span className="chat__goal-history-meta">{view.metadata}</span>}
        {view.hasPlan && (
          <details className="chat__goal-history-details">
            <summary>View plan</summary>
            <GoalPlanDetails plan={summary.plan} />
          </details>
        )}
        {canClear && <>
          <button type="button" className="chat__goal-history-clear"
            onClick={() => clearConfirmed ? retained.onClear({ goalId: summary.id }) : setClearConfirmed(true)}>
            {clearConfirmed ? 'Confirm clear Goal' : 'Clear retained Goal'}
          </button>
          {retained.error && <span role="alert">{retained.error}</span>}
        </>}
      </div>
    </aside>
  )
}
