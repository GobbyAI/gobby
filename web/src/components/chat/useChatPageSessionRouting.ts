import { useCallback, useEffect, useRef, useState } from "react";

import type {
  ChatState,
  ConversationState,
  SessionObservationMeta,
  SwappedSessionTarget,
} from "../../types/chat";
import type { GobbySession } from "../../types/sessions";
import type { ActivityTab } from "../activity/ActivityPanelTabs";

interface UseChatPageSessionRoutingArgs {
  chat: ChatState;
  conversations: ConversationState;
  projectId?: string | null;
  showTab: (tab: ActivityTab) => void;
  dismissOnMobile: () => void;
}

export interface UseChatPageSessionRoutingResult {
  activeSession: GobbySession | undefined;
  mainSessionMeta: SessionObservationMeta | null;
  activeTitle: string | null;
  effectiveSessionRef: string | null;
  focusSessionId: string | null;
  activityPanelChatSessionId: string | null;
  handleFocusSessionHandled: () => void;
  handleSwapSession: (target: SwappedSessionTarget) => void;
  handleResumeSessionFromActivity: (sessionId: string) => Promise<string>;
  handleAddFileToChat: (filePath: string) => void;
  handleNewChat: (agentName?: string) => void;
}

function sessionToObservationMeta(
  session: GobbySession,
): SessionObservationMeta {
  return {
    ref: session.seq_num != null ? `#${session.seq_num}` : null,
    source: session.source,
    title: session.title ?? null,
    status: session.status,
    model: session.model ?? null,
    externalId: session.external_id,
    chatMode: session.chat_mode ?? null,
    gitBranch: session.git_branch ?? null,
    contextWindow: null,
    agentRunId: session.agent_run_id ?? null,
    workflowName: null,
    agentName: null,
    sessionType: "web_chat",
  };
}

export function useChatPageSessionRouting({
  chat,
  conversations,
  projectId,
  showTab,
  dismissOnMobile,
}: UseChatPageSessionRoutingArgs): UseChatPageSessionRoutingResult {
  const activeSession = conversations.sessions.find(
    (session) => session.id === conversations.activeSessionId,
  );
  const mainSessionMeta =
    chat.mainSessionMeta ?? (activeSession ? sessionToObservationMeta(activeSession) : null);
  const activeTitle = chat.sessionTitle ?? mainSessionMeta?.title ?? null;
  const effectiveSessionRef =
    chat.sessionRef ??
    mainSessionMeta?.ref ??
    (activeSession?.seq_num != null ? `#${activeSession.seq_num}` : null);

  const [focusSessionId, setFocusSessionId] = useState<string | null>(null);
  const onSendRef = useRef(chat.onSend);

  useEffect(() => {
    onSendRef.current = chat.onSend;
  }, [chat.onSend]);

  const handleFocusSessionHandled = useCallback(() => {
    setFocusSessionId(null);
  }, []);

  const parkCurrentSession = useCallback(
    (nextSessionId?: string) => {
      const currentSessionId = chat.dbSessionId;
      if (!currentSessionId || currentSessionId === nextSessionId) {
        return;
      }
      setFocusSessionId(currentSessionId);
      showTab("sessions");
    },
    [chat.dbSessionId, showTab],
  );

  const handleSwapSession = useCallback(
    (target: SwappedSessionTarget) => {
      parkCurrentSession(target.sessionId);

      if (target.sessionType === "web_chat") {
        const targetSession = conversations.sessions.find(
          (session) => session.id === target.sessionId,
        );
        if (targetSession) {
          conversations.onSelectSession(targetSession);
        }
        dismissOnMobile();
        return;
      }

      chat.viewSession?.(target.sessionId, { forceRefresh: true });
      chat.observeSession?.(target.sessionId, "observe");
      dismissOnMobile();
    },
    [chat, conversations, dismissOnMobile, parkCurrentSession],
  );

  const handleResumeSessionFromActivity = useCallback(
    async (sessionId: string) => {
      if (!chat.continueSessionInChat) {
        return "";
      }
      parkCurrentSession(sessionId);
      return chat.continueSessionInChat(sessionId, projectId ?? undefined, {
        fallbackContext: "auto",
      });
    },
    [chat, parkCurrentSession, projectId],
  );

  const handleAddFileToChat = useCallback((filePath: string) => {
    onSendRef.current?.(`Read and reference this file: ${filePath}`);
  }, []);

  const handleNewChat = useCallback(
    (agentName?: string) => {
      parkCurrentSession();
      conversations.onNewChat(agentName);
    },
    [conversations, parkCurrentSession],
  );

  const activityPanelChatSessionId =
    chat.viewingSessionId ?? chat.attachedSessionId ?? chat.dbSessionId ?? null;

  return {
    activeSession,
    mainSessionMeta,
    activeTitle,
    effectiveSessionRef,
    focusSessionId,
    activityPanelChatSessionId,
    handleFocusSessionHandled,
    handleSwapSession,
    handleResumeSessionFromActivity,
    handleAddFileToChat,
    handleNewChat,
  };
}
