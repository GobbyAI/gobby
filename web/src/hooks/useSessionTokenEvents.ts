import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

import { useTokenEventsStream } from './useTokenEventsStream'
import type { TokenEvent } from '../types/tokens'

function getBaseUrl(): string {
  return import.meta.env.VITE_API_BASE_URL || ''
}

export function useSessionTokenEvents(sessionId: string | null, limit = 500) {
  const [isLoading, setIsLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const abortRef = useRef<AbortController | null>(null)
  const mountedRef = useRef(true)
  const latestEventAtRef = useRef<string | null>(null)

  const { events, setEvents } = useTokenEventsStream({ sessionId, limit })

  useEffect(() => {
    latestEventAtRef.current = events[0]?.event_at ?? null
  }, [events])

  const fetchEvents = useCallback(
    async (options?: { since?: string | null; replace?: boolean }) => {
      if (!sessionId) {
        return
      }

      abortRef.current?.abort()
      const controller = new AbortController()
      abortRef.current = controller

      try {
        if (!options?.since) {
          setIsLoading(true)
        }

        let url = `${getBaseUrl()}/api/sessions/${encodeURIComponent(sessionId)}/token-events?limit=${limit}`
        if (options?.since) {
          url += `&since=${encodeURIComponent(options.since)}`
        }
        const response = await fetch(url, { signal: controller.signal })
        if (!response.ok) {
          throw new Error(`HTTP ${response.status}`)
        }

        const data = await response.json()
        if (!mountedRef.current) {
          return
        }

        const fetchedEvents = Array.isArray(data.events) ? (data.events as TokenEvent[]) : []
        setEvents((prev) => {
          if (options?.replace) {
            return fetchedEvents
          }

          const merged = [...fetchedEvents, ...prev]
          const deduped = new Map<string, TokenEvent>()
          for (const event of merged) {
            const key = event.message_id
              ? `${event.session_id}:${event.message_id}`
              : `${event.session_id}:${event.event_at}:${event.model ?? ''}:${event.input_tokens}:${event.output_tokens}`
            if (!deduped.has(key)) {
              deduped.set(key, event)
            }
          }
          return Array.from(deduped.values())
            .sort((a, b) => new Date(b.event_at).getTime() - new Date(a.event_at).getTime())
            .slice(0, limit)
        })
        setError(null)
      } catch (err) {
        if (err instanceof DOMException && err.name === 'AbortError') {
          return
        }
        if (!mountedRef.current) {
          return
        }
        setError(String(err))
      } finally {
        if (mountedRef.current) {
          setIsLoading(false)
        }
      }
    },
    [limit, sessionId, setEvents],
  )

  useEffect(() => {
    mountedRef.current = true
    if (!sessionId) {
      setEvents([])
      setError(null)
      setIsLoading(false)
      return () => {
        mountedRef.current = false
        abortRef.current?.abort()
      }
    }

    void fetchEvents({ replace: true })
    const interval = window.setInterval(() => {
      void fetchEvents({ since: latestEventAtRef.current, replace: false })
    }, 30_000)

    return () => {
      mountedRef.current = false
      window.clearInterval(interval)
      abortRef.current?.abort()
    }
  }, [fetchEvents, sessionId, setEvents])

  const breakdown = useMemo(() => {
    const grouped = new Map<
      string,
      {
        family: string
        totalTokens: number
        inputTokens: number
        outputTokens: number
        models: Map<string, number>
      }
    >()

    for (const event of events) {
      const family = event.model_family || 'unknown'
      const totalTokens = event.input_tokens + event.output_tokens
      const entry = grouped.get(family) ?? {
        family,
        totalTokens: 0,
        inputTokens: 0,
        outputTokens: 0,
        models: new Map<string, number>(),
      }
      entry.totalTokens += totalTokens
      entry.inputTokens += event.input_tokens
      entry.outputTokens += event.output_tokens
      if (event.model) {
        entry.models.set(event.model, (entry.models.get(event.model) ?? 0) + totalTokens)
      }
      grouped.set(family, entry)
    }

    return Array.from(grouped.values())
      .map((entry) => ({
        family: entry.family,
        totalTokens: entry.totalTokens,
        inputTokens: entry.inputTokens,
        outputTokens: entry.outputTokens,
        models: Array.from(entry.models.entries())
          .map(([model, totalTokens]) => ({ model, totalTokens }))
          .sort((a, b) => b.totalTokens - a.totalTokens),
      }))
      .sort((a, b) => b.totalTokens - a.totalTokens)
  }, [events])

  return {
    events,
    breakdown,
    isLoading,
    error,
    refresh: fetchEvents,
  }
}
