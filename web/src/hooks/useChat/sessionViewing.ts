/* eslint-disable react-hooks/exhaustive-deps -- Extracted useChat session-viewing callbacks intentionally close over parent refs and stable setters to preserve the original hook behavior. */
import { useCallback, useEffect, useRef } from "react";
import type { Dispatch, SetStateAction } from "react";
import type { SessionObservationMeta } from "../../types/chat";
import { normalizeChatMode } from "../../types/chat";
import {
  mapApiMessages,
  mapRenderedMessageToChatMessage,
} from "../../lib/chatMessageMapping";
import { clearFreshChatDraft } from "../../lib/sessionPersistence";
import { canProxyAttachSessionRecord } from "../../lib/sessionProxyAttach";
import {
  buildContextUsageFromTotals,
  computeContextUsageFromSessionData,
} from "./contextUsage";
import type { ContextUsage } from "../../types/chat";
import { loadDbSessionId } from "./conversationPersistence";
import { clearPendingProxyMessages } from "./pendingProxyMessages";
import {
  normalizeSessionType,
} from "./sessionRecords";

type Setter<T> = Dispatch<SetStateAction<T>>;
type ViewSessionOptions = { forceRefresh?: boolean };

interface UseChatSessionViewingParams extends Record<string, any> {
  resolveAgentName: (agentRunId: string) => Promise<string | null>;
  setContextUsage: Setter<ContextUsage>;
  setViewingSessionMeta: Setter<SessionObservationMeta | null>;
}

export function useChatSessionViewing(params: UseChatSessionViewingParams) {
  const {
    activeRequestIdRef,
    attachedSessionIdRef,
    attachedSessionMetaRef,
    contextUsage,
    initialViewingModeRef,
    initialViewingReconnectRetryRef,
    initialViewingRestoreRef,
    initialViewingSessionIdRef,
    isConnected,
    lastSeqRef,
    mainSessionMeta,
    messagesRef,
    observedSessionIdRef,
    observedSessionMetaRef,
    onModeChangedRef,
    pendingProxyMessagesRef,
    pendingProxySessionQueuesRef,
    pendingSessionInteractionModeRef,
    preAttachContextUsageRef,
    resolveAgentName,
    sessionInteractionModeRef,
    setAttachedSessionId,
    setAttachedSessionMeta,
    setContextUsage,
    setCurrentBranch,
    setIsLoadingMessages,
    setIsStreaming,
    setIsThinking,
    setMessages,
    setObservedSessionId,
    setProxyDeliveryNotice,
    setSessionInteractionMode,
    setSessionRef,
    setSessionTitle,
    setViewingSessionId,
    setViewingSessionMeta,
    shouldApplyHydratedUsage,
    viewingSessionId,
    viewingSessionIdRef,
    viewingSessionMetaRef,
    wsRef,
  } = params;

const viewRequestSeqRef = useRef(0);

// View a CLI session (read-only, no WS subscription — loads via REST)
const viewSession = useCallback(
  (sessionId: string, options?: ViewSessionOptions) => {
    const forceRefresh = options?.forceRefresh ?? false;
    clearFreshChatDraft();
    // Skip if already viewing/attached to this session
    if (
      !forceRefresh &&
      ((viewingSessionIdRef.current === sessionId &&
        (viewingSessionMetaRef.current || messagesRef.current.length > 0)) ||
        observedSessionIdRef.current === sessionId)
    ) {
      return;
    }
    const requestSeq = ++viewRequestSeqRef.current;
    const isCurrentRequest = () =>
      viewRequestSeqRef.current === requestSeq &&
      viewingSessionIdRef.current === sessionId;

    // Detach from any active WS subscription first
    const observedSessionId = observedSessionIdRef.current;
    if (observedSessionId && observedSessionId !== sessionId) {
      if (wsRef.current?.readyState === WebSocket.OPEN) {
        wsRef.current.send(
          JSON.stringify({
            type: "detach_from_session",
            session_id: observedSessionId,
          }),
        );
      }
      observedSessionIdRef.current = null;
      setObservedSessionId(null);
      observedSessionMetaRef.current = null;
      attachedSessionIdRef.current = null;
      setAttachedSessionId(null);
      attachedSessionMetaRef.current = null;
      setAttachedSessionMeta(null);
      clearPendingProxyMessages(
        pendingProxyMessagesRef.current,
        pendingProxySessionQueuesRef.current,
      );
      sessionInteractionModeRef.current = "none";
      setProxyDeliveryNotice(null);
      setSessionInteractionMode("none");
    }

    // Reset chat state
    lastSeqRef.current = 0;
    activeRequestIdRef.current = null;
    setIsStreaming(false);
    setIsThinking(false);
    setMessages([]);
    setIsLoadingMessages(true);
    setProxyDeliveryNotice(null);
    setContextUsage(buildContextUsageFromTotals({}));

    // Set viewing state
    viewingSessionIdRef.current = sessionId;
    setViewingSessionId(sessionId);

    // Fetch messages via REST
    const baseUrl = import.meta.env.VITE_API_BASE_URL || "";
    fetch(`${baseUrl}/api/sessions/${sessionId}/messages?limit=100&offset=0`)
      .then((res) => (res.ok ? res.json() : null))
      .then((data) => {
        if (!isCurrentRequest()) return;
        const mapped = data?.messages?.length ? mapApiMessages(data.messages) : [];
        setMessages(mapped);
      })
      .catch((err) => console.error("Failed to fetch session messages:", err))
      .finally(() => {
        if (isCurrentRequest()) setIsLoadingMessages(false);
      });

    // Fetch session metadata
    const metadataFetchStartedAt = Date.now();
    fetch(`${baseUrl}/api/sessions/${sessionId}`)
      .then((res) => (res.ok ? res.json() : null))
      .then((data) => {
        const s = data?.session;
        if (!s || !isCurrentRequest()) return;
        const ref = s.seq_num ? `#${s.seq_num}` : null;
        setSessionRef(ref);
        const nextMeta: SessionObservationMeta = {
          ref,
          source: s.source || "unknown",
          title: s.title || null,
          status: s.status || "unknown",
          canProxyAttach: canProxyAttachSessionRecord(s),
          model: s.model || null,
          reasoningEffort: s.reasoning_effort || null,
          externalId: s.external_id || "",
          chatMode: s.chat_mode || null,
          gitBranch: s.git_branch || null,
          contextWindow: s.context_window || null,
          agentRunId: s.agent_run_id || null,
          workflowName: s.workflow_name || null,
          agentName: null,
          sessionType: normalizeSessionType(s.session_type),
        };
        viewingSessionMetaRef.current = nextMeta;
        setViewingSessionMeta(nextMeta);
        if (nextMeta.agentRunId) {
          void resolveAgentName(nextMeta.agentRunId).then((agentName) => {
            if (!agentName || !isCurrentRequest()) return;
            setViewingSessionMeta((prev) =>
              prev && isCurrentRequest() ? { ...prev, agentName } : prev,
            );
          });
        }
        if (!shouldApplyHydratedUsage(sessionId, metadataFetchStartedAt)) {
          return;
        }
        setContextUsage(computeContextUsageFromSessionData(s));
      })
      .catch((err) =>
        console.error("Failed to fetch session metadata:", err),
      );
    // resolveAgentName is a stable useCallback (its own deps are []) — safe
    // to reference here without re-creating the callback every render.
  },
  [resolveAgentName, setContextUsage, shouldApplyHydratedUsage],
);

// Clear viewing state and restore previous web chat
const clearViewingSession = useCallback(() => {
  viewRequestSeqRef.current += 1;
  // Detach from any active WS subscription
  const observedSessionId = observedSessionIdRef.current;
  if (observedSessionId) {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(
        JSON.stringify({
          type: "detach_from_session",
          session_id: observedSessionId,
        }),
      );
    }
    observedSessionIdRef.current = null;
    setObservedSessionId(null);
    observedSessionMetaRef.current = null;
    attachedSessionIdRef.current = null;
    setAttachedSessionId(null);
    attachedSessionMetaRef.current = null;
    setAttachedSessionMeta(null);
    clearPendingProxyMessages(
      pendingProxyMessagesRef.current,
      pendingProxySessionQueuesRef.current,
    );
    sessionInteractionModeRef.current = "none";
    setProxyDeliveryNotice(null);
    setSessionInteractionMode("none");
  }

  viewingSessionIdRef.current = null;
  viewingSessionMetaRef.current = null;
  setViewingSessionId(null);
  setViewingSessionMeta(null);
  setMessages([]);
  setProxyDeliveryNotice(null);

  if (mainSessionMeta) {
    setSessionRef(mainSessionMeta.ref);
    setSessionTitle(mainSessionMeta.title ?? null);
    setCurrentBranch(mainSessionMeta.gitBranch ?? null);
    if (mainSessionMeta.contextWindow) {
      setContextUsage((prev) => ({
        ...prev,
        contextWindow: mainSessionMeta.contextWindow ?? null,
      }));
    } else {
      setContextUsage(buildContextUsageFromTotals({}));
    }
  } else {
    setSessionRef(null);
    setSessionTitle(null);
    setCurrentBranch(null);
    setContextUsage(buildContextUsageFromTotals({}));
  }

  // Restore previous conversation messages and chat mode from DB
  const prevDbSid = loadDbSessionId();
  if (prevDbSid) {
    const baseUrl = import.meta.env.VITE_API_BASE_URL || "";
    fetch(`${baseUrl}/api/chat/${prevDbSid}/messages?limit=100&after_seq=0`)
      .then((res) => (res.ok ? res.json() : null))
      .then((data) => {
        if (
          !data?.messages?.length ||
          viewingSessionIdRef.current ||
          loadDbSessionId() !== prevDbSid
        ) {
          return;
        }
        const mapped = data.messages.map((m: Record<string, unknown>) =>
          mapRenderedMessageToChatMessage(m),
        );
        if (mapped.length > 0) setMessages(mapped);
      })
      .catch((err) => console.error("Failed to restore messages:", err));

    // Restore chat mode from DB (prevents stale mode from viewed session)
    fetch(`${baseUrl}/api/sessions/${prevDbSid}`)
      .then((res) => (res.ok ? res.json() : null))
      .then((data) => {
        const s = data?.session;
        if (viewingSessionIdRef.current || loadDbSessionId() !== prevDbSid) {
          return;
        }
        if (s?.chat_mode) {
          onModeChangedRef.current?.(normalizeChatMode(s.chat_mode));
        }
      })
      .catch(() => {});
  }
}, [mainSessionMeta, setContextUsage]);

// Attach to a CLI session (interactive mode with WS subscription)
const attachToSession = useCallback(
  (sessionId: string, mode: "observe" | "proxy" = "proxy") => {
    if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) return;
    clearFreshChatDraft();
    pendingSessionInteractionModeRef.current = mode;
    setProxyDeliveryNotice(null);

    const observedSessionId = observedSessionIdRef.current;
    if (observedSessionId && observedSessionId !== sessionId) {
      wsRef.current.send(
        JSON.stringify({
          type: "detach_from_session",
          session_id: observedSessionId,
        }),
      );
      observedSessionIdRef.current = null;
      observedSessionMetaRef.current = null;
      attachedSessionIdRef.current = null;
      attachedSessionMetaRef.current = null;
      clearPendingProxyMessages(
        pendingProxyMessagesRef.current,
        pendingProxySessionQueuesRef.current,
      );
      sessionInteractionModeRef.current = "none";
      setObservedSessionId(null);
      setAttachedSessionId(null);
      setAttachedSessionMeta(null);
      setSessionInteractionMode("none");
    }

    // Don't reset messages if already viewing this session
    if (viewingSessionIdRef.current !== sessionId) {
      if (preAttachContextUsageRef.current === null) {
        preAttachContextUsageRef.current = contextUsage;
      }
      activeRequestIdRef.current = null;
      setIsStreaming(false);
      setIsThinking(false);
      setMessages([]);
      setContextUsage(buildContextUsageFromTotals({}));
    }

    wsRef.current.send(
      JSON.stringify({
        type: "attach_to_session",
        session_id: sessionId,
      }),
    );
  },
  [contextUsage, setContextUsage],
);

useEffect(() => {
  const restoredViewingSessionId = initialViewingSessionIdRef.current;
  if (!restoredViewingSessionId || initialViewingRestoreRef.current) {
    return;
  }
  initialViewingRestoreRef.current = true;
  viewSession(restoredViewingSessionId);
}, [viewSession]);

useEffect(() => {
  if (!isConnected) {
    initialViewingReconnectRetryRef.current = false;
    return;
  }

  const restoredViewingSessionId = initialViewingSessionIdRef.current;
  if (
    !restoredViewingSessionId ||
    !initialViewingRestoreRef.current ||
    initialViewingReconnectRetryRef.current ||
    viewingSessionIdRef.current !== restoredViewingSessionId ||
    viewingSessionMetaRef.current ||
    messagesRef.current.length > 0
  ) {
    return;
  }

  initialViewingReconnectRetryRef.current = true;
  viewSession(restoredViewingSessionId);
}, [isConnected, viewSession, viewingSessionId]);

useEffect(() => {
  const restoredViewingSessionId = initialViewingSessionIdRef.current;
  if (
    !restoredViewingSessionId ||
    initialViewingModeRef.current === "none" ||
    !isConnected ||
    viewingSessionIdRef.current !== restoredViewingSessionId ||
    observedSessionIdRef.current === restoredViewingSessionId
  ) {
    return;
  }
  attachToSession(restoredViewingSessionId, initialViewingModeRef.current);
  initialViewingModeRef.current = "none";
}, [attachToSession, isConnected, viewingSessionId]);

// Attach to the currently viewed session (button click from view-only mode)
const attachToViewed = useCallback(() => {
  const sid = viewingSessionIdRef.current;
  if (!sid) {
    return;
  }
  const viewedMeta = viewingSessionMetaRef.current;
  if (
    viewedMeta?.sessionType !== "terminal" ||
    viewedMeta.agentRunId
  ) {
    return;
  }
  attachToSession(sid, "proxy");
}, [attachToSession]);

// Disable proxy mode but keep the live observation subscription.
const detachFromSession = useCallback(() => {
  if (!attachedSessionIdRef.current) return;

  clearPendingProxyMessages(
    pendingProxyMessagesRef.current,
    pendingProxySessionQueuesRef.current,
  );
  attachedSessionIdRef.current = null;
  attachedSessionMetaRef.current = null;
  sessionInteractionModeRef.current = observedSessionIdRef.current
    ? "observe"
    : "none";
  setAttachedSessionId(null);
  setAttachedSessionMeta(null);
  setProxyDeliveryNotice(null);
  setSessionInteractionMode(
    observedSessionIdRef.current ? "observe" : "none",
  );
}, []);



  return {
    viewSession,
    clearViewingSession,
    attachToSession,
    attachToViewed,
    detachFromSession,
  };
}
