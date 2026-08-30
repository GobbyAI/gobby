import { useEffect, useRef, useSyncExternalStore } from "react";

// ---------------------------------------------------------------------------
// Singleton WebSocket connection shared across all consumers
// ---------------------------------------------------------------------------

type Handler = (data: Record<string, unknown>) => void;

let ws: WebSocket | null = null;
let reconnectTimer: number | null = null;
let stableOpenTimer: number | null = null;
let closed = false;
let reconnectAttempts = 0;
let connectionGeneration = 0;
const BASE_DELAY = 1000;
const MAX_DELAY = 30000;
const STABLE_OPEN_MS = 1000;

/** event-type → Set of handler callbacks */
const handlers = new Map<string, Set<Handler>>();

/** All event types any consumer has registered for */
const subscribedTypes = new Set<string>();

/** Singleton connection state — lets consumers (e.g. polling fallbacks)
 * distinguish "events will arrive" from "the socket is down". */
let connected = false;
const connectionListeners = new Set<() => void>();

function setConnected(next: boolean) {
  if (connected === next) return;
  connected = next;
  for (const listener of connectionListeners) {
    listener();
  }
}

function subscribeConnection(listener: () => void): () => void {
  connectionListeners.add(listener);
  return () => {
    connectionListeners.delete(listener);
  };
}

function readConnection(): boolean {
  return connected;
}

function getWsUrl(): string {
  const wsProtocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${wsProtocol}//${window.location.host}/ws`;
}

function sendSubscriptions() {
  if (ws?.readyState === WebSocket.OPEN && subscribedTypes.size > 0) {
    ws.send(
      JSON.stringify({ type: "subscribe", events: [...subscribedTypes] }),
    );
  }
}

function onMessage(evt: MessageEvent) {
  try {
    const data = JSON.parse(evt.data);
    const type = data?.type as string | undefined;
    if (!type) return;
    const typeHandlers = handlers.get(type);
    if (typeHandlers) {
      for (const handler of typeHandlers) {
        handler(data);
      }
    }
  } catch {
    // ignore parse errors
  }
}

function connect() {
  if (closed) return;
  const generation = ++connectionGeneration;
  const socket = new WebSocket(getWsUrl());
  ws = socket;

  socket.onopen = () => {
    if (closed || generation !== connectionGeneration) return;
    setConnected(true);
    sendSubscriptions();
    // Accept-then-close (e.g. 4401 after accept) must not reset backoff;
    // only a socket that stays open counts as a recovered connection.
    if (stableOpenTimer !== null) {
      window.clearTimeout(stableOpenTimer);
    }
    stableOpenTimer = window.setTimeout(() => {
      stableOpenTimer = null;
      if (closed || generation !== connectionGeneration) return;
      reconnectAttempts = 0;
    }, STABLE_OPEN_MS);
  };

  socket.onmessage = (event) => {
    if (closed || generation !== connectionGeneration) return;
    onMessage(event);
  };

  socket.onclose = () => {
    if (closed || generation !== connectionGeneration) return;
    ws = null;
    setConnected(false);
    if (stableOpenTimer !== null) {
      window.clearTimeout(stableOpenTimer);
      stableOpenTimer = null;
    }
    const baseDelay = Math.min(BASE_DELAY * 2 ** reconnectAttempts, MAX_DELAY);
    const jitter = baseDelay * 0.2 * (Math.random() * 2 - 1);
    const delay = Math.max(0, baseDelay + jitter);
    reconnectAttempts++;
    if (reconnectTimer !== null) {
      window.clearTimeout(reconnectTimer);
    }
    reconnectTimer = window.setTimeout(() => {
      reconnectTimer = null;
      ensureConnection();
    }, delay);
  };

  socket.onerror = () => {
    if (closed || generation !== connectionGeneration) return;
    // onclose fires after onerror
  };
}

function ensureConnection() {
  if (!ws && !closed) {
    connect();
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
  const handlerRef = useRef(handler);

  useEffect(() => {
    handlerRef.current = handler;
  }, [handler]);

  useEffect(() => {
    // Stable wrapper so we can swap the handler without re-subscribing
    const stableHandler: Handler = (data) => handlerRef.current(data);

    // Register
    if (!handlers.has(eventType)) {
      handlers.set(eventType, new Set());
    }
    handlers.get(eventType)!.add(stableHandler);

    // Track subscription and (re-)send to server
    const wasNew = !subscribedTypes.has(eventType);
    subscribedTypes.add(eventType);
    if (wasNew) {
      sendSubscriptions();
    }

    ensureConnection();

    return () => {
      // Unregister
      const typeHandlers = handlers.get(eventType);
      if (typeHandlers) {
        typeHandlers.delete(stableHandler);
        if (typeHandlers.size === 0) {
          handlers.delete(eventType);
          subscribedTypes.delete(eventType);
        }
      }

      // Close connection if no handlers remain
      if (handlers.size === 0) {
        closed = true;
        connectionGeneration++;
        if (reconnectTimer) {
          window.clearTimeout(reconnectTimer);
          reconnectTimer = null;
        }
        if (stableOpenTimer !== null) {
          window.clearTimeout(stableOpenTimer);
          stableOpenTimer = null;
        }
        if (ws) {
          ws.close();
          ws = null;
        }
        setConnected(false);
        // Reset so next mount can reconnect
        closed = false;
        reconnectAttempts = 0;
      }
    };
  }, [eventType]);
}

/**
 * Read the singleton event-socket connection state.
 *
 * Consumers that must not depend on the WebSocket alone (e.g. §5.2's
 * research-run polling fallback) use this to switch to polling while the
 * socket is down. Subscribing here does NOT open the connection — pair it
 * with a `useWebSocketEvent` consumer somewhere in the tree.
 */
export function useWebSocketConnected(): boolean {
  return useSyncExternalStore(subscribeConnection, readConnection);
}
