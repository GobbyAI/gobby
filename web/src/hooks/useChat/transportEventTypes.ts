import type { ToolResult } from "../../types/chat";

export interface ChatStreamChunk {
  type: "chat_stream";
  message_id: string;
  request_id?: string;
  content: string;
  done: boolean;
  tool_calls_count?: number;
  session_ref?: string;
  sdk_session_id?: string;
  usage?: {
    input_tokens: number;
    output_tokens: number;
    cache_read_input_tokens?: number;
    cache_creation_input_tokens?: number;
    total_input_tokens?: number;
  };
  context_window?: number;
}

export interface ChatError {
  type: "chat_error";
  message_id?: string;
  request_id?: string;
  error: string;
  error_detail?: string;
}

export interface ToolStatusMessage {
  type: "tool_status";
  message_id: string;
  request_id?: string;
  tool_call_id: string;
  status: "calling" | "completed" | "error" | "pending_approval";
  tool_name?: string;
  server_name?: string;
  arguments?: Record<string, unknown>;
  result?: ToolResult;
  error?: string;
}

export interface ChatThinkingMessage {
  type: "chat_thinking";
  message_id: string;
  request_id?: string;
  conversation_id: string;
  content?: string;
}

export interface ModelSwitchedMessage {
  type: "model_switched";
  conversation_id: string;
  old_model: string;
  new_model: string;
}

export interface VoiceTranscriptionMessage {
  type: "voice_transcription";
  conversation_id?: string;
  text: string;
  request_id: string;
}

export interface SessionUsageUpdatedMessage {
  type: "session_usage_updated";
  session_id: string;
  project_id?: string | null;
  model?: string | null;
  context_window?: number | null;
  context_used_tokens?: number | null;
  context_usage_ratio?: number | null;
  context_usage_source?: string | null;
  context_usage_confidence?: string | null;
  last_prompt_input_tokens?: number | null;
  last_prompt_uncached_input_tokens?: number | null;
  last_prompt_cache_read_tokens?: number | null;
  last_prompt_cache_creation_tokens?: number | null;
  last_completion_output_tokens?: number | null;
  usage_input_tokens?: number;
  usage_output_tokens?: number;
  usage_cache_creation_tokens?: number;
  usage_cache_read_tokens?: number;
  updated_at?: string;
}

export interface TokenEventMessage {
  type: "token_event";
  session_id: string;
  project_id?: string | null;
  message_id?: string | null;
  source?: string | null;
  origin?: string | null;
  event_at: string;
  model?: string | null;
  model_family?: string | null;
  input_tokens?: number;
  output_tokens?: number;
  cache_creation_tokens?: number;
  cache_read_tokens?: number;
  context_window?: number | null;
  session_totals?: {
    input_tokens?: number;
    output_tokens?: number;
    cache_creation_tokens?: number;
    cache_read_tokens?: number;
  };
}

export interface PlanPendingApprovalMessage {
  type: "plan_pending_approval";
  conversation_id?: string;
  plan_content?: string;
}

export interface ModeChangedMessage {
  type: "mode_changed";
  conversation_id?: string;
  mode?: string;
  reason?: string;
}

export interface SessionInfoMessage {
  type: "session_info";
  session_ref?: string;
  db_session_id?: string;
  conversation_id?: string;
  current_branch?: string;
  worktree_path?: string;
  agent_name?: string;
}

export interface WorktreeSwitchedMessage {
  type: "worktree_switched";
  new_branch?: string | null;
  worktree_path?: string | null;
}

export interface AgentChangedMessage {
  type: "agent_changed";
  agent_name?: string;
}

export interface SessionContinuedMessage {
  type: "session_continued";
  conversation_id?: string;
  db_session_id?: string;
  resume_notice?: string;
}

export interface TransportErrorMessage {
  type: "error";
  message?: string;
}

export interface ConnectionEstablishedMessage {
  type: "connection_established";
  conversation_ids?: string[];
}

export interface CanvasEventMessage {
  type: "canvas_event";
  event?: string;
  canvas_id?: string;
  conversation_id?: string;
  mode?: string;
  surface?: unknown;
  data_model?: unknown;
  root_component_id?: string;
  completed?: boolean;
  url?: string;
  html_url?: string;
  title?: string;
  width?: number;
  height?: number;
  surfaces?: unknown[];
}

export interface ArtifactEventMessage {
  type: "artifact_event";
  event?: string;
  artifact_type?: string;
  content?: string;
  language?: string;
  title?: string;
}

export interface AttachToSessionResultMessage {
  type: "attach_to_session_result";
  session_id?: string;
  messages?: unknown[];
}

export interface DetachFromSessionResultMessage {
  type: "detach_from_session_result";
  session_id?: string;
}

export interface SessionMessage {
  type: "session_message";
  session_id: string;
  message?: unknown;
}

export interface SendToCliSessionResultMessage {
  type: "send_to_cli_session_result";
  client_message_id?: string;
  message_id?: string;
  delivered?: boolean;
  delivery_method?: string;
}

export interface SubscribeSuccessMessage {
  type: "subscribe_success";
  event?: string;
}

export interface ChatDeletedMessage {
  type: "chat_deleted";
  conversation_id?: string;
}

export interface ChatClearedMessage {
  type: "chat_cleared";
  conversation_id?: string;
}

export interface VoiceAudioChunkMessage {
  type: "voice_audio_chunk";
  conversation_id?: string;
  request_id?: string;
  audio?: string;
}

export interface VoiceStatusMessage {
  type: "voice_status" | "tts_status";
  conversation_id?: string;
  request_id?: string;
  status?: string;
}

export interface TtsAudioMessage {
  type: "tts_audio";
  conversation_id?: string;
  request_id?: string;
  audio?: string;
}

export type WebSocketMessage =
  | ChatStreamChunk
  | ChatError
  | ToolStatusMessage
  | ChatThinkingMessage
  | ModelSwitchedMessage
  | VoiceTranscriptionMessage
  | SessionUsageUpdatedMessage
  | TokenEventMessage
  | PlanPendingApprovalMessage
  | ModeChangedMessage
  | SessionInfoMessage
  | WorktreeSwitchedMessage
  | AgentChangedMessage
  | SessionContinuedMessage
  | TransportErrorMessage
  | ConnectionEstablishedMessage
  | CanvasEventMessage
  | ArtifactEventMessage
  | AttachToSessionResultMessage
  | DetachFromSessionResultMessage
  | SessionMessage
  | SendToCliSessionResultMessage
  | SubscribeSuccessMessage
  | ChatDeletedMessage
  | ChatClearedMessage
  | VoiceAudioChunkMessage
  | VoiceStatusMessage
  | TtsAudioMessage;
