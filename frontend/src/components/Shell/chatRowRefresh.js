/**
 * Coalesce run and wait events into scoped reads of just the affected drawer
 * rows, instead of re-reading the complete chat list for every event.
 *
 * One scoped read is in flight at a time; ids that arrive meanwhile wait for
 * the next read, so two answers for the same row never land out of order.
 * Complete list reads still own first load, reconnects, and mutations:
 * - a complete read already in flight when a batch is due began before those
 *   events and would overwrite their rows, so the batch becomes a fresh
 *   complete read instead;
 * - a complete read that starts while a scoped read is in flight began after
 *   its events, so it supersedes the scoped answer, which is dropped.
 * A failed scoped read, including one over the server's id bound, becomes a
 * complete read.
 */
export function createChatRowRefresh({
  readRows,
  applyRows,
  refreshAll,
  fullReadInFlight,
  fullReadsStarted,
  batchMs = 250,
  schedule = setTimeout,
  unschedule = clearTimeout,
}) {
  let pending = new Set()
  let timer = null
  let reading = false

  function arm() {
    if (reading || timer != null || pending.size === 0) return
    timer = schedule(flush, batchMs)
  }

  async function flush() {
    timer = null
    reading = true
    const ids = [...pending]
    pending = new Set()
    try {
      if (fullReadInFlight()) {
        await refreshAll()
        return
      }
      const started = fullReadsStarted()
      const rows = await readRows(ids)
      if (fullReadsStarted() === started) applyRows(ids, rows)
    } catch {
      await refreshAll()
    } finally {
      reading = false
      arm()
    }
  }

  return {
    request(chatId) {
      if (chatId == null) return
      pending.add(String(chatId))
      arm()
    },
    cancel() {
      if (timer != null) unschedule(timer)
      timer = null
    },
  }
}
