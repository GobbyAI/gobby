import { useCallback, useMemo, useState } from 'react'
import type { SetStateAction } from 'react'

import { useWebSocketEvent } from './useWebSocketEvent'
import type { TokenEvent } from '../types/tokens'

interface Options {
  sessionId?: string | null
  limit?: number
}

function eventKey(event: TokenEvent): string {
  if (event.message_id) {
    return `${event.session_id}:${event.message_id}`
  }
  return [
    event.session_id,
    event.event_at,
    event.model ?? '',
    event.input_tokens,
    event.output_tokens,
    event.cache_creation_tokens,
    event.cache_read_tokens,
  ].join(':')
}

function normalizeTokenEvent(data: Record<string, unknown>): TokenEvent | null {
  if (typeof data.session_id !== 'string' || typeof data.event_at !== 'string') {
    return null
  }

  return {
    session_id: data.session_id,
    project_id: typeof data.project_id === 'string' ? data.project_id : null,
    message_id: typeof data.message_id === 'string' ? data.message_id : null,
    source: typeof data.source === 'string' ? data.source : null,
    origin: typeof data.origin === 'string' ? data.origin : null,
    model: typeof data.model === 'string' ? data.model : null,
    model_family: typeof data.model_family === 'string' ? data.model_family : null,
    input_tokens: typeof data.input_tokens === 'number' ? data.input_tokens : 0,
    output_tokens: typeof data.output_tokens === 'number' ? data.output_tokens : 0,
    cache_creation_tokens:
      typeof data.cache_creation_tokens === 'number' ? data.cache_creation_tokens : 0,
    cache_read_tokens:
      typeof data.cache_read_tokens === 'number' ? data.cache_read_tokens : 0,
    context_window: typeof data.context_window === 'number' ? data.context_window : null,
    event_at: data.event_at,
    session_totals:
      data.session_totals && typeof data.session_totals === 'object'
        ? {
            input_tokens:
              typeof (data.session_totals as Record<string, unknown>).input_tokens === 'number'
                ? ((data.session_totals as Record<string, unknown>).input_tokens as number)
                : 0,
            output_tokens:
              typeof (data.session_totals as Record<string, unknown>).output_tokens === 'number'
                ? ((data.session_totals as Record<string, unknown>).output_tokens as number)
                : 0,
            cache_creation_tokens:
              typeof (data.session_totals as Record<string, unknown>).cache_creation_tokens ===
              'number'
                ? ((data.session_totals as Record<string, unknown>)
                    .cache_creation_tokens as number)
                : 0,
            cache_read_tokens:
              typeof (data.session_totals as Record<string, unknown>).cache_read_tokens ===
              'number'
                ? ((data.session_totals as Record<string, unknown>).cache_read_tokens as number)
                : 0,
          }
        : undefined,
  }
}

export function useTokenEventsStream({ sessionId = null, limit = 200 }: Options = {}) {
  const [events, setEventsState] = useState<TokenEvent[]>([])
  const [prevSessionId, setPrevSessionId] = useState(sessionId)

  if (prevSessionId !== sessionId) {
    setPrevSessionId(sessionId)
    setEventsState([])
  }

  const setEvents = useCallback((next: SetStateAction<TokenEvent[]>) => {
    setEventsState((prev) => (typeof next === 'function' ? next(prev) : next))
  }, [])

  const appendEvent = useCallback(
    (nextEvent: TokenEvent | null) => {
      if (!nextEvent) {
        return
      }
      if (sessionId && nextEvent.session_id !== sessionId) {
        return
      }

      setEvents((prev) => {
        const key = eventKey(nextEvent)
        const seen = new Set(prev.map(eventKey))
        if (seen.has(key)) {
          return prev
        }
        const merged = [nextEvent, ...prev]
          .sort((a, b) => new Date(b.event_at).getTime() - new Date(a.event_at).getTime())
          .slice(0, limit)
        return merged
      })
    },
    [limit, sessionId, setEvents],
  )

  useWebSocketEvent(
    'token_event',
    useCallback(
      (data: Record<string, unknown>) => {
        appendEvent(normalizeTokenEvent(data))
      },
      [appendEvent],
    ),
  )

  const lastEventTs = useMemo(
    () => (events[0]?.event_at ? new Date(events[0].event_at).getTime() : null),
    [events],
  )

  return {
    events,
    setEvents,
    appendEvent,
    lastEventTs,
  }
}
