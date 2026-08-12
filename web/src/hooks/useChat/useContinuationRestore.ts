import { useCallback, type MutableRefObject } from "react";
import type {
  ChatMessage,
  ChatMode,
  ContentBlock,
  SessionObservationMeta,
  ToolCall,
  ToolResult,
} from "../../types/chat";
import { CHAT_MODES, normalizeChatMode } from "../../types/chat";
import {
  saveConversationId,
  saveDbSessionId,
  saveViewingSessionId,
  saveViewingSessionMode,
} from "./conversationPersistence";
import type { ContinuationRollbackSnapshot } from "./sessionRecords";

type Setter<T> = (value: T) => void;

export const RESTORABLE_CHAT_MODES = new Set<ChatMode>(
  CHAT_MODES.map(({ id }) => id),
);
const RESTORABLE_INTERACTION_MODES = new Set(["none", "observe", "proxy"]);

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function nullableString(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

function validDate(value: unknown): Date | null {
  if (
    !(value instanceof Date) &&
    typeof value !== "string" &&
    typeof value !== "number"
  ) {
    return null;
  }
  const date = value instanceof Date ? value : new Date(value);
  return Number.isNaN(date.valueOf()) ? null : date;
}

function normalizeRestorableChatMode(value: unknown): ChatMode {
  if (typeof value !== "string") return "plan";
  const normalized = normalizeChatMode(value);
  return RESTORABLE_CHAT_MODES.has(normalized) ? normalized : "plan";
}

function isRestorableInteractionMode(
  value: unknown,
): value is ContinuationRollbackSnapshot["sessionInteractionMode"] {
  return typeof value === "string" && RESTORABLE_INTERACTION_MODES.has(value);
}

function isToolResult(value: unknown): value is ToolResult {
  if (!isRecord(value)) return false;
  if (!["text", "json", "image", "error"].includes(String(value.kind))) {
    return false;
  }
  if (typeof value.truncated !== "boolean") return false;
  return value.metadata === undefined || isRecord(value.metadata);
}

function isToolCall(value: unknown): value is ToolCall {
  if (!isRecord(value)) return false;
  if (typeof value.id !== "string") return false;
  if (typeof value.tool_name !== "string") return false;
  if (typeof value.server_name !== "string") return false;
  if (typeof value.tool_type !== "string") return false;
  if (
    !["calling", "completed", "error", "pending", "pending_approval"].includes(
      String(value.status),
    )
  ) {
    return false;
  }
  if (value.arguments !== undefined && !isRecord(value.arguments)) return false;
  if (value.result !== undefined && !isToolResult(value.result)) return false;
  return value.error === undefined || typeof value.error === "string";
}

function optionalString(value: unknown): boolean {
  return value === undefined || typeof value === "string";
}

function optionalNumber(value: unknown): boolean {
  return value === undefined || typeof value === "number";
}

function isImageUrl(value: unknown): boolean {
  return (
    typeof value === "string" || (isRecord(value) && optionalString(value.url))
  );
}

export function isContentBlock(value: unknown): value is ContentBlock {
  if (!isRecord(value) || typeof value.type !== "string") return false;

  switch (value.type) {
    case "text":
    case "thinking":
    case "compaction_summary":
      return typeof value.content === "string";
    case "tool_chain":
      return (
        Array.isArray(value.tool_calls) && value.tool_calls.every(isToolCall)
      );
    case "tool_reference":
      return (
        typeof value.tool_name === "string" &&
        typeof value.server_name === "string"
      );
    case "attachment":
      return isRecord(value.attachment);
    case "image":
      return (
        (value.source === undefined || isRecord(value.source)) &&
        (value.image_url === undefined || isImageUrl(value.image_url)) &&
        optionalString(value.url)
      );
    case "document":
      return isRecord(value.source);
    case "web_search_result":
      return isRecord(value.content);
    case "resource_link":
      return (
        typeof value.uri === "string" &&
        optionalString(value.name) &&
        optionalString(value.description) &&
        optionalString(value.mime_type)
      );
    case "resource":
      return isRecord(value.resource);
    case "audio":
      return (
        optionalString(value.data) &&
        optionalString(value.url) &&
        optionalString(value.mime_type)
      );
    case "diff":
      return (
        optionalString(value.path) &&
        optionalString(value.old_text) &&
        optionalString(value.new_text)
      );
    case "terminal":
      return optionalString(value.terminal_id);
    case "unknown":
      return (
        typeof value.block_type === "string" &&
        isRecord(value.raw) &&
        optionalNumber(value.source_line)
      );
    default:
      return false;
  }
}

function normalizeMessages(value: unknown): ChatMessage[] | null {
  if (!Array.isArray(value)) return null;
  const messages: ChatMessage[] = [];
  for (const item of value) {
    if (!isRecord(item)) return null;
    if (typeof item.id !== "string") return null;
    if (
      item.role !== "user" &&
      item.role !== "assistant" &&
      item.role !== "system"
    ) {
      return null;
    }
    if (typeof item.content !== "string") return null;
    const timestamp = validDate(item.timestamp);
    if (!timestamp) return null;

    const message: ChatMessage = {
      id: item.id,
      role: item.role,
      content: item.content,
      timestamp,
    };
    if (Array.isArray(item.toolCalls) && item.toolCalls.every(isToolCall)) {
      message.toolCalls = item.toolCalls;
    }
    if (typeof item.thinkingContent === "string") {
      message.thinkingContent = item.thinkingContent;
    }
    if (
      Array.isArray(item.contentBlocks) &&
      item.contentBlocks.every(isContentBlock)
    ) {
      message.contentBlocks = item.contentBlocks;
    }
    messages.push(message);
  }
  return messages;
}

function normalizeSessionMeta(value: unknown): SessionObservationMeta | null {
  if (!isRecord(value)) return null;
  if (typeof value.source !== "string") return null;
  if (typeof value.status !== "string") return null;
  const sessionType =
    value.sessionType === "terminal" || value.sessionType === "web_chat"
      ? value.sessionType
      : null;
  const rawChatMode = nullableString(value.chatMode);
  return {
    ref: nullableString(value.ref),
    source: value.source,
    title: nullableString(value.title),
    status: value.status,
    canProxyAttach:
      typeof value.canProxyAttach === "boolean"
        ? value.canProxyAttach
        : undefined,
    model: nullableString(value.model),
    reasoningEffort: nullableString(value.reasoningEffort) ?? undefined,
    externalId: typeof value.externalId === "string" ? value.externalId : "",
    chatMode: rawChatMode ? normalizeChatMode(rawChatMode) : null,
    gitBranch: nullableString(value.gitBranch),
    contextWindow:
      typeof value.contextWindow === "number" ? value.contextWindow : null,
    agentRunId: nullableString(value.agentRunId),
    workflowName: nullableString(value.workflowName),
    agentName: nullableString(value.agentName),
    sessionType,
  };
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
  const messages = normalizeMessages(snapshot.messages);
  if (!messages) return null;

  const currentMode = normalizeRestorableChatMode(snapshot.currentMode);
  const sessionInteractionMode = isRestorableInteractionMode(
    snapshot.sessionInteractionMode,
  )
    ? snapshot.sessionInteractionMode
    : "none";

  return {
    sourceSessionId: snapshot.sourceSessionId,
    conversationId: snapshot.conversationId,
    dbSessionId: nullableString(snapshot.dbSessionId),
    mainSessionMeta: normalizeSessionMeta(snapshot.mainSessionMeta),
    sessionTitle: nullableString(snapshot.sessionTitle),
    sessionRef: nullableString(snapshot.sessionRef),
    selectedProvider: nullableString(snapshot.selectedProvider),
    messages,
    contextUsage: normalizeContextUsage(snapshot.contextUsage),
    currentMode,
    currentBranch: nullableString(snapshot.currentBranch),
    worktreePath: nullableString(snapshot.worktreePath),
    viewingSessionId: nullableString(snapshot.viewingSessionId),
    viewingSessionMeta: normalizeSessionMeta(snapshot.viewingSessionMeta),
    observedSessionId: nullableString(snapshot.observedSessionId),
    observedSessionMeta: normalizeSessionMeta(snapshot.observedSessionMeta),
    attachedSessionId: nullableString(snapshot.attachedSessionId),
    attachedSessionMeta: normalizeSessionMeta(snapshot.attachedSessionMeta),
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
    attachedSessionMetaRef: MutableRefObject<
      ContinuationRollbackSnapshot["attachedSessionMeta"]
    >;
    observedSessionMetaRef: MutableRefObject<
      ContinuationRollbackSnapshot["observedSessionMeta"]
    >;
    viewingSessionMetaRef: MutableRefObject<
      ContinuationRollbackSnapshot["viewingSessionMeta"]
    >;
    wsRef: MutableRefObject<WebSocket | null>;
  };
  conversationSetters: {
    setAttachedSessionMeta: Setter<
      ContinuationRollbackSnapshot["attachedSessionMeta"]
    >;
    setContextUsage: Setter<ContinuationRollbackSnapshot["contextUsage"]>;
    setCurrentBranch: Setter<string | null>;
    setCurrentMode: (mode: ChatMode) => void;
    setIsLoadingMessages: Setter<boolean>;
    setMainSessionMeta: Setter<ContinuationRollbackSnapshot["mainSessionMeta"]>;
    setMessages: Setter<ContinuationRollbackSnapshot["messages"]>;
    setProxyDeliveryNotice: Setter<string | null>;
    setViewingSessionMeta: Setter<
      ContinuationRollbackSnapshot["viewingSessionMeta"]
    >;
    setWorktreePath: Setter<string | null>;
  };
  interactionMode: {
    pendingSessionInteractionModeRef: MutableRefObject<"observe" | "proxy">;
    sessionInteractionModeRef: MutableRefObject<
      ContinuationRollbackSnapshot["sessionInteractionMode"]
    >;
    setSessionInteractionMode: Setter<
      ContinuationRollbackSnapshot["sessionInteractionMode"]
    >;
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
          console.warn(
            "Failed to serialize restored session attach request",
            error,
          );
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
