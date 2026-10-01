import { useEffect, useMemo, useRef, useState } from 'react'
import { apiFetch, jsonOrThrow } from '../../../api/client.js'

/** Own lazy reads, not their presentation. Equivalent range URLs keep the same
 * request alive through transcript renders; current peer rows are merged by
 * the caller after reading. A changed plan can never expose an old result. */
export default function useActivityDetails({
  urls,
  requested,
  attempt = 0,
  onReady,
  request = apiFetch,
}) {
  const planKey = JSON.stringify(urls)
  const plan = useMemo(() => JSON.parse(planKey), [planKey])
  const resultKey = `${planKey}:${attempt}`
  const [result, setResult] = useState(null)
  const readyRef = useRef(onReady)
  readyRef.current = onReady
  const current = result?.key === resultKey ? result : null

  useEffect(() => {
    if (!requested || !plan.some(Boolean) || current) return undefined
    const controller = new AbortController()
    let active = true
    // apiFetch's deadline ends at headers. This owner must bound body decoding
    // too, since a restored-open read holds the chat's reading-layout gate.
    const deadline = setTimeout(() => {
      const error = new Error('Activity detail timed out')
      error.name = 'TimeoutError'
      controller.abort(error)
    }, 15_000)
    Promise.all(plan.map(url => url
      ? request(url, { signal: controller.signal })
        .then(res => jsonOrThrow(res, 'Activity detail failed'))
        .then(data => Array.isArray(data.entries) ? data.entries : [])
      : null))
      .then(entries => {
        if (!active) return
        readyRef.current?.()
        setResult({ key: resultKey, entries, error: false })
      })
      .catch(() => {
        // Only this effect's cleanup is cancellation; an active abort is a
        // terminal read failure and must release restored-layout readiness.
        if (!active) return
        readyRef.current?.()
        setResult({ key: resultKey, entries: null, error: true })
      })
      .finally(() => clearTimeout(deadline))
    return () => {
      active = false
      clearTimeout(deadline)
      controller.abort()
    }
  }, [current, plan, requested, request, resultKey])

  return { entries: current?.entries ?? null, error: current?.error ?? false }
}
