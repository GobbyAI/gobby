import { useCallback, type MutableRefObject } from "react";
import type { ChatMode } from "../../types/chat";
import {
  saveConversationId,
  saveDbSessionId,
  saveViewingSessionId,
  saveViewingSessionMode,
  type ContinuationRollbackSnapshot,
} from "./core";

type Setter<T> = (value: T) => void;

interface UseContinuationRestoreParams {
  attachedSessionIdRef: MutableRefObject<string | null>;
  attachedSessionMetaRef: MutableRefObject<ContinuationRollbackSnapshot["attachedSessionMeta"]>;
  conversationIdRef: MutableRefObject<string>;
  dbSessionIdRef: MutableRefObject<string | null>;
  observedSessionIdRef: MutableRefObject<string | null>;
  observedSessionMetaRef: MutableRefObject<ContinuationRollbackSnapshot["observedSessionMeta"]>;
  pendingSessionInteractionModeRef: MutableRefObject<"observe" | "proxy">;
  sessionInteractionModeRef: MutableRefObject<ContinuationRollbackSnapshot["sessionInteractionMode"]>;
  setAttachedSessionId: Setter<string | null>;
  setAttachedSessionMeta: Setter<ContinuationRollbackSnapshot["attachedSessionMeta"]>;
  setContextUsage: Setter<ContinuationRollbackSnapshot["contextUsage"]>;
  setConversationId: Setter<string>;
  setCurrentBranch: Setter<string | null>;
  setCurrentMode: (mode: ChatMode) => ChatMode;
  setDbSessionId: Setter<string | null>;
  setIsLoadingMessages: Setter<boolean>;
  setMainSessionMeta: Setter<ContinuationRollbackSnapshot["mainSessionMeta"]>;
  setMessages: Setter<ContinuationRollbackSnapshot["messages"]>;
  setObservedSessionId: Setter<string | null>;
  setProxyDeliveryNotice: Setter<string | null>;
  setSelectedProvider: Setter<string | null>;
  setSessionInteractionMode: Setter<ContinuationRollbackSnapshot["sessionInteractionMode"]>;
  setSessionRef: Setter<string | null>;
  setSessionTitle: Setter<string | null>;
  setViewingSessionId: Setter<string | null>;
  setViewingSessionMeta: Setter<ContinuationRollbackSnapshot["viewingSessionMeta"]>;
  setWorktreePath: Setter<string | null>;
  viewingSessionIdRef: MutableRefObject<string | null>;
  viewingSessionMetaRef: MutableRefObject<ContinuationRollbackSnapshot["viewingSessionMeta"]>;
  wsRef: MutableRefObject<WebSocket | null>;
}

export function useContinuationRestore({
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
}: UseContinuationRestoreParams) {
  return useCallback(
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
        snapshot.viewingSessionId ? snapshot.sessionInteractionMode : "none",
      );
      setProxyDeliveryNotice(snapshot.proxyDeliveryNotice);
      setIsLoadingMessages(false);
      setCurrentMode(snapshot.currentMode);

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
    [
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
    ],
  );
}
