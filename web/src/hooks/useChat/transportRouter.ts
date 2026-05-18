import {
  type ChatError,
  type ChatStreamChunk,
  type ChatThinkingMessage,
  type ModelSwitchedMessage,
  type ToolStatusMessage,
  type WebSocketMessage,
} from "./core";
import {
  handleArtifactTransportEvent,
  handleCanvasTransportEvent,
} from "./transportCanvasEvents";
import {
  handleAgentChanged,
  handleChatCleared,
  handleChatDeleted,
  handleConnectionEstablished,
  handleModeChanged,
  handlePlanPendingApproval,
  handleSessionContinued,
  handleSessionInfo,
  handleSubscribeSuccess,
  handleTransportError,
  handleWorktreeSwitched,
} from "./transportConversationEvents";
import {
  handleAttachToSessionResult,
  handleCliSessionSendResult,
  handleDetachFromSessionResult,
  handleObservedSessionMessage,
} from "./transportProxyEvents";
import { handleSessionUsageUpdated, handleTokenEvent } from "./transportUsageEvents";
import {
  handleVoiceTransportEvent,
  isVoiceTransportEvent,
} from "./transportVoiceEvents";
import type { UseChatTransportParams } from "./transportTypes";

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isChatStreamChunk(data: unknown): data is ChatStreamChunk {
  return (
    isRecord(data) &&
    data.type === "chat_stream" &&
    typeof data.message_id === "string" &&
    typeof data.content === "string" &&
    typeof data.done === "boolean"
  );
}

function isChatError(data: unknown): data is ChatError {
  return (
    isRecord(data) &&
    data.type === "chat_error" &&
    typeof data.error === "string"
  );
}

function isToolStatusMessage(data: unknown): data is ToolStatusMessage {
  return (
    isRecord(data) &&
    data.type === "tool_status" &&
    typeof data.message_id === "string" &&
    typeof data.tool_call_id === "string" &&
    typeof data.status === "string"
  );
}

function isChatThinkingMessage(data: unknown): data is ChatThinkingMessage {
  return (
    isRecord(data) &&
    data.type === "chat_thinking" &&
    typeof data.message_id === "string" &&
    typeof data.conversation_id === "string"
  );
}

function isModelSwitchedMessage(data: unknown): data is ModelSwitchedMessage {
  return (
    isRecord(data) &&
    data.type === "model_switched" &&
    typeof data.conversation_id === "string" &&
    typeof data.old_model === "string" &&
    typeof data.new_model === "string"
  );
}

export function routeTransportMessage(
  event: MessageEvent<string | ArrayBuffer>,
  ctx: UseChatTransportParams,
) {
  // Binary frames are TTS audio data — route to voice handler
  if (event.data instanceof ArrayBuffer) {
    try {
      ctx.handleBinaryMessageRef.current(event.data);
    } catch (err) {
      console.error("TTS binary message error:", err);
    }
    return;
  }

  try {
    const parsed: unknown = JSON.parse(event.data);
    if (!isRecord(parsed) || typeof parsed.type !== "string") {
      return;
    }
    const data = parsed as WebSocketMessage;
    if (import.meta.env.DEV) {
      console.debug("WebSocket message:", data.type, data);
    }

    switch (data.type) {
      case "chat_stream":
        if (isChatStreamChunk(data)) ctx.handleChatStreamRef.current(data);
        return;
      case "chat_error":
        if (isChatError(data)) ctx.handleChatErrorRef.current(data);
        return;
      case "tool_status":
        if (isToolStatusMessage(data)) ctx.handleToolStatusRef.current(data);
        return;
      case "chat_thinking":
        if (isChatThinkingMessage(data)) {
          ctx.handleChatThinkingRef.current(data);
        }
        return;
      case "model_switched":
        if (isModelSwitchedMessage(data)) {
          ctx.handleModelSwitchedRef.current(data);
        }
        return;
      case "plan_pending_approval":
        handlePlanPendingApproval(data, ctx);
        return;
      case "mode_changed":
        handleModeChanged(data, ctx);
        return;
      case "session_info":
        handleSessionInfo(data, ctx);
        return;
      case "worktree_switched":
        handleWorktreeSwitched(data, ctx);
        return;
      case "agent_changed":
        handleAgentChanged(data, ctx);
        return;
      case "session_continued":
        handleSessionContinued(data, ctx);
        return;
      case "error":
        handleTransportError(data, ctx);
        return;
      case "connection_established":
        handleConnectionEstablished(data, ctx);
        return;
      case "canvas_event":
        handleCanvasTransportEvent(data, ctx);
        return;
      case "artifact_event":
        handleArtifactTransportEvent(data, ctx);
        return;
      case "attach_to_session_result":
        handleAttachToSessionResult(data, ctx);
        return;
      case "detach_from_session_result":
        handleDetachFromSessionResult(data, ctx);
        return;
      case "session_message":
        if (data.session_id) {
          handleObservedSessionMessage(data, ctx);
        }
        return;
      case "send_to_cli_session_result":
        handleCliSessionSendResult(data, ctx);
        return;
      case "session_usage_updated":
        handleSessionUsageUpdated(data, ctx);
        return;
      case "token_event":
        handleTokenEvent(data, ctx);
        return;
      case "subscribe_success":
        handleSubscribeSuccess(data);
        return;
      case "chat_deleted":
        handleChatDeleted(data, ctx);
        return;
      case "chat_cleared":
        handleChatCleared(data, ctx);
        return;
      default:
        if (isVoiceTransportEvent(data.type)) {
          handleVoiceTransportEvent(data, ctx);
        }
    }
  } catch (e) {
    console.error("Failed to parse WebSocket message:", e);
  }
}
