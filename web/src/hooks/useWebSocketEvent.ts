import { useEffect, useRef } from 'react'

// ---------------------------------------------------------------------------
// Singleton WebSocket connection shared across all consumers
// ---------------------------------------------------------------------------

type Handler = (data: Record<string, unknown>) => void

let ws: WebSocket | null = null
let reconnectTimer: number | null = null
let closed = false

/** event-type → Set of handler callbacks */
const handlers = new Map<string, Set<Handler>>()

/** All event types any consumer has registered for */
const subscribedTypes = new Set<string>()

function getWsUrl(): string {
  const wsProtocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${wsProtocol}//${window.location.host}/ws`
}

function sendSubscriptions() {
  if (ws?.readyState === WebSocket.OPEN && subscribedTypes.size > 0) {
    ws.send(JSON.stringify({ type: 'subscribe', events: [...subscribedTypes] }))
  }
}

function onMessage(evt: MessageEvent) {
  try {
    const data = JSON.parse(evt.data)
    const type = data?.type as string | undefined
    if (!type) return
    const typeHandlers = handlers.get(type)
    if (typeHandlers) {
      for (const handler of typeHandlers) {
        handler(data)
      }
    }
  } catch {
    // ignore parse errors
  }
}

function connect() {
  if (closed) return
  ws = new WebSocket(getWsUrl())

  ws.onopen = () => {
    sendSubscriptions()
  }

  ws.onmessage = onMessage

  ws.onclose = () => {
    ws = null
    if (!closed) {
      reconnectTimer = window.setTimeout(connect, 3000)
    }
  }

  ws.onerror = () => {
    // onclose fires after onerror
  }
}

function ensureConnection() {
  if (!ws && !closed) {
    connect()
  }
}

// ---------------------------------------------------------------------------
// Public hook
// ---------------------------------------------------------------------------

/**
 * Subscribe to a WebSocket event type with a handler.
 *
 * Uses a singleton WebSocket connection shared across all hook consumers.
 * On mount: registers the handler and ensures the connection is alive.
 * On unmount: unregisters the handler and closes the connection if no
 * handlers remain.
 *
 * @param eventType - The WebSocket message `type` to subscribe to (e.g. "task_event")
 * @param handler - Callback receiving the parsed message data
 */
export function useWebSocketEvent(eventType: string, handler: Handler): void {
  const handlerRef = useRef(handler)
  handlerRef.current = handler

  useEffect(() => {
    // Stable wrapper so we can swap the handler without re-subscribing
    const stableHandler: Handler = (data) => handlerRef.current(data)

    // Register
    if (!handlers.has(eventType)) {
      handlers.set(eventType, new Set())
    }
    handlers.get(eventType)!.add(stableHandler)

    // Track subscription and (re-)send to server
    const wasNew = !subscribedTypes.has(eventType)
    subscribedTypes.add(eventType)
    if (wasNew) {
      sendSubscriptions()
    }

    ensureConnection()

    return () => {
      // Unregister
      const typeHandlers = handlers.get(eventType)
      if (typeHandlers) {
        typeHandlers.delete(stableHandler)
        if (typeHandlers.size === 0) {
          handlers.delete(eventType)
          subscribedTypes.delete(eventType)
        }
      }

      // Close connection if no handlers remain
      if (handlers.size === 0) {
        closed = true
        if (reconnectTimer) {
          window.clearTimeout(reconnectTimer)
          reconnectTimer = null
        }
        if (ws) {
          ws.close()
          ws = null
        }
        // Reset so next mount can reconnect
        closed = false
      }
    }
  }, [eventType])
}
