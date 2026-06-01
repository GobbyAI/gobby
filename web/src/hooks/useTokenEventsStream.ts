import { useCallback, useMemo, useRef, useState } from 'react'
import type { SetStateAction } from 'react'

import { useWebSocketEvent } from './useWebSocketEvent'
import { normalizeTokenEventMessage } from './useChat/transportUsageEvents'
import type { TokenEvent } from '../types/tokens'
import type { TokenEventMessage } from './useChat/transportEventTypes'

interface Options {
  sessionId?: string | null
  limit?: number
}

const EMPTY_EVENTS: TokenEvent[] = []

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

function tokenEventFromMessage(data: TokenEventMessage): TokenEvent {
  return {
    session_id: data.session_id,
    project_id: data.project_id ?? null,
    message_id: data.message_id ?? null,
    source: data.source ?? null,
    origin: data.origin ?? null,
    model: data.model ?? null,
    model_family: data.model_family ?? null,
    input_tokens: data.input_tokens ?? 0,
    output_tokens: data.output_tokens ?? 0,
    cache_creation_tokens: data.cache_creation_tokens ?? 0,
    cache_read_tokens: data.cache_read_tokens ?? 0,
    context_window: data.context_window ?? null,
    event_at: data.event_at,
    session_totals:
      data.session_totals
        ? {
            input_tokens: data.session_totals.input_tokens ?? 0,
            output_tokens: data.session_totals.output_tokens ?? 0,
            cache_creation_tokens: data.session_totals.cache_creation_tokens ?? 0,
            cache_read_tokens: data.session_totals.cache_read_tokens ?? 0,
          }
        : undefined,
  }
}

export function useTokenEventsStream({ sessionId = null, limit = 200 }: Options = {}) {
  const [eventsState, setEventsState] = useState<{
    sessionId: string | null
    events: TokenEvent[]
  }>({
    sessionId,
    events: [],
  })
  const seenKeysRef = useRef<Set<string>>(new Set())
  const events = useMemo(
    () => (eventsState.sessionId === sessionId ? eventsState.events : EMPTY_EVENTS),
    [eventsState.events, eventsState.sessionId, sessionId],
  )

  const setEvents = useCallback((next: SetStateAction<TokenEvent[]>) => {
    setEventsState((prev) => {
      const baseEvents = prev.sessionId === sessionId ? prev.events : []
      const resolved = typeof next === 'function' ? next(baseEvents) : next
      seenKeysRef.current = new Set(resolved.map(eventKey))
      return {
        sessionId,
        events: resolved,
      }
    })
  }, [sessionId])

  const appendEvent = useCallback(
    (nextEvent: TokenEvent | null) => {
      if (!nextEvent) {
        return
      }
      if (sessionId && nextEvent.session_id !== sessionId) {
        return
      }

      const key = eventKey(nextEvent)
      if (seenKeysRef.current.has(key)) {
        return
      }

      setEventsState((prev) => {
        const baseEvents = prev.sessionId === sessionId ? prev.events : []
        const key = eventKey(nextEvent)
        if (seenKeysRef.current.has(key)) {
          return prev
        }
        const merged = [nextEvent, ...baseEvents]
          .sort((a, b) => new Date(b.event_at).getTime() - new Date(a.event_at).getTime())
          .slice(0, limit)
        seenKeysRef.current = new Set(merged.map(eventKey))
        return {
          sessionId,
          events: merged,
        }
      })
    },
    [limit, sessionId],
  )

  useWebSocketEvent(
    'token_event',
    useCallback(
      (data: Record<string, unknown>) => {
        const tokenEvent = normalizeTokenEventMessage({ ...data, type: 'token_event' })
        appendEvent(tokenEvent ? tokenEventFromMessage(tokenEvent) : null)
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
