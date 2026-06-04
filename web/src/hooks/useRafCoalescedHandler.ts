import { useCallback, useEffect, useLayoutEffect, useRef } from 'react'

/**
 * Coalesce a high-frequency handler so a burst of calls within one animation
 * frame results in a single invocation with the most recent argument.
 *
 * Over higher-latency links (e.g. Tailscale) WebSocket events such as
 * `session_usage_updated` arrive in batched bursts; without coalescing each one
 * triggers its own React state update and re-render, monopolizing the main
 * thread and delaying input. Each usage event is a full snapshot, so applying
 * only the latest per frame is lossless for the rendered value.
 *
 * The returned callback is stable for the lifetime of the component. The latest
 * `handler` is always used at flush time, so callers may pass an inline closure.
 */
export function useRafCoalescedHandler<T>(handler: (latest: T) => void): (value: T) => void {
  const handlerRef = useRef(handler)
  useLayoutEffect(() => {
    handlerRef.current = handler
  }, [handler])

  const frameRef = useRef<number | null>(null)
  const hasPendingRef = useRef(false)
  const pendingRef = useRef<T | undefined>(undefined)

  useEffect(() => {
    return () => {
      if (frameRef.current !== null) {
        cancelAnimationFrame(frameRef.current)
        frameRef.current = null
      }
      hasPendingRef.current = false
      pendingRef.current = undefined
    }
  }, [])

  return useCallback((value: T) => {
    pendingRef.current = value
    hasPendingRef.current = true
    if (frameRef.current !== null) return
    frameRef.current = requestAnimationFrame(() => {
      frameRef.current = null
      if (!hasPendingRef.current) return
      const latest = pendingRef.current!
      hasPendingRef.current = false
      pendingRef.current = undefined
      handlerRef.current(latest)
    })
  }, [])
}
