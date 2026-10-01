/* Present a retained Goal's exact continuation in the conversation, not only its plan rail. */
import { useState } from 'react'
import CompactHandoff from './CompactHandoff.jsx'

export default function GoalHandoff({ handoff, goal, resumeState, onContinue, disabled }) {
  const [expanded, setExpanded] = useState(false)
  return <CompactHandoff
    state={handoff.state}
    stateLabel={handoff.label}
    ariaLabel={`${handoff.label}: ${goal.objective}`}
    description={handoff.description}
    meta={handoff.state === 'on_hold' ? 'No answer needed now · Work is saved' : 'Original outcome is unfinished'}
    expanded={expanded}
    onToggle={() => setExpanded(value => !value)}
    rows={[
      { label: 'Goal', value: goal.objective },
      { label: 'Next move', value: handoff.next },
      { label: 'Scope', value: handoff.boundary },
    ]}
    action={{
      label: resumeState.pending ? 'Continuing…' : handoff.actionLabel,
      disabled: disabled || resumeState.pending || resumeState.unavailable,
      error: resumeState.error,
      onClick: onContinue,
    }}
  />
}
