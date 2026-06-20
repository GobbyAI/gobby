/* eslint-disable react-hooks/exhaustive-deps -- Extracted useChat callbacks intentionally close over parent refs and stable setters to preserve the original hook behavior. */
import { useCallback } from "react";
import { normalizeChatMode } from "../../types/chat";
import type {
  ChatControlActions,
  DeleteConversationAction,
  RespondToApprovalAction,
  RespondToQuestionAction,
  SendAgentChangeAction,
  SendAttachedSessionModeAction,
  SendModeAction,
  SendProjectChangeAction,
  SendWorktreeChangeAction,
  StartNewChatAction,
  UseChatActionsParams,
} from "./actionTypes";

export function useChatControlActions(
  params: UseChatActionsParams,
  startNewChat: StartNewChatAction,
): ChatControlActions {
  const {
    activeRequestIdRef,
    attachedSessionIdRef,
    attachedSessionMetaRef,
    conversationIdRef,
    currentModeRef,
    sessionInteractionModeRef,
    setActiveAgent,
    setCurrentMode,
    setIsStreaming,
    setIsThinking,
    setPlanPendingApproval,
    wsRef,
  } = params;

  const clearHistory = useCallback((): boolean => {
    const oldConversationId = conversationIdRef.current;
    if (oldConversationId == null || oldConversationId === "") {
      return false;
    }
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

  const deleteConversation: DeleteConversationAction = useCallback(
    (id, sessionId) => {
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

  const sendMode: SendModeAction = useCallback((mode) => {
    const normalizedMode = normalizeChatMode(mode);
    if (currentModeRef.current === normalizedMode) return;
    setCurrentMode(normalizedMode);
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

  const sendAttachedSessionMode: SendAttachedSessionModeAction = useCallback(
    (targetSessionId, mode) => {
      if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) return;
      const normalizedMode = normalizeChatMode(mode);
      wsRef.current.send(
        JSON.stringify({
          type: "set_mode",
          mode: normalizedMode,
          target_session_id: targetSessionId,
        }),
      );
    },
    [],
  );

  const sendProjectChange: SendProjectChangeAction = useCallback((projectId) => {
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

  const sendAgentChange: SendAgentChangeAction = useCallback((agentName) => {
    if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) return;
    const proxySessionId = attachedSessionIdRef.current;
    const isProxyTerminal =
      proxySessionId &&
      sessionInteractionModeRef.current === "proxy" &&
      attachedSessionMetaRef.current?.sessionType === "terminal";
    setActiveAgent(agentName);
    if (isProxyTerminal) {
      wsRef.current.send(
        JSON.stringify({
          type: "set_agent",
          agent_name: agentName,
          target_session_id: proxySessionId,
        }),
      );
      return;
    }
    if (!conversationIdRef.current) return;
    wsRef.current.send(
      JSON.stringify({
        type: "set_agent",
        agent_name: agentName,
        conversation_id: conversationIdRef.current,
      }),
    );
  }, []);

  const sendWorktreeChange: SendWorktreeChangeAction = useCallback(
    (worktreePath, worktreeId) => {
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

  const respondToQuestion: RespondToQuestionAction = useCallback(
    (toolCallId, answers) => {
      if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) {
        return false;
      }
      if (!conversationIdRef.current) {
        return false;
      }
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

  const respondToApproval: RespondToApprovalAction = useCallback(
    (toolCallId, decision) => {
      if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) {
        return false;
      }
      if (!conversationIdRef.current) {
        return false;
      }
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

  return {
    clearHistory,
    deleteConversation,
    stopStreaming,
    sendMode,
    sendAttachedSessionMode,
    sendProjectChange,
    sendAgentChange,
    sendWorktreeChange,
    respondToQuestion,
    respondToApproval,
  };
}
