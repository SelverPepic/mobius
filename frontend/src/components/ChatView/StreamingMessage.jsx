import MsgContent from './MsgContent.jsx'


/**
 * Stable row shell for the one active assistant answer.
 *
 * The DB partial and the live SSE payload both flow through MsgContent. This
 * wrapper never selects a renderer; it only owns the invariant DOM anchor the
 * scroll state machine resolves through `[data-key]`.
 */
export default function StreamingMessage({
  msg,
  dataKey,
  chatId,
  activityMessageId,
  activitySourceBlocks,
  onAnswer,
  onPrepareAnswer,
  onCancelAnswer,
  onResume,
  resumeState,
  onInternalNav,
  autoResumeEnabled,
  autoResumeAvailable,
  autoResumeSaving,
  autoResumeError,
  onAutoResumeChange,
  limitResetElapsed,
  submissionBlocked,
  liveQuestionId,
  pendingQuestionRef,
  resumeCardRef,
  isStreaming,
  isActiveAnswer = true,
  isLastMsg = true,
  anchorKey,
  replyFragment = false,
  emptyReplyRow = false,
  hideReferences = false,
  continuationWait,
  recoveryCredit,
  suppressedQuestionKeys = null,
}) {
  return (
    <li
      className={`chat__msg chat__msg--assistant${replyFragment ? ' chat__msg--reply-fragment' : ''}${emptyReplyRow ? ' chat__msg--reply-empty' : ''}`}
      data-key={dataKey}
      data-source-key={msg.id && msg.id !== dataKey ? msg.id : undefined}
      data-text-owner-key={msg.reply_text_owner_key}
      data-anchor-key={anchorKey && anchorKey !== dataKey ? anchorKey : undefined}
      data-active-assistant={isActiveAnswer ? 'true' : undefined}
      tabIndex={-1}
    >
      <MsgContent
        msg={msg}
        chatId={chatId}
      activityMessageId={activityMessageId}
      activitySourceBlocks={activitySourceBlocks}
        messageKey={dataKey}
        onQuestionAnswer={onAnswer}
        onQuestionSubmitIntent={onPrepareAnswer}
        onQuestionSubmitCancel={onCancelAnswer}
        onResume={onResume}
        resumeState={resumeState}
        onInternalNav={onInternalNav}
        autoResumeEnabled={autoResumeEnabled}
        autoResumeAvailable={autoResumeAvailable}
        autoResumeSaving={autoResumeSaving}
        autoResumeError={autoResumeError}
        onAutoResumeChange={onAutoResumeChange}
        limitResetElapsed={limitResetElapsed}
        submissionBlocked={submissionBlocked}
        isLastMsg={isLastMsg}
        liveQuestionId={liveQuestionId}
        pendingQuestionRef={pendingQuestionRef}
        resumeCardRef={resumeCardRef}
        isActiveAnswer={isActiveAnswer}
        isStreaming={isStreaming}
        hideReferences={hideReferences}
        continuationWait={continuationWait}
        recoveryCredit={recoveryCredit}
        suppressedQuestionKeys={suppressedQuestionKeys}
      />
    </li>
  )
}
