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

const RESTORABLE_CHAT_MODES = new Set<ChatMode>([
  "accept_edits",
  "bypass",
  "normal",
  "plan",
]);
const RESTORABLE_INTERACTION_MODES = new Set(["none", "observe", "proxy"]);

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function nullableString(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

function nullableRecord<T>(value: unknown): T | null {
  return isRecord(value) ? (value as T) : null;
}

function normalizeContextUsage(
  value: unknown,
): ContinuationRollbackSnapshot["contextUsage"] {
  const record = isRecord(value) ? value : {};
  const numberOrZero = (key: string) => {
    const field = record[key];
    return typeof field === "number" ? field : 0;
  };
  const contextWindow =
    typeof record.contextWindow === "number" ? record.contextWindow : null;
  return {
    totalInputTokens: numberOrZero("totalInputTokens"),
    outputTokens: numberOrZero("outputTokens"),
    contextWindow,
    uncachedInputTokens: numberOrZero("uncachedInputTokens"),
    cacheReadTokens: numberOrZero("cacheReadTokens"),
    cacheCreationTokens: numberOrZero("cacheCreationTokens"),
  };
}

function normalizeContinuationSnapshot(
  snapshot: unknown,
): ContinuationRollbackSnapshot | null {
  if (!isRecord(snapshot)) return null;
  if (typeof snapshot.sourceSessionId !== "string") return null;
  if (typeof snapshot.conversationId !== "string") return null;
  if (!Array.isArray(snapshot.messages)) return null;

  const currentMode = RESTORABLE_CHAT_MODES.has(snapshot.currentMode as ChatMode)
    ? snapshot.currentMode as ChatMode
    : "plan";
  const sessionInteractionMode = RESTORABLE_INTERACTION_MODES.has(
    snapshot.sessionInteractionMode as string,
  )
    ? snapshot.sessionInteractionMode as ContinuationRollbackSnapshot["sessionInteractionMode"]
    : "none";

  return {
    sourceSessionId: snapshot.sourceSessionId,
    conversationId: snapshot.conversationId,
    dbSessionId: nullableString(snapshot.dbSessionId),
    mainSessionMeta:
      nullableRecord<ContinuationRollbackSnapshot["mainSessionMeta"]>(
        snapshot.mainSessionMeta,
      ),
    sessionTitle: nullableString(snapshot.sessionTitle),
    sessionRef: nullableString(snapshot.sessionRef),
    selectedProvider: nullableString(snapshot.selectedProvider),
    messages: snapshot.messages as ContinuationRollbackSnapshot["messages"],
    contextUsage: normalizeContextUsage(snapshot.contextUsage),
    currentMode,
    currentBranch: nullableString(snapshot.currentBranch),
    worktreePath: nullableString(snapshot.worktreePath),
    viewingSessionId: nullableString(snapshot.viewingSessionId),
    viewingSessionMeta:
      nullableRecord<ContinuationRollbackSnapshot["viewingSessionMeta"]>(
        snapshot.viewingSessionMeta,
      ),
    observedSessionId: nullableString(snapshot.observedSessionId),
    observedSessionMeta:
      nullableRecord<ContinuationRollbackSnapshot["observedSessionMeta"]>(
        snapshot.observedSessionMeta,
      ),
    attachedSessionId: nullableString(snapshot.attachedSessionId),
    attachedSessionMeta:
      nullableRecord<ContinuationRollbackSnapshot["attachedSessionMeta"]>(
        snapshot.attachedSessionMeta,
      ),
    sessionInteractionMode,
    proxyDeliveryNotice: nullableString(snapshot.proxyDeliveryNotice),
  };
}

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
      const restored = normalizeContinuationSnapshot(snapshot);
      if (!restored) {
        console.warn("Skipped invalid continuation rollback snapshot");
        setIsLoadingMessages(false);
        return;
      }

      conversationIdRef.current = restored.conversationId;
      setConversationId(restored.conversationId);
      saveConversationId(restored.conversationId);
      dbSessionIdRef.current = restored.dbSessionId;
      setDbSessionId(restored.dbSessionId);
      saveDbSessionId(restored.dbSessionId);
      setMessages(restored.messages);
      setMainSessionMeta(restored.mainSessionMeta);
      setSessionTitle(restored.sessionTitle);
      setSessionRef(restored.sessionRef);
      setSelectedProvider(restored.selectedProvider);
      setContextUsage(restored.contextUsage);
      setCurrentBranch(restored.currentBranch);
      setWorktreePath(restored.worktreePath);
      setViewingSessionId(restored.viewingSessionId);
      viewingSessionIdRef.current = restored.viewingSessionId;
      saveViewingSessionId(restored.viewingSessionId);
      setViewingSessionMeta(restored.viewingSessionMeta);
      viewingSessionMetaRef.current = restored.viewingSessionMeta;
      observedSessionIdRef.current = restored.observedSessionId;
      observedSessionMetaRef.current = restored.observedSessionMeta;
      setObservedSessionId(restored.observedSessionId);
      attachedSessionIdRef.current = restored.attachedSessionId;
      setAttachedSessionId(restored.attachedSessionId);
      attachedSessionMetaRef.current = restored.attachedSessionMeta;
      setAttachedSessionMeta(restored.attachedSessionMeta);
      sessionInteractionModeRef.current = restored.sessionInteractionMode;
      setSessionInteractionMode(restored.sessionInteractionMode);
      saveViewingSessionMode(
        restored.viewingSessionId ? restored.sessionInteractionMode : "none",
      );
      setProxyDeliveryNotice(restored.proxyDeliveryNotice);
      setIsLoadingMessages(false);
      setCurrentMode(restored.currentMode);

      if (
        restored.observedSessionId &&
        restored.sessionInteractionMode !== "none" &&
        wsRef.current?.readyState === WebSocket.OPEN
      ) {
        let payload: string;
        try {
          payload = JSON.stringify({
            type: "attach_to_session",
            session_id: restored.observedSessionId,
          });
        } catch (error) {
          console.warn("Failed to serialize restored session attach request", error);
          return;
        }
        const ws = wsRef.current;
        if (ws?.readyState !== WebSocket.OPEN) return;
        try {
          ws.send(payload);
          pendingSessionInteractionModeRef.current =
            restored.sessionInteractionMode === "proxy" ? "proxy" : "observe";
        } catch (error) {
          console.warn("Failed to send restored session attach request", error);
        }
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
