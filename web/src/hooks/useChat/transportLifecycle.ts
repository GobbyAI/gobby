import type { ChatMessage } from "../../types/chat";
import { mapStoredChatMessage } from "./core";
import { routeTransportMessage } from "./transportRouter";
import type {
  TransportConnectRef,
  UseChatTransportParams,
} from "./transportTypes";

const RECONNECT_DELAY_MS = 2000;

function reconnectAfterCriticalRoutingError(
  ctx: UseChatTransportParams,
  connectRef: TransportConnectRef,
  ws: WebSocket,
) {
  if (ctx.wsRef.current !== ws) return;

  ctx.reportTransportError("Transport message handling failed; reconnecting");
  ctx.wsRef.current = null;
  ctx.setIsConnected(false);
  ctx.setIsReconnecting(true);
  if (ctx.reconnectTimeoutRef.current) {
    window.clearTimeout(ctx.reconnectTimeoutRef.current);
  }
  try {
    ws.close();
  } catch (error) {
    console.error("Failed to close WebSocket after routing error:", error);
  }
  ctx.reconnectTimeoutRef.current = window.setTimeout(() => {
    ctx.reconnectTimeoutRef.current = null;
    connectRef.current?.();
  }, RECONNECT_DELAY_MS);
}

export function connectChatTransport(
  ctx: UseChatTransportParams,
  connectRef: TransportConnectRef,
) {
  if (ctx.wsRef.current && ctx.wsRef.current.readyState !== WebSocket.CLOSED) {
    return;
  }

  const wsProtocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  const wsUrl = `${wsProtocol}//${window.location.host}/ws`;

  if (import.meta.env.DEV) {
    console.debug("Connecting to WebSocket:", wsUrl);
  }
  const ws = new WebSocket(wsUrl);
  ws.binaryType = "arraybuffer"; // For TTS audio binary frames
  ctx.wsRef.current = ws;

  ws.onopen = () => {
    if (ctx.wsRef.current !== ws) return;
    if (import.meta.env.DEV) {
      console.debug("WebSocket connected");
    }
    ctx.setIsConnected(true);
    ctx.setIsReconnecting(false);

    // Flush queued messages that were sent while disconnected
    if (ctx.pendingMessagesRef.current.length > 0) {
      const queued = [...ctx.pendingMessagesRef.current];
      ctx.pendingMessagesRef.current = [];
      // Send each queued message after a brief delay to let WS fully initialize
      setTimeout(() => {
        for (const msg of queued) {
          ctx.sendMessageRef.current?.(
            msg.content,
            msg.model ?? null,
            msg.files,
            msg.projectId,
            undefined,
            msg.reasoningEffort,
            msg.ttsEnabled,
          );
        }
      }, 500);
    }

    // Backfill missed messages on reconnect (lastSeqRef > 0 means we had messages before)
    if (ctx.lastSeqRef.current > 0 && ctx.conversationIdRef.current) {
      const baseUrl = import.meta.env.VITE_API_BASE_URL || "";
      const convId = ctx.conversationIdRef.current;
      const afterSeq = ctx.lastSeqRef.current;
      fetch(`${baseUrl}/api/chat/${convId}/messages?after_seq=${afterSeq}`)
        .then((res) => (res.ok ? res.json() : null))
        .then((data) => {
          if (ctx.viewingSessionIdRef.current) return;
          if (!data?.messages?.length) return;
          const backfilled: ChatMessage[] = data.messages.map(mapStoredChatMessage);
          ctx.setMessages((prev) => {
            const existingIds = new Set(prev.map((m) => m.id));
            const newMsgs = backfilled.filter((m) => !existingIds.has(m.id));
            return newMsgs.length > 0 ? [...prev, ...newMsgs] : prev;
          });
          if (data.max_seq) ctx.lastSeqRef.current = data.max_seq;
        })
        .catch((err) => console.error("Failed to backfill messages:", err));
    }

    ws.send(
      JSON.stringify({
        type: "subscribe",
        events: [
          "chat_stream",
          "chat_error",
          "tool_status",
          "chat_thinking",
          "canvas_event",
          "artifact_event",
          "session_message",
          "session_usage_updated",
          "token_event",
        ],
      }),
    );

    // Rebind this client to the current main-chat conversation without
    // mutating the server-owned session. The backend uses conversation_id
    // on the websocket client metadata to scope broadcasts for the active
    // chat stream and pending approvals.
    if (ctx.conversationIdRef.current) {
      ws.send(
        JSON.stringify({
          type: "heartbeat",
          conversation_id: ctx.conversationIdRef.current,
        }),
      );
    }
  };

  ws.onclose = () => {
    if (ctx.wsRef.current !== ws) return;
    ctx.wsRef.current = null;
    if (import.meta.env.DEV) {
      console.debug("WebSocket disconnected");
    }
    ctx.setIsConnected(false);
    ctx.setIsReconnecting(true);
    // Don't clear isStreaming/isThinking/activeRequestIdRef — the backend
    // may still be working. Clearing these causes post-reconnect tool_status
    // updates to be dropped as "stale". Only clear on explicit cancel or
    // if reconnect timeout expires (30s).
    const disconnectTimer = window.setTimeout(() => {
      // If still disconnected after 30s, assume the stream is dead
      if (!ctx.wsRef.current || ctx.wsRef.current.readyState !== WebSocket.OPEN) {
        ctx.setIsStreaming(false);
        ctx.setIsThinking(false);
        ctx.activeRequestIdRef.current = null;
      }
    }, 30_000);

    ctx.reconnectTimeoutRef.current = window.setTimeout(() => {
      clearTimeout(disconnectTimer);
      connectRef.current?.();
    }, RECONNECT_DELAY_MS);
  };

  ws.onerror = (error) => {
    if (ctx.wsRef.current !== ws) return;
    console.error("WebSocket error:", error);
  };

  ws.onmessage = (event) => {
    if (ctx.wsRef.current !== ws) return;
    try {
      routeTransportMessage(event as MessageEvent<string | ArrayBuffer>, ctx);
    } catch (error) {
      console.error("WebSocket message routing failed", {
        payload: event.data,
        error,
      });
      reconnectAfterCriticalRoutingError(ctx, connectRef, ws);
    }
  };
}
