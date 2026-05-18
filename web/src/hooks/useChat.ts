import { useState, useEffect, useCallback, useRef } from "react";
import type { ChatMessage, QueuedFile } from "../types/chat";
import type { A2UISurfaceState } from "../components/canvas/types";
import type { CanvasPanelState } from "../components/canvas/hooks/useCanvasPanel";
import {
  clearPendingProxyMessages,
  saveDbSessionId,
  saveViewingSessionId,
  saveViewingSessionMode,
  uuid,
  type ChatError,
  type ChatStreamChunk,
  type ChatThinkingMessage,
  type ModelSwitchedMessage,
  type ToolStatusMessage,
} from "./useChat/core";
import { useChatActions } from "./useChat/actions";
import { usePlanArtifactCallbacks } from "./useChat/callbacksState";
import {
  createEmptyContextUsage,
  useContextUsageState,
} from "./useChat/contextUsageState";
import { useChatLifecycle } from "./useChat/lifecycle";
import { useChatMessageHandlers } from "./useChat/handlers";
import { useProviderAgentState } from "./useChat/providerAgentState";
import { useSessionAttachmentState } from "./useChat/sessionAttachmentState";
import { useSessionIdentityState } from "./useChat/sessionIdentityState";
import { useChatSessionViewing } from "./useChat/sessionViewing";
import { useChatTransport } from "./useChat/transport";
import { useContinuationRestore } from "./useChat/useContinuationRestore";

export function useChat() {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const messagesRef = useRef(messages);
  const [isConnected, setIsConnected] = useState(false);
  const [isReconnecting, setIsReconnecting] = useState(false);
  const [isStreaming, setIsStreaming] = useState(false);
  const [isThinking, setIsThinking] = useState(false);
  const [isLoadingMessages, setIsLoadingMessages] = useState(false);

  // Canvas state
  const [canvasSurfaces, setCanvasSurfaces] = useState<
    Map<string, A2UISurfaceState>
  >(new Map());
  const [canvasPanel, setCanvasPanel] = useState<CanvasPanelState | null>(null);

  // Keep a ref so onopen/reconnect can read the current project
  const projectIdRef = useRef<string | null>(null);
  const setProjectIdRef = useCallback((id: string | null) => {
    projectIdRef.current = id;
  }, []);

  const wsRef = useRef<WebSocket | null>(null);
  const reconnectTimeoutRef = useRef<number | null>(null);
  const lastServerModeTimestampRef = useRef<number>(0);

  const {
    activeAgent,
    activeAgentRef,
    selectedProvider,
    selectedProviderRef,
    setActiveAgent,
    setSelectedProvider,
  } = useProviderAgentState();

  const {
    currentModeRef,
    onArtifactEventRef,
    onChatClearedRef,
    onChatDeletedRef,
    onModeChangedRef,
    onPlanReadyRef,
    pendingPlanFeedbackRef,
    planContentRef,
    planPendingApproval,
    setOnArtifactEvent,
    setOnChatCleared,
    setOnChatDeleted,
    setCurrentMode,
    setOnModeChanged,
    setOnPlanReady,
    setPlanPendingApproval,
  } = usePlanArtifactCallbacks();

  const {
    clearPreAttachContextUsage,
    contextUsage,
    contextUsageUpdatedAt,
    markSessionUsageFresh,
    preAttachContextUsageRef,
    setContextUsage,
    shouldApplyHydratedUsage,
  } = useContextUsageState();

  const {
    applyMainSessionMeta,
    bindActiveSession,
    conversationId,
    conversationIdRef,
    conversationSwitchKey,
    currentBranch,
    dbSessionId,
    dbSessionIdRef,
    ensureMainSession,
    lastSeqRef,
    mainSessionMeta,
    sessionRef,
    sessionRefRef,
    sessionTitle,
    setConversationId,
    setConversationSwitchKey,
    setCurrentBranch,
    setDbSessionId,
    setMainSessionMeta,
    setSessionRef,
    setSessionTitle,
    setWorktreePath,
    worktreePath,
  } = useSessionIdentityState({
    activeAgentRef,
    currentModeRef,
    onModeChangedRef,
    projectIdRef,
    selectedProviderRef,
    setContextUsage,
    setSelectedProvider,
    wsRef,
  });

  const {
    attachedSessionId,
    attachedSessionIdRef,
    attachedSessionMeta,
    attachedSessionMetaRef,
    clearContinuationRollback,
    clearContinuingSession,
    continuingSessionIdRef,
    continuationRollbackRef,
    initialViewingModeRef,
    initialViewingReconnectRetryRef,
    initialViewingRestoreRef,
    initialViewingSessionIdRef,
    isContinuingSession,
    observedSessionId,
    observedSessionIdRef,
    observedSessionMetaRef,
    pendingProxyMessagesRef,
    pendingProxySessionQueuesRef,
    pendingSessionInteractionModeRef,
    proxyDeliveryNotice,
    resolveAgentName,
    sessionInteractionMode,
    sessionInteractionModeRef,
    setAttachedSessionId,
    setAttachedSessionMeta,
    setIsContinuingSession,
    setObservedSessionId,
    setProxyDeliveryNotice,
    setSessionInteractionMode,
    setViewingSessionId,
    setViewingSessionMeta,
    viewingSessionId,
    viewingSessionIdRef,
    viewingSessionMeta,
    viewingSessionMetaRef,
  } = useSessionAttachmentState();

  // Stable ref to sendMessage for use inside WS handlers / callbacks
  // defined before sendMessage itself. Updated after sendMessage is created.
  const sendMessageRef = useRef<
    | ((
        content: string,
        model?: string | null,
        files?: QueuedFile[],
        projectId?: string | null,
        injectContext?: string,
        reasoningEffort?: string | null,
        ttsEnabled?: boolean,
      ) => boolean)
    | null
  >(null);

  const clearSessionObservationState = useCallback(
    ({ preserveViewing = false }: { preserveViewing?: boolean } = {}) => {
      const observedSessionId = observedSessionIdRef.current;
      if (observedSessionId && wsRef.current?.readyState === WebSocket.OPEN) {
        wsRef.current.send(
          JSON.stringify({
            type: "detach_from_session",
            session_id: observedSessionId,
          }),
        );
      }
      if (!preserveViewing) {
        viewingSessionIdRef.current = null;
        viewingSessionMetaRef.current = null;
        setViewingSessionId(null);
        setViewingSessionMeta(null);
      }
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
      setProxyDeliveryNotice(null);
    },
    [
      attachedSessionIdRef,
      attachedSessionMetaRef,
      observedSessionIdRef,
      observedSessionMetaRef,
      pendingProxyMessagesRef,
      pendingProxySessionQueuesRef,
      sessionInteractionModeRef,
      setAttachedSessionId,
      setAttachedSessionMeta,
      setObservedSessionId,
      setProxyDeliveryNotice,
      setSessionInteractionMode,
      setViewingSessionId,
      setViewingSessionMeta,
      viewingSessionIdRef,
      viewingSessionMetaRef,
    ],
  );

  const resetMainChatState = useCallback(() => {
    lastSeqRef.current = 0;
    activeRequestIdRef.current = null;
    setIsStreaming(false);
    setIsThinking(false);
    setSessionRef(null);
    setSessionTitle(null);
    setMainSessionMeta(null);
    setCurrentBranch(null);
    setWorktreePath(null);
    setCanvasSurfaces(new Map());
    setCanvasPanel(null);
    setPlanPendingApproval(false);
    planContentRef.current = null;
    setContextUsage(createEmptyContextUsage());
    setMessages([]);
    setIsLoadingMessages(false);
  }, [
    lastSeqRef,
    planContentRef,
    setContextUsage,
    setCurrentBranch,
    setMainSessionMeta,
    setPlanPendingApproval,
    setSessionRef,
    setSessionTitle,
    setWorktreePath,
  ]);

  useEffect(() => {
    messagesRef.current = messages;
  }, [messages]);

  // Track the active chat request to filter stale stream chunks from cancelled requests
  const activeRequestIdRef = useRef<string | null>(null);

  // Queue for messages sent while disconnected — flushed on reconnect
  const pendingMessagesRef = useRef<
    {
      content: string;
      model?: string | null;
      files?: QueuedFile[];
      projectId?: string | null;
      reasoningEffort?: string | null;
      ttsEnabled?: boolean;
    }[]
  >([]);

  const restoreContinuationState = useContinuationRestore({
    attachedSessionIdRef,
    attachedSessionMetaRef,
    conversationIdRef,
    dbSessionIdRef,
    observedSessionIdRef,
    observedSessionMetaRef,
    pendingSessionInteractionModeRef,
    sessionInteractionModeRef,
    setAttachedSessionId,
    setAttachedSessionMeta,
    setContextUsage,
    setConversationId,
    setCurrentBranch,
    setCurrentMode,
    setDbSessionId,
    setIsLoadingMessages,
    setMainSessionMeta,
    setMessages,
    setObservedSessionId,
    setProxyDeliveryNotice,
    setSelectedProvider,
    setSessionInteractionMode,
    setSessionRef,
    setSessionTitle,
    setViewingSessionId,
    setViewingSessionMeta,
    setWorktreePath,
    viewingSessionIdRef,
    viewingSessionMetaRef,
    wsRef,
  });

  /** Returns true if the chunk belongs to the currently active request. */
  function isActiveRequest(requestId?: string): boolean {
    return requestId === activeRequestIdRef.current;
  }

  // Refs for handlers to avoid stale closures in WebSocket callbacks
  const handleChatStreamRef = useRef<(chunk: ChatStreamChunk) => void>(
    () => {},
  );
  const handleChatErrorRef = useRef<(error: ChatError) => void>(() => {});
  const handleToolStatusRef = useRef<(status: ToolStatusMessage) => void>(
    () => {},
  );
  const handleChatThinkingRef = useRef<(msg: ChatThinkingMessage) => void>(
    () => {},
  );
  const handleModelSwitchedRef = useRef<(msg: ModelSwitchedMessage) => void>(
    () => {},
  );
  const handleVoiceMessageRef = useRef<(data: Record<string, unknown>) => void>(
    () => {},
  );
  const handleBinaryMessageRef = useRef<(data: ArrayBuffer) => void>(() => {});

  const connect = useChatTransport({
    activeRequestIdRef,
    applyMainSessionMeta,
    attachedSessionIdRef,
    attachedSessionMetaRef,
    clearContinuationRollback,
    clearContinuingSession,
    conversationIdRef,
    continuingSessionIdRef,
    continuationRollbackRef,
    currentModeRef,
    dbSessionIdRef,
    handleBinaryMessageRef,
    handleChatErrorRef,
    handleChatStreamRef,
    handleChatThinkingRef,
    handleModelSwitchedRef,
    handleToolStatusRef,
    handleVoiceMessageRef,
    lastSeqRef,
    lastServerModeTimestampRef,
    markSessionUsageFresh,
    messagesRef,
    observedSessionIdRef,
    observedSessionMetaRef,
    onArtifactEventRef,
    onChatClearedRef,
    onChatDeletedRef,
    onModeChangedRef,
    onPlanReadyRef,
    pendingMessagesRef,
    pendingPlanFeedbackRef,
    pendingProxyMessagesRef,
    pendingProxySessionQueuesRef,
    pendingSessionInteractionModeRef,
    planContentRef,
    preAttachContextUsageRef,
    reconnectTimeoutRef,
    resolveAgentName,
    restoreContinuationState,
    sendMessageRef,
    sessionInteractionModeRef,
    sessionRefRef,
    setActiveAgent,
    setAttachedSessionId,
    setAttachedSessionMeta,
    setCanvasPanel,
    setCanvasSurfaces,
    setContextUsage,
    setConversationId,
    setCurrentBranch,
    setDbSessionId,
    setIsConnected,
    setIsReconnecting,
    setIsStreaming,
    setIsThinking,
    setMainSessionMeta,
    setMessages,
    setObservedSessionId,
    setPlanPendingApproval,
    setProxyDeliveryNotice,
    setSelectedProvider,
    setSessionInteractionMode,
    setSessionRef,
    setSessionTitle,
    setViewingSessionId,
    setViewingSessionMeta,
    setWorktreePath,
    viewingSessionIdRef,
    viewingSessionMetaRef,
    wsRef,
  });

  useChatMessageHandlers({
    attachedSessionId,
    attachedSessionIdRef,
    attachedSessionMeta,
    attachedSessionMetaRef,
    dbSessionId,
    dbSessionIdRef,
    handleChatErrorRef,
    handleChatStreamRef,
    handleChatThinkingRef,
    handleModelSwitchedRef,
    handleToolStatusRef,
    isActiveRequest,
    conversationIdRef,
    pendingPlanFeedbackRef,
    observedSessionId,
    observedSessionIdRef,
    saveDbSessionId,
    saveViewingSessionId,
    saveViewingSessionMode,
    sendMessageRef,
    sessionInteractionMode,
    sessionInteractionModeRef,
    setContextUsage,
    setIsStreaming,
    setIsThinking,
    setMainSessionMeta,
    setMessages,
    setSessionRef,
    viewingSessionId,
    viewingSessionIdRef,
    viewingSessionMeta,
    viewingSessionMetaRef,
  });

  const {
    switchConversation,
    startNewChat,
    switchProvider,
    resumeSession,
    continueSessionInChat,
    clearHistory,
    deleteConversation,
    stopStreaming,
    sendMode,
    sendAttachedSessionMode,
    sendProjectChange,
    sendAgentChange,
    sendWorktreeChange,
    sendMessage,
    respondToQuestion,
    respondToApproval,
    respondToCanvas,
    approvePlan,
    requestPlanChanges,
  } = useChatActions({
    activeRequestIdRef,
    applyMainSessionMeta,
    attachedSessionId,
    attachedSessionIdRef,
    attachedSessionMeta,
    attachedSessionMetaRef,
    bindActiveSession,
    clearPreAttachContextUsage,
    clearSessionObservationState,
    contextUsage,
    continuingSessionIdRef,
    continuationRollbackRef,
    conversationId,
    conversationIdRef,
    currentBranch,
    currentModeRef,
    dbSessionId,
    dbSessionIdRef,
    ensureMainSession,
    isStreaming,
    lastSeqRef,
    lastServerModeTimestampRef,
    mainSessionMeta,
    messages,
    messagesRef,
    observedSessionId,
    observedSessionMetaRef,
    onModeChangedRef,
    pendingMessagesRef,
    pendingPlanFeedbackRef,
    pendingProxyMessagesRef,
    pendingProxySessionQueuesRef,
    planContentRef,
    projectIdRef,
    proxyDeliveryNotice,
    resetMainChatState,
    selectedProvider,
    selectedProviderRef,
    sendMessageRef,
    sessionInteractionMode,
    sessionInteractionModeRef,
    sessionRef,
    sessionTitle,
    setActiveAgent,
    setContextUsage,
    setConversationId,
    setConversationSwitchKey,
    setCurrentMode,
    setIsContinuingSession,
    setIsLoadingMessages,
    setIsStreaming,
    setIsThinking,
    setMessages,
    setPlanPendingApproval,
    setProxyDeliveryNotice,
    setSelectedProvider,
    viewingSessionId,
    viewingSessionIdRef,
    viewingSessionMeta,
    worktreePath,
    wsRef,
  });

  const {
    viewSession,
    clearViewingSession,
    attachToSession,
    attachToViewed,
    detachFromSession,
  } = useChatSessionViewing({
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
  });

  // Add a local system message to the chat (no backend round-trip)
  const addSystemMessage = useCallback((content: string) => {
    setMessages((prev) => [
      ...prev,
      {
        id: `system-${uuid()}`,
        role: "system" as const,
        content,
        timestamp: new Date(),
      },
    ]);
  }, []);


  useChatLifecycle({
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
  });

  return {
    messages,
    conversationId,
    conversationSwitchKey,
    sessionRef,
    sessionTitle,
    dbSessionId,
    currentBranch,
    worktreePath,
    isConnected,
    isReconnecting,
    isStreaming,
    isThinking,
    isLoadingMessages,
    contextUsage,
    contextUsageUpdatedAt,
    sendMessage,
    sendMode,
    sendProjectChange,
    projectIdRef,
    setProjectIdRef,
    sendWorktreeChange,
    sendAgentChange,
    activeAgent,
    stopStreaming,
    clearHistory,
    deleteConversation,
    respondToQuestion,
    respondToApproval,
    canvasSurfaces,
    canvasPanel,
    onCanvasInteraction: respondToCanvas,
    planPendingApproval,
    approvePlan,
    requestPlanChanges,
    switchConversation,
    startNewChat,
    switchProvider,
    resumeSession,
    continueSessionInChat,
    setOnModeChanged,
    setOnPlanReady,
    setOnArtifactEvent,
    addSystemMessage,
    viewSession,
    clearViewingSession,
    mainSessionMeta,
    viewingSessionId,
    viewingSessionMeta,
    isContinuingSession,
    observeSession: attachToSession,
    attachToViewed,
    detachFromSession,
    attachedSessionId,
    attachedSessionMeta,
    sessionInteractionMode,
    proxyDeliveryNotice,
    sendAttachedSessionMode,
    wsRef,
    handleVoiceMessageRef,
    handleBinaryMessageRef,
    setOnChatDeleted,
    setOnChatCleared,
    selectedProvider,
    setSelectedProvider,
  };
}
