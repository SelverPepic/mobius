/* ActiveAssistantSurface keeps source rows stable through live and saved reply handoffs. */

import { Fragment, memo, useMemo, useContext } from 'react'
import StreamingMessage from './StreamingMessage.jsx'
import {
  carryDurableBlockState,
  streamItemsToAssistantPayload,
} from './streamPromotion.js'
import { projectSteerContinuationMessage } from './steerContinuity.js'
import { mergeProjectedPeerActivity } from './peerTimeline.js'
import { presentAssistantReply, replyRowHasContent } from './assistantReplies.js'
import { PeerTimelineRows } from './PeerTimeline.jsx'
import { PeerTimelineContext } from './peerTimelineContext.js'


/**
 * The active answer is often the most expensive subtree in the shell: a long
 * turn can contain dozens of tool, thinking, text, and question blocks.
 * Composer text belongs to a separate interaction, so a keystroke must not
 * rebuild that tree while the stream inputs themselves are unchanged.
 *
 * React.memo supplies the component boundary. The memoized payload also keeps
 * MsgContent's existing identity comparison effective when some other
 * ChatView-only state changes without advancing the stream.
 */
function ActiveAssistantSurface({
  activeMirrorMsg,
  useDbActivePayload,
  hasLivePayload,
  streamItems,
  chatId,
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
  sealedSteerAssistant,
  replyGroup,
  activeRowIndex = 0,
  isLastMsg = true,
  continuationWait,
  recoveryCredit,
  suppressedQuestionKeys,
}) {
  const positions = useContext(PeerTimelineContext)?.positions
  const msg = useMemo(() => {
    let source = null
    if (useDbActivePayload) {
      source = activeMirrorMsg
    } else if (hasLivePayload) {
      const livePayload = streamItemsToAssistantPayload(streamItems, { finalize: false })
      const blocksWithPeerActivity = mergeProjectedPeerActivity(
        livePayload.blocks,
        activeMirrorMsg?.blocks || [],
        activitySourceBlocks || [],
      )
      source = {
        ...(activeMirrorMsg || replyGroup.rows[activeRowIndex]?.message || {}),
        role: 'assistant',
        // Live rendering keeps running tool state and thinking clock anchors;
        // final promotion converts the same items with finalize=true. The
        // mirrored DB blocks supply only durable interaction state: a catch-up
        // replay can be richer overall while still carrying the original blank
        // form of a question whose answer has already committed.
        ...livePayload,
        blocks: carryDurableBlockState(
          blocksWithPeerActivity,
          activeMirrorMsg?.blocks || [],
        ),
      }
    }
    return replyGroup.rows.length > 1 ? source : projectSteerContinuationMessage(
      sealedSteerAssistant,
      source,
      { active: isStreaming },
    )
  }, [
    activeMirrorMsg,
    activitySourceBlocks,
    hasLivePayload,
    isStreaming,
    sealedSteerAssistant,
    streamItems,
    useDbActivePayload,
    replyGroup,
    activeRowIndex,
  ])

  const rows = useMemo(() => {
    const sourceRows = replyGroup.rows.map((row, index) => (
      index === (activeRowIndex >= 0 ? activeRowIndex : replyGroup.rows.length - 1)
        ? { ...row, message: msg || row.message } : row
    ))
    return presentAssistantReply(sourceRows, {
      activeIndex: isStreaming ? activeRowIndex : -1,
      positions,
    })
  }, [replyGroup, activeRowIndex, msg, isStreaming, positions])

  if (!msg) return null

  const lastVisibleRow = rows.findLastIndex(item => !item.message.hidden)
  return rows.map((row, index) => {
    const active = index === activeRowIndex
    const tail = index === lastVisibleRow
    return <Fragment key={row.key}>
      <PeerTimelineRows notes={row.notes} chatId={chatId} onInternalNav={onInternalNav} />
      {!row.message.hidden && <StreamingMessage
        msg={row.message}
        dataKey={row.key}
        anchorKey={row.anchorKey}
        chatId={chatId}
        activityMessageId={row.message.id}
        activitySourceBlocks={active ? activitySourceBlocks : replyGroup.rows[index].message.blocks}
        onAnswer={onAnswer}
        onPrepareAnswer={onPrepareAnswer}
        onCancelAnswer={onCancelAnswer}
        onResume={tail ? onResume : undefined}
        resumeState={resumeState}
        onInternalNav={onInternalNav}
        autoResumeEnabled={tail && autoResumeEnabled}
        autoResumeAvailable={tail && autoResumeAvailable}
        autoResumeSaving={tail && autoResumeSaving}
        autoResumeError={tail ? autoResumeError : ''}
        onAutoResumeChange={tail ? onAutoResumeChange : undefined}
        limitResetElapsed={tail && limitResetElapsed}
        submissionBlocked={submissionBlocked}
        liveQuestionId={liveQuestionId}
        pendingQuestionRef={pendingQuestionRef}
        resumeCardRef={resumeCardRef}
        isStreaming={active && isStreaming}
        isActiveAnswer={active}
        isLastMsg={tail && isLastMsg}
        hideReferences={isStreaming}
        continuationWait={tail ? continuationWait : null}
        recoveryCredit={tail ? recoveryCredit : null}
        suppressedQuestionKeys={suppressedQuestionKeys}
        replyFragment={index > 0}
        emptyReplyRow={index > 0 && !replyRowHasContent(row.message)}
      />}
    </Fragment>
  })

}

export default memo(ActiveAssistantSurface)
