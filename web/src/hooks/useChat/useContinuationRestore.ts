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
  sessionRefs: {
    attachedSessionIdRef: MutableRefObject<string | null>;
    conversationIdRef: MutableRefObject<string>;
    dbSessionIdRef: MutableRefObject<string | null>;
    observedSessionIdRef: MutableRefObject<string | null>;
    viewingSessionIdRef: MutableRefObject<string | null>;
  };
  sessionSetters: {
    setAttachedSessionId: Setter<string | null>;
    setConversationId: Setter<string>;
    setDbSessionId: Setter<string | null>;
    setObservedSessionId: Setter<string | null>;
    setSelectedProvider: Setter<string | null>;
    setSessionRef: Setter<string | null>;
    setSessionTitle: Setter<string | null>;
    setViewingSessionId: Setter<string | null>;
  };
  conversationRefs: {
    attachedSessionMetaRef: MutableRefObject<ContinuationRollbackSnapshot["attachedSessionMeta"]>;
    observedSessionMetaRef: MutableRefObject<ContinuationRollbackSnapshot["observedSessionMeta"]>;
    viewingSessionMetaRef: MutableRefObject<ContinuationRollbackSnapshot["viewingSessionMeta"]>;
    wsRef: MutableRefObject<WebSocket | null>;
  };
  conversationSetters: {
    setAttachedSessionMeta: Setter<ContinuationRollbackSnapshot["attachedSessionMeta"]>;
    setContextUsage: Setter<ContinuationRollbackSnapshot["contextUsage"]>;
    setCurrentBranch: Setter<string | null>;
    setCurrentMode: (mode: ChatMode) => ChatMode;
    setIsLoadingMessages: Setter<boolean>;
    setMainSessionMeta: Setter<ContinuationRollbackSnapshot["mainSessionMeta"]>;
    setMessages: Setter<ContinuationRollbackSnapshot["messages"]>;
    setProxyDeliveryNotice: Setter<string | null>;
    setViewingSessionMeta: Setter<ContinuationRollbackSnapshot["viewingSessionMeta"]>;
    setWorktreePath: Setter<string | null>;
  };
  interactionMode: {
    pendingSessionInteractionModeRef: MutableRefObject<"observe" | "proxy">;
    sessionInteractionModeRef: MutableRefObject<ContinuationRollbackSnapshot["sessionInteractionMode"]>;
    setSessionInteractionMode: Setter<ContinuationRollbackSnapshot["sessionInteractionMode"]>;
  };
}

export function useContinuationRestore({
  sessionRefs,
  sessionSetters,
  conversationRefs,
  conversationSetters,
  interactionMode,
}: UseContinuationRestoreParams) {
  const {
    attachedSessionIdRef,
    conversationIdRef,
    dbSessionIdRef,
    observedSessionIdRef,
    viewingSessionIdRef,
  } = sessionRefs;
  const {
    setAttachedSessionId,
    setConversationId,
    setDbSessionId,
    setObservedSessionId,
    setSelectedProvider,
    setSessionRef,
    setSessionTitle,
    setViewingSessionId,
  } = sessionSetters;
  const {
    attachedSessionMetaRef,
    observedSessionMetaRef,
    viewingSessionMetaRef,
    wsRef,
  } = conversationRefs;
  const {
    setAttachedSessionMeta,
    setContextUsage,
    setCurrentBranch,
    setCurrentMode,
    setIsLoadingMessages,
    setMainSessionMeta,
    setMessages,
    setProxyDeliveryNotice,
    setViewingSessionMeta,
    setWorktreePath,
  } = conversationSetters;
  const {
    pendingSessionInteractionModeRef,
    sessionInteractionModeRef,
    setSessionInteractionMode,
  } = interactionMode;

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
