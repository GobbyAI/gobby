import { useState, useEffect, useCallback, useRef } from "react";
import type {
  ChatMessage,
  ChatMode,
  QueuedFile,
  SessionInteractionMode,
  SessionObservationMeta,
} from "../types/chat";
import { normalizeChatMode } from "../types/chat";
import type { A2UISurfaceState } from "../components/canvas/types";
import type { CanvasPanelState } from "../components/canvas/hooks/useCanvasPanel";
import {
  clearPendingProxyMessages,
  computeContextUsageFromSessionData,
  createWebChatSession,
  hasSessionUsage,
  isChatProvider,
  loadConversationId,
  loadDbSessionId,
  loadViewingSessionId,
  loadViewingSessionMode,
  saveConversationId,
  saveDbSessionId,
  saveViewingSessionId,
  saveViewingSessionMode,
  toSessionObservationMeta,
  uuid,
  type ChatError,
  type ChatStreamChunk,
  type ChatThinkingMessage,
  type ContinuationRollbackSnapshot,
  type ModelSwitchedMessage,
  type PendingProxyMessage,
  type ToolStatusMessage,
} from "./useChat/core";
import { useChatActions } from "./useChat/actions";
import { useChatLifecycle } from "./useChat/lifecycle";
import { useChatMessageHandlers } from "./useChat/handlers";
import { useChatSessionViewing } from "./useChat/sessionViewing";
import { useChatTransport } from "./useChat/transport";

export function useChat() {
  const [conversationId, setConversationId] = useState<string>(() =>
    loadConversationId(),
  );
  const conversationIdRef = useRef<string>(conversationId);

  // Counter that increments only on intentional conversation switches (not SDK
  // session ID adoption).  Used by the mode-restore effect in App.tsx so that
  // adopting the SDK session ID doesn't reset the user's mode to the default.
  const [conversationSwitchKey, setConversationSwitchKey] = useState(0);

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

  // Session ref tracking (e.g. "#158")
  const [sessionRef, setSessionRef] = useState<string | null>(null);

  // DB session ID — used by title synthesis to call session APIs directly
  // without waiting for sessions list polling
  const [dbSessionId, setDbSessionId] = useState<string | null>(() =>
    loadDbSessionId(),
  );
  const dbSessionIdRef = useRef<string | null>(dbSessionId);
  const creatingSessionIdRef = useRef<Promise<string | null> | null>(null);
  const lastSeqRef = useRef<number>(0);

  // Branch/worktree tracking
  const [currentBranch, setCurrentBranch] = useState<string | null>(null);
  const [worktreePath, setWorktreePath] = useState<string | null>(null);

  // Active agent tracking — persisted to survive page reloads
  const ACTIVE_AGENT_KEY = "gobby-active-agent";
  const [activeAgent, setActiveAgent] = useState<string>(
    () => localStorage.getItem(ACTIVE_AGENT_KEY) || "default",
  );

  // Session title — stored from switchConversation to survive filtered list race
  const [sessionTitle, setSessionTitle] = useState<string | null>(null);
  const [mainSessionMeta, setMainSessionMeta] =
    useState<SessionObservationMeta | null>(null);
  const sessionRefRef = useRef<string | null>(sessionRef);
  useEffect(() => {
    sessionRefRef.current = sessionRef;
  }, [sessionRef]);

  // LLM provider selection (null = server default), persisted to localStorage
  const [selectedProvider, setSelectedProviderRaw] = useState<string | null>(
    () => {
      try {
        return localStorage.getItem("gobby-selected-provider") || null;
      } catch {
        return null;
      }
    },
  );
  const setSelectedProvider = useCallback((provider: string | null) => {
    setSelectedProviderRaw(provider);
    try {
      if (provider) {
        localStorage.setItem("gobby-selected-provider", provider);
      } else {
        localStorage.removeItem("gobby-selected-provider");
      }
    } catch {
      /* ignore */
    }
  }, []);
  const selectedProviderRef = useRef<string | null>(selectedProvider);
  useEffect(() => {
    selectedProviderRef.current = selectedProvider;
  }, [selectedProvider]);

  // Session viewing tracking (read-only observation of CLI sessions via REST)
  const [viewingSessionId, setViewingSessionId] = useState<string | null>(() =>
    loadViewingSessionId(),
  );
  const viewingSessionIdRef = useRef<string | null>(null);
  const [viewingSessionMeta, setViewingSessionMeta] =
    useState<SessionObservationMeta | null>(null);
  const viewingSessionMetaRef = useRef<SessionObservationMeta | null>(null);
  const initialViewingSessionIdRef = useRef<string | null>(
    loadViewingSessionId(),
  );
  const initialViewingModeRef = useRef<"none" | "observe">(
    loadViewingSessionMode(),
  );
  const initialViewingRestoreRef = useRef(false);
  const initialViewingReconnectRetryRef = useRef(false);

  // Live terminal observation is distinct from interactive proxy mode.
  const [observedSessionId, setObservedSessionId] = useState<string | null>(
    null,
  );
  const observedSessionIdRef = useRef<string | null>(null);
  const observedSessionMetaRef = useRef<SessionObservationMeta | null>(null);
  const [sessionInteractionMode, setSessionInteractionMode] =
    useState<SessionInteractionMode>("none");
  const sessionInteractionModeRef = useRef<SessionInteractionMode>("none");
  const pendingSessionInteractionModeRef = useRef<"observe" | "proxy">("proxy");
  const [proxyDeliveryNotice, setProxyDeliveryNotice] = useState<string | null>(
    null,
  );
  const agentNameCacheRef = useRef<Map<string, string | null>>(new Map());

  // Session attachment tracking (interactive proxy mode only)
  const [attachedSessionId, setAttachedSessionId] = useState<string | null>(
    null,
  );
  const attachedSessionIdRef = useRef<string | null>(null);
  const [attachedSessionMeta, setAttachedSessionMeta] =
    useState<SessionObservationMeta | null>(null);
  const attachedSessionMetaRef = useRef<SessionObservationMeta | null>(null);
  const pendingProxyMessagesRef = useRef<Map<string, PendingProxyMessage>>(
    new Map(),
  );
  const pendingProxySessionQueuesRef = useRef<Map<string, string[]>>(new Map());
  const [isContinuingSession, setIsContinuingSession] = useState(false);
  const continuingSessionIdRef = useRef<string | null>(null);
  const continuationRollbackRef = useRef<ContinuationRollbackSnapshot | null>(
    null,
  );

  // Keep a ref so onopen/reconnect can read the current agent
  const activeAgentRef = useRef(activeAgent);
  useEffect(() => {
    activeAgentRef.current = activeAgent;
    localStorage.setItem(ACTIVE_AGENT_KEY, activeAgent);
  }, [activeAgent]);

  // Keep a ref so onopen/reconnect can read the current project
  const projectIdRef = useRef<string | null>(null);
  const setProjectIdRef = useCallback((id: string | null) => {
    projectIdRef.current = id;
  }, []);
  const clearContinuingSession = useCallback(() => {
    continuingSessionIdRef.current = null;
    setIsContinuingSession(false);
  }, []);

  const resolveAgentName = useCallback(async (agentRunId: string) => {
    const cached = agentNameCacheRef.current.get(agentRunId);
    if (cached !== undefined) {
      return cached;
    }

    const baseUrl = import.meta.env.VITE_API_BASE_URL || "";
    try {
      const res = await fetch(`${baseUrl}/api/agents/runs/${agentRunId}`);
      if (!res.ok) {
        agentNameCacheRef.current.set(agentRunId, null);
        return null;
      }
      const data = await res.json();
      const resolved =
        data?.run?.agent_name || data?.run?.workflow_name || null;
      agentNameCacheRef.current.set(agentRunId, resolved);
      return resolved;
    } catch {
      agentNameCacheRef.current.set(agentRunId, null);
      return null;
    }
  }, []);

  // Plan mode approval tracking
  const [planPendingApproval, setPlanPendingApproval] = useState(false);
  const planContentRef = useRef<string | null>(null);
  const currentModeRef = useRef<ChatMode>("plan");

  // Callback for backend-initiated mode changes (e.g. agent EnterPlanMode)
  const onModeChangedRef = useRef<((mode: ChatMode) => void) | null>(null);
  const setOnModeChanged = useCallback((fn: (mode: ChatMode) => void) => {
    onModeChangedRef.current = fn;
  }, []);

  // Callback when plan content is ready (for artifact creation)
  const onPlanReadyRef = useRef<((content: string | null) => void) | null>(
    null,
  );
  const setOnPlanReady = useCallback((fn: (content: string | null) => void) => {
    onPlanReadyRef.current = fn;
  }, []);

  // Callback when artifact event arrives from backend (show_file)
  const onArtifactEventRef = useRef<
    | ((
        type: string,
        content: string,
        language?: string,
        title?: string,
      ) => void)
    | null
  >(null);
  const setOnArtifactEvent = useCallback(
    (
      fn: (
        type: string,
        content: string,
        language?: string,
        title?: string,
      ) => void,
    ) => {
      onArtifactEventRef.current = fn;
    },
    [],
  );

  // Callback when backend confirms a chat deletion
  const onChatDeletedRef = useRef<((conversationId: string) => void) | null>(
    null,
  );
  const setOnChatDeleted = useCallback(
    (fn: (conversationId: string) => void) => {
      onChatDeletedRef.current = fn;
    },
    [],
  );

  // Callback when backend confirms a chat clear
  const onChatClearedRef = useRef<((conversationId: string) => void) | null>(
    null,
  );
  const setOnChatCleared = useCallback(
    (fn: (conversationId: string) => void) => {
      onChatClearedRef.current = fn;
    },
    [],
  );

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

  const bindActiveSession = useCallback((sessionId: string | null) => {
    const nextId = sessionId ?? "";
    lastSeqRef.current = 0;
    conversationIdRef.current = nextId;
    setConversationId(nextId);
    setDbSessionId(sessionId);
    dbSessionIdRef.current = sessionId;
    saveDbSessionId(sessionId);
    saveConversationId(nextId);
  }, []);

  const markSessionUsageFresh = useCallback((sessionId: string, rawTimestamp?: string) => {
    const parsed = rawTimestamp ? new Date(rawTimestamp).getTime() : NaN;
    lastLiveUsageBySessionRef.current.set(
      sessionId,
      Number.isFinite(parsed) ? parsed : Date.now(),
    );
  }, []);

  const shouldApplyHydratedUsage = useCallback((sessionId: string, fetchStartedAt: number) => {
    const lastLive = lastLiveUsageBySessionRef.current.get(sessionId);
    return lastLive == null || lastLive <= fetchStartedAt;
  }, []);

  const applyMainSessionMeta = useCallback(
    (session: Record<string, unknown> | null) => {
      const nextMeta = toSessionObservationMeta(session, {
        sessionType: "web_chat",
      });
      setMainSessionMeta(nextMeta);
      setSessionTitle(nextMeta?.title ?? null);
      if (nextMeta?.ref) {
        setSessionRef(nextMeta.ref);
      }
      setCurrentBranch(nextMeta?.gitBranch ?? null);
      if (nextMeta && isChatProvider(nextMeta.source)) {
        setSelectedProvider(nextMeta.source);
      }
      if (nextMeta?.chatMode) {
        const restored = normalizeChatMode(nextMeta.chatMode);
        if (restored !== currentModeRef.current) {
          currentModeRef.current = restored;
          onModeChangedRef.current?.(restored);
        }
      }
      if (hasSessionUsage(session)) {
        setContextUsage(computeContextUsageFromSessionData(session));
      }
    },
    [setSelectedProvider],
  );

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
    [],
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
    setContextUsage({
      totalInputTokens: 0,
      outputTokens: 0,
      contextWindow: null,
      uncachedInputTokens: 0,
      cacheReadTokens: 0,
      cacheCreationTokens: 0,
    });
    setMessages([]);
    setIsLoadingMessages(false);
  }, []);

  const ensureMainSession = useCallback(
    async (options?: {
      projectId?: string | null;
      provider?: string | null;
      model?: string | null;
      reasoningEffort?: string | null;
      chatMode?: ChatMode | null;
      title?: string | null;
      forceNew?: boolean;
    }): Promise<string | null> => {
      if (!options?.forceNew && dbSessionIdRef.current) {
        return dbSessionIdRef.current;
      }
      if (!options?.forceNew && creatingSessionIdRef.current) {
        return await creatingSessionIdRef.current;
      }

      const pending = createWebChatSession({
        projectId: options?.projectId ?? projectIdRef.current,
        provider: options?.provider ?? selectedProviderRef.current,
        model: options?.model ?? null,
        reasoningEffort: options?.reasoningEffort ?? null,
        chatMode: options?.chatMode ?? currentModeRef.current,
        title: options?.title ?? null,
      })
        .then((session) => {
          bindActiveSession(session.id);
          applyMainSessionMeta(session as Record<string, unknown>);
          if (
            wsRef.current?.readyState === WebSocket.OPEN &&
            activeAgentRef.current
          ) {
            wsRef.current.send(
              JSON.stringify({
                type: "set_agent",
                conversation_id: session.id,
                agent_name: activeAgentRef.current,
              }),
            );
          }
          return session.id;
        })
        .catch((error) => {
          console.error("Failed to create web chat session:", error);
          return null;
        })
        .finally(() => {
          creatingSessionIdRef.current = null;
        });

      creatingSessionIdRef.current = pending;
      return await pending;
    },
    [applyMainSessionMeta, bindActiveSession],
  );

  useEffect(() => {
    conversationIdRef.current = conversationId;
  }, [conversationId]);

  useEffect(() => {
    messagesRef.current = messages;
  }, [messages]);

  useEffect(() => {
    dbSessionIdRef.current = dbSessionId;
  }, [dbSessionId]);

  // Context usage tracking — accumulated across turns.
  // totalInputTokens = uncached + cacheRead + cacheCreation (the real context size).
  const [contextUsage, setContextUsage] = useState<{
    totalInputTokens: number;
    outputTokens: number;
    contextWindow: number | null;
    // Per-category breakdown for tooltip
    uncachedInputTokens: number;
    cacheReadTokens: number;
    cacheCreationTokens: number;
  }>({
    totalInputTokens: 0,
    outputTokens: 0,
    contextWindow: null,
    uncachedInputTokens: 0,
    cacheReadTokens: 0,
    cacheCreationTokens: 0,
  });
  const [contextUsageUpdatedAt, setContextUsageUpdatedAt] = useState<number | null>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectTimeoutRef = useRef<number | null>(null);
  // Timestamp of last server-authoritative mode_changed — used to suppress
  // redundant set_mode emissions on WS reconnect and session restore
  const lastServerModeTimestampRef = useRef<number>(0);

  // Main-chat contextUsage captured before attaching to an observed session,
  // so detach can restore it. Null when no attach is active. Only set on the
  // first attach in a sequence so chained attaches don't overwrite the
  // original main-chat snapshot.
  const preAttachContextUsageRef = useRef<{
    totalInputTokens: number;
    outputTokens: number;
    contextWindow: number | null;
    uncachedInputTokens: number;
    cacheReadTokens: number;
    cacheCreationTokens: number;
  } | null>(null);
  const didTrackContextUsageRef = useRef(false);
  const lastLiveUsageBySessionRef = useRef<Map<string, number>>(new Map());
  const clearPreAttachContextUsage = useCallback(() => {
    preAttachContextUsageRef.current = null;
  }, []);

  // Track the active chat request to filter stale stream chunks from cancelled requests
  const activeRequestIdRef = useRef<string | null>(null);

  // Queue for messages sent while disconnected — flushed on reconnect
  const pendingMessagesRef = useRef<
    {
      content: string;
      model?: string | null;
      projectId?: string | null;
      reasoningEffort?: string | null;
      ttsEnabled?: boolean;
    }[]
  >([]);

  const clearContinuationRollback = useCallback(() => {
    continuationRollbackRef.current = null;
  }, []);

  useEffect(() => {
    if (!didTrackContextUsageRef.current) {
      didTrackContextUsageRef.current = true;
      return;
    }
    setContextUsageUpdatedAt(Date.now());
  }, [contextUsage]);

  const restoreContinuationState = useCallback(
    (snapshot: ContinuationRollbackSnapshot) => {
      conversationIdRef.current = snapshot.conversationId;
      setConversationId(snapshot.conversationId);
      saveConversationId(snapshot.conversationId);
      dbSessionIdRef.current = snapshot.dbSessionId;
      setDbSessionId(snapshot.dbSessionId);
      saveDbSessionId(snapshot.dbSessionId);
      setMessages(snapshot.messages);
      setMainSessionMeta(snapshot.mainSessionMeta);
      setSessionTitle(snapshot.sessionTitle);
      setSessionRef(snapshot.sessionRef);
      setSelectedProvider(snapshot.selectedProvider);
      setContextUsage(snapshot.contextUsage);
      setCurrentBranch(snapshot.currentBranch);
      setWorktreePath(snapshot.worktreePath);
      setViewingSessionId(snapshot.viewingSessionId);
      viewingSessionIdRef.current = snapshot.viewingSessionId;
      saveViewingSessionId(snapshot.viewingSessionId);
      setViewingSessionMeta(snapshot.viewingSessionMeta);
      viewingSessionMetaRef.current = snapshot.viewingSessionMeta;
      observedSessionIdRef.current = snapshot.observedSessionId;
      observedSessionMetaRef.current = snapshot.observedSessionMeta;
      setObservedSessionId(snapshot.observedSessionId);
      attachedSessionIdRef.current = snapshot.attachedSessionId;
      setAttachedSessionId(snapshot.attachedSessionId);
      attachedSessionMetaRef.current = snapshot.attachedSessionMeta;
      setAttachedSessionMeta(snapshot.attachedSessionMeta);
      sessionInteractionModeRef.current = snapshot.sessionInteractionMode;
      setSessionInteractionMode(snapshot.sessionInteractionMode);
      saveViewingSessionMode(
        snapshot.viewingSessionId &&
          snapshot.sessionInteractionMode === "observe"
          ? "observe"
          : "none",
      );
      setProxyDeliveryNotice(snapshot.proxyDeliveryNotice);
      setIsLoadingMessages(false);
      currentModeRef.current = normalizeChatMode(snapshot.currentMode);
      onModeChangedRef.current?.(normalizeChatMode(snapshot.currentMode));

      if (
        snapshot.observedSessionId &&
        snapshot.sessionInteractionMode !== "none" &&
        wsRef.current?.readyState === WebSocket.OPEN
      ) {
        pendingSessionInteractionModeRef.current =
          snapshot.sessionInteractionMode === "proxy" ? "proxy" : "observe";
        wsRef.current.send(
          JSON.stringify({
            type: "attach_to_session",
            session_id: snapshot.observedSessionId,
          }),
        );
      }
    },
    [setContextUsage, setSelectedProvider],
  );

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

  const pendingPlanFeedbackRef = useRef<string | null>(null);

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
    wsRef,
    handleVoiceMessageRef,
    handleBinaryMessageRef,
    setOnChatDeleted,
    setOnChatCleared,
    selectedProvider,
    setSelectedProvider,
  };
}
