/* eslint-disable react-hooks/exhaustive-deps -- Extracted useChat lifecycle effects intentionally close over parent refs and stable setters to preserve the original hook behavior. */
import { useEffect } from "react";
import type { ChatMessage } from "../../types/chat";
import {
  isRestorableSessionRecord,
  loadConversationId,
  loadDbSessionId,
  mapStoredChatMessage,
  saveConversationId,
  saveDbSessionId,
} from "./core";

type UseChatLifecycleParams = Record<string, any>;

export function useChatLifecycle(params: UseChatLifecycleParams) {
  const {
    applyMainSessionMeta,
    bindActiveSession,
    connect,
    conversationIdRef,
    dbSessionIdRef,
    initialViewingSessionIdRef,
    lastSeqRef,
    reconnectTimeoutRef,
    setIsLoadingMessages,
    setMessages,
    wsRef,
  } = params;

// Connect on mount, handle page lifecycle and heartbeat
useEffect(() => {
  let cancelled = false;
  const persistedMainSessionId = loadDbSessionId();

  if (!initialViewingSessionIdRef.current) {
    const baseUrl = import.meta.env.VITE_API_BASE_URL || "";
    void (async () => {
      let restoreTargetId: string | null =
        persistedMainSessionId || conversationIdRef.current || null;

      if (persistedMainSessionId) {
        try {
          const response = await fetch(`${baseUrl}/api/sessions/${persistedMainSessionId}`);
          const data = response.ok ? await response.json() : null;
          if (cancelled) {
            return;
          }

          const session = data?.session as Record<string, unknown> | undefined;
          if (session && isRestorableSessionRecord(session)) {
            if (dbSessionIdRef.current === persistedMainSessionId) {
              applyMainSessionMeta(session);
            }
            restoreTargetId = persistedMainSessionId;
          } else if (response.status === 404 || session) {
            const activePersistedBinding =
              dbSessionIdRef.current === persistedMainSessionId ||
              conversationIdRef.current === persistedMainSessionId;
            if (activePersistedBinding) {
              bindActiveSession(null);
            } else {
              saveDbSessionId(null);
              if (loadConversationId() === persistedMainSessionId) {
                saveConversationId("");
              }
            }
            restoreTargetId =
              conversationIdRef.current &&
              conversationIdRef.current !== persistedMainSessionId
                ? conversationIdRef.current
                : null;
          } else {
            // Daemon startup or transient API failure: keep the persisted main-chat
            // binding parked and let reconnect/catalog hydration recover it later.
            restoreTargetId = null;
          }
        } catch (err) {
          console.error("Failed to restore main session metadata:", err);
          restoreTargetId = null;
        }
      }

      if (!restoreTargetId) {
        return;
      }

      setIsLoadingMessages(true);
      fetch(`${baseUrl}/api/chat/${restoreTargetId}/messages?limit=100&after_seq=0`)
        .then((res) => (res.ok ? res.json() : null))
        .then((data) => {
          if (
            !data?.messages?.length ||
            conversationIdRef.current !== restoreTargetId
          ) {
            return;
          }
          const restored: ChatMessage[] = data.messages.map(mapStoredChatMessage);
          setMessages(restored);
          if (data.max_seq) {
            lastSeqRef.current = data.max_seq;
          }
        })
        .catch((err) => console.error("Failed to restore chat messages:", err))
        .finally(() => setIsLoadingMessages(false));
    })();
  }

  connect();

  // Immediate reconnect when returning to tab (mobile app switch, screen lock).
  // If already connected, send a heartbeat to reset idle timeout on the backend
  // (JS timers are throttled/suspended while backgrounded, so scheduled heartbeats
  // won't fire reliably — this catch-up heartbeat on return is the critical one).
  const handleVisibilityChange = () => {
    if (document.visibilityState !== "visible") return;
    if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) {
      // WS dropped while backgrounded — reconnect immediately
      if (reconnectTimeoutRef.current) {
        clearTimeout(reconnectTimeoutRef.current);
        reconnectTimeoutRef.current = null;
      }
      connect();
    } else if (conversationIdRef.current) {
      // Still connected — send immediate heartbeat to reset idle timer
      wsRef.current.send(
        JSON.stringify({
          type: "heartbeat",
          conversation_id: conversationIdRef.current,
        }),
      );
    }
  };
  document.addEventListener("visibilitychange", handleVisibilityChange);

  // Heartbeat every 60s to keep backend session alive during idle periods
  const heartbeatInterval = window.setInterval(() => {
    if (
      wsRef.current?.readyState === WebSocket.OPEN &&
      conversationIdRef.current
    ) {
      wsRef.current.send(
        JSON.stringify({
          type: "heartbeat",
          conversation_id: conversationIdRef.current,
        }),
      );
    }
  }, 60_000);

  return () => {
    cancelled = true;
    document.removeEventListener("visibilitychange", handleVisibilityChange);
    clearInterval(heartbeatInterval);
    if (reconnectTimeoutRef.current) {
      clearTimeout(reconnectTimeoutRef.current);
    }
    wsRef.current?.close();
  };
}, [applyMainSessionMeta, bindActiveSession, connect]);
}
