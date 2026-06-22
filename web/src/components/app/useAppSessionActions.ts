import { useCallback, useEffect, useRef } from "react";

import type {
  ContinueSessionInChatAction,
  DeleteConversationAction,
  SwitchConversationAction,
} from "../../hooks/useChat/actionTypes";
import type { GobbySession } from "../../types/sessions";

interface UseAppSessionActionsArgs {
  attachedSessionId: string | null;
  clearViewingSession: () => void;
  confirmSessionDeleted: (sessionId: string) => void;
  continueSessionInChat: ContinueSessionInChatAction;
  deleteConversation: DeleteConversationAction;
  detachFromSession: () => void;
  markSessionDeleting: (sessionId: string) => void;
  restoreSession: (sessionId: string) => void;
  setActiveTab: (tab: string) => void;
  setOnChatDeleted: (handler: (sessionId: string) => void) => void;
  showToast: (msg: string, durationMs?: number) => void;
  switchConversation: SwitchConversationAction;
  viewingSessionId: string | null;
}

export function useAppSessionActions({
  attachedSessionId,
  clearViewingSession,
  confirmSessionDeleted,
  continueSessionInChat,
  deleteConversation,
  detachFromSession,
  markSessionDeleting,
  restoreSession,
  setActiveTab,
  setOnChatDeleted,
  showToast,
  switchConversation,
  viewingSessionId,
}: UseAppSessionActionsArgs) {
  const deleteTimeoutsRef = useRef<
    Map<string, { sessionId: string; timerId: number }>
  >(new Map());

  useEffect(() => {
    setOnChatDeleted((sessionId: string) => {
      const entry = deleteTimeoutsRef.current.get(sessionId);
      if (entry) {
        window.clearTimeout(entry.timerId);
        deleteTimeoutsRef.current.delete(sessionId);
      }
      confirmSessionDeleted(sessionId);
    });
  }, [confirmSessionDeleted, setOnChatDeleted]);

  const handleSelectConversation = useCallback(
    (session: GobbySession) => {
      if (viewingSessionId) {
        clearViewingSession();
      } else if (attachedSessionId) {
        detachFromSession();
      }
      switchConversation(session.id);
    },
    [
      switchConversation,
      viewingSessionId,
      attachedSessionId,
      clearViewingSession,
      detachFromSession,
    ],
  );

  const handleDeleteConversation = useCallback(
    (session: GobbySession) => {
      const sent = deleteConversation(session.id, session.id);
      if (!sent) {
        showToast("Cannot delete: disconnected from server");
        return;
      }
      markSessionDeleting(session.id);
      const timerId = window.setTimeout(() => {
        restoreSession(session.id);
        deleteTimeoutsRef.current.delete(session.id);
        showToast("Delete failed: server did not respond");
      }, 5000);
      deleteTimeoutsRef.current.set(session.id, {
        sessionId: session.id,
        timerId,
      });
    },
    [
      deleteConversation,
      markSessionDeleting,
      restoreSession,
      showToast,
    ],
  );

  const handleKillAgent = useCallback(
    async (runId: string) => {
      try {
        const res = await fetch(
          `/api/agents/runs/${encodeURIComponent(runId)}/cancel`,
          { method: "POST" },
        );
        if (res.ok) {
          showToast("Agent cancelled");
          return true;
        }
        showToast("Failed to cancel agent");
        return false;
      } catch {
        showToast("Failed to cancel agent");
        return false;
      }
    },
    [showToast],
  );

  const handleExpireSession = useCallback(
    async (sessionId: string) => {
      try {
        const res = await fetch(
          `/api/sessions/${encodeURIComponent(sessionId)}/expire`,
          { method: "POST" },
        );
        if (!res.ok) {
          showToast("Failed to expire session");
          return false;
        }
        return true;
      } catch {
        showToast("Failed to expire session");
        return false;
      }
    },
    [showToast],
  );

  const handleContinueInChat = useCallback(
    async (session: GobbySession) => {
      setActiveTab("chat");
      await continueSessionInChat(session.id, session.project_id, {
        fallbackContext: "auto",
      });
    },
    [continueSessionInChat, setActiveTab],
  );

  return {
    handleContinueInChat,
    handleDeleteConversation,
    handleExpireSession,
    handleKillAgent,
    handleSelectConversation,
  };
}
