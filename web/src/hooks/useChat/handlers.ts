/* eslint-disable react-hooks/exhaustive-deps -- Extracted useChat handlers intentionally close over parent refs and stable setters to preserve the original hook behavior. */
import { useCallback, useEffect } from "react";
import type { Dispatch, MutableRefObject, SetStateAction } from "react";
import type { ChatMessage, SessionObservationMeta, ToolCall } from "../../types/chat";
import { classifyTool } from "../../types/chat";
import {
  type ChatError,
  type ChatStreamChunk,
  type ChatThinkingMessage,
  type ContextUsage,
  type ModelSwitchedMessage,
  type ToolStatusMessage,
  extractServerName,
  uuid,
} from "./core";

type Setter<T> = Dispatch<SetStateAction<T>>;

interface UseChatMessageHandlersParams extends Record<string, any> {
  conversationIdRef: MutableRefObject<string>;
  dbSessionIdRef: MutableRefObject<string | null>;
  isActiveRequest: (requestId?: string) => boolean;
  observedSessionIdRef: MutableRefObject<string | null>;
  setContextUsage: Setter<ContextUsage>;
  setIsStreaming: Setter<boolean>;
  setIsThinking: Setter<boolean>;
  setMainSessionMeta: Setter<SessionObservationMeta | null>;
  setMessages: Setter<ChatMessage[]>;
  setSessionRef: Setter<string | null>;
  viewingSessionIdRef: MutableRefObject<string | null>;
  viewingSessionMetaRef: MutableRefObject<SessionObservationMeta | null>;
}

export function useChatMessageHandlers(params: UseChatMessageHandlersParams) {
  const {
    attachedSessionId,
    attachedSessionIdRef,
    attachedSessionMeta,
    attachedSessionMetaRef,
    dbSessionId,
    dbSessionIdRef,
    handleChatErrorRef,
    handleChatStreamRef,
    handleChatThinkingRef,
    handleModelSwitchedRef,
    handleToolStatusRef,
    isActiveRequest,
    conversationIdRef,
    pendingPlanFeedbackRef,
    observedSessionId,
    observedSessionIdRef,
    saveDbSessionId,
    saveViewingSessionId,
    saveViewingSessionMode,
    sendMessageRef,
    sessionInteractionMode,
    sessionInteractionModeRef,
    setContextUsage,
    setIsStreaming,
    setIsThinking,
    setMainSessionMeta,
    setMessages,
    setSessionRef,
    viewingSessionId,
    viewingSessionIdRef,
    viewingSessionMeta,
    viewingSessionMetaRef,
  } = params;

// Handle streaming chat chunks
const handleChatStream = useCallback((chunk: ChatStreamChunk) => {
  if (!isActiveRequest(chunk.request_id)) {
    console.debug(
      "Dropping stale chat_stream chunk, request_id:",
      chunk.request_id,
    );
    return;
  }

  if (chunk.content) {
    setIsThinking(false);
  }

  setMessages((prev) => {
    const existingIndex = prev.findIndex((m) => m.id === chunk.message_id);

    if (existingIndex >= 0) {
      const updated = [...prev];
      const existing = updated[existingIndex];
      // Build interleaved content blocks
      const blocks = [...(existing.contentBlocks || [])];
      if (chunk.content) {
        const lastBlock = blocks[blocks.length - 1];
        if (lastBlock?.type === "text") {
          blocks[blocks.length - 1] = {
            ...lastBlock,
            content: lastBlock.content + chunk.content,
          };
        } else {
          blocks.push({ type: "text", content: chunk.content });
        }
      }
      updated[existingIndex] = {
        ...existing,
        content: existing.content + chunk.content,
        contentBlocks: blocks,
      };
      return updated;
    } else {
      return [
        ...prev,
        {
          id: chunk.message_id,
          role: "assistant" as const,
          content: chunk.content,
          timestamp: new Date(),
          contentBlocks: chunk.content
            ? [{ type: "text" as const, content: chunk.content }]
            : [],
        },
      ];
    }
  });

  if (chunk.done) {
    setIsStreaming(false);
    setIsThinking(false);
    // Pick up session_ref from done message (fallback if session_info was missed)
    if (chunk.session_ref) {
      setSessionRef(chunk.session_ref);
    }
    // Update context usage from usage data in done message.
    // Each turn sends the full conversation to Claude, so the latest turn's
    // total_input_tokens IS the current context size — replace, don't accumulate.
    // Output tokens are genuinely incremental, so those accumulate.
    if (chunk.usage) {
      const u = chunk.usage;
      // Prefer total_input_tokens from backend; fall back to sum of parts
      const turnTotal =
        u.total_input_tokens ??
        (u.input_tokens ?? 0) +
          (u.cache_read_input_tokens ?? 0) +
          (u.cache_creation_input_tokens ?? 0);
      setContextUsage((prev) => ({
        // Input tokens: REPLACE with latest turn's values (each turn sends
        // the full conversation, so the latest total IS the current context size)
        totalInputTokens: turnTotal,
        uncachedInputTokens: u.input_tokens ?? 0,
        cacheReadTokens: u.cache_read_input_tokens ?? 0,
        cacheCreationTokens: u.cache_creation_input_tokens ?? 0,
        // Output tokens: ACCUMULATE (genuinely incremental per turn)
        outputTokens: prev.outputTokens + (u.output_tokens ?? 0),
        contextWindow: chunk.context_window ?? prev.contextWindow,
      }));
    } else if (chunk.context_window) {
      setContextUsage((prev) => ({
        ...prev,
        contextWindow: chunk.context_window ?? prev.contextWindow,
      }));
    }

    if (pendingPlanFeedbackRef.current) {
      const feedback = pendingPlanFeedbackRef.current;
      pendingPlanFeedbackRef.current = null;
      setTimeout(() => {
        sendMessageRef.current?.(feedback);
      }, 200);
    }
  }
}, []);

// Handle chat errors
const handleChatError = useCallback((error: ChatError) => {
  if (!isActiveRequest(error.request_id)) {
    console.debug("Dropping stale chat_error, request_id:", error.request_id);
    return;
  }

  setIsStreaming(false);
  setIsThinking(false);
  if (import.meta.env.DEV && error.error_detail) {
    console.error("Chat startup error detail:", error.error_detail);
  }
  setMessages((prev) => [
    ...prev,
    {
      id: error.message_id || `error-${uuid()}`,
      role: "system" as const,
      content: `Error: ${error.error}`,
      timestamp: new Date(),
    },
  ]);
}, []);

// Handle tool status updates
const handleToolStatus = useCallback((status: ToolStatusMessage) => {
  if (status.status !== "pending_approval" && !isActiveRequest(status.request_id)) {
    console.debug(
      "Dropping stale tool_status, request_id:",
      status.request_id,
    );
    return;
  }

  if (status.status === "calling") {
    setIsThinking(false);
  }

  setMessages((prev) => {
    const result = status.result ?? undefined;
    const idx = prev.findIndex((m) => m.id === status.message_id);
    if (idx < 0) {
      // Tool status arrived before any text/thinking — create the message
      const toolName = status.tool_name || "unknown";
      const newCall: ToolCall = {
        id: status.tool_call_id,
        tool_name: toolName,
        server_name: status.server_name || extractServerName(toolName),
        tool_type: classifyTool(toolName),
        status: status.status,
        arguments: status.arguments,
        result,
        error: status.error,
      };
      return [
        ...prev,
        {
          id: status.message_id,
          role: "assistant" as const,
          content: "",
          timestamp: new Date(),
          toolCalls: [newCall],
          contentBlocks: [
            { type: "tool_chain" as const, tool_calls: [newCall] },
          ],
        },
      ];
    }

    const updated = [...prev];
    const msg = updated[idx];
    const toolCalls = [...(msg.toolCalls || [])];
    const existingIdx = toolCalls.findIndex(
      (t) => t.id === status.tool_call_id,
    );

    let callRef: ToolCall;
    if (existingIdx >= 0) {
      const existing = toolCalls[existingIdx];
      callRef = {
        ...existing,
        status: status.status,
        result,
        error: status.error,
      };
      toolCalls[existingIdx] = callRef;
    } else {
      const toolName = status.tool_name || "unknown";
      callRef = {
        id: status.tool_call_id,
        tool_name: toolName,
        server_name: status.server_name || extractServerName(toolName),
        tool_type: classifyTool(toolName),
        status: status.status,
        arguments: status.arguments,
        result,
        error: status.error,
      };
      toolCalls.push(callRef);
    }

    // Update interleaved content blocks
    const blocks = [...(msg.contentBlocks || [])];
    if (existingIdx >= 0) {
      // Update existing tool call in its block
      for (let bi = 0; bi < blocks.length; bi++) {
        const block = blocks[bi];
        if (block.type === "tool_chain") {
          const tcIdx = block.tool_calls.findIndex(
            (c) => c.id === status.tool_call_id,
          );
          if (tcIdx >= 0) {
            const updatedCalls = [...block.tool_calls];
            updatedCalls[tcIdx] = callRef;
            blocks[bi] = { ...block, tool_calls: updatedCalls };
            break;
          }
        }
      }
    } else {
      // New tool call — always start a fresh tool_chain block to match the
      // historical-replay parser, which emits one tool_chain block per tool
      // call (transcript_renderer.py never merges tool_chain blocks). The
      // frontend then groups visually via groupToolCalls when 3+ same-tool
      // calls run consecutively.
      blocks.push({ type: "tool_chain" as const, tool_calls: [callRef] });
    }

    updated[idx] = { ...msg, toolCalls, contentBlocks: blocks };
    return updated;
  });
}, []);

// Handle thinking events
const handleChatThinking = useCallback((msg: ChatThinkingMessage) => {
  if (!isActiveRequest(msg.request_id)) {
    console.debug(
      "Dropping stale chat_thinking, request_id:",
      msg.request_id,
    );
    return;
  }

  setIsThinking(true);
  setMessages((prev) => {
    const existingIndex = prev.findIndex((m) => m.id === msg.message_id);
    if (existingIndex >= 0) {
      const updated = [...prev];
      const existing = updated[existingIndex];
      const blocks = [...(existing.contentBlocks || [])];
      // Mirror the historical-replay rule: thinking is its own content block,
      // which forces any subsequent tool calls to start a fresh tool_chain
      // block instead of clumping into the previous one. Coalesce contiguous
      // thinking deltas into the last thinking block.
      if (msg.content) {
        const lastBlock = blocks[blocks.length - 1];
        if (lastBlock?.type === "thinking") {
          blocks[blocks.length - 1] = {
            ...lastBlock,
            content: lastBlock.content + msg.content,
          };
        } else {
          blocks.push({ type: "thinking", content: msg.content });
        }
      }
      updated[existingIndex] = {
        ...existing,
        thinkingContent:
          (existing.thinkingContent || "") + (msg.content || ""),
        contentBlocks: blocks,
      };
      return updated;
    } else {
      const initialBlocks = msg.content
        ? [{ type: "thinking" as const, content: msg.content }]
        : [];
      return [
        ...prev,
        {
          id: msg.message_id,
          role: "assistant" as const,
          content: "",
          timestamp: new Date(),
          thinkingContent: msg.content || "",
          contentBlocks: initialBlocks,
        },
      ];
    }
  });
}, []);

// Handle model switch notifications
const handleModelSwitched = useCallback((msg: ModelSwitchedMessage) => {
  const matchesActiveConversation =
    msg.conversation_id === conversationIdRef.current ||
    msg.conversation_id === dbSessionIdRef.current;
  if (matchesActiveConversation) {
    setMainSessionMeta((prev) =>
      prev
        ? {
            ...prev,
            model: msg.new_model,
          }
        : prev,
    );
  }
  setMessages((prev) => [
    ...prev,
    {
      id: `model-switch-${uuid()}`,
      role: "system" as const,
      content: `Model switched from ${msg.old_model} to ${msg.new_model}`,
      timestamp: new Date(),
    },
  ]);
}, []);

// Keep refs updated to avoid stale closures
useEffect(() => {
  handleChatStreamRef.current = handleChatStream;
  handleChatErrorRef.current = handleChatError;
  handleToolStatusRef.current = handleToolStatus;
  handleChatThinkingRef.current = handleChatThinking;
  handleModelSwitchedRef.current = handleModelSwitched;
}, [
  handleChatStream,
  handleChatError,
  handleToolStatus,
  handleChatThinking,
  handleModelSwitched,
]);

// Persist dbSessionId to localStorage so next page load can fetch from DB immediately
useEffect(() => {
  if (viewingSessionMeta?.sessionType === "terminal") {
    return;
  }
  saveDbSessionId(dbSessionId);
}, [dbSessionId, viewingSessionMeta?.sessionType]);
useEffect(() => {
  saveViewingSessionId(viewingSessionId);
}, [viewingSessionId]);
useEffect(() => {
  saveViewingSessionMode(viewingSessionId ? sessionInteractionMode : "none");
}, [sessionInteractionMode, viewingSessionId]);

// Keep refs in sync
useEffect(() => {
  attachedSessionIdRef.current = attachedSessionId;
}, [attachedSessionId]);
useEffect(() => {
  attachedSessionMetaRef.current = attachedSessionMeta;
}, [attachedSessionMeta]);
useEffect(() => {
  viewingSessionIdRef.current = viewingSessionId;
}, [viewingSessionId]);
useEffect(() => {
  viewingSessionMetaRef.current = viewingSessionMeta;
}, [viewingSessionMeta]);
useEffect(() => {
  observedSessionIdRef.current = observedSessionId;
}, [observedSessionId]);
useEffect(() => {
  sessionInteractionModeRef.current = sessionInteractionMode;
}, [sessionInteractionMode]);

}
