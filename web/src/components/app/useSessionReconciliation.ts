import { useEffect, type MutableRefObject } from "react";
import type { GobbySession } from "../../types/sessions";
import {
  loadPersistedConversationId,
  loadPersistedDbSessionId,
} from "../../lib/sessionPersistence";

interface UseSessionReconciliationArgs {
  initialReconciliationDoneRef: MutableRefObject<boolean>;
  projectReady: boolean;
  effectiveProjectId: string | null;
  isLoadingSessions: boolean;
  webChatSessions: GobbySession[];
  dbSessionId: string | null;
  viewingSessionId: string | null;
  switchConversation: (
    sessionId: string,
    options?: { preserveViewing?: boolean },
  ) => void;
  startNewChat: () => void;
}

export function useSessionReconciliation({
  initialReconciliationDoneRef,
  projectReady,
  effectiveProjectId,
  isLoadingSessions,
  webChatSessions,
  dbSessionId,
  viewingSessionId,
  switchConversation,
  startNewChat,
}: UseSessionReconciliationArgs) {
  useEffect(() => {
    if (!projectReady) return;
    if (initialReconciliationDoneRef.current) return;
    if (!effectiveProjectId || isLoadingSessions) return;

    const sessionsMatchProject =
      webChatSessions.length === 0 ||
      webChatSessions.some((s) => s.project_id === effectiveProjectId);
    if (!sessionsMatchProject) return;

    const persistedConversationId = loadPersistedConversationId();
    const persistedDbSessionId = loadPersistedDbSessionId();

    const activeMainChatId = dbSessionId || persistedDbSessionId;
    const match = activeMainChatId
      ? webChatSessions.find((s) => s.id === activeMainChatId)
      : undefined;

    if (match) {
      initialReconciliationDoneRef.current = true;
      if (dbSessionId === match.id) {
        return;
      }
      switchConversation(match.id, {
        preserveViewing: Boolean(viewingSessionId),
      });
    } else if (viewingSessionId) {
      return;
    } else if (persistedDbSessionId && webChatSessions.length === 0) {
      return;
    } else if (persistedConversationId && !persistedDbSessionId) {
      initialReconciliationDoneRef.current = true;
      return;
    } else if (webChatSessions.length > 0) {
      initialReconciliationDoneRef.current = true;
      const mostRecent = webChatSessions[0];
      switchConversation(mostRecent.id);
    } else {
      initialReconciliationDoneRef.current = true;
      startNewChat();
    }
  }, [
    projectReady,
    effectiveProjectId,
    isLoadingSessions,
    webChatSessions,
    dbSessionId,
    viewingSessionId,
    switchConversation,
    startNewChat,
    initialReconciliationDoneRef,
  ]);
}
