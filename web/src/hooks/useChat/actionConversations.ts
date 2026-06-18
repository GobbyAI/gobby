/* eslint-disable react-hooks/exhaustive-deps -- Extracted useChat callbacks intentionally close over parent refs and stable setters to preserve the original hook behavior. */
import { useCallback } from "react";
import { normalizeChatMode } from "../../types/chat";
import {
  mapApiMessages,
  mapRenderedMessageToChatMessage,
} from "../../lib/chatMessageMapping";
import { markFreshChatDraft } from "../../lib/sessionPersistence";
import {
  computeContextUsageFromSessionData,
  hasSessionUsage,
} from "./contextUsage";
import { saveConversationId, uuid } from "./conversationPersistence";
import {
  isChatProvider,
  normalizeReasoningEffort,
} from "./sessionRecords";
import type {
  ContinueSessionInChatAction,
  ConversationActions,
  ResumeSessionAction,
  StartNewChatAction,
  SwitchConversationAction,
  SwitchProviderAction,
  UseChatActionsParams,
} from "./actionTypes";

export function useConversationActions(
  params: UseChatActionsParams,
): ConversationActions {
  const {
    activeRequestIdRef,
    applyMainSessionMeta,
    attachedSessionId,
    attachedSessionMeta,
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
    projectIdRef,
    proxyDeliveryNotice,
    resetMainChatState,
    selectedProvider,
    selectedProviderRef,
    sessionInteractionMode,
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
    setSelectedProvider,
    viewingSessionId,
    viewingSessionIdRef,
    viewingSessionMeta,
    worktreePath,
    wsRef,
  } = params;

  const switchConversation: SwitchConversationAction = useCallback(
    (id, options) => {
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
            if (viewingSessionIdRef.current || dbSessionIdRef.current !== id) {
              return;
            }
            if (!data?.messages?.length || conversationIdRef.current !== id) {
              return;
            }
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
          if (
            !s ||
            conversationIdRef.current !== id ||
            dbSessionIdRef.current !== id
          ) {
            return;
          }
          if (!preserveViewing && viewingSessionIdRef.current) return;
          applyMainSessionMeta(s);
          if (s.chat_mode) {
            const restored = normalizeChatMode(s.chat_mode);
            const previousMode = currentModeRef.current;
            if (
              restored !== previousMode &&
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
            if (restored !== previousMode) {
              setCurrentMode(restored);
            }
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
      setCurrentMode,
    ],
  );

  const startNewChat: StartNewChatAction = useCallback(
    (agentName) => {
      const effectiveAgent = agentName || "default";
      setActiveAgent(effectiveAgent);
      clearPreAttachContextUsage();
      clearSessionObservationState();
      resetMainChatState();
      bindActiveSession(null);
      markFreshChatDraft();
      setConversationSwitchKey((k) => k + 1);
    },
    [
      bindActiveSession,
      clearPreAttachContextUsage,
      clearSessionObservationState,
      resetMainChatState,
    ],
  );

  const switchProvider: SwitchProviderAction = useCallback(
    async (newProvider, options) => {
      const hadServerBackedChat =
        Boolean(dbSessionIdRef.current) || messagesRef.current.length > 0;
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
      setSelectedProvider(newProvider);
      setConversationSwitchKey((k) => k + 1);

      if (!hadServerBackedChat) {
        return;
      }

      try {
        await ensureMainSession({
          projectId: projectIdRef.current,
          provider: newProvider,
          model: options?.model ?? null,
          reasoningEffort: options?.reasoningEffort ?? null,
          forceNew: true,
        });
      } catch (error) {
        console.error("Failed to create chat session for provider change:", error);
      }
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

  const resumeSession: ResumeSessionAction = useCallback((externalId) => {
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

  const continueSessionInChat: ContinueSessionInChatAction = useCallback(
    async (sourceDbSessionId, projectId, options) => {
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
        setCurrentMode(sourceChatMode);
      }

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

      if (hasSessionUsage(sourceSession)) {
        setContextUsage(computeContextUsageFromSessionData(sourceSession));
      }

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
      setCurrentMode,
      setSelectedProvider,
      sessionInteractionMode,
      sessionRef,
      sessionTitle,
      viewingSessionId,
      viewingSessionMeta,
      worktreePath,
    ],
  );

  return {
    switchConversation,
    startNewChat,
    switchProvider,
    resumeSession,
    continueSessionInChat,
  };
}
