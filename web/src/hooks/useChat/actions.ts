/* eslint-disable react-hooks/exhaustive-deps -- Extracted useChat callbacks intentionally close over parent refs and stable setters to preserve the original hook behavior. */
import { useCallback, useEffect } from "react";
import type { Dispatch, SetStateAction } from "react";
import type {
  ChatMessage,
  ChatMode,
  FallbackContextMode,
  QueuedFile,
} from "../../types/chat";
import { normalizeChatMode } from "../../types/chat";
import type { UserAction } from "../../components/canvas/types";
import { mapRenderedMessageToChatMessage } from "../../lib/chatMessageMapping";
import {
  computeContextUsageFromSessionData,
  enqueuePendingProxyMessage,
  hasSessionUsage,
  isChatProvider,
  mapApiMessages,
  normalizeReasoningEffort,
  saveConversationId,
  uuid,
} from "./core";

type Setter<T> = Dispatch<SetStateAction<T>>;

interface UseChatActionsParams extends Record<string, any> {
  ensureMainSession: (options: {
    projectId?: string | null;
    provider?: string | null;
    model?: string | null;
    reasoningEffort?: string | null;
    forceNew?: boolean;
  }) => Promise<string | null>;
  setConversationSwitchKey: Setter<number>;
  setMessages: Setter<ChatMessage[]>;
}

export function useChatActions(params: UseChatActionsParams) {
  const {
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
  } = params;

// Switch to an existing server-owned web-chat session by DB session ID.
const switchConversation = useCallback(
  (id: string, options?: { preserveViewing?: boolean }) => {
    if (!id) return;
    const preserveViewing = options?.preserveViewing ?? false;
    if (
      id === dbSessionIdRef.current &&
      !preserveViewing &&
      messagesRef.current.length > 0
    ) {
      return;
    }

    if (!preserveViewing) {
      clearPreAttachContextUsage();
      clearSessionObservationState();
      resetMainChatState();
    }
    bindActiveSession(id);
    setConversationSwitchKey((k) => k + 1);

    const baseUrl = import.meta.env.VITE_API_BASE_URL || "";
    if (!preserveViewing) {
      setIsLoadingMessages(true);
      fetch(`${baseUrl}/api/chat/${id}/messages?limit=100&after_seq=0`)
        .then((res) => (res.ok ? res.json() : null))
        .then((data) => {
          if (viewingSessionIdRef.current || dbSessionIdRef.current !== id) return;
          if (!data?.messages?.length || conversationIdRef.current !== id)
            return;
          const mapped = data.messages.map((m: Record<string, unknown>) =>
            mapRenderedMessageToChatMessage(m),
          );
          if (mapped.length > 0) {
            setMessages(mapped);
          }
          if (data.max_seq) {
            lastSeqRef.current = data.max_seq as number;
          }
        })
        .catch((err) => console.error("Failed to fetch chat messages:", err))
        .finally(() => {
          if (!viewingSessionIdRef.current && conversationIdRef.current === id) {
            setIsLoadingMessages(false);
          }
        });
    }

    fetch(`${baseUrl}/api/sessions/${id}`)
      .then((res) => (res.ok ? res.json() : null))
      .then((data) => {
        const s = data?.session;
        if (!s || conversationIdRef.current !== id || dbSessionIdRef.current !== id) {
          return;
        }
        if (!preserveViewing && viewingSessionIdRef.current) return;
        applyMainSessionMeta(s);
        if (s.chat_mode) {
          const restored = normalizeChatMode(s.chat_mode);
          if (
            restored !== currentModeRef.current &&
            wsRef.current?.readyState === WebSocket.OPEN &&
            Date.now() - lastServerModeTimestampRef.current > 2000
          ) {
            wsRef.current.send(
              JSON.stringify({
                type: "set_mode",
                mode: restored,
                conversation_id: id,
              }),
            );
          }
          currentModeRef.current = restored;
        }
      })
      .catch(() => {});
  },
  [
    applyMainSessionMeta,
    bindActiveSession,
    clearPreAttachContextUsage,
    clearSessionObservationState,
    resetMainChatState,
  ],
);

// Start a new chat conversation, optionally with a specific agent
const startNewChat = useCallback(
  (agentName?: string) => {
    const effectiveAgent = agentName || "default";
    setActiveAgent(effectiveAgent);
    clearPreAttachContextUsage();
    clearSessionObservationState();
    resetMainChatState();
    bindActiveSession(null);
    setConversationSwitchKey((k) => k + 1);
  },
  [
    bindActiveSession,
    clearPreAttachContextUsage,
    clearSessionObservationState,
    resetMainChatState,
  ],
);

// Switch provider. Existing conversations fork to a new server-owned session;
// a blank draft stays local until the first user send.
const switchProvider = useCallback(
  (
    newProvider: string,
    options?: { model?: string | null; reasoningEffort?: string | null },
  ) => {
    if (isStreaming && wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(
        JSON.stringify({
          type: "stop_chat",
          conversation_id: conversationIdRef.current,
        }),
      );
    }
    clearPreAttachContextUsage();
    clearSessionObservationState();
    resetMainChatState();
    bindActiveSession(null);
    setConversationSwitchKey((k) => k + 1);

    // Keep fresh-chat provider changes local until the first user send
    // actually creates the backing web chat session.
    if (!dbSessionIdRef.current && messagesRef.current.length === 0) {
      setSelectedProvider(newProvider);
      return;
    }

    void ensureMainSession({
      projectId: projectIdRef.current,
      provider: newProvider,
      model: options?.model ?? null,
      reasoningEffort: options?.reasoningEffort ?? null,
      forceNew: true,
    });
  },
  [
    bindActiveSession,
    clearPreAttachContextUsage,
    clearSessionObservationState,
    ensureMainSession,
    isStreaming,
    resetMainChatState,
    setSelectedProvider,
  ],
);

// Resume a CLI session (e.g., Claude) — sets the conversation ID
// so the next message triggers server-side resume
const resumeSession = useCallback((externalId: string) => {
  lastSeqRef.current = 0;
  conversationIdRef.current = externalId;
  setConversationId(externalId);
  setConversationSwitchKey((k) => k + 1);
  saveConversationId(externalId);

  setMessages([
    {
      id: `system-resume-${uuid()}`,
      role: "system" as const,
      content: "Resuming session. Send a message to continue.",
      timestamp: new Date(),
    },
  ]);

  activeRequestIdRef.current = null;
  setIsStreaming(false);
  setIsThinking(false);
}, []);

// Continue a CLI/external session in the web chat UI with full history
const continueSessionInChat = useCallback(
  async (
    sourceDbSessionId: string,
    projectId?: string,
    options?: {
      provider?: string | null;
      model?: string | null;
      reasoningEffort?: string | null;
      chatMode?: string | null;
      fallbackContext?: FallbackContextMode;
    },
  ): Promise<string> => {
    const reasoningEffort = normalizeReasoningEffort(
      options?.reasoningEffort ?? null,
    );
    const fallbackContext = options?.fallbackContext ?? null;
    if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) {
      return "";
    }
    if (continuingSessionIdRef.current) {
      return "";
    }

    continuationRollbackRef.current = {
      sourceSessionId: sourceDbSessionId,
      conversationId,
      dbSessionId,
      mainSessionMeta,
      sessionTitle,
      sessionRef,
      selectedProvider,
      messages,
      contextUsage,
      currentMode: currentModeRef.current,
      currentBranch,
      worktreePath,
      viewingSessionId,
      viewingSessionMeta,
      observedSessionId,
      observedSessionMeta: observedSessionMetaRef.current,
      attachedSessionId,
      attachedSessionMeta,
      sessionInteractionMode,
      proxyDeliveryNotice,
    };

    clearPreAttachContextUsage();
    clearSessionObservationState();
    resetMainChatState();
    bindActiveSession(sourceDbSessionId);
    setConversationSwitchKey((k) => k + 1);
    continuingSessionIdRef.current = sourceDbSessionId;
    setIsContinuingSession(true);

    const baseUrl = import.meta.env.VITE_API_BASE_URL || "";
    let sourceSession: Record<string, unknown> | null = null;
    try {
      const sessionRes = await fetch(
        `${baseUrl}/api/sessions/${sourceDbSessionId}`,
      );
      if (sessionRes.ok) {
        const sessionData = await sessionRes.json();
        sourceSession =
          (sessionData?.session as Record<string, unknown>) ?? null;
      }
    } catch {
      sourceSession = null;
    }

    const continuationProvider =
      options?.provider ??
      (isChatProvider(sourceSession?.source) ? sourceSession.source : null) ??
      selectedProviderRef.current;
    const continuationModel =
      options?.model ??
      (typeof sourceSession?.model === "string" ? sourceSession.model : null);
    const continuationChatMode =
      typeof options?.chatMode === "string" && options.chatMode
        ? normalizeChatMode(options.chatMode)
        : null;
    // Propagate the source session's chat mode into the new continuation
    // session BEFORE calling ensureMainSession — otherwise the server-side
    // session is created with whatever local mode happened to be active.
    const sourceChatMode =
      continuationChatMode ??
      (typeof sourceSession?.chat_mode === "string"
        ? normalizeChatMode(sourceSession.chat_mode)
        : null);

    applyMainSessionMeta(sourceSession);
    if (continuationProvider) {
      setSelectedProvider(continuationProvider);
    }
    if (sourceChatMode) {
      currentModeRef.current = sourceChatMode;
      onModeChangedRef.current?.(sourceChatMode);
    }

    // Fetch source session's messages for display
    try {
      const res = await fetch(
        `${baseUrl}/api/sessions/${sourceDbSessionId}/messages?limit=100`,
      );
      if (res.ok) {
        const data = await res.json();
        const mapped = mapApiMessages(data.messages || []);
        if (mapped.length > 0) {
          setMessages(mapped);
        }
      }
    } catch (err) {
      console.error("Failed to fetch source session messages:", err);
    }

    // Hydrate context usage and chat mode from source session
    const s = sourceSession;
    if (hasSessionUsage(s)) {
      setContextUsage(computeContextUsageFromSessionData(s));
    }
    // chat_mode was already propagated above before the backend handoff.

    // Tell backend to prepare the continuation session
    wsRef.current.send(
      JSON.stringify({
        type: "continue_in_chat",
        conversation_id: sourceDbSessionId,
        source_session_id: sourceDbSessionId,
        project_id:
          projectId ??
          (typeof sourceSession?.project_id === "string"
            ? sourceSession.project_id
            : undefined),
        provider: continuationProvider,
        model: continuationModel,
        reasoning_effort: reasoningEffort,
        chat_mode: sourceChatMode,
        fallback_context: fallbackContext,
      }),
    );

    return sourceDbSessionId;
  },
  [
    applyMainSessionMeta,
    attachedSessionId,
    attachedSessionMeta,
    bindActiveSession,
    clearPreAttachContextUsage,
    clearSessionObservationState,
    contextUsage,
    conversationId,
    currentBranch,
    dbSessionId,
    mainSessionMeta,
    messages,
    observedSessionId,
    proxyDeliveryNotice,
    resetMainChatState,
    selectedProvider,
    setContextUsage,
    setSelectedProvider,
    sessionInteractionMode,
    sessionRef,
    sessionTitle,
    viewingSessionId,
    viewingSessionMeta,
    worktreePath,
  ],
);

// Clear chat history — notifies backend to teardown session, then resets frontend.
// Returns false if WS send failed (caller can show error).
const clearHistory = useCallback((): boolean => {
  const oldConversationId = conversationIdRef.current;
  // Notify backend to generate summary + teardown session
  if (wsRef.current?.readyState !== WebSocket.OPEN) {
    return false;
  }
  wsRef.current.send(
    JSON.stringify({
      type: "clear_chat",
      conversation_id: oldConversationId,
    }),
  );
  startNewChat();
  return true;
}, [startNewChat]);

// Delete a conversation — sends WS message, returns true if sent.
// Caller is responsible for UI updates (via onChatDeleted callback).
const deleteConversation = useCallback(
  (id: string, sessionId?: string): boolean => {
    if (wsRef.current?.readyState !== WebSocket.OPEN) {
      return false;
    }
    const payload: Record<string, unknown> = {
      type: "delete_chat",
      conversation_id: id,
    };
    if (sessionId !== undefined) {
      payload.session_id = sessionId;
    }
    wsRef.current.send(JSON.stringify(payload));

    if (id === conversationIdRef.current) {
      startNewChat();
    }
    return true;
  },
  [startNewChat],
);

// Stop the current streaming response
const stopStreaming = useCallback(() => {
  if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) return;
  if (!conversationIdRef.current) return;
  wsRef.current.send(
    JSON.stringify({
      type: "stop_chat",
      conversation_id: conversationIdRef.current,
    }),
  );
  activeRequestIdRef.current = null;
  setIsStreaming(false);
  setIsThinking(false);
}, []);

// Send mode change to backend
const sendMode = useCallback((mode: ChatMode) => {
  const normalizedMode = normalizeChatMode(mode);
  if (currentModeRef.current === normalizedMode) return;
  currentModeRef.current = normalizedMode;
  if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) return;
  if (!conversationIdRef.current) return;
  setPlanPendingApproval(false);
  wsRef.current.send(
    JSON.stringify({
      type: "set_mode",
      mode: normalizedMode,
      conversation_id: conversationIdRef.current,
    }),
  );
}, []);

// Notify backend that the project changed — stops the CLI subprocess
// so the next chat_message recreates it with the correct CWD.
const sendProjectChange = useCallback((projectId: string) => {
  if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) return;
  if (!conversationIdRef.current) return;
  wsRef.current.send(
    JSON.stringify({
      type: "set_project",
      project_id: projectId,
      conversation_id: conversationIdRef.current,
    }),
  );
}, []);

// Notify backend that the agent changed — stops the CLI subprocess
// so the next chat_message recreates it with the new agent context.
const sendAgentChange = useCallback((agentName: string) => {
  if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) return;
  if (!conversationIdRef.current) return;
  setActiveAgent(agentName);
  wsRef.current.send(
    JSON.stringify({
      type: "set_agent",
      agent_name: agentName,
      conversation_id: conversationIdRef.current,
    }),
  );
}, []);

// Notify backend that the worktree changed — stops the CLI subprocess
// so the next chat_message recreates it with the correct CWD.
const sendWorktreeChange = useCallback(
  (worktreePath: string, worktreeId?: string) => {
    if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) return;
    if (!conversationIdRef.current) return;
    wsRef.current.send(
      JSON.stringify({
        type: "set_worktree",
        worktree_path: worktreePath,
        worktree_id: worktreeId,
        conversation_id: conversationIdRef.current,
      }),
    );
  },
  [],
);

// Send a message (allowed even while streaming — cancels the active stream)
const sendMessage = useCallback(
  (
    content: string,
    model?: string | null,
    files?: QueuedFile[],
    projectId?: string | null,
    injectContext?: string,
    reasoningEffort?: string | null,
    ttsEnabled?: boolean,
  ): boolean => {
    console.log(
      "sendMessage called:",
      content,
      "model:",
      model,
      "files:",
      files?.length,
    );
    const normalizedReasoningEffort = normalizeReasoningEffort(reasoningEffort);

    if (continuingSessionIdRef.current) {
      return false;
    }

    const needsSession =
      !conversationIdRef.current || !dbSessionIdRef.current;
    const isProxyTerminal =
      attachedSessionIdRef.current &&
      sessionInteractionModeRef.current === "proxy" &&
      attachedSessionMetaRef.current?.sessionType === "terminal";

    if (needsSession && !isProxyTerminal) {
      void ensureMainSession({
        projectId: projectId ?? projectIdRef.current,
        provider: selectedProviderRef.current,
        model: model ?? null,
        reasoningEffort: normalizedReasoningEffort,
      }).then((sessionId) => {
        if (!sessionId) return;
        sendMessageRef.current?.(
          content,
          model,
          files,
          projectId,
          injectContext,
          normalizedReasoningEffort,
          ttsEnabled,
        );
      });
      return true;
    }

    if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) {
      // Queue the message to send on reconnect
      console.warn("WebSocket disconnected — queuing message for reconnect");
      pendingMessagesRef.current.push({
        content,
        model,
        projectId,
        reasoningEffort: normalizedReasoningEffort,
        ttsEnabled,
      });
      // Still add the user message to the UI so it's visible
      const queuedId = `user-${uuid()}`;
      setMessages((prev) => [
        ...prev,
        {
          id: queuedId,
          role: "user" as const,
          content,
          toolCalls: [],
          timestamp: new Date(),
        },
      ]);
      return true;
    }

    // Route to a swapped terminal session when proxy mode is active.
    if (isProxyTerminal) {
      const proxySessionId = attachedSessionIdRef.current;
      if (!proxySessionId) {
        return false;
      }
      const clientMessageId = uuid();
      const messageId = `user-${clientMessageId}`;
      const pendingProxyMessage = {
        clientMessageId,
        currentMessageId: messageId,
        sessionId: proxySessionId,
      };
      pendingProxyMessagesRef.current.set(clientMessageId, pendingProxyMessage);
      enqueuePendingProxyMessage(
        pendingProxySessionQueuesRef.current,
        pendingProxyMessage,
      );
      setMessages((prev) => [
        ...prev,
        {
          id: messageId,
          role: "user",
          content,
          timestamp: new Date(),
        },
      ]);
      setProxyDeliveryNotice(null);
      wsRef.current.send(
        JSON.stringify({
          type: "send_to_cli_session",
          session_id: proxySessionId,
          content,
          client_message_id: clientMessageId,
        }),
      );
      return true;
    }

    const messageId = `user-${uuid()}`;
    const requestId = uuid();
    activeRequestIdRef.current = requestId;

    setMessages((prev) => [
      ...prev,
      {
        id: messageId,
        role: "user",
        content,
        timestamp: new Date(),
      },
    ]);

    saveConversationId(conversationIdRef.current);

    const payload: Record<string, unknown> = {
      type: "chat_message",
      content,
      message_id: messageId,
      conversation_id: conversationIdRef.current,
      request_id: requestId,
    };

    if (model) {
      payload.model = model;
    }

    if (projectId) {
      payload.project_id = projectId;
    }

    if (injectContext) {
      payload.inject_context = injectContext;
    }

    if (normalizedReasoningEffort) {
      payload.reasoning_effort = normalizedReasoningEffort;
    }

    if (typeof ttsEnabled === "boolean") {
      payload.tts_enabled = ttsEnabled;
    }

    if (selectedProviderRef.current) {
      payload.provider = selectedProviderRef.current;
    }

    if (files && files.length > 0) {
      const contentBlocks: Array<Record<string, unknown>> = [];
      for (const qf of files) {
        if (qf.file.type.startsWith("image/") && qf.base64) {
          contentBlocks.push({
            type: "image",
            source: {
              type: "base64",
              media_type: qf.file.type,
              data: qf.base64,
            },
          });
        } else if (qf.base64) {
          contentBlocks.push({
            type: "text",
            text: `[File: ${qf.file.name}]\n${atob(qf.base64)}`,
          });
        }
      }
      if (content) {
        contentBlocks.push({ type: "text", text: content });
      }
      payload.content_blocks = contentBlocks;
    }

    console.log("Sending WebSocket message:", payload);
    wsRef.current.send(JSON.stringify(payload));

    setIsStreaming(true);
    setIsThinking(true);
    return true;
  },
  [ensureMainSession],
);

// Update sendMessageRef with the latest sendMessage callback
useEffect(() => {
  sendMessageRef.current = sendMessage;
}, [sendMessage]);

// Respond to an AskUserQuestion pending in the backend.
// Returns false if WS is not connected (caller can show feedback).
const respondToQuestion = useCallback(
  (toolCallId: string, answers: Record<string, string>): boolean => {
    if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN)
      return false;
    wsRef.current.send(
      JSON.stringify({
        type: "ask_user_response",
        conversation_id: conversationIdRef.current,
        tool_call_id: toolCallId,
        answers,
      }),
    );
    return true;
  },
  [],
);

// Respond to a tool approval request.
// Returns false if WS is not connected (caller can show feedback).
const respondToApproval = useCallback(
  (
    toolCallId: string,
    decision: "approve" | "reject" | "approve_always",
  ): boolean => {
    if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN)
      return false;
    wsRef.current.send(
      JSON.stringify({
        type: "tool_approval_response",
        conversation_id: conversationIdRef.current,
        tool_call_id: toolCallId,
        decision,
      }),
    );
    return true;
  },
  [],
);

// Respond to a Canvas surface interaction
const respondToCanvas = useCallback(
  (canvasId: string, action: UserAction) => {
    if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) return;
    wsRef.current.send(
      JSON.stringify({
        type: "canvas_interaction",
        conversation_id: conversationIdRef.current,
        canvas_id: canvasId,
        action,
      }),
    );
  },
  [],
);


// Approve the current plan. The backend's mode_changed event is authoritative.
const approvePlan = useCallback(() => {
  if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) return;
  if (!conversationIdRef.current) return;
  if (!planContentRef.current) return;
  wsRef.current.send(
    JSON.stringify({
      type: "plan_approval_response",
      conversation_id: conversationIdRef.current,
      decision: "approve",
    }),
  );
}, []);

// Request changes to the plan with feedback
const requestPlanChanges = useCallback((feedback: string) => {
  if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) return;
  if (!conversationIdRef.current) return;
  if (!planContentRef.current) return;
  pendingPlanFeedbackRef.current = feedback;
  // Eagerly clear approval UI to prevent ghost flash when artifact panel closes
  setPlanPendingApproval(false);
  planContentRef.current = null;
  wsRef.current.send(
    JSON.stringify({
      type: "plan_approval_response",
      conversation_id: conversationIdRef.current,
      decision: "request_changes",
      feedback,
    }),
  );
}, []);



  return {
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
  };
}
