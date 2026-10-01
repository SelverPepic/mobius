/* Pure ownership policy for the one actionable recovery card at transcript tail. */

// This provider rejection has no structured pause descriptor. Interpret it at
// the presentation boundary, including saved transcripts, without rewriting
// messages or treating unrelated payment failures as recoverable credit waits.
export function isCreditPause(block) {
  return block?.type === 'error' && (
    block.pause?.kind === 'credits'
    || block.message?.trim() === 'Your workspace is out of credits. Add credits to continue.'
  )
}

export function isResumableError(block) {
  return block?.type === 'error' && (block.resumable === true || isCreditPause(block))
}

export function ownsRecoveryAction({
  block,
  entryIndex,
  lastEntryIndex,
  isLastMessage,
  canResume,
  questionOwnsTurn = false,
}) {
  return !!(
    isResumableError(block)
    && !questionOwnsTurn
    && isLastMessage
    && canResume
    && entryIndex === lastEntryIndex
  )
}
