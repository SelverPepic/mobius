/* A compact handoff keeps its reason and action visible, with supporting detail on demand. */
import { ChevronRight, Clock, Pause, Warning } from '@openai/apps-sdk-ui/components/Icon'
import './CompactHandoff.css'

export default function CompactHandoff({
  expanded, onToggle, ariaLabel, title, text, meta, description,
  rows = [], children, action, stateLabel = 'Waiting', state = 'automatic',
}) {
  const Icon = state === 'on_hold' ? Pause : state === 'recovery' ? Warning : Clock
  return (
    <section className={`chat__handoff chat__handoff--${state}`} aria-label={ariaLabel}>
      <div className="chat__handoff-heading" title={title}>
        <span className="chat__progress-identity" aria-hidden="true"><Icon width={15} height={15} /></span>
        <strong>{stateLabel}{text ? ` · ${text}` : ''}</strong>
      </div>
      {description && <p className="chat__handoff-description">{description}</p>}
      {meta && <p className="chat__handoff-meta">{meta}</p>}
      {action && <div className="chat__handoff-action">
        <button type="button" className="chat__handoff-continue" onClick={action.onClick} disabled={action.disabled}>
          {action.label}
        </button>
        {action.error && <p className="chat__handoff-error" role="alert">{action.error}</p>}
      </div>}
      {(rows.length > 0 || children) && <>
        <button type="button" className="chat__handoff-toggle" aria-expanded={expanded}
          aria-label={`${expanded ? 'Collapse' : 'Expand'} ${ariaLabel}`} onClick={onToggle}>
          <ChevronRight width={14} height={14} aria-hidden="true" />
          Next move and details
        </button>
        {expanded && <div className="chat__handoff-details">
          <dl className="chat__wait-detail-list">
            {rows.map(row => <div key={row.label} className={`chat__wait-detail-row${row.primary ? ' chat__wait-detail-row--primary' : ''}`}>
              <dt>{row.label}</dt><dd>{row.value}</dd>
            </div>)}
          </dl>
          {children}
        </div>}
      </>}
    </section>
  )
}
