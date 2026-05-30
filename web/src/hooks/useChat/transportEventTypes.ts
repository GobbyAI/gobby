import type { ToolResult } from "../../types/chat";

export interface WebSocketMessage {
  type: string;
  [key: string]: unknown;
}

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
