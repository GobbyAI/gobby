import { useCallback, useEffect, useRef, useState } from "react";
import type {
  Dispatch,
  MutableRefObject,
  SetStateAction,
} from "react";
import type { ChatMode, SessionObservationMeta } from "../../types/chat";
import { normalizeChatMode } from "../../types/chat";
import { clearFreshChatDraft } from "../../lib/sessionPersistence";
import {
  computeContextUsageFromSessionData,
  createWebChatSession,
  hasSessionUsage,
  isChatProvider,
  loadConversationId,
  loadDbSessionId,
  saveConversationId,
  saveDbSessionId,
  toSessionObservationMeta,
  type ContextUsage,
} from "./core";

type Setter<T> = Dispatch<SetStateAction<T>>;

interface UseSessionIdentityStateParams {
  activeAgentRef: MutableRefObject<string>;
  currentModeRef: MutableRefObject<ChatMode>;
  onModeChangedRef: MutableRefObject<((mode: ChatMode) => void) | null>;
  projectIdRef: MutableRefObject<string | null>;
  selectedProviderRef: MutableRefObject<string | null>;
  setContextUsage: Setter<ContextUsage>;
  setSelectedProvider: (provider: string | null) => void;
  wsRef: MutableRefObject<WebSocket | null>;
}

export function useSessionIdentityState({
  activeAgentRef,
  currentModeRef,
  onModeChangedRef,
  projectIdRef,
  selectedProviderRef,
  setContextUsage,
  setSelectedProvider,
  wsRef,
}: UseSessionIdentityStateParams) {
  const [conversationId, setConversationId] = useState<string>(() =>
    loadConversationId(),
  );
  const conversationIdRef = useRef<string>(conversationId);
  // Increments only on intentional conversation switches, not SDK session ID adoption.
  const [conversationSwitchKey, setConversationSwitchKey] = useState(0);

  const [sessionRef, setSessionRef] = useState<string | null>(null);
  const sessionRefRef = useRef<string | null>(sessionRef);

  const [dbSessionId, setDbSessionId] = useState<string | null>(() =>
    loadDbSessionId(),
  );
  const dbSessionIdRef = useRef<string | null>(dbSessionId);
  const creatingSessionIdRef = useRef<Promise<string | null> | null>(null);
  const creatingForceNewSessionIdRef = useRef<Promise<string | null> | null>(null);
  const lastSeqRef = useRef<number>(0);

  const [currentBranch, setCurrentBranch] = useState<string | null>(null);
  const [worktreePath, setWorktreePath] = useState<string | null>(null);
  const [sessionTitle, setSessionTitle] = useState<string | null>(null);
  const [mainSessionMeta, setMainSessionMeta] =
    useState<SessionObservationMeta | null>(null);

  useEffect(() => {
    conversationIdRef.current = conversationId;
  }, [conversationId]);

  useEffect(() => {
    dbSessionIdRef.current = dbSessionId;
  }, [dbSessionId]);

  useEffect(() => {
    sessionRefRef.current = sessionRef;
  }, [sessionRef]);

  const bindActiveSession = useCallback((sessionId: string | null) => {
    const nextId = sessionId ?? "";
    lastSeqRef.current = 0;
    conversationIdRef.current = nextId;
    setConversationId(nextId);
    setDbSessionId(sessionId);
    dbSessionIdRef.current = sessionId;
    saveDbSessionId(sessionId);
    saveConversationId(nextId);
    if (sessionId) {
      clearFreshChatDraft();
    }
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
    [currentModeRef, onModeChangedRef, setContextUsage, setSelectedProvider],
  );

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
      if (options?.forceNew && creatingForceNewSessionIdRef.current) {
        return await creatingForceNewSessionIdRef.current;
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
          const ws = wsRef.current;
          const agentName = activeAgentRef.current;
          if (ws?.readyState === WebSocket.OPEN && agentName && agentName !== "default") {
            try {
              ws.send(
                JSON.stringify({
                  type: "set_agent",
                  conversation_id: session.id,
                  agent_name: agentName,
                }),
              );
            } catch (error) {
              console.warn("Failed to send set_agent for new chat session", error);
            }
          }
          return session.id;
        })
        .catch((error) => {
          console.error("Failed to create web chat session:", error);
          throw error;
        })
        .finally(() => {
          if (options?.forceNew) {
            if (creatingForceNewSessionIdRef.current === pending) {
              creatingForceNewSessionIdRef.current = null;
            }
            return;
          }
          if (creatingSessionIdRef.current === pending) {
            creatingSessionIdRef.current = null;
          }
        });

      if (options?.forceNew) {
        creatingForceNewSessionIdRef.current = pending;
      } else {
        creatingSessionIdRef.current = pending;
      }
      return await pending;
    },
    [
      activeAgentRef,
      applyMainSessionMeta,
      bindActiveSession,
      currentModeRef,
      projectIdRef,
      selectedProviderRef,
      wsRef,
    ],
  );

  return {
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
  };
}
