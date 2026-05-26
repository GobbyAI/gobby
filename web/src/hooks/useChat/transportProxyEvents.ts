import type { ChatMessage, SessionObservationMeta } from "../../types/chat";
import { normalizeChatMode } from "../../types/chat";
import { mapRenderedMessageToChatMessage } from "../../lib/chatMessageMapping";
import { canProxyAttachObservationMeta } from "../../lib/sessionProxyAttach";
import {
  type ApiMessage,
  clearPendingProxyMessages,
  computeContextUsageFromSessionData,
  consumePendingProxyMessage,
  hasSessionUsage,
  mapApiMessages,
  removePendingProxyMessageFromQueue,
  toSessionObservationMeta,
} from "./core";
import type { UseChatTransportParams } from "./transportTypes";

const UNKNOWN_SESSION_META = {
  ref: null,
  source: "unknown",
  title: null,
  status: "unknown",
  canProxyAttach: false,
  model: null,
  externalId: "",
  chatMode: null,
  gitBranch: null,
  contextWindow: null,
  agentRunId: null,
  workflowName: null,
  agentName: null,
  sessionType: null,
} satisfies SessionObservationMeta;

export function handleAttachToSessionResult(
  data: Record<string, unknown>,
  ctx: UseChatTransportParams,
) {
  const sid = data.session_id as string;
  const meta = toSessionObservationMeta(data) ?? UNKNOWN_SESSION_META;
  ctx.setObservedSessionId(sid);
  ctx.observedSessionIdRef.current = sid;
  ctx.observedSessionMetaRef.current = meta;
  // Also set viewing state (attached implies viewing)
  ctx.setViewingSessionId(sid);
  ctx.viewingSessionIdRef.current = sid;
  ctx.setViewingSessionMeta(meta);
  ctx.viewingSessionMetaRef.current = meta;
  const requestedMode = ctx.pendingSessionInteractionModeRef.current;
  const proxyCapable = canProxyAttachObservationMeta(meta);
  const nextMode =
    requestedMode === "proxy" && !proxyCapable ? "none" : requestedMode;
  clearPendingProxyMessages(
    ctx.pendingProxyMessagesRef.current,
    ctx.pendingProxySessionQueuesRef.current,
  );
  ctx.setSessionInteractionMode(nextMode);
  ctx.sessionInteractionModeRef.current = nextMode;
  if (nextMode === "proxy") {
    ctx.setAttachedSessionId(sid);
    ctx.attachedSessionIdRef.current = sid;
    ctx.setAttachedSessionMeta(meta);
    ctx.attachedSessionMetaRef.current = meta;
    ctx.setProxyDeliveryNotice(null);
    if (
      meta.chatMode === "act" ||
      meta.chatMode === "accept_edits" ||
      meta.chatMode === "bypass" ||
      meta.chatMode === "normal" ||
      meta.chatMode === "plan"
    ) {
      const restored = normalizeChatMode(meta.chatMode);
      if (restored !== ctx.currentModeRef.current) {
        ctx.currentModeRef.current = restored;
        ctx.onModeChangedRef.current?.(restored);
      }
    }
  } else {
    ctx.setAttachedSessionId(null);
    ctx.attachedSessionIdRef.current = null;
    ctx.setAttachedSessionMeta(null);
    ctx.attachedSessionMetaRef.current = null;
    ctx.setProxyDeliveryNotice(
      requestedMode === "proxy" && meta.sessionType === "terminal"
        ? meta.status === "paused"
          ? "This terminal session is paused. Use Resume Session to continue it in web chat."
          : "This terminal session can only be resumed in web chat right now."
        : null,
    );
  }
  // Map initial messages into chat format with proper tool call grouping
  const msgs = (data.messages as ApiMessage[]) || [];
  const mapped: ChatMessage[] = mapApiMessages(msgs);
  // Preserve REST-loaded transcript when re-attaching to viewed session
  if (ctx.viewingSessionIdRef.current === sid && ctx.messagesRef.current.length > 0) {
    const mappedById = new Map(mapped.map((m) => [m.id, m]));
    // Merge updates into existing messages, then append truly new ones
    const existingIds = new Set(ctx.messagesRef.current.map((m) => m.id));
    const merged = ctx.messagesRef.current.map((m) => mappedById.get(m.id) ?? m);
    const newMsgs = mapped.filter((m) => !existingIds.has(m.id));
    const hasMergedChanges = merged.some(
      (message, index) => message !== ctx.messagesRef.current[index],
    );
    if (newMsgs.length > 0 || hasMergedChanges) {
      ctx.setMessages([...merged, ...newMsgs]);
    }
  } else {
    ctx.setMessages(mapped);
  }
  ctx.setIsStreaming(false);
  ctx.setIsThinking(false);
  ctx.setSessionRef((data.ref as string) ?? null);
  if (hasSessionUsage(data)) {
    ctx.setContextUsage(computeContextUsageFromSessionData(data));
  }
  // Do NOT set dbSessionId here. Under the unified session identity
  // model, dbSessionId mirrors the user's main chat conversation id,
  // not an observed/attached session. Observed state lives on
  // observedSessionIdRef / viewingSessionIdRef / attachedSessionIdRef.
  // Overwriting dbSessionId here would diverge it from conversationId
  // and trap sendMessage in an infinite ensureMainSession retry loop.
  if (!meta.agentName && meta.agentRunId) {
    void ctx.resolveAgentName(meta.agentRunId).then((agentName) => {
      if (!agentName || ctx.viewingSessionIdRef.current !== sid) return;
      ctx.observedSessionMetaRef.current = {
        ...(ctx.observedSessionMetaRef.current ?? meta),
        agentName,
      };
      ctx.setViewingSessionMeta((prev) =>
        prev && ctx.viewingSessionIdRef.current === sid
          ? { ...prev, agentName }
          : prev,
      );
      ctx.setAttachedSessionMeta((prev) =>
        prev && ctx.attachedSessionIdRef.current === sid
          ? { ...prev, agentName }
          : prev,
      );
    });
  }
}

export function handleDetachFromSessionResult(
  data: Record<string, unknown>,
  ctx: UseChatTransportParams,
) {
  const sid = typeof data.session_id === "string" ? data.session_id : null;
  if (sid) {
    const isCurrentObserved = ctx.observedSessionIdRef.current === sid;
    const isCurrentAttached = ctx.attachedSessionIdRef.current === sid;
    const isCurrentViewedTerminal =
      ctx.viewingSessionIdRef.current === sid &&
      ctx.viewingSessionMetaRef.current?.sessionType === "terminal";
    if (!isCurrentObserved && !isCurrentAttached && !isCurrentViewedTerminal) {
      return;
    }
  }
  ctx.setObservedSessionId(null);
  ctx.observedSessionMetaRef.current = null;
  ctx.setAttachedSessionId(null);
  ctx.attachedSessionIdRef.current = null;
  ctx.setAttachedSessionMeta(null);
  ctx.attachedSessionMetaRef.current = null;
  clearPendingProxyMessages(
    ctx.pendingProxyMessagesRef.current,
    ctx.pendingProxySessionQueuesRef.current,
  );
  ctx.setProxyDeliveryNotice(null);
  ctx.setSessionInteractionMode("none");
  ctx.sessionInteractionModeRef.current = "none";
  // Restore main-chat contextUsage snapshot taken at first attach,
  // so the pie stops showing the observed session's percentages.
  if (ctx.preAttachContextUsageRef.current !== null) {
    ctx.setContextUsage(ctx.preAttachContextUsageRef.current);
    ctx.preAttachContextUsageRef.current = null;
  } else {
    ctx.setContextUsage({
      totalInputTokens: 0,
      outputTokens: 0,
      contextWindow: null,
      uncachedInputTokens: 0,
      cacheReadTokens: 0,
      cacheCreationTokens: 0,
    });
  }
  // Keep viewingSessionId/Meta — return to view-only mode
}

export function handleObservedSessionMessage(
  data: Record<string, unknown>,
  ctx: UseChatTransportParams,
) {
  const smSessionId = data.session_id as string;
  const isObservedSession =
    smSessionId &&
    (smSessionId === ctx.observedSessionIdRef.current ||
      smSessionId === ctx.viewingSessionIdRef.current);
  if (!isObservedSession) {
    return;
  }

  const msg = data.message as Record<string, unknown> | undefined;
  if (!msg) {
    return;
  }

  const renderedMessage = mapRenderedMessageToChatMessage(msg);
  const pendingProxyMessage =
    renderedMessage.role === "user" &&
    smSessionId === ctx.attachedSessionIdRef.current &&
    ctx.sessionInteractionModeRef.current === "proxy"
      ? consumePendingProxyMessage(
          ctx.pendingProxyMessagesRef.current,
          ctx.pendingProxySessionQueuesRef.current,
          smSessionId,
        )
      : null;
  ctx.setMessages((prev) => {
    const existingIdx = prev.findIndex(
      (message) => message.id === renderedMessage.id,
    );
    if (existingIdx >= 0) {
      const updated = [...prev];
      updated[existingIdx] = renderedMessage;
      return updated;
    }
    if (pendingProxyMessage) {
      const pendingIdx = prev.findIndex(
        (message) => message.id === pendingProxyMessage.currentMessageId,
      );
      if (pendingIdx >= 0) {
        const updated = [...prev];
        updated[pendingIdx] = renderedMessage;
        return updated;
      }
    }
    if (
      renderedMessage.role === "user" &&
      smSessionId === ctx.attachedSessionIdRef.current &&
      ctx.sessionInteractionModeRef.current === "proxy"
    ) {
      const optimisticIdx = prev.findIndex(
        (message) =>
          message.role === "user" &&
          message.id.startsWith("user-") &&
          message.content === renderedMessage.content,
      );
      if (optimisticIdx >= 0) {
        const updated = [...prev];
        updated[optimisticIdx] = renderedMessage;
        return updated;
      }
    }
    return [...prev, renderedMessage];
  });
  if (pendingProxyMessage) {
    pendingProxyMessage.currentMessageId = renderedMessage.id;
  }
}

export function handleCliSessionSendResult(
  data: Record<string, unknown>,
  ctx: UseChatTransportParams,
) {
  const clientMessageId =
    typeof data.client_message_id === "string" ? data.client_message_id : null;
  const messageId = typeof data.message_id === "string" ? data.message_id : null;
  if (clientMessageId) {
    const pendingProxyMessage =
      ctx.pendingProxyMessagesRef.current.get(clientMessageId) ?? null;
    if (pendingProxyMessage) {
      if (messageId && data.delivered !== false) {
        ctx.setMessages((prev) => {
          const messageIdx = prev.findIndex(
            (message) =>
              message.id === pendingProxyMessage.currentMessageId ||
              message.id === messageId,
          );
          if (messageIdx < 0) {
            return prev;
          }
          const updated = [...prev];
          updated[messageIdx] = {
            ...updated[messageIdx],
            id: messageId,
          };
          return updated;
        });
        pendingProxyMessage.currentMessageId = messageId;
        removePendingProxyMessageFromQueue(
          ctx.pendingProxySessionQueuesRef.current,
          pendingProxyMessage.sessionId,
          clientMessageId,
        );
        ctx.pendingProxyMessagesRef.current.delete(clientMessageId);
      }
    }
  }
  ctx.setProxyDeliveryNotice(
    data.delivered === false ? "Message queued until the session yields." : null,
  );
  if (import.meta.env.DEV) {
    console.debug("Message sent to CLI session:", data.delivery_method);
  }
}
