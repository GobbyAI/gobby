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
    const data = JSON.parse(event.data) as WebSocketMessage;
    console.log("WebSocket message:", data.type, data);

    switch (data.type) {
      case "chat_stream":
        ctx.handleChatStreamRef.current(data as unknown as ChatStreamChunk);
        return;
      case "chat_error":
        ctx.handleChatErrorRef.current(data as unknown as ChatError);
        return;
      case "tool_status":
        ctx.handleToolStatusRef.current(data as unknown as ToolStatusMessage);
        return;
      case "chat_thinking":
        ctx.handleChatThinkingRef.current(data as unknown as ChatThinkingMessage);
        return;
      case "model_switched":
        ctx.handleModelSwitchedRef.current(data as unknown as ModelSwitchedMessage);
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
