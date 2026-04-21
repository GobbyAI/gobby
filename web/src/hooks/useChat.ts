import { useState, useEffect, useCallback, useRef } from "react";
import type {
  ChatMessage,
  ToolCall,
  ChatMode,
  ContentBlock,
  FallbackContextMode,
  SessionInteractionMode,
  SessionObservationMeta,
  TokenUsage,
  ToolResult,
} from "../types/chat";
import { classifyTool, normalizeChatMode } from "../types/chat";
import type { QueuedFile } from "../types/chat";
import type { A2UISurfaceState, UserAction } from "../components/canvas/types";
import type { CanvasPanelState } from "../components/canvas/hooks/useCanvasPanel";
import {
  mapRenderedMessageToChatMessage,
  normalizeChatRole,
} from "../lib/chatMessageMapping";
import { AUTO_REASONING_EFFORT } from "../lib/providerModels";
import {
  canProxyAttachObservationMeta,
  canProxyAttachSessionRecord,
} from "../lib/sessionProxyAttach";

const CONVERSATION_ID_KEY = "gobby-conversation-id";
const DB_SESSION_ID_KEY = "gobby-db-session-id";
const VIEWING_SESSION_ID_KEY = "gobby-viewing-session-id";
const VIEWING_SESSION_MODE_KEY = "gobby-viewing-session-mode";
const CHAT_PROVIDERS = new Set(["claude", "gemini", "qwen", "codex"]);

interface WebSocketMessage {
  type: string;
  [key: string]: unknown;
}

interface ChatStreamChunk {
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

interface ChatError {
  type: "chat_error";
  message_id?: string;
  request_id?: string;
  error: string;
  error_detail?: string;
}

interface PendingProxyMessage {
  clientMessageId: string;
  currentMessageId: string;
  sessionId: string;
}

interface ToolStatusMessage {
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

interface ChatThinkingMessage {
  type: "chat_thinking";
  message_id: string;
  request_id?: string;
  conversation_id: string;
  content?: string;
}

interface ModelSwitchedMessage {
  type: "model_switched";
  conversation_id: string;
  old_model: string;
  new_model: string;
}

interface VoiceTranscriptionMessage {
  type: "voice_transcription";
  text: string;
  request_id: string;
}

interface SessionUsageUpdatedMessage {
  type: "session_usage_updated";
  session_id: string;
  project_id?: string | null;
  model?: string | null;
  context_window?: number | null;
  usage_input_tokens?: number;
  usage_output_tokens?: number;
  usage_cache_creation_tokens?: number;
  usage_cache_read_tokens?: number;
  updated_at?: string;
}

interface TokenEventMessage {
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

/** crypto.randomUUID() requires a secure context (HTTPS/localhost). Fall back for HTTP access (e.g. Tailscale IP). */
function uuid(): string {
  if (
    typeof crypto !== "undefined" &&
    typeof crypto.randomUUID === "function"
  ) {
    try {
      return crypto.randomUUID();
    } catch {
      /* non-secure context */
    }
  }
  // Fallback using crypto.getRandomValues (works in all contexts)
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  bytes[6] = (bytes[6] & 0x0f) | 0x40; // version 4
  bytes[8] = (bytes[8] & 0x3f) | 0x80; // variant 1
  const hex = Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join(
    "",
  );
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

function loadConversationId(): string {
  return (
    localStorage.getItem(CONVERSATION_ID_KEY) ||
    localStorage.getItem(DB_SESSION_ID_KEY) ||
    ""
  );
}

function saveConversationId(id: string): void {
  if (!id) {
    localStorage.removeItem(CONVERSATION_ID_KEY);
    return;
  }
  localStorage.setItem(CONVERSATION_ID_KEY, id);
}

function loadDbSessionId(): string | null {
  return localStorage.getItem(DB_SESSION_ID_KEY);
}

function saveDbSessionId(id: string | null): void {
  if (id) {
    localStorage.setItem(DB_SESSION_ID_KEY, id);
  } else {
    localStorage.removeItem(DB_SESSION_ID_KEY);
  }
}

function loadViewingSessionId(): string | null {
  return localStorage.getItem(VIEWING_SESSION_ID_KEY);
}

function saveViewingSessionId(id: string | null): void {
  if (id) {
    localStorage.setItem(VIEWING_SESSION_ID_KEY, id);
  } else {
    localStorage.removeItem(VIEWING_SESSION_ID_KEY);
  }
}

function loadViewingSessionMode(): "none" | "observe" {
  const persisted = localStorage.getItem(VIEWING_SESSION_MODE_KEY);
  return persisted === "observe" ? "observe" : "none";
}

function saveViewingSessionMode(mode: "none" | "observe"): void {
  if (mode === "observe") {
    localStorage.setItem(VIEWING_SESSION_MODE_KEY, mode);
  } else {
    localStorage.removeItem(VIEWING_SESSION_MODE_KEY);
  }
}

function isChatProvider(value: unknown): value is string {
  return typeof value === "string" && CHAT_PROVIDERS.has(value);
}

function isValidSessionType(value: unknown): value is "terminal" | "web_chat" {
  return value === "terminal" || value === "web_chat";
}

function enqueuePendingProxyMessage(
  pendingQueues: Map<string, string[]>,
  entry: PendingProxyMessage,
): void {
  const queue = pendingQueues.get(entry.sessionId) ?? [];
  queue.push(entry.clientMessageId);
  pendingQueues.set(entry.sessionId, queue);
}

function consumePendingProxyMessage(
  pending: Map<string, PendingProxyMessage>,
  pendingQueues: Map<string, string[]>,
  sessionId: string,
): PendingProxyMessage | null {
  const queue = pendingQueues.get(sessionId);
  if (!queue) {
    return null;
  }

  while (queue.length > 0) {
    const clientMessageId = queue.shift();
    if (!clientMessageId) {
      continue;
    }
    const entry = pending.get(clientMessageId) ?? null;
    if (entry) {
      if (queue.length === 0) {
        pendingQueues.delete(sessionId);
      } else {
        pendingQueues.set(sessionId, queue);
      }
      return entry;
    }
  }

  pendingQueues.delete(sessionId);
  return null;
}

function removePendingProxyMessageFromQueue(
  pendingQueues: Map<string, string[]>,
  sessionId: string,
  clientMessageId: string,
): void {
  const queue = pendingQueues.get(sessionId);
  if (!queue) {
    return;
  }

  const next = queue.filter((id) => id !== clientMessageId);
  if (next.length === 0) {
    pendingQueues.delete(sessionId);
    return;
  }
  pendingQueues.set(sessionId, next);
}

function clearPendingProxyMessages(
  pending: Map<string, PendingProxyMessage>,
  pendingQueues: Map<string, string[]>,
): void {
  pending.clear();
  pendingQueues.clear();
}

function normalizeSessionType(value: unknown): "terminal" | "web_chat" | null {
  return isValidSessionType(value) ? value : null;
}

function normalizeReasoningEffort(value: string | null | undefined): string | null {
  if (!value) {
    return null;
  }
  const normalized = value.trim().toLowerCase();
  if (!normalized || normalized === AUTO_REASONING_EFFORT) {
    return null;
  }
  return normalized;
}

interface CreatedWebChatSession extends Record<string, unknown> {
  id: string;
  source: string;
  model: string | null;
  chat_mode: string | null;
  seq_num: number | null;
  title: string | null;
  status?: string | null;
  external_id?: string | null;
  git_branch?: string | null;
  context_window?: number | null;
}

interface ContinuationRollbackSnapshot {
  sourceSessionId: string;
  conversationId: string;
  dbSessionId: string | null;
  mainSessionMeta: SessionObservationMeta | null;
  sessionTitle: string | null;
  sessionRef: string | null;
  selectedProvider: string | null;
  messages: ChatMessage[];
  contextUsage: {
    totalInputTokens: number;
    outputTokens: number;
    contextWindow: number | null;
    uncachedInputTokens: number;
    cacheReadTokens: number;
    cacheCreationTokens: number;
  };
  currentMode: ChatMode;
  currentBranch: string | null;
  worktreePath: string | null;
  viewingSessionId: string | null;
  viewingSessionMeta: SessionObservationMeta | null;
  observedSessionId: string | null;
  observedSessionMeta: SessionObservationMeta | null;
  attachedSessionId: string | null;
  attachedSessionMeta: SessionObservationMeta | null;
  sessionInteractionMode: SessionInteractionMode;
  proxyDeliveryNotice: string | null;
}

async function createWebChatSession(params?: {
  projectId?: string | null;
  provider?: string | null;
  model?: string | null;
  reasoningEffort?: string | null;
  chatMode?: ChatMode | null;
  title?: string | null;
}): Promise<CreatedWebChatSession> {
  const baseUrl = import.meta.env.VITE_API_BASE_URL || "";
  const reasoningEffort = normalizeReasoningEffort(params?.reasoningEffort ?? null);
  const response = await fetch(`${baseUrl}/api/sessions/web-chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      project_id: params?.projectId ?? null,
      provider: params?.provider ?? null,
      model: params?.model ?? null,
      reasoning_effort: reasoningEffort,
      chat_mode: params?.chatMode ?? null,
      title: params?.title ?? null,
    }),
  });

  if (!response.ok) {
    throw new Error(`Failed to create web chat session: ${response.status}`);
  }

  const data = await response.json();
  return data.session as CreatedWebChatSession;
}

function computeContextUsageFromSessionData(
  session: Record<string, unknown> | null,
): {
  totalInputTokens: number;
  outputTokens: number;
  contextWindow: number | null;
  uncachedInputTokens: number;
  cacheReadTokens: number;
  cacheCreationTokens: number;
} {
  const totalInputTokens =
    typeof session?.usage_input_tokens === "number"
      ? session.usage_input_tokens
      : 0;
  const outputTokens =
    typeof session?.usage_output_tokens === "number"
      ? session.usage_output_tokens
      : 0;
  const cacheReadTokens =
    typeof session?.usage_cache_read_tokens === "number"
      ? session.usage_cache_read_tokens
      : 0;
  const cacheCreationTokens =
    typeof session?.usage_cache_creation_tokens === "number"
      ? session.usage_cache_creation_tokens
      : 0;
  const contextWindow =
    typeof session?.context_window === "number" ? session.context_window : null;

  return {
    totalInputTokens,
    outputTokens,
    contextWindow,
    uncachedInputTokens: Math.max(
      0,
      totalInputTokens - cacheReadTokens - cacheCreationTokens,
    ),
    cacheReadTokens,
    cacheCreationTokens,
  };
}

function hasSessionUsage(session: Record<string, unknown> | null): boolean {
  if (!session) return false;
  return (
    (typeof session.usage_input_tokens === "number" &&
      session.usage_input_tokens > 0) ||
    (typeof session.usage_output_tokens === "number" &&
      session.usage_output_tokens > 0) ||
    typeof session.context_window === "number"
  );
}

function isWebChatSessionRecord(
  session: Record<string, unknown> | null | undefined,
): boolean {
  return normalizeSessionType(session?.session_type) === "web_chat";
}

const RESTORABLE_SESSION_STATUSES = new Set([
  "active",
  "paused",
  "handoff_ready",
]);

function isRestorableSessionRecord(
  session: Record<string, unknown> | null | undefined,
): boolean {
  if (!isWebChatSessionRecord(session)) {
    return false;
  }
  const status = typeof session?.status === "string" ? session.status : null;
  // Treat unknown/missing status as restorable so transient backend hiccups
  // don't drop a working session; only refuse to restore explicit terminal
  // states (expired / closed / ended / etc).
  if (!status) return true;
  return RESTORABLE_SESSION_STATUSES.has(status);
}

function buildContextUsageFromTotals(params: {
  totalInputTokens?: number | null;
  outputTokens?: number | null;
  cacheReadTokens?: number | null;
  cacheCreationTokens?: number | null;
  contextWindow?: number | null;
}) {
  const totalInputTokens = params.totalInputTokens ?? 0;
  const outputTokens = params.outputTokens ?? 0;
  const cacheReadTokens = params.cacheReadTokens ?? 0;
  const cacheCreationTokens = params.cacheCreationTokens ?? 0;

  return {
    totalInputTokens,
    outputTokens,
    contextWindow: params.contextWindow ?? null,
    uncachedInputTokens: Math.max(
      0,
      totalInputTokens - cacheReadTokens - cacheCreationTokens,
    ),
    cacheReadTokens,
    cacheCreationTokens,
  };
}

function toSessionObservationMeta(
  session: Record<string, unknown> | null,
  overrides?: Partial<SessionObservationMeta>,
): SessionObservationMeta | null {
  if (!session) return null;
  return {
    ref:
      overrides?.ref ??
      (typeof session.seq_num === "number" ? `#${session.seq_num}` : null),
    source:
      overrides?.source ??
      (typeof session.source === "string" ? session.source : "unknown"),
    title:
      overrides?.title ??
      (typeof session.title === "string" ? session.title : null),
    status:
      overrides?.status ??
      (typeof session.status === "string" ? session.status : "unknown"),
    canProxyAttach:
      overrides?.canProxyAttach ??
      (typeof session.can_proxy_attach === "boolean"
        ? session.can_proxy_attach
        : undefined),
    model:
      overrides?.model ??
      (typeof session.model === "string" ? session.model : null),
    reasoningEffort:
      overrides?.reasoningEffort ??
      (typeof session.reasoning_effort === "string"
        ? session.reasoning_effort
        : null),
    externalId:
      overrides?.externalId ??
      (typeof session.external_id === "string" ? session.external_id : ""),
    chatMode:
      overrides?.chatMode ??
      (typeof session.chat_mode === "string" ? session.chat_mode : null),
    gitBranch:
      overrides?.gitBranch ??
      (typeof session.git_branch === "string" ? session.git_branch : null),
    contextWindow:
      overrides?.contextWindow ??
      (typeof session.context_window === "number"
        ? session.context_window
        : null),
    agentRunId:
      overrides?.agentRunId ??
      (typeof session.agent_run_id === "string" ? session.agent_run_id : null),
    workflowName:
      overrides?.workflowName ??
      (typeof session.workflow_name === "string"
        ? session.workflow_name
        : null),
    agentName:
      overrides?.agentName ??
      (typeof session.agent_name === "string" ? session.agent_name : null),
    sessionType:
      overrides?.sessionType ?? normalizeSessionType(session.session_type),
  };
}

interface ApiMessage {
  id?: string;
  role: string;
  content: string;
  content_type?: string;
  tool_name?: string;
  tool_input?: string;
  tool_result?: string;
  tool_use_id?: string;
  timestamp: string;
  message_index?: number;
  content_blocks?: ContentBlock[]; // Snake case from RenderedMessage shape
  model?: string | null;
  usage?: TokenUsage | null;
}

function mapStoredChatMessage(m: {
  id: string;
  role: string;
  content: string;
  tool_calls?: ToolCall[];
  created_at: string;
}): ChatMessage {
  return {
    id: m.id,
    role: normalizeChatRole(m.role, m.content),
    content: m.content,
    contentBlocks: m.content ? [{ type: "text" as const, content: m.content }] : [],
    toolCalls: m.tool_calls ?? [],
    timestamp: new Date(m.created_at),
  };
}

function tryParseJSON(value: unknown): unknown {
  if (value === undefined || value === null) return undefined;
  if (typeof value !== "string") return value;
  try {
    return JSON.parse(value);
  } catch {
    return value;
  }
}

/** Helper: append to or create a text content block on the current assistant message. */
function appendTextBlock(msg: ChatMessage, text: string) {
  if (!msg.contentBlocks) msg.contentBlocks = [];
  const last = msg.contentBlocks[msg.contentBlocks.length - 1];
  if (last?.type === "text") {
    if (last.content && !last.content.endsWith("\n")) last.content += "\n";
    last.content += text;
  } else {
    msg.contentBlocks.push({ type: "text", content: text });
  }
}

/** Helper: append a tool call to the current tool_chain block, or start a new one. */
function appendToolBlock(msg: ChatMessage, tc: ToolCall) {
  if (!msg.contentBlocks) msg.contentBlocks = [];
  const last = msg.contentBlocks[msg.contentBlocks.length - 1];
  if (last?.type === "tool_chain") {
    last.tool_calls.push(tc);
  } else {
    msg.contentBlocks.push({ type: "tool_chain", tool_calls: [tc] });
  }
}

/** Find a tool call by its tool_use_id across contentBlocks and flat toolCalls. */
function findToolCallById(
  msg: ChatMessage,
  toolUseId: string,
): ToolCall | undefined {
  if (msg.contentBlocks) {
    for (const block of msg.contentBlocks) {
      if (block.type === "tool_chain") {
        const found = block.tool_calls.find((tc) => tc.id === toolUseId);
        if (found) return found;
      }
    }
  }
  return msg.toolCalls?.find((tc) => tc.id === toolUseId);
}

/** Find the last pending tool call across contentBlocks and flat toolCalls. */
function findPendingToolCall(msg: ChatMessage): ToolCall | undefined {
  // Check contentBlocks first (interleaved model)
  if (msg.contentBlocks) {
    for (let i = msg.contentBlocks.length - 1; i >= 0; i--) {
      const block = msg.contentBlocks[i];
      if (block.type === "tool_chain") {
        const pending = block.tool_calls.find(
          (tc) => tc.status !== "completed",
        );
        if (pending) return pending;
      }
    }
  }
  // Fallback to flat toolCalls
  return msg.toolCalls?.find((tc) => tc.status !== "completed");
}

function extractServerName(toolName: string): string {
  const parts = toolName.split("__");
  if (parts.length >= 3 && parts[0] === "mcp") return parts[1];
  return "builtin";
}

function isHookFeedback(content: string): boolean {
  return (
    /^Stop hook feedback:/.test(content) ||
    /^(Pre|Post)ToolUse hook/.test(content) ||
    /^UserPromptSubmit hook/.test(content)
  );
}

function extractUserText(content: string): string | null {
  if (!content.startsWith("[") || !content.endsWith("]")) return null;
  let blocks: Array<{ type?: string; text?: string; content?: string }> | null =
    null;
  try {
    const parsed = JSON.parse(content);
    if (Array.isArray(parsed)) blocks = parsed;
  } catch {
    return null;
  }
  if (!blocks || blocks.length === 0) return null;
  const texts: string[] = [];
  for (const block of blocks) {
    const text = block.text ?? block.content ?? "";
    if (!text) continue;
    if (text.includes("<hook_context>") || text.includes("</hook_context>"))
      continue;
    if (
      text.includes("<system-reminder>") ||
      text.includes("</system-reminder>")
    )
      continue;
    if (
      text.includes("<system_instructions>") ||
      text.includes("</system_instructions>")
    )
      continue;
    texts.push(text);
  }
  return texts.length > 0 ? texts.join("\n\n") : "";
}

function mapApiMessages(messages: ApiMessage[]): ChatMessage[] {
  const result: ChatMessage[] = [];
  let currentAssistant: ChatMessage | null = null;

  function flushAssistant() {
    if (currentAssistant) {
      result.push(currentAssistant);
      currentAssistant = null;
    }
  }

  for (const m of messages) {
    const id = m.id || `msg-${m.message_index ?? result.length}`;
    const timestamp = new Date(m.timestamp);

    // If message already has pre-rendered content_blocks, use them directly (RenderedMessage shape)
    if (m.content_blocks && m.content_blocks.length > 0) {
      flushAssistant();

      const chatMsg: ChatMessage = {
        id,
        role: normalizeChatRole(m.role, m.content, m.content_blocks),
        content: m.content || "",
        timestamp,
        contentBlocks: m.content_blocks,
      };

      // Extract toolCalls and thinkingContent for legacy component compatibility
      for (const block of m.content_blocks) {
        if (block.type === "tool_chain" && block.tool_calls) {
          chatMsg.toolCalls = [
            ...(chatMsg.toolCalls || []),
            ...block.tool_calls,
          ];
        } else if (block.type === "thinking") {
          chatMsg.thinkingContent =
            (chatMsg.thinkingContent || "") + block.content;
        }
      }

      result.push(chatMsg);
      continue;
    }

    const content = (m.content || "").trim();

    if (m.role === "user") {
      if (m.content_type === "tool_result" || m.tool_use_id) {
        // Tool result in a user message — prefer ID-based match, fall back to positional
        if (currentAssistant) {
          const match = m.tool_use_id
            ? findToolCallById(currentAssistant, m.tool_use_id)
            : findPendingToolCall(currentAssistant);
          if (match) {
            match.result = tryParseJSON(m.content) as ToolResult | undefined;
            match.status = "completed";
          }
        }
        continue;
      }

      // Skip tool_result protocol messages (user messages with raw tool_result JSON arrays)
      if (content.startsWith("[{") && content.includes("tool_result")) {
        continue;
      }

      // Hook feedback → attach to last tool call as error, or render as system message
      if (isHookFeedback(content)) {
        if (currentAssistant?.toolCalls?.length) {
          const lastTc =
            currentAssistant.toolCalls[currentAssistant.toolCalls.length - 1];
          lastTc.error = content;
          lastTc.status = "error";
          if (currentAssistant.contentBlocks) {
            for (const block of currentAssistant.contentBlocks) {
              if (block.type === "tool_chain") {
                const tcMatch = block.tool_calls.find(
                  (c) => c.id === lastTc.id,
                );
                if (tcMatch) {
                  tcMatch.error = content;
                  tcMatch.status = "error";
                }
              }
            }
          }
        } else {
          flushAssistant();
          result.push({
            id,
            role: "system",
            content,
            timestamp: new Date(m.timestamp),
          });
        }
        continue;
      }

      // User messages with serialized content block arrays → extract user text
      if (content.startsWith("[")) {
        const extracted = extractUserText(content);
        if (extracted !== null) {
          if (!extracted.trim()) continue;
          flushAssistant();
          result.push({
            id,
            role: "user",
            content: extracted,
            timestamp: new Date(m.timestamp),
          });
          continue;
        }
      }

      // Regular user message
      flushAssistant();
      result.push({
        id,
        role: "user",
        content: m.content || "",
        timestamp: new Date(m.timestamp),
      });
    } else if (m.role === "assistant") {
      if (m.content_type === "tool_use" || m.tool_name) {
        // Tool invocation — attach to current assistant message or create one
        if (!currentAssistant) {
          currentAssistant = {
            id,
            role: "assistant",
            content: "",
            timestamp: new Date(m.timestamp),
            toolCalls: [],
            contentBlocks: [],
          };
        }
        const toolName = m.tool_name || "unknown";
        const toolCall: ToolCall = {
          id: m.tool_use_id || id,
          tool_name: toolName,
          server_name: extractServerName(toolName),
          tool_type: classifyTool(toolName),
          status: m.tool_result ? "completed" : "calling",
          arguments: tryParseJSON(m.tool_input) as
            | Record<string, unknown>
            | undefined,
          result: m.tool_result
            ? (tryParseJSON(m.tool_result) as ToolResult)
            : undefined,
        };
        // Add to flat list (backward compat)
        currentAssistant.toolCalls = [
          ...(currentAssistant.toolCalls || []),
          toolCall,
        ];
        // Add to interleaved blocks
        appendToolBlock(currentAssistant, toolCall);
      } else if (m.content_type === "thinking") {
        if (!currentAssistant) {
          currentAssistant = {
            id,
            role: "assistant",
            content: "",
            timestamp: new Date(m.timestamp),
          };
        }
        currentAssistant.thinkingContent =
          (currentAssistant.thinkingContent || "") + (m.content || "");
      } else if (content.startsWith("[{") && content.includes("tool_use")) {
        // Assistant message that is a JSON array of tool_use blocks
        try {
          const calls = JSON.parse(content) as Array<{
            type?: string;
            id?: string;
            name?: string;
            input?: unknown;
          }>;
          const tools = calls.filter((c) => c.type === "tool_use");
          if (tools.length > 0) {
            const toolCalls: ToolCall[] = tools.map((t) => {
              const toolName = t.name || "unknown";
              return {
                id: t.id || `tool-${id}-${toolName}`,
                tool_name: toolName,
                server_name: extractServerName(toolName),
                tool_type: classifyTool(toolName),
                status: "completed" as const,
                arguments:
                  typeof t.input === "object" && t.input !== null
                    ? (t.input as Record<string, unknown>)
                    : undefined,
              };
            });
            if (!currentAssistant) {
              currentAssistant = {
                id,
                role: "assistant",
                content: "",
                timestamp: new Date(m.timestamp),
                toolCalls,
                contentBlocks: [
                  { type: "tool_chain", tool_calls: [...toolCalls] },
                ],
              };
            } else {
              currentAssistant.toolCalls = [
                ...(currentAssistant.toolCalls || []),
                ...toolCalls,
              ];
              for (const tc of toolCalls) appendToolBlock(currentAssistant, tc);
            }
            continue;
          }
        } catch {
          // Fall through to normal text handling
        }
        // Regular assistant text
        if (currentAssistant) {
          if (content) {
            if (
              currentAssistant.content &&
              !currentAssistant.content.endsWith("\n")
            )
              currentAssistant.content += "\n";
            currentAssistant.content += content;
            appendTextBlock(currentAssistant, content);
          }
        } else {
          currentAssistant = {
            id,
            role: "assistant",
            content: content || "",
            timestamp: new Date(m.timestamp),
            contentBlocks: content ? [{ type: "text", content }] : [],
          };
        }
      } else {
        // Regular assistant text
        if (currentAssistant) {
          if (m.content) {
            if (
              currentAssistant.content &&
              !currentAssistant.content.endsWith("\n")
            )
              currentAssistant.content += "\n";
            currentAssistant.content += m.content;
            appendTextBlock(currentAssistant, m.content);
          }
        } else {
          currentAssistant = {
            id,
            role: "assistant",
            content: m.content || "",
            timestamp: new Date(m.timestamp),
            contentBlocks: m.content
              ? [{ type: "text", content: m.content }]
              : [],
          };
        }
      }
    } else if (m.role === "tool") {
      // Tool result message — prefer ID-based match, fall back to positional
      if (currentAssistant) {
        const match = m.tool_use_id
          ? findToolCallById(currentAssistant, m.tool_use_id)
          : findPendingToolCall(currentAssistant);
        if (match) {
          match.result = tryParseJSON(m.content) as ToolResult | undefined;
          match.status = "completed";
        }
      }
    }
  }

  flushAssistant();
  return result;
}

export function useChat() {
  const [conversationId, setConversationId] = useState<string>(() =>
    loadConversationId(),
  );
  const conversationIdRef = useRef<string>(conversationId);

  // Counter that increments only on intentional conversation switches (not SDK
  // session ID adoption).  Used by the mode-restore effect in App.tsx so that
  // adopting the SDK session ID doesn't reset the user's mode to the default.
  const [conversationSwitchKey, setConversationSwitchKey] = useState(0);

  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const messagesRef = useRef(messages);
  const [isConnected, setIsConnected] = useState(false);
  const [isReconnecting, setIsReconnecting] = useState(false);
  const [isStreaming, setIsStreaming] = useState(false);
  const [isThinking, setIsThinking] = useState(false);
  const [isLoadingMessages, setIsLoadingMessages] = useState(false);

  // Canvas state
  const [canvasSurfaces, setCanvasSurfaces] = useState<
    Map<string, A2UISurfaceState>
  >(new Map());
  const [canvasPanel, setCanvasPanel] = useState<CanvasPanelState | null>(null);

  // Session ref tracking (e.g. "#158")
  const [sessionRef, setSessionRef] = useState<string | null>(null);

  // DB session ID — used by title synthesis to call session APIs directly
  // without waiting for sessions list polling
  const [dbSessionId, setDbSessionId] = useState<string | null>(() =>
    loadDbSessionId(),
  );
  const dbSessionIdRef = useRef<string | null>(dbSessionId);
  const creatingSessionIdRef = useRef<Promise<string | null> | null>(null);
  const lastSeqRef = useRef<number>(0);

  // Branch/worktree tracking
  const [currentBranch, setCurrentBranch] = useState<string | null>(null);
  const [worktreePath, setWorktreePath] = useState<string | null>(null);

  // Active agent tracking — persisted to survive page reloads
  const ACTIVE_AGENT_KEY = "gobby-active-agent";
  const [activeAgent, setActiveAgent] = useState<string>(
    () => localStorage.getItem(ACTIVE_AGENT_KEY) || "default",
  );

  // Session title — stored from switchConversation to survive filtered list race
  const [sessionTitle, setSessionTitle] = useState<string | null>(null);
  const [mainSessionMeta, setMainSessionMeta] =
    useState<SessionObservationMeta | null>(null);
  const sessionRefRef = useRef<string | null>(sessionRef);
  useEffect(() => {
    sessionRefRef.current = sessionRef;
  }, [sessionRef]);

  // LLM provider selection (null = server default), persisted to localStorage
  const [selectedProvider, setSelectedProviderRaw] = useState<string | null>(
    () => {
      try {
        return localStorage.getItem("gobby-selected-provider") || null;
      } catch {
        return null;
      }
    },
  );
  const setSelectedProvider = useCallback((provider: string | null) => {
    setSelectedProviderRaw(provider);
    try {
      if (provider) {
        localStorage.setItem("gobby-selected-provider", provider);
      } else {
        localStorage.removeItem("gobby-selected-provider");
      }
    } catch {
      /* ignore */
    }
  }, []);
  const selectedProviderRef = useRef<string | null>(selectedProvider);
  useEffect(() => {
    selectedProviderRef.current = selectedProvider;
  }, [selectedProvider]);

  // Session viewing tracking (read-only observation of CLI sessions via REST)
  const [viewingSessionId, setViewingSessionId] = useState<string | null>(() =>
    loadViewingSessionId(),
  );
  const viewingSessionIdRef = useRef<string | null>(null);
  const [viewingSessionMeta, setViewingSessionMeta] =
    useState<SessionObservationMeta | null>(null);
  const viewingSessionMetaRef = useRef<SessionObservationMeta | null>(null);
  const initialViewingSessionIdRef = useRef<string | null>(
    loadViewingSessionId(),
  );
  const initialViewingModeRef = useRef<"none" | "observe">(
    loadViewingSessionMode(),
  );
  const initialViewingRestoreRef = useRef(false);
  const initialViewingReconnectRetryRef = useRef(false);

  // Live terminal observation is distinct from interactive proxy mode.
  const [observedSessionId, setObservedSessionId] = useState<string | null>(
    null,
  );
  const observedSessionIdRef = useRef<string | null>(null);
  const observedSessionMetaRef = useRef<SessionObservationMeta | null>(null);
  const [sessionInteractionMode, setSessionInteractionMode] =
    useState<SessionInteractionMode>("none");
  const sessionInteractionModeRef = useRef<SessionInteractionMode>("none");
  const pendingSessionInteractionModeRef = useRef<"observe" | "proxy">("proxy");
  const [proxyDeliveryNotice, setProxyDeliveryNotice] = useState<string | null>(
    null,
  );
  const agentNameCacheRef = useRef<Map<string, string | null>>(new Map());

  // Session attachment tracking (interactive proxy mode only)
  const [attachedSessionId, setAttachedSessionId] = useState<string | null>(
    null,
  );
  const attachedSessionIdRef = useRef<string | null>(null);
  const [attachedSessionMeta, setAttachedSessionMeta] =
    useState<SessionObservationMeta | null>(null);
  const attachedSessionMetaRef = useRef<SessionObservationMeta | null>(null);
  const pendingProxyMessagesRef = useRef<Map<string, PendingProxyMessage>>(
    new Map(),
  );
  const pendingProxySessionQueuesRef = useRef<Map<string, string[]>>(new Map());
  const [isContinuingSession, setIsContinuingSession] = useState(false);
  const continuingSessionIdRef = useRef<string | null>(null);
  const continuationRollbackRef = useRef<ContinuationRollbackSnapshot | null>(
    null,
  );

  // Keep a ref so onopen/reconnect can read the current agent
  const activeAgentRef = useRef(activeAgent);
  useEffect(() => {
    activeAgentRef.current = activeAgent;
    localStorage.setItem(ACTIVE_AGENT_KEY, activeAgent);
  }, [activeAgent]);

  // Keep a ref so onopen/reconnect can read the current project
  const projectIdRef = useRef<string | null>(null);
  const setProjectIdRef = useCallback((id: string | null) => {
    projectIdRef.current = id;
  }, []);
  const clearContinuingSession = useCallback(() => {
    continuingSessionIdRef.current = null;
    setIsContinuingSession(false);
  }, []);

  const resolveAgentName = useCallback(async (agentRunId: string) => {
    const cached = agentNameCacheRef.current.get(agentRunId);
    if (cached !== undefined) {
      return cached;
    }

    const baseUrl = import.meta.env.VITE_API_BASE_URL || "";
    try {
      const res = await fetch(`${baseUrl}/api/agents/runs/${agentRunId}`);
      if (!res.ok) {
        agentNameCacheRef.current.set(agentRunId, null);
        return null;
      }
      const data = await res.json();
      const resolved =
        data?.run?.agent_name || data?.run?.workflow_name || null;
      agentNameCacheRef.current.set(agentRunId, resolved);
      return resolved;
    } catch {
      agentNameCacheRef.current.set(agentRunId, null);
      return null;
    }
  }, []);

  // Plan mode approval tracking
  const [planPendingApproval, setPlanPendingApproval] = useState(false);
  const planContentRef = useRef<string | null>(null);
  const currentModeRef = useRef<ChatMode>("plan");

  // Callback for backend-initiated mode changes (e.g. agent EnterPlanMode)
  const onModeChangedRef = useRef<((mode: ChatMode) => void) | null>(null);
  const setOnModeChanged = useCallback((fn: (mode: ChatMode) => void) => {
    onModeChangedRef.current = fn;
  }, []);

  // Callback when plan content is ready (for artifact creation)
  const onPlanReadyRef = useRef<((content: string | null) => void) | null>(
    null,
  );
  const setOnPlanReady = useCallback((fn: (content: string | null) => void) => {
    onPlanReadyRef.current = fn;
  }, []);

  // Callback when artifact event arrives from backend (show_file)
  const onArtifactEventRef = useRef<
    | ((
        type: string,
        content: string,
        language?: string,
        title?: string,
      ) => void)
    | null
  >(null);
  const setOnArtifactEvent = useCallback(
    (
      fn: (
        type: string,
        content: string,
        language?: string,
        title?: string,
      ) => void,
    ) => {
      onArtifactEventRef.current = fn;
    },
    [],
  );

  // Callback when backend confirms a chat deletion
  const onChatDeletedRef = useRef<((conversationId: string) => void) | null>(
    null,
  );
  const setOnChatDeleted = useCallback(
    (fn: (conversationId: string) => void) => {
      onChatDeletedRef.current = fn;
    },
    [],
  );

  // Callback when backend confirms a chat clear
  const onChatClearedRef = useRef<((conversationId: string) => void) | null>(
    null,
  );
  const setOnChatCleared = useCallback(
    (fn: (conversationId: string) => void) => {
      onChatClearedRef.current = fn;
    },
    [],
  );

  // Stable ref to sendMessage for use inside WS handlers / callbacks
  // defined before sendMessage itself. Updated after sendMessage is created.
  const sendMessageRef = useRef<
    | ((
        content: string,
        model?: string | null,
        files?: QueuedFile[],
        projectId?: string | null,
        injectContext?: string,
        reasoningEffort?: string | null,
      ) => boolean)
    | null
  >(null);

  const bindActiveSession = useCallback((sessionId: string | null) => {
    const nextId = sessionId ?? "";
    lastSeqRef.current = 0;
    conversationIdRef.current = nextId;
    setConversationId(nextId);
    setDbSessionId(sessionId);
    dbSessionIdRef.current = sessionId;
    saveDbSessionId(sessionId);
    saveConversationId(nextId);
  }, []);

  const markSessionUsageFresh = useCallback((sessionId: string, rawTimestamp?: string) => {
    const parsed = rawTimestamp ? new Date(rawTimestamp).getTime() : NaN;
    lastLiveUsageBySessionRef.current.set(
      sessionId,
      Number.isFinite(parsed) ? parsed : Date.now(),
    );
  }, []);

  const shouldApplyHydratedUsage = useCallback((sessionId: string, fetchStartedAt: number) => {
    const lastLive = lastLiveUsageBySessionRef.current.get(sessionId);
    return lastLive == null || lastLive <= fetchStartedAt;
  }, []);

  const applyMainSessionMeta = useCallback(
    (session: Record<string, unknown> | null) => {
      const nextMeta = toSessionObservationMeta(session, {
        sessionType: "web_chat",
      });
      setMainSessionMeta(nextMeta);
      setSessionTitle(nextMeta?.title ?? null);
      if (nextMeta?.ref) {
        setSessionRef(nextMeta.ref);
      }
      setCurrentBranch(nextMeta?.gitBranch ?? null);
      if (nextMeta && isChatProvider(nextMeta.source)) {
        setSelectedProvider(nextMeta.source);
      }
      if (nextMeta?.chatMode) {
        const restored = normalizeChatMode(nextMeta.chatMode);
        if (restored !== currentModeRef.current) {
          currentModeRef.current = restored;
          onModeChangedRef.current?.(restored);
        }
      }
      if (hasSessionUsage(session)) {
        setContextUsage(computeContextUsageFromSessionData(session));
      }
    },
    [setSelectedProvider],
  );

  const clearSessionObservationState = useCallback(
    ({ preserveViewing = false }: { preserveViewing?: boolean } = {}) => {
      const observedSessionId = observedSessionIdRef.current;
      if (observedSessionId && wsRef.current?.readyState === WebSocket.OPEN) {
        wsRef.current.send(
          JSON.stringify({
            type: "detach_from_session",
            session_id: observedSessionId,
          }),
        );
      }
      if (!preserveViewing) {
        viewingSessionIdRef.current = null;
        viewingSessionMetaRef.current = null;
        setViewingSessionId(null);
        setViewingSessionMeta(null);
      }
      observedSessionIdRef.current = null;
      observedSessionMetaRef.current = null;
      attachedSessionIdRef.current = null;
      attachedSessionMetaRef.current = null;
      clearPendingProxyMessages(
        pendingProxyMessagesRef.current,
        pendingProxySessionQueuesRef.current,
      );
      sessionInteractionModeRef.current = "none";
      setObservedSessionId(null);
      setAttachedSessionId(null);
      setAttachedSessionMeta(null);
      setSessionInteractionMode("none");
      setProxyDeliveryNotice(null);
    },
    [],
  );

  const resetMainChatState = useCallback(() => {
    lastSeqRef.current = 0;
    activeRequestIdRef.current = null;
    setIsStreaming(false);
    setIsThinking(false);
    setSessionRef(null);
    setSessionTitle(null);
    setMainSessionMeta(null);
    setCurrentBranch(null);
    setWorktreePath(null);
    setCanvasSurfaces(new Map());
    setCanvasPanel(null);
    setPlanPendingApproval(false);
    planContentRef.current = null;
    setContextUsage({
      totalInputTokens: 0,
      outputTokens: 0,
      contextWindow: null,
      uncachedInputTokens: 0,
      cacheReadTokens: 0,
      cacheCreationTokens: 0,
    });
    setMessages([]);
    setIsLoadingMessages(false);
  }, []);

  const ensureMainSession = useCallback(
    async (options?: {
      projectId?: string | null;
      provider?: string | null;
      model?: string | null;
      reasoningEffort?: string | null;
      chatMode?: ChatMode | null;
      title?: string | null;
      forceNew?: boolean;
    }): Promise<string | null> => {
      if (!options?.forceNew && dbSessionIdRef.current) {
        return dbSessionIdRef.current;
      }
      if (!options?.forceNew && creatingSessionIdRef.current) {
        return await creatingSessionIdRef.current;
      }

      const pending = createWebChatSession({
        projectId: options?.projectId ?? projectIdRef.current,
        provider: options?.provider ?? selectedProviderRef.current,
        model: options?.model ?? null,
        reasoningEffort: options?.reasoningEffort ?? null,
        chatMode: options?.chatMode ?? currentModeRef.current,
        title: options?.title ?? null,
      })
        .then((session) => {
          bindActiveSession(session.id);
          applyMainSessionMeta(session as Record<string, unknown>);
          if (
            wsRef.current?.readyState === WebSocket.OPEN &&
            activeAgentRef.current
          ) {
            wsRef.current.send(
              JSON.stringify({
                type: "set_agent",
                conversation_id: session.id,
                agent_name: activeAgentRef.current,
              }),
            );
          }
          return session.id;
        })
        .catch((error) => {
          console.error("Failed to create web chat session:", error);
          return null;
        })
        .finally(() => {
          creatingSessionIdRef.current = null;
        });

      creatingSessionIdRef.current = pending;
      return await pending;
    },
    [applyMainSessionMeta, bindActiveSession],
  );

  useEffect(() => {
    conversationIdRef.current = conversationId;
  }, [conversationId]);

  useEffect(() => {
    messagesRef.current = messages;
  }, [messages]);

  useEffect(() => {
    dbSessionIdRef.current = dbSessionId;
  }, [dbSessionId]);

  // Context usage tracking — accumulated across turns.
  // totalInputTokens = uncached + cacheRead + cacheCreation (the real context size).
  const [contextUsage, setContextUsage] = useState<{
    totalInputTokens: number;
    outputTokens: number;
    contextWindow: number | null;
    // Per-category breakdown for tooltip
    uncachedInputTokens: number;
    cacheReadTokens: number;
    cacheCreationTokens: number;
  }>({
    totalInputTokens: 0,
    outputTokens: 0,
    contextWindow: null,
    uncachedInputTokens: 0,
    cacheReadTokens: 0,
    cacheCreationTokens: 0,
  });
  const [contextUsageUpdatedAt, setContextUsageUpdatedAt] = useState<number | null>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectTimeoutRef = useRef<number | null>(null);
  // Timestamp of last server-authoritative mode_changed — used to suppress
  // redundant set_mode emissions on WS reconnect and session restore
  const lastServerModeTimestampRef = useRef<number>(0);

  // Main-chat contextUsage captured before attaching to an observed session,
  // so detach can restore it. Null when no attach is active. Only set on the
  // first attach in a sequence so chained attaches don't overwrite the
  // original main-chat snapshot.
  const preAttachContextUsageRef = useRef<{
    totalInputTokens: number;
    outputTokens: number;
    contextWindow: number | null;
    uncachedInputTokens: number;
    cacheReadTokens: number;
    cacheCreationTokens: number;
  } | null>(null);
  const didTrackContextUsageRef = useRef(false);
  const lastLiveUsageBySessionRef = useRef<Map<string, number>>(new Map());
  const clearPreAttachContextUsage = useCallback(() => {
    preAttachContextUsageRef.current = null;
  }, []);

  // Track the active chat request to filter stale stream chunks from cancelled requests
  const activeRequestIdRef = useRef<string | null>(null);

  // Queue for messages sent while disconnected — flushed on reconnect
  const pendingMessagesRef = useRef<
    {
      content: string;
      model?: string | null;
      projectId?: string | null;
      reasoningEffort?: string | null;
    }[]
  >([]);

  const clearContinuationRollback = useCallback(() => {
    continuationRollbackRef.current = null;
  }, []);

  useEffect(() => {
    if (!didTrackContextUsageRef.current) {
      didTrackContextUsageRef.current = true;
      return;
    }
    setContextUsageUpdatedAt(Date.now());
  }, [contextUsage]);

  const restoreContinuationState = useCallback(
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
        snapshot.viewingSessionId &&
          snapshot.sessionInteractionMode === "observe"
          ? "observe"
          : "none",
      );
      setProxyDeliveryNotice(snapshot.proxyDeliveryNotice);
      setIsLoadingMessages(false);
      currentModeRef.current = normalizeChatMode(snapshot.currentMode);
      onModeChangedRef.current?.(normalizeChatMode(snapshot.currentMode));

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
    [setContextUsage, setSelectedProvider],
  );

  /** Returns true if the chunk belongs to the currently active request. */
  function isActiveRequest(requestId?: string): boolean {
    return requestId === activeRequestIdRef.current;
  }

  // Refs for handlers to avoid stale closures in WebSocket callbacks
  const handleChatStreamRef = useRef<(chunk: ChatStreamChunk) => void>(
    () => {},
  );
  const handleChatErrorRef = useRef<(error: ChatError) => void>(() => {});
  const handleToolStatusRef = useRef<(status: ToolStatusMessage) => void>(
    () => {},
  );
  const handleChatThinkingRef = useRef<(msg: ChatThinkingMessage) => void>(
    () => {},
  );
  const handleModelSwitchedRef = useRef<(msg: ModelSwitchedMessage) => void>(
    () => {},
  );
  const handleVoiceMessageRef = useRef<(data: Record<string, unknown>) => void>(
    () => {},
  );
  const handleBinaryMessageRef = useRef<(data: ArrayBuffer) => void>(() => {});

  // Connect to WebSocket
  const connect = useCallback(() => {
    if (wsRef.current?.readyState === WebSocket.OPEN) return;

    const wsProtocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    const wsUrl = `${wsProtocol}//${window.location.host}/ws`;

    console.log("Connecting to WebSocket:", wsUrl);
    const ws = new WebSocket(wsUrl);
    ws.binaryType = "arraybuffer"; // For TTS audio binary frames
    wsRef.current = ws;

    ws.onopen = () => {
      console.log("WebSocket connected");
      setIsConnected(true);
      setIsReconnecting(false);

      // Flush queued messages that were sent while disconnected
      if (pendingMessagesRef.current.length > 0) {
        const queued = [...pendingMessagesRef.current];
        pendingMessagesRef.current = [];
        // Send each queued message after a brief delay to let WS fully initialize
        setTimeout(() => {
          for (const msg of queued) {
            sendMessageRef.current?.(
              msg.content,
              msg.model ?? null,
              undefined,
              msg.projectId,
              undefined,
              msg.reasoningEffort,
            );
          }
        }, 500);
      }

      // Backfill missed messages on reconnect (lastSeqRef > 0 means we had messages before)
      if (lastSeqRef.current > 0 && conversationIdRef.current) {
        const baseUrl = import.meta.env.VITE_API_BASE_URL || "";
        const convId = conversationIdRef.current;
        const afterSeq = lastSeqRef.current;
        fetch(`${baseUrl}/api/chat/${convId}/messages?after_seq=${afterSeq}`)
          .then((res) => (res.ok ? res.json() : null))
          .then((data) => {
            if (viewingSessionIdRef.current) return;
            if (!data?.messages?.length) return;
            const backfilled: ChatMessage[] = data.messages.map(mapStoredChatMessage);
            setMessages((prev) => {
              const existingIds = new Set(prev.map((m) => m.id));
              const newMsgs = backfilled.filter((m) => !existingIds.has(m.id));
              return newMsgs.length > 0 ? [...prev, ...newMsgs] : prev;
            });
            if (data.max_seq) lastSeqRef.current = data.max_seq;
          })
          .catch((err) => console.error("Failed to backfill messages:", err));
      }

      ws.send(
        JSON.stringify({
          type: "subscribe",
          events: [
            "chat_stream",
            "chat_error",
            "tool_status",
            "chat_thinking",
            "canvas_event",
            "artifact_event",
            "session_message",
            "session_usage_updated",
            "token_event",
          ],
        }),
      );

      // Rebind this client to the current main-chat conversation without
      // mutating the server-owned session. The backend uses conversation_id
      // on the websocket client metadata to scope broadcasts for the active
      // chat stream and pending approvals.
      if (conversationIdRef.current) {
        ws.send(
          JSON.stringify({
            type: "heartbeat",
            conversation_id: conversationIdRef.current,
          }),
        );
      }
    };

    ws.onclose = () => {
      console.log("WebSocket disconnected");
      setIsConnected(false);
      setIsReconnecting(true);
      // Don't clear isStreaming/isThinking/activeRequestIdRef — the backend
      // may still be working. Clearing these causes post-reconnect tool_status
      // updates to be dropped as "stale". Only clear on explicit cancel or
      // if reconnect timeout expires (30s).
      const disconnectTimer = window.setTimeout(() => {
        // If still disconnected after 30s, assume the stream is dead
        if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) {
          setIsStreaming(false);
          setIsThinking(false);
          activeRequestIdRef.current = null;
        }
      }, 30_000);

      reconnectTimeoutRef.current = window.setTimeout(() => {
        clearTimeout(disconnectTimer);
        connect();
      }, 2000);
    };

    ws.onerror = (error) => {
      console.error("WebSocket error:", error);
    };

    ws.onmessage = (event) => {
      // Binary frames are TTS audio data — route to voice handler
      if (event.data instanceof ArrayBuffer) {
        try {
          handleBinaryMessageRef.current(event.data);
        } catch (err) {
          console.error("TTS binary message error:", err);
        }
        return;
      }

      try {
        const data = JSON.parse(event.data) as WebSocketMessage;
        console.log("WebSocket message:", data.type, data);

        if (data.type === "chat_stream") {
          handleChatStreamRef.current(data as unknown as ChatStreamChunk);
        } else if (data.type === "chat_error") {
          handleChatErrorRef.current(data as unknown as ChatError);
        } else if (data.type === "tool_status") {
          handleToolStatusRef.current(data as unknown as ToolStatusMessage);
        } else if (data.type === "chat_thinking") {
          handleChatThinkingRef.current(data as unknown as ChatThinkingMessage);
        } else if (data.type === "model_switched") {
          handleModelSwitchedRef.current(
            data as unknown as ModelSwitchedMessage,
          );
        } else if (
          data.type === "voice_transcription" ||
          data.type === "voice_audio_chunk" ||
          data.type === "voice_status" ||
          data.type === "tts_audio" ||
          data.type === "tts_status"
        ) {
          try {
            // When STT transcription arrives, inject it as a user message and
            // register the request_id so the assistant's response stream is accepted.
            if (data.type === "voice_transcription") {
              const voiceMsg = data as unknown as VoiceTranscriptionMessage;
              const text =
                typeof voiceMsg.text === "string" ? voiceMsg.text : "";
              const reqId =
                typeof voiceMsg.request_id === "string"
                  ? voiceMsg.request_id
                  : "";
              if (text && reqId) {
                activeRequestIdRef.current = reqId;
                setMessages((prev) => [
                  ...prev,
                  {
                    id: `user-voice-${reqId}`,
                    role: "user" as const,
                    content: text,
                    timestamp: new Date(),
                  },
                ]);
                setIsStreaming(true);
                setIsThinking(true);
              }
            }
            handleVoiceMessageRef.current(data as Record<string, unknown>);
          } catch (err) {
            console.error("Voice message handling error:", err);
            setIsStreaming(false);
            setIsThinking(false);
          }
        } else if (data.type === "plan_pending_approval") {
          const msgConvId = (data as Record<string, unknown>)
            .conversation_id as string | undefined;
          // Only accept plans for the current conversation (or unscoped legacy events)
          if (!msgConvId || msgConvId === conversationIdRef.current) {
            const planContent = (data as Record<string, unknown>)
              .plan_content as string | undefined;
            if (planContent) {
              setPlanPendingApproval(true);
              planContentRef.current = planContent;
              onPlanReadyRef.current?.(planContent);
            }
          }
        } else if (data.type === "mode_changed") {
          const msgConvId = (data as Record<string, unknown>)
            .conversation_id as string | undefined;
          // Only apply mode changes for the CURRENT conversation
          if (!msgConvId || msgConvId === conversationIdRef.current) {
            const rawMode = (data as Record<string, unknown>).mode as
              | string
              | undefined;
            const newMode = rawMode ? normalizeChatMode(rawMode) : undefined;
            const reason = (data as Record<string, unknown>).reason as
              | string
              | undefined;
            if (newMode) {
              lastServerModeTimestampRef.current = Date.now();
              // Clear plan state on approval — for rejection, the eager
              // clear in requestPlanChanges() already handled it, and
              // clearing here would race with a new plan_pending_approval
              // that may have arrived before this mode_changed.
              if (reason === "plan_approved") {
                setPlanPendingApproval(false);
                planContentRef.current = null;
              }
              if (
                reason === "plan_changes_requested" &&
                pendingPlanFeedbackRef.current
              ) {
                const feedback = pendingPlanFeedbackRef.current;
                pendingPlanFeedbackRef.current = null;
                setTimeout(() => {
                  sendMessageRef.current?.(feedback);
                }, 200);
              }
              // Only update mode and notify if it actually changed —
              // prevents set_mode → mode_changed → setState → set_mode loop
              if (newMode !== currentModeRef.current) {
                currentModeRef.current = newMode;
                onModeChangedRef.current?.(newMode);
              }
            }
          }
        } else if (data.type === "session_info") {
          const info = data as Record<string, unknown>;
          const ref = info.session_ref as string | undefined;
          if (ref) setSessionRef(ref);
          const dbSid = info.db_session_id as string | undefined;
          const infoConvId = info.conversation_id as string | undefined;
          if (
            dbSid &&
            (!infoConvId || infoConvId === conversationIdRef.current)
          ) {
            setDbSessionId(dbSid);
          }
          const branch = info.current_branch as string | undefined;
          if (branch !== undefined) setCurrentBranch(branch);
          const wtPath = info.worktree_path as string | undefined;
          if (wtPath !== undefined) setWorktreePath(wtPath);
          const agentName = info.agent_name as string | undefined;
          if (agentName) setActiveAgent(agentName);
        } else if (data.type === "worktree_switched") {
          const wt = data as Record<string, unknown>;
          setCurrentBranch((wt.new_branch as string) ?? null);
          setWorktreePath((wt.worktree_path as string) ?? null);
        } else if (data.type === "agent_changed") {
          const ac = data as Record<string, unknown>;
          const agentName = ac.agent_name as string | undefined;
          if (agentName) setActiveAgent(agentName);
        } else if (data.type === "session_continued") {
          const continued = data as Record<string, unknown>;
          clearContinuingSession();
          const nextConversationId =
            (continued.conversation_id as string | undefined) ?? null;
          const nextDbSessionId = (continued.db_session_id as string) ?? null;
          if (
            nextConversationId &&
            nextConversationId !== conversationIdRef.current
          ) {
            conversationIdRef.current = nextConversationId;
            setConversationId(nextConversationId);
            saveConversationId(nextConversationId);
          }
          setDbSessionId(nextDbSessionId);
          dbSessionIdRef.current = nextDbSessionId;
          saveDbSessionId(nextDbSessionId);
          const continuedMeta = toSessionObservationMeta(continued, {
            ref: (continued.ref as string | undefined) ?? sessionRefRef.current,
            status: (continued.status as string | undefined) ?? "active",
            sessionType:
              normalizeSessionType(continued.session_type) ?? "web_chat",
          });
          if (continuedMeta) {
            setMainSessionMeta(continuedMeta);
            setSessionTitle(continuedMeta.title ?? null);
            if (continuedMeta.ref) {
              setSessionRef(continuedMeta.ref);
            }
            setCurrentBranch(continuedMeta.gitBranch ?? null);
            if (continuedMeta.source && isChatProvider(continuedMeta.source)) {
              setSelectedProvider(continuedMeta.source);
            }
            if (continuedMeta.chatMode) {
              const restored = normalizeChatMode(continuedMeta.chatMode);
              currentModeRef.current = restored;
              onModeChangedRef.current?.(restored);
            }
          }
          if (nextDbSessionId) {
            const baseUrl = import.meta.env.VITE_API_BASE_URL || "";
            fetch(`${baseUrl}/api/sessions/${nextDbSessionId}`)
              .then((res) => (res.ok ? res.json() : null))
              .then((payload) => {
                const session = payload?.session;
                if (!session || dbSessionIdRef.current !== nextDbSessionId)
                  return;
                applyMainSessionMeta(session);
              })
              .catch(() => {});
          }
          clearContinuationRollback();
          const resumeNotice =
            typeof continued.resume_notice === "string"
              ? continued.resume_notice
              : null;
          if (resumeNotice) {
            setMessages((prev) => [
              ...prev,
              {
                id: `system-resume-notice-${uuid()}`,
                role: "system" as const,
                content: resumeNotice,
                timestamp: new Date(),
              },
            ]);
          }
          console.log("Session continued:", data);
        } else if (data.type === "error") {
          const err = data as Record<string, unknown>;
          if (continuingSessionIdRef.current) {
            const activeContinuationId = continuingSessionIdRef.current;
            clearContinuingSession();
            const rollback = continuationRollbackRef.current;
            if (rollback && rollback.sourceSessionId === activeContinuationId) {
              clearContinuationRollback();
              restoreContinuationState(rollback);
            }
          }
          const errorMessage =
            typeof err.message === "string" ? err.message : "Unknown error";
          setMessages((prev) => [
            ...prev,
            {
              id: `system-error-${uuid()}`,
              role: "system" as const,
              content: errorMessage,
              timestamp: new Date(),
            },
          ]);
        } else if (data.type === "connection_established") {
          const serverConversations = (data.conversation_ids as string[]) || [];
          if (serverConversations.includes(conversationIdRef.current)) {
            console.log(
              "Reconnected to existing conversation:",
              conversationIdRef.current,
            );
          }
          console.log("Connection established:", data);
        } else if (data.type === "canvas_event") {
          const ev = data as any;
          if (ev.event === "surface_update") {
            setCanvasSurfaces((prev: Map<string, A2UISurfaceState>) => {
              const next = new Map(prev);
              next.set(ev.canvas_id, {
                canvasId: ev.canvas_id,
                conversationId: ev.conversation_id,
                mode: ev.mode,
                surface: ev.surface,
                dataModel: ev.data_model,
                rootComponentId: ev.root_component_id,
                completed: ev.completed,
              });
              return next;
            });
          } else if (
            ev.event === "interaction_confirmed" ||
            ev.event === "close_canvas"
          ) {
            setCanvasSurfaces((prev: Map<string, A2UISurfaceState>) => {
              const next = new Map(prev);
              const s = next.get(ev.canvas_id);
              if (s) {
                next.set(ev.canvas_id, { ...s, completed: true });
              }
              return next;
            });
            if (ev.event === "close_canvas") {
              setCanvasPanel((prev) =>
                prev?.canvasId === ev.canvas_id ? null : prev,
              );
            }
          } else if (ev.event === "panel_present") {
            setCanvasPanel((prev: CanvasPanelState | null) => ({
              ...prev,
              canvasId: ev.canvas_id,
              title: ev.title,
              url: ev.html_url,
              width: ev.width || prev?.width,
              height: ev.height || prev?.height,
            }));
          } else if (ev.event === "canvas_rehydrate") {
            setCanvasSurfaces((prev: Map<string, A2UISurfaceState>) => {
              const next = new Map(prev);
              for (const s of ev.surfaces || []) {
                if (s.mode === "a2ui") {
                  next.set(s.canvas_id, {
                    canvasId: s.canvas_id,
                    conversationId: s.conversation_id,
                    mode: s.mode,
                    surface: s.surface,
                    dataModel: s.data_model,
                    rootComponentId: s.root_component_id,
                    completed: s.completed,
                  });
                } else if (s.mode === "html" && !s.completed) {
                  setCanvasPanel({
                    canvasId: s.canvas_id,
                    title: s.title,
                    url: s.html_url,
                  });
                }
              }
              return next;
            });
          }
        } else if (data.type === "artifact_event") {
          const ev = data as any;
          if (ev.event === "show_file") {
            onArtifactEventRef.current?.(
              ev.artifact_type,
              ev.content,
              ev.language,
              ev.title,
            );
          }
        } else if (data.type === "attach_to_session_result") {
          const result = data as Record<string, unknown>;
          const sid = result.session_id as string;
          const meta =
            toSessionObservationMeta(result) ??
            ({
              ref: null,
              source: "unknown",
              title: null,
              status: "unknown",
              canProxyAttach: false,
              model: null,
              externalId: "",
              chatMode: null,
              gitBranch: null,
              contextWindow: null,
              agentRunId: null,
              workflowName: null,
              agentName: null,
              sessionType: null,
            } satisfies SessionObservationMeta);
          setObservedSessionId(sid);
          observedSessionIdRef.current = sid;
          observedSessionMetaRef.current = meta;
          // Also set viewing state (attached implies viewing)
          setViewingSessionId(sid);
          viewingSessionIdRef.current = sid;
          setViewingSessionMeta(meta);
          viewingSessionMetaRef.current = meta;
          const requestedMode = pendingSessionInteractionModeRef.current;
          const proxyCapable = canProxyAttachObservationMeta(meta);
          const nextMode =
            requestedMode === "proxy" && !proxyCapable ? "none" : requestedMode;
          clearPendingProxyMessages(
            pendingProxyMessagesRef.current,
            pendingProxySessionQueuesRef.current,
          );
          setSessionInteractionMode(nextMode);
          sessionInteractionModeRef.current = nextMode;
          if (nextMode === "proxy") {
            setAttachedSessionId(sid);
            attachedSessionIdRef.current = sid;
            setAttachedSessionMeta(meta);
            attachedSessionMetaRef.current = meta;
            setProxyDeliveryNotice(null);
            if (
              meta.chatMode === "act" ||
              meta.chatMode === "accept_edits" ||
              meta.chatMode === "bypass" ||
              meta.chatMode === "normal" ||
              meta.chatMode === "plan"
            ) {
              const restored = normalizeChatMode(meta.chatMode);
              if (restored !== currentModeRef.current) {
                currentModeRef.current = restored;
                onModeChangedRef.current?.(restored);
              }
            }
          } else {
            setAttachedSessionId(null);
            attachedSessionIdRef.current = null;
            setAttachedSessionMeta(null);
            attachedSessionMetaRef.current = null;
            setProxyDeliveryNotice(
              requestedMode === "proxy" && meta.sessionType === "terminal"
                ? meta.status === "paused"
                  ? "This terminal session is paused. Use Resume Session to continue it in web chat."
                  : "This terminal session can only be resumed in web chat right now."
                : null,
            );
          }
          // Map initial messages into chat format with proper tool call grouping
          const msgs = (result.messages as ApiMessage[]) || [];
          const mapped = mapApiMessages(msgs);
          // Preserve REST-loaded transcript when re-attaching to viewed session
          if (
            viewingSessionIdRef.current === sid &&
            messagesRef.current.length > 0
          ) {
            const mappedById = new Map(mapped.map((m) => [m.id, m]));
            // Merge updates into existing messages, then append truly new ones
            const existingIds = new Set(messagesRef.current.map((m) => m.id));
            const merged = messagesRef.current.map(
              (m) => mappedById.get(m.id) ?? m,
            );
            const newMsgs = mapped.filter((m) => !existingIds.has(m.id));
            if (newMsgs.length > 0 || mappedById.size > 0) {
              setMessages([...merged, ...newMsgs]);
            }
          } else {
            setMessages(mapped);
          }
          setIsStreaming(false);
          setIsThinking(false);
          setSessionRef((result.ref as string) ?? null);
          if (hasSessionUsage(result)) {
            setContextUsage(computeContextUsageFromSessionData(result));
          }
          // Do NOT set dbSessionId here. Under the unified session identity
          // model, dbSessionId mirrors the user's main chat conversation id,
          // not an observed/attached session. Observed state lives on
          // observedSessionIdRef / viewingSessionIdRef / attachedSessionIdRef.
          // Overwriting dbSessionId here would diverge it from conversationId
          // and trap sendMessage in an infinite ensureMainSession retry loop.
          if (!meta.agentName && meta.agentRunId) {
            void resolveAgentName(meta.agentRunId).then((agentName) => {
              if (!agentName || viewingSessionIdRef.current !== sid) return;
              observedSessionMetaRef.current = {
                ...(observedSessionMetaRef.current ?? meta),
                agentName,
              };
              setViewingSessionMeta((prev) =>
                prev && viewingSessionIdRef.current === sid
                  ? { ...prev, agentName }
                  : prev,
              );
              setAttachedSessionMeta((prev) =>
                prev && attachedSessionIdRef.current === sid
                  ? { ...prev, agentName }
                  : prev,
              );
            });
          }
        } else if (data.type === "detach_from_session_result") {
          const sid =
            typeof (data as Record<string, unknown>).session_id === "string"
              ? ((data as Record<string, unknown>).session_id as string)
              : null;
          if (sid) {
            const isCurrentObserved = observedSessionIdRef.current === sid;
            const isCurrentAttached = attachedSessionIdRef.current === sid;
            const isCurrentViewedTerminal =
              viewingSessionIdRef.current === sid &&
              viewingSessionMetaRef.current?.sessionType === "terminal";
            if (
              !isCurrentObserved &&
              !isCurrentAttached &&
              !isCurrentViewedTerminal
            ) {
              return;
            }
          }
          setObservedSessionId(null);
          observedSessionMetaRef.current = null;
          setAttachedSessionId(null);
          attachedSessionIdRef.current = null;
          setAttachedSessionMeta(null);
          attachedSessionMetaRef.current = null;
          clearPendingProxyMessages(
            pendingProxyMessagesRef.current,
            pendingProxySessionQueuesRef.current,
          );
          setProxyDeliveryNotice(null);
          setSessionInteractionMode("none");
          sessionInteractionModeRef.current = "none";
          // Restore main-chat contextUsage snapshot taken at first attach,
          // so the pie stops showing the observed session's percentages.
          if (preAttachContextUsageRef.current !== null) {
            setContextUsage(preAttachContextUsageRef.current);
            preAttachContextUsageRef.current = null;
          } else {
            setContextUsage({
              totalInputTokens: 0,
              outputTokens: 0,
              contextWindow: null,
              uncachedInputTokens: 0,
              cacheReadTokens: 0,
              cacheCreationTokens: 0,
            });
          }
          // Keep viewingSessionId/Meta — return to view-only mode
        } else if (
          data.type === "session_message" &&
          (data as Record<string, unknown>).session_id
        ) {
          const sm = data as Record<string, unknown>;
          const smSessionId = sm.session_id as string;
          const isObservedSession =
            smSessionId &&
            (smSessionId === observedSessionIdRef.current ||
              smSessionId === viewingSessionIdRef.current);
          if (!isObservedSession) {
            return;
          }

          const msg = sm.message as Record<string, unknown> | undefined;
          if (!msg) {
            return;
          }

          const renderedMessage = mapRenderedMessageToChatMessage(msg);
          const pendingProxyMessage =
            renderedMessage.role === "user" &&
            smSessionId === attachedSessionIdRef.current &&
            sessionInteractionModeRef.current === "proxy"
              ? consumePendingProxyMessage(
                  pendingProxyMessagesRef.current,
                  pendingProxySessionQueuesRef.current,
                  smSessionId,
                )
              : null;
          setMessages((prev) => {
            const existingIdx = prev.findIndex(
              (message) => message.id === renderedMessage.id,
            );
            if (existingIdx >= 0) {
              const updated = [...prev];
              updated[existingIdx] = renderedMessage;
              return updated;
            }
            if (pendingProxyMessage) {
              const pendingIdx = prev.findIndex(
                (message) => message.id === pendingProxyMessage.currentMessageId,
              );
              if (pendingIdx >= 0) {
                const updated = [...prev];
                updated[pendingIdx] = renderedMessage;
                return updated;
              }
            }
            if (
              renderedMessage.role === "user" &&
              smSessionId === attachedSessionIdRef.current &&
              sessionInteractionModeRef.current === "proxy"
            ) {
              const optimisticIdx = prev.findIndex(
                (message) =>
                  message.role === "user" &&
                  message.id.startsWith("user-") &&
                  message.content === renderedMessage.content,
              );
              if (optimisticIdx >= 0) {
                const updated = [...prev];
                updated[optimisticIdx] = renderedMessage;
                return updated;
              }
            }
            return [...prev, renderedMessage];
          });
          if (pendingProxyMessage) {
            pendingProxyMessage.currentMessageId = renderedMessage.id;
          }
        } else if (data.type === "send_to_cli_session_result") {
          const result = data as Record<string, unknown>;
          const clientMessageId =
            typeof result.client_message_id === "string"
              ? result.client_message_id
              : null;
          const messageId =
            typeof result.message_id === "string" ? result.message_id : null;
          if (clientMessageId) {
            const pendingProxyMessage =
              pendingProxyMessagesRef.current.get(clientMessageId) ?? null;
            if (pendingProxyMessage) {
              if (messageId && result.delivered !== false) {
                setMessages((prev) => {
                  const messageIdx = prev.findIndex(
                    (message) =>
                      message.id === pendingProxyMessage.currentMessageId ||
                      message.id === messageId,
                  );
                  if (messageIdx < 0) {
                    return prev;
                  }
                  const updated = [...prev];
                  updated[messageIdx] = {
                    ...updated[messageIdx],
                    id: messageId,
                  };
                  return updated;
                });
                pendingProxyMessage.currentMessageId = messageId;
                removePendingProxyMessageFromQueue(
                  pendingProxySessionQueuesRef.current,
                  pendingProxyMessage.sessionId,
                  clientMessageId,
                );
                pendingProxyMessagesRef.current.delete(clientMessageId);
              }
            }
          }
          setProxyDeliveryNotice(
            result.delivered === false
              ? "Message queued until the session yields."
              : null,
          );
          console.log("Message sent to CLI session:", result.delivery_method);
        } else if (data.type === "session_usage_updated") {
          const update = data as unknown as SessionUsageUpdatedMessage;
          const visibleSessionId =
            viewingSessionIdRef.current ?? dbSessionIdRef.current;
          if (update.session_id === visibleSessionId) {
            markSessionUsageFresh(update.session_id, update.updated_at);
            setContextUsage((prev) =>
              buildContextUsageFromTotals({
                totalInputTokens:
                  update.usage_input_tokens ?? prev.totalInputTokens,
                outputTokens: update.usage_output_tokens ?? prev.outputTokens,
                cacheReadTokens:
                  update.usage_cache_read_tokens ?? prev.cacheReadTokens,
                cacheCreationTokens:
                  update.usage_cache_creation_tokens ??
                  prev.cacheCreationTokens,
                contextWindow:
                  typeof update.context_window === "number"
                    ? update.context_window
                    : prev.contextWindow,
              }),
            );
          }
          if (viewingSessionIdRef.current === update.session_id) {
            setViewingSessionMeta((prev) =>
              prev
                ? {
                    ...prev,
                    model:
                      typeof update.model === "string" ? update.model : prev.model,
                    contextWindow:
                      typeof update.context_window === "number"
                        ? update.context_window
                        : prev.contextWindow,
                  }
                : prev,
            );
          } else if (dbSessionIdRef.current === update.session_id) {
            setMainSessionMeta((prev) =>
              prev
                ? {
                    ...prev,
                    model:
                      typeof update.model === "string" ? update.model : prev.model,
                    contextWindow:
                      typeof update.context_window === "number"
                        ? update.context_window
                        : prev.contextWindow,
                  }
                : prev,
            );
          }
        } else if (data.type === "token_event") {
          const eventData = data as unknown as TokenEventMessage;
          const visibleSessionId =
            viewingSessionIdRef.current ?? dbSessionIdRef.current;
          const sessionTotals = eventData.session_totals;
          if (
            eventData.session_id === visibleSessionId &&
            sessionTotals
          ) {
            markSessionUsageFresh(eventData.session_id, eventData.event_at);
            setContextUsage((prev) =>
              buildContextUsageFromTotals({
                totalInputTokens:
                  sessionTotals.input_tokens ?? prev.totalInputTokens,
                outputTokens:
                  sessionTotals.output_tokens ?? prev.outputTokens,
                cacheReadTokens:
                  sessionTotals.cache_read_tokens ?? prev.cacheReadTokens,
                cacheCreationTokens:
                  sessionTotals.cache_creation_tokens ??
                  prev.cacheCreationTokens,
                contextWindow:
                  typeof eventData.context_window === "number"
                    ? eventData.context_window
                    : prev.contextWindow,
              }),
            );
          }
        } else if (data.type === "subscribe_success") {
          console.log("Subscribed to events:", data);
        } else if (data.type === "chat_deleted") {
          const cid = (data as Record<string, unknown>)
            .conversation_id as string;
          console.log("Chat deleted confirmed:", cid);
          onChatDeletedRef.current?.(cid);
        } else if (data.type === "chat_cleared") {
          const cid = (data as Record<string, unknown>)
            .conversation_id as string;
          console.log("Chat cleared confirmed:", cid);
          onChatClearedRef.current?.(cid);
        }
      } catch (e) {
        console.error("Failed to parse WebSocket message:", e);
      }
    };
    // resolveAgentName is a stable useCallback (its own deps are []) — safe
    // to reference here without re-creating connect every render.
  }, [
    applyMainSessionMeta,
    clearContinuingSession,
    clearContinuationRollback,
    markSessionUsageFresh,
    resolveAgentName,
    restoreContinuationState,
    setSelectedProvider,
  ]);

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
          result: status.result,
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
          result: status.result,
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
          result: status.result,
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
        // New tool call — append to last tool_chain or create new one
        const lastBlock = blocks[blocks.length - 1];
        if (lastBlock?.type === "tool_chain") {
          blocks[blocks.length - 1] = {
            ...lastBlock,
            tool_calls: [...lastBlock.tool_calls, callRef],
          };
        } else {
          blocks.push({ type: "tool_chain" as const, tool_calls: [callRef] });
        }
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
        updated[existingIndex] = {
          ...updated[existingIndex],
          thinkingContent:
            (updated[existingIndex].thinkingContent || "") +
            (msg.content || ""),
        };
        return updated;
      } else {
        return [
          ...prev,
          {
            id: msg.message_id,
            role: "assistant" as const,
            content: "",
            timestamp: new Date(),
            thinkingContent: msg.content || "",
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
    saveViewingSessionMode(
      viewingSessionId && sessionInteractionMode === "observe"
        ? "observe"
        : "none",
    );
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

  // Switch to an existing server-owned web-chat session by DB session ID.
  const switchConversation = useCallback(
    (id: string, options?: { preserveViewing?: boolean }) => {
      if (!id) return;
      const preserveViewing = options?.preserveViewing ?? false;
      if (
        id === dbSessionIdRef.current &&
        !preserveViewing &&
        messagesRef.current.length > 0
      ) {
        return;
      }

      if (!preserveViewing) {
        clearPreAttachContextUsage();
        clearSessionObservationState();
        resetMainChatState();
      }
      bindActiveSession(id);
      setConversationSwitchKey((k) => k + 1);

      const baseUrl = import.meta.env.VITE_API_BASE_URL || "";
      if (!preserveViewing) {
        setIsLoadingMessages(true);
        fetch(`${baseUrl}/api/chat/${id}/messages?limit=100&after_seq=0`)
          .then((res) => (res.ok ? res.json() : null))
          .then((data) => {
            if (viewingSessionIdRef.current) return;
            if (!data?.messages?.length || conversationIdRef.current !== id)
              return;
            const mapped = data.messages.map((m: Record<string, unknown>) =>
              mapRenderedMessageToChatMessage(m),
            );
            if (mapped.length > 0) {
              setMessages(mapped);
            }
            if (data.max_seq) {
              lastSeqRef.current = data.max_seq as number;
            }
          })
          .catch((err) => console.error("Failed to fetch chat messages:", err))
          .finally(() => setIsLoadingMessages(false));
      }

      fetch(`${baseUrl}/api/sessions/${id}`)
        .then((res) => (res.ok ? res.json() : null))
        .then((data) => {
          const s = data?.session;
          if (!s || conversationIdRef.current !== id) return;
          applyMainSessionMeta(s);
          if (s.chat_mode) {
            const restored = normalizeChatMode(s.chat_mode);
            if (
              wsRef.current?.readyState === WebSocket.OPEN &&
              Date.now() - lastServerModeTimestampRef.current > 2000
            ) {
              wsRef.current.send(
                JSON.stringify({
                  type: "set_mode",
                  mode: restored,
                  conversation_id: id,
                }),
              );
            }
          }
        })
        .catch(() => {});
    },
    [
      applyMainSessionMeta,
      bindActiveSession,
      clearPreAttachContextUsage,
      clearSessionObservationState,
      resetMainChatState,
    ],
  );

  // Start a new chat conversation, optionally with a specific agent
  const startNewChat = useCallback(
    (agentName?: string) => {
      const effectiveAgent = agentName || "default";
      setActiveAgent(effectiveAgent);
      clearPreAttachContextUsage();
      clearSessionObservationState();
      resetMainChatState();
      bindActiveSession(null);
      setConversationSwitchKey((k) => k + 1);
    },
    [
      bindActiveSession,
      clearPreAttachContextUsage,
      clearSessionObservationState,
      resetMainChatState,
    ],
  );

  // Switch provider. Existing conversations fork to a new server-owned session;
  // a blank draft stays local until the first user send.
  const switchProvider = useCallback(
    (
      newProvider: string,
      options?: { model?: string | null; reasoningEffort?: string | null },
    ) => {
      if (isStreaming && wsRef.current?.readyState === WebSocket.OPEN) {
        wsRef.current.send(
          JSON.stringify({
            type: "stop_chat",
            conversation_id: conversationIdRef.current,
          }),
        );
      }
      clearPreAttachContextUsage();
      clearSessionObservationState();
      resetMainChatState();
      bindActiveSession(null);
      setConversationSwitchKey((k) => k + 1);

      // Keep fresh-chat provider changes local until the first user send
      // actually creates the backing web chat session.
      if (!dbSessionIdRef.current && messagesRef.current.length === 0) {
        setSelectedProvider(newProvider);
        return;
      }

      void ensureMainSession({
        projectId: projectIdRef.current,
        provider: newProvider,
        model: options?.model ?? null,
        reasoningEffort: options?.reasoningEffort ?? null,
        forceNew: true,
      });
    },
    [
      bindActiveSession,
      clearPreAttachContextUsage,
      clearSessionObservationState,
      ensureMainSession,
      isStreaming,
      resetMainChatState,
      setSelectedProvider,
    ],
  );

  // Resume a CLI session (e.g., Claude) — sets the conversation ID
  // so the next message triggers server-side resume
  const resumeSession = useCallback((externalId: string) => {
    lastSeqRef.current = 0;
    conversationIdRef.current = externalId;
    setConversationId(externalId);
    setConversationSwitchKey((k) => k + 1);
    saveConversationId(externalId);

    setMessages([
      {
        id: `system-resume-${uuid()}`,
        role: "system" as const,
        content: "Resuming session. Send a message to continue.",
        timestamp: new Date(),
      },
    ]);

    activeRequestIdRef.current = null;
    setIsStreaming(false);
    setIsThinking(false);
  }, []);

  // Continue a CLI/external session in the web chat UI with full history
  const continueSessionInChat = useCallback(
    async (
      sourceDbSessionId: string,
      projectId?: string,
      options?: {
        provider?: string | null;
        model?: string | null;
        reasoningEffort?: string | null;
        chatMode?: string | null;
        fallbackContext?: FallbackContextMode;
      },
    ): Promise<string> => {
      const reasoningEffort = normalizeReasoningEffort(
        options?.reasoningEffort ?? null,
      );
      const fallbackContext = options?.fallbackContext ?? null;
      if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) {
        return "";
      }
      if (continuingSessionIdRef.current) {
        return "";
      }

      continuationRollbackRef.current = {
        sourceSessionId: sourceDbSessionId,
        conversationId,
        dbSessionId,
        mainSessionMeta,
        sessionTitle,
        sessionRef,
        selectedProvider,
        messages,
        contextUsage,
        currentMode: currentModeRef.current,
        currentBranch,
        worktreePath,
        viewingSessionId,
        viewingSessionMeta,
        observedSessionId,
        observedSessionMeta: observedSessionMetaRef.current,
        attachedSessionId,
        attachedSessionMeta,
        sessionInteractionMode,
        proxyDeliveryNotice,
      };

      clearPreAttachContextUsage();
      clearSessionObservationState();
      resetMainChatState();
      bindActiveSession(sourceDbSessionId);
      setConversationSwitchKey((k) => k + 1);
      continuingSessionIdRef.current = sourceDbSessionId;
      setIsContinuingSession(true);

      const baseUrl = import.meta.env.VITE_API_BASE_URL || "";
      let sourceSession: Record<string, unknown> | null = null;
      try {
        const sessionRes = await fetch(
          `${baseUrl}/api/sessions/${sourceDbSessionId}`,
        );
        if (sessionRes.ok) {
          const sessionData = await sessionRes.json();
          sourceSession =
            (sessionData?.session as Record<string, unknown>) ?? null;
        }
      } catch {
        sourceSession = null;
      }

      const continuationProvider =
        options?.provider ??
        (isChatProvider(sourceSession?.source) ? sourceSession.source : null) ??
        selectedProviderRef.current;
      const continuationModel =
        options?.model ??
        (typeof sourceSession?.model === "string" ? sourceSession.model : null);
      const continuationChatMode =
        typeof options?.chatMode === "string" && options.chatMode
          ? normalizeChatMode(options.chatMode)
          : null;
      // Propagate the source session's chat mode into the new continuation
      // session BEFORE calling ensureMainSession — otherwise the server-side
      // session is created with whatever local mode happened to be active.
      const sourceChatMode =
        continuationChatMode ??
        (typeof sourceSession?.chat_mode === "string"
          ? normalizeChatMode(sourceSession.chat_mode)
          : null);

      applyMainSessionMeta(sourceSession);
      if (continuationProvider) {
        setSelectedProvider(continuationProvider);
      }
      if (sourceChatMode) {
        currentModeRef.current = sourceChatMode;
        onModeChangedRef.current?.(sourceChatMode);
      }

      // Fetch source session's messages for display
      try {
        const res = await fetch(
          `${baseUrl}/api/sessions/${sourceDbSessionId}/messages?limit=100`,
        );
        if (res.ok) {
          const data = await res.json();
          const mapped = mapApiMessages(data.messages || []);
          if (mapped.length > 0) {
            setMessages(mapped);
          }
        }
      } catch (err) {
        console.error("Failed to fetch source session messages:", err);
      }

      // Hydrate context usage and chat mode from source session
      const s = sourceSession;
      if (hasSessionUsage(s)) {
        setContextUsage(computeContextUsageFromSessionData(s));
      }
      // chat_mode was already propagated above before the backend handoff.

      // Tell backend to prepare the continuation session
      wsRef.current.send(
        JSON.stringify({
          type: "continue_in_chat",
          conversation_id: sourceDbSessionId,
          source_session_id: sourceDbSessionId,
          project_id:
            projectId ??
            (typeof sourceSession?.project_id === "string"
              ? sourceSession.project_id
              : undefined),
          provider: continuationProvider,
          model: continuationModel,
          reasoning_effort: reasoningEffort,
          chat_mode: sourceChatMode,
          fallback_context: fallbackContext,
        }),
      );

      return sourceDbSessionId;
    },
    [
      applyMainSessionMeta,
      attachedSessionId,
      attachedSessionMeta,
      bindActiveSession,
      clearPreAttachContextUsage,
      clearSessionObservationState,
      contextUsage,
      conversationId,
      currentBranch,
      dbSessionId,
      mainSessionMeta,
      messages,
      observedSessionId,
      proxyDeliveryNotice,
      resetMainChatState,
      selectedProvider,
      setContextUsage,
      setSelectedProvider,
      sessionInteractionMode,
      sessionRef,
      sessionTitle,
      viewingSessionId,
      viewingSessionMeta,
      worktreePath,
    ],
  );

  // Clear chat history — notifies backend to teardown session, then resets frontend.
  // Returns false if WS send failed (caller can show error).
  const clearHistory = useCallback((): boolean => {
    const oldConversationId = conversationIdRef.current;
    // Notify backend to generate summary + teardown session
    if (wsRef.current?.readyState !== WebSocket.OPEN) {
      return false;
    }
    wsRef.current.send(
      JSON.stringify({
        type: "clear_chat",
        conversation_id: oldConversationId,
      }),
    );
    startNewChat();
    return true;
  }, [startNewChat]);

  // Delete a conversation — sends WS message, returns true if sent.
  // Caller is responsible for UI updates (via onChatDeleted callback).
  const deleteConversation = useCallback(
    (id: string, sessionId?: string): boolean => {
      if (wsRef.current?.readyState !== WebSocket.OPEN) {
        return false;
      }
      const payload: Record<string, unknown> = {
        type: "delete_chat",
        conversation_id: id,
      };
      if (sessionId !== undefined) {
        payload.session_id = sessionId;
      }
      wsRef.current.send(JSON.stringify(payload));

      if (id === conversationIdRef.current) {
        startNewChat();
      }
      return true;
    },
    [startNewChat],
  );

  // Stop the current streaming response
  const stopStreaming = useCallback(() => {
    if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) return;
    if (!conversationIdRef.current) return;
    wsRef.current.send(
      JSON.stringify({
        type: "stop_chat",
        conversation_id: conversationIdRef.current,
      }),
    );
    activeRequestIdRef.current = null;
    setIsStreaming(false);
    setIsThinking(false);
  }, []);

  // Send mode change to backend
  const sendMode = useCallback((mode: ChatMode) => {
    const normalizedMode = normalizeChatMode(mode);
    currentModeRef.current = normalizedMode; // Always track latest intended mode
    if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) return;
    if (!conversationIdRef.current) return;
    setPlanPendingApproval(false);
    wsRef.current.send(
      JSON.stringify({
        type: "set_mode",
        mode: normalizedMode,
        conversation_id: conversationIdRef.current,
      }),
    );
  }, []);

  // Notify backend that the project changed — stops the CLI subprocess
  // so the next chat_message recreates it with the correct CWD.
  const sendProjectChange = useCallback((projectId: string) => {
    if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) return;
    if (!conversationIdRef.current) return;
    wsRef.current.send(
      JSON.stringify({
        type: "set_project",
        project_id: projectId,
        conversation_id: conversationIdRef.current,
      }),
    );
  }, []);

  // Notify backend that the agent changed — stops the CLI subprocess
  // so the next chat_message recreates it with the new agent context.
  const sendAgentChange = useCallback((agentName: string) => {
    if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) return;
    if (!conversationIdRef.current) return;
    setActiveAgent(agentName);
    wsRef.current.send(
      JSON.stringify({
        type: "set_agent",
        agent_name: agentName,
        conversation_id: conversationIdRef.current,
      }),
    );
  }, []);

  // Notify backend that the worktree changed — stops the CLI subprocess
  // so the next chat_message recreates it with the correct CWD.
  const sendWorktreeChange = useCallback(
    (worktreePath: string, worktreeId?: string) => {
      if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) return;
      if (!conversationIdRef.current) return;
      wsRef.current.send(
        JSON.stringify({
          type: "set_worktree",
          worktree_path: worktreePath,
          worktree_id: worktreeId,
          conversation_id: conversationIdRef.current,
        }),
      );
    },
    [],
  );

  // Send a message (allowed even while streaming — cancels the active stream)
  const sendMessage = useCallback(
    (
      content: string,
      model?: string | null,
      files?: QueuedFile[],
      projectId?: string | null,
      injectContext?: string,
      reasoningEffort?: string | null,
    ): boolean => {
      console.log(
        "sendMessage called:",
        content,
        "model:",
        model,
        "files:",
        files?.length,
      );
      const normalizedReasoningEffort = normalizeReasoningEffort(reasoningEffort);

      if (continuingSessionIdRef.current) {
        return false;
      }

      const needsSession =
        !conversationIdRef.current || !dbSessionIdRef.current;
      const isProxyTerminal =
        attachedSessionIdRef.current &&
        sessionInteractionModeRef.current === "proxy" &&
        attachedSessionMetaRef.current?.sessionType === "terminal";

      if (needsSession && !isProxyTerminal) {
        void ensureMainSession({
          projectId: projectId ?? projectIdRef.current,
          provider: selectedProviderRef.current,
          model: model ?? null,
          reasoningEffort: normalizedReasoningEffort,
        }).then((sessionId) => {
          if (!sessionId) return;
          sendMessageRef.current?.(
            content,
            model,
            files,
            projectId,
            injectContext,
            normalizedReasoningEffort,
          );
        });
        return true;
      }

      if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) {
        // Queue the message to send on reconnect
        console.warn("WebSocket disconnected — queuing message for reconnect");
        pendingMessagesRef.current.push({
          content,
          model,
          projectId,
          reasoningEffort: normalizedReasoningEffort,
        });
        // Still add the user message to the UI so it's visible
        const queuedId = `user-${uuid()}`;
        setMessages((prev) => [
          ...prev,
          {
            id: queuedId,
            role: "user" as const,
            content,
            toolCalls: [],
            timestamp: new Date(),
          },
        ]);
        return true;
      }

      // Route to a swapped terminal session when proxy mode is active.
      if (isProxyTerminal) {
        const proxySessionId = attachedSessionIdRef.current;
        if (!proxySessionId) {
          return false;
        }
        const clientMessageId = uuid();
        const messageId = `user-${clientMessageId}`;
        const pendingProxyMessage = {
          clientMessageId,
          currentMessageId: messageId,
          sessionId: proxySessionId,
        };
        pendingProxyMessagesRef.current.set(clientMessageId, pendingProxyMessage);
        enqueuePendingProxyMessage(
          pendingProxySessionQueuesRef.current,
          pendingProxyMessage,
        );
        setMessages((prev) => [
          ...prev,
          {
            id: messageId,
            role: "user",
            content,
            timestamp: new Date(),
          },
        ]);
        setProxyDeliveryNotice(null);
        wsRef.current.send(
          JSON.stringify({
            type: "send_to_cli_session",
            session_id: proxySessionId,
            content,
            client_message_id: clientMessageId,
          }),
        );
        return true;
      }

      const messageId = `user-${uuid()}`;
      const requestId = uuid();
      activeRequestIdRef.current = requestId;

      setMessages((prev) => [
        ...prev,
        {
          id: messageId,
          role: "user",
          content,
          timestamp: new Date(),
        },
      ]);

      saveConversationId(conversationIdRef.current);

      const payload: Record<string, unknown> = {
        type: "chat_message",
        content,
        message_id: messageId,
        conversation_id: conversationIdRef.current,
        request_id: requestId,
      };

      if (model) {
        payload.model = model;
      }

      if (projectId) {
        payload.project_id = projectId;
      }

      if (injectContext) {
        payload.inject_context = injectContext;
      }

      if (normalizedReasoningEffort) {
        payload.reasoning_effort = normalizedReasoningEffort;
      }

      if (selectedProviderRef.current) {
        payload.provider = selectedProviderRef.current;
      }

      if (files && files.length > 0) {
        const contentBlocks: Array<Record<string, unknown>> = [];
        for (const qf of files) {
          if (qf.file.type.startsWith("image/") && qf.base64) {
            contentBlocks.push({
              type: "image",
              source: {
                type: "base64",
                media_type: qf.file.type,
                data: qf.base64,
              },
            });
          } else if (qf.base64) {
            contentBlocks.push({
              type: "text",
              text: `[File: ${qf.file.name}]\n${atob(qf.base64)}`,
            });
          }
        }
        if (content) {
          contentBlocks.push({ type: "text", text: content });
        }
        payload.content_blocks = contentBlocks;
      }

      console.log("Sending WebSocket message:", payload);
      wsRef.current.send(JSON.stringify(payload));

      setIsStreaming(true);
      setIsThinking(true);
      return true;
    },
    [ensureMainSession],
  );

  // Update sendMessageRef with the latest sendMessage callback
  useEffect(() => {
    sendMessageRef.current = sendMessage;
  }, [sendMessage]);

  // Respond to an AskUserQuestion pending in the backend.
  // Returns false if WS is not connected (caller can show feedback).
  const respondToQuestion = useCallback(
    (toolCallId: string, answers: Record<string, string>): boolean => {
      if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN)
        return false;
      wsRef.current.send(
        JSON.stringify({
          type: "ask_user_response",
          conversation_id: conversationIdRef.current,
          tool_call_id: toolCallId,
          answers,
        }),
      );
      return true;
    },
    [],
  );

  // Respond to a tool approval request.
  // Returns false if WS is not connected (caller can show feedback).
  const respondToApproval = useCallback(
    (
      toolCallId: string,
      decision: "approve" | "reject" | "approve_always",
    ): boolean => {
      if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN)
        return false;
      wsRef.current.send(
        JSON.stringify({
          type: "tool_approval_response",
          conversation_id: conversationIdRef.current,
          tool_call_id: toolCallId,
          decision,
        }),
      );
      return true;
    },
    [],
  );

  // Respond to a Canvas surface interaction
  const respondToCanvas = useCallback(
    (canvasId: string, action: UserAction) => {
      if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) return;
      wsRef.current.send(
        JSON.stringify({
          type: "canvas_interaction",
          conversation_id: conversationIdRef.current,
          canvas_id: canvasId,
          action,
        }),
      );
    },
    [],
  );

  const pendingPlanFeedbackRef = useRef<string | null>(null);

  // Approve the current plan. The backend's mode_changed event is authoritative.
  const approvePlan = useCallback(() => {
    if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) return;
    if (!conversationIdRef.current) return;
    if (!planContentRef.current) return;
    wsRef.current.send(
      JSON.stringify({
        type: "plan_approval_response",
        conversation_id: conversationIdRef.current,
        decision: "approve",
      }),
    );
  }, []);

  // Request changes to the plan with feedback
  const requestPlanChanges = useCallback((feedback: string) => {
    if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) return;
    if (!conversationIdRef.current) return;
    if (!planContentRef.current) return;
    pendingPlanFeedbackRef.current = feedback;
    // Eagerly clear approval UI to prevent ghost flash when artifact panel closes
    setPlanPendingApproval(false);
    planContentRef.current = null;
    wsRef.current.send(
      JSON.stringify({
        type: "plan_approval_response",
        conversation_id: conversationIdRef.current,
        decision: "request_changes",
        feedback,
      }),
    );
  }, []);

  // View a CLI session (read-only, no WS subscription — loads via REST)
  const viewSession = useCallback(
    (sessionId: string) => {
      // Skip if already viewing/attached to this session
      if (
        (viewingSessionIdRef.current === sessionId &&
          (viewingSessionMetaRef.current || messagesRef.current.length > 0)) ||
        observedSessionIdRef.current === sessionId
      ) {
        return;
      }

      // Detach from any active WS subscription first
      const observedSessionId = observedSessionIdRef.current;
      if (observedSessionId) {
        if (wsRef.current?.readyState === WebSocket.OPEN) {
          wsRef.current.send(
            JSON.stringify({
              type: "detach_from_session",
              session_id: observedSessionId,
            }),
          );
        }
        observedSessionIdRef.current = null;
        setObservedSessionId(null);
        observedSessionMetaRef.current = null;
        attachedSessionIdRef.current = null;
        setAttachedSessionId(null);
        attachedSessionMetaRef.current = null;
        setAttachedSessionMeta(null);
        clearPendingProxyMessages(
          pendingProxyMessagesRef.current,
          pendingProxySessionQueuesRef.current,
        );
        sessionInteractionModeRef.current = "none";
        setProxyDeliveryNotice(null);
        setSessionInteractionMode("none");
      }

      // Reset chat state
      lastSeqRef.current = 0;
      activeRequestIdRef.current = null;
      setIsStreaming(false);
      setIsThinking(false);
      setMessages([]);
      setIsLoadingMessages(true);
      setProxyDeliveryNotice(null);
      setContextUsage(buildContextUsageFromTotals({}));

      // Set viewing state
      viewingSessionIdRef.current = sessionId;
      setViewingSessionId(sessionId);

      // Fetch messages via REST
      const baseUrl = import.meta.env.VITE_API_BASE_URL || "";
      fetch(`${baseUrl}/api/sessions/${sessionId}/messages?limit=100&offset=0`)
        .then((res) => (res.ok ? res.json() : null))
        .then((data) => {
          if (!data?.messages?.length) return;
          if (viewingSessionIdRef.current !== sessionId) return;
          const mapped = mapApiMessages(data.messages);
          setMessages(mapped);
        })
        .catch((err) => console.error("Failed to fetch session messages:", err))
        .finally(() => setIsLoadingMessages(false));

      // Fetch session metadata
      const metadataFetchStartedAt = Date.now();
      fetch(`${baseUrl}/api/sessions/${sessionId}`)
        .then((res) => (res.ok ? res.json() : null))
        .then((data) => {
          const s = data?.session;
          if (!s || viewingSessionIdRef.current !== sessionId) return;
          const ref = s.seq_num ? `#${s.seq_num}` : null;
          setSessionRef(ref);
          const nextMeta: SessionObservationMeta = {
            ref,
            source: s.source || "unknown",
            title: s.title || null,
            status: s.status || "unknown",
            canProxyAttach: canProxyAttachSessionRecord(s),
            model: s.model || null,
            reasoningEffort: s.reasoning_effort || null,
            externalId: s.external_id || "",
            chatMode: s.chat_mode || null,
            gitBranch: s.git_branch || null,
            contextWindow: s.context_window || null,
            agentRunId: s.agent_run_id || null,
            workflowName: s.workflow_name || null,
            agentName: null,
            sessionType: normalizeSessionType(s.session_type),
          };
          viewingSessionMetaRef.current = nextMeta;
          setViewingSessionMeta(nextMeta);
          if (nextMeta.agentRunId) {
            void resolveAgentName(nextMeta.agentRunId).then((agentName) => {
              if (!agentName || viewingSessionIdRef.current !== sessionId)
                return;
              setViewingSessionMeta((prev) =>
                prev && viewingSessionIdRef.current === sessionId
                  ? { ...prev, agentName }
                  : prev,
              );
            });
          }
          if (!shouldApplyHydratedUsage(sessionId, metadataFetchStartedAt)) {
            return;
          }
          setContextUsage(computeContextUsageFromSessionData(s));
        })
        .catch((err) =>
          console.error("Failed to fetch session metadata:", err),
        );
      // resolveAgentName is a stable useCallback (its own deps are []) — safe
      // to reference here without re-creating the callback every render.
    },
    [resolveAgentName, setContextUsage, shouldApplyHydratedUsage],
  );

  // Clear viewing state and restore previous web chat
  const clearViewingSession = useCallback(() => {
    // Detach from any active WS subscription
    const observedSessionId = observedSessionIdRef.current;
    if (observedSessionId) {
      if (wsRef.current?.readyState === WebSocket.OPEN) {
        wsRef.current.send(
          JSON.stringify({
            type: "detach_from_session",
            session_id: observedSessionId,
          }),
        );
      }
      observedSessionIdRef.current = null;
      setObservedSessionId(null);
      observedSessionMetaRef.current = null;
      attachedSessionIdRef.current = null;
      setAttachedSessionId(null);
      attachedSessionMetaRef.current = null;
      setAttachedSessionMeta(null);
      clearPendingProxyMessages(
        pendingProxyMessagesRef.current,
        pendingProxySessionQueuesRef.current,
      );
      sessionInteractionModeRef.current = "none";
      setProxyDeliveryNotice(null);
      setSessionInteractionMode("none");
    }

    viewingSessionIdRef.current = null;
    viewingSessionMetaRef.current = null;
    setViewingSessionId(null);
    setViewingSessionMeta(null);
    setMessages([]);
    setProxyDeliveryNotice(null);

    if (mainSessionMeta) {
      setSessionRef(mainSessionMeta.ref);
      setSessionTitle(mainSessionMeta.title ?? null);
      setCurrentBranch(mainSessionMeta.gitBranch ?? null);
      if (mainSessionMeta.contextWindow) {
        setContextUsage((prev) => ({
          ...prev,
          contextWindow: mainSessionMeta.contextWindow ?? null,
        }));
      } else {
        setContextUsage(buildContextUsageFromTotals({}));
      }
    } else {
      setSessionRef(null);
      setSessionTitle(null);
      setCurrentBranch(null);
      setContextUsage(buildContextUsageFromTotals({}));
    }

    // Restore previous conversation messages and chat mode from DB
    const prevDbSid = loadDbSessionId();
    if (prevDbSid) {
      const baseUrl = import.meta.env.VITE_API_BASE_URL || "";
      fetch(`${baseUrl}/api/chat/${prevDbSid}/messages?limit=100&after_seq=0`)
        .then((res) => (res.ok ? res.json() : null))
        .then((data) => {
          if (!data?.messages?.length) return;
          const mapped = data.messages.map((m: Record<string, unknown>) =>
            mapRenderedMessageToChatMessage(m),
          );
          if (mapped.length > 0) setMessages(mapped);
        })
        .catch((err) => console.error("Failed to restore messages:", err));

      // Restore chat mode from DB (prevents stale mode from viewed session)
      fetch(`${baseUrl}/api/sessions/${prevDbSid}`)
        .then((res) => (res.ok ? res.json() : null))
        .then((data) => {
          const s = data?.session;
          if (s?.chat_mode) {
            onModeChangedRef.current?.(normalizeChatMode(s.chat_mode));
          }
        })
        .catch(() => {});
    }
  }, [mainSessionMeta, setContextUsage]);

  // Attach to a CLI session (interactive mode with WS subscription)
  const attachToSession = useCallback(
    (sessionId: string, mode: "observe" | "proxy" = "proxy") => {
      if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) return;
      pendingSessionInteractionModeRef.current = mode;
      setProxyDeliveryNotice(null);

      const observedSessionId = observedSessionIdRef.current;
      if (observedSessionId && observedSessionId !== sessionId) {
        wsRef.current.send(
          JSON.stringify({
            type: "detach_from_session",
            session_id: observedSessionId,
          }),
        );
        observedSessionIdRef.current = null;
        observedSessionMetaRef.current = null;
        attachedSessionIdRef.current = null;
        attachedSessionMetaRef.current = null;
        clearPendingProxyMessages(
          pendingProxyMessagesRef.current,
          pendingProxySessionQueuesRef.current,
        );
        sessionInteractionModeRef.current = "none";
        setObservedSessionId(null);
        setAttachedSessionId(null);
        setAttachedSessionMeta(null);
        setSessionInteractionMode("none");
      }

      // Don't reset messages if already viewing this session
      if (viewingSessionIdRef.current !== sessionId) {
        if (preAttachContextUsageRef.current === null) {
          preAttachContextUsageRef.current = contextUsage;
        }
        activeRequestIdRef.current = null;
        setIsStreaming(false);
        setIsThinking(false);
        setMessages([]);
        setContextUsage(buildContextUsageFromTotals({}));
      }

      wsRef.current.send(
        JSON.stringify({
          type: "attach_to_session",
          session_id: sessionId,
        }),
      );
    },
    [contextUsage, setContextUsage],
  );

  useEffect(() => {
    const restoredViewingSessionId = initialViewingSessionIdRef.current;
    if (!restoredViewingSessionId || initialViewingRestoreRef.current) {
      return;
    }
    initialViewingRestoreRef.current = true;
    viewSession(restoredViewingSessionId);
  }, [viewSession]);

  useEffect(() => {
    if (!isConnected) {
      initialViewingReconnectRetryRef.current = false;
      return;
    }

    const restoredViewingSessionId = initialViewingSessionIdRef.current;
    if (
      !restoredViewingSessionId ||
      !initialViewingRestoreRef.current ||
      initialViewingReconnectRetryRef.current ||
      viewingSessionIdRef.current !== restoredViewingSessionId ||
      viewingSessionMetaRef.current ||
      messagesRef.current.length > 0
    ) {
      return;
    }

    initialViewingReconnectRetryRef.current = true;
    viewSession(restoredViewingSessionId);
  }, [isConnected, viewSession, viewingSessionId]);

  useEffect(() => {
    const restoredViewingSessionId = initialViewingSessionIdRef.current;
    if (
      !restoredViewingSessionId ||
      initialViewingModeRef.current !== "observe" ||
      !isConnected ||
      viewingSessionIdRef.current !== restoredViewingSessionId ||
      observedSessionIdRef.current === restoredViewingSessionId
    ) {
      return;
    }
    attachToSession(restoredViewingSessionId, "observe");
    initialViewingModeRef.current = "none";
  }, [attachToSession, isConnected, viewingSessionId]);

  // Attach to the currently viewed session (button click from view-only mode)
  const attachToViewed = useCallback(() => {
    const sid = viewingSessionIdRef.current;
    if (!sid) {
      return;
    }
    const viewedMeta = viewingSessionMetaRef.current;
    if (
      viewedMeta?.sessionType !== "terminal" ||
      viewedMeta.agentRunId
    ) {
      return;
    }
    attachToSession(sid, "proxy");
  }, [attachToSession]);

  // Disable proxy mode but keep the live observation subscription.
  const detachFromSession = useCallback(() => {
    if (!attachedSessionIdRef.current) return;

    clearPendingProxyMessages(
      pendingProxyMessagesRef.current,
      pendingProxySessionQueuesRef.current,
    );
    attachedSessionIdRef.current = null;
    attachedSessionMetaRef.current = null;
    sessionInteractionModeRef.current = observedSessionIdRef.current
      ? "observe"
      : "none";
    setAttachedSessionId(null);
    setAttachedSessionMeta(null);
    setProxyDeliveryNotice(null);
    setSessionInteractionMode(
      observedSessionIdRef.current ? "observe" : "none",
    );
  }, []);

  // Add a local system message to the chat (no backend round-trip)
  const addSystemMessage = useCallback((content: string) => {
    setMessages((prev) => [
      ...prev,
      {
        id: `system-${uuid()}`,
        role: "system" as const,
        content,
        timestamp: new Date(),
      },
    ]);
  }, []);

  // Connect on mount, handle page lifecycle and heartbeat
  useEffect(() => {
    let cancelled = false;
    const persistedMainSessionId = loadDbSessionId();

    if (!initialViewingSessionIdRef.current) {
      const baseUrl = import.meta.env.VITE_API_BASE_URL || "";
      void (async () => {
        let restoreTargetId: string | null =
          persistedMainSessionId || conversationIdRef.current || null;

        if (persistedMainSessionId) {
          try {
            const response = await fetch(`${baseUrl}/api/sessions/${persistedMainSessionId}`);
            const data = response.ok ? await response.json() : null;
            if (cancelled) {
              return;
            }

            const session = data?.session as Record<string, unknown> | undefined;
            if (session && isRestorableSessionRecord(session)) {
              if (dbSessionIdRef.current === persistedMainSessionId) {
                applyMainSessionMeta(session);
              }
              restoreTargetId = persistedMainSessionId;
            } else if (response.status === 404 || session) {
              const activePersistedBinding =
                dbSessionIdRef.current === persistedMainSessionId ||
                conversationIdRef.current === persistedMainSessionId;
              if (activePersistedBinding) {
                bindActiveSession(null);
              } else {
                saveDbSessionId(null);
                if (loadConversationId() === persistedMainSessionId) {
                  saveConversationId("");
                }
              }
              restoreTargetId =
                conversationIdRef.current &&
                conversationIdRef.current !== persistedMainSessionId
                  ? conversationIdRef.current
                  : null;
            } else {
              // Daemon startup or transient API failure: keep the persisted main-chat
              // binding parked and let reconnect/catalog hydration recover it later.
              restoreTargetId = null;
            }
          } catch (err) {
            console.error("Failed to restore main session metadata:", err);
            restoreTargetId = null;
          }
        }

        if (!restoreTargetId) {
          return;
        }

        setIsLoadingMessages(true);
        fetch(`${baseUrl}/api/chat/${restoreTargetId}/messages?limit=100&after_seq=0`)
          .then((res) => (res.ok ? res.json() : null))
          .then((data) => {
            if (
              !data?.messages?.length ||
              conversationIdRef.current !== restoreTargetId
            ) {
              return;
            }
            const restored: ChatMessage[] = data.messages.map(mapStoredChatMessage);
            setMessages(restored);
            if (data.max_seq) {
              lastSeqRef.current = data.max_seq;
            }
          })
          .catch((err) => console.error("Failed to restore chat messages:", err))
          .finally(() => setIsLoadingMessages(false));
      })();
    }

    connect();

    // Immediate reconnect when returning to tab (mobile app switch, screen lock).
    // If already connected, send a heartbeat to reset idle timeout on the backend
    // (JS timers are throttled/suspended while backgrounded, so scheduled heartbeats
    // won't fire reliably — this catch-up heartbeat on return is the critical one).
    const handleVisibilityChange = () => {
      if (document.visibilityState !== "visible") return;
      if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) {
        // WS dropped while backgrounded — reconnect immediately
        if (reconnectTimeoutRef.current) {
          clearTimeout(reconnectTimeoutRef.current);
          reconnectTimeoutRef.current = null;
        }
        connect();
      } else if (conversationIdRef.current) {
        // Still connected — send immediate heartbeat to reset idle timer
        wsRef.current.send(
          JSON.stringify({
            type: "heartbeat",
            conversation_id: conversationIdRef.current,
          }),
        );
      }
    };
    document.addEventListener("visibilitychange", handleVisibilityChange);

    // Heartbeat every 60s to keep backend session alive during idle periods
    const heartbeatInterval = window.setInterval(() => {
      if (
        wsRef.current?.readyState === WebSocket.OPEN &&
        conversationIdRef.current
      ) {
        wsRef.current.send(
          JSON.stringify({
            type: "heartbeat",
            conversation_id: conversationIdRef.current,
          }),
        );
      }
    }, 60_000);

    return () => {
      cancelled = true;
      document.removeEventListener("visibilitychange", handleVisibilityChange);
      clearInterval(heartbeatInterval);
      if (reconnectTimeoutRef.current) {
        clearTimeout(reconnectTimeoutRef.current);
      }
      wsRef.current?.close();
    };
  }, [applyMainSessionMeta, bindActiveSession, connect]);

  return {
    messages,
    conversationId,
    conversationSwitchKey,
    sessionRef,
    sessionTitle,
    dbSessionId,
    currentBranch,
    worktreePath,
    isConnected,
    isReconnecting,
    isStreaming,
    isThinking,
    isLoadingMessages,
    contextUsage,
    contextUsageUpdatedAt,
    sendMessage,
    sendMode,
    sendProjectChange,
    setProjectIdRef,
    sendWorktreeChange,
    sendAgentChange,
    activeAgent,
    stopStreaming,
    clearHistory,
    deleteConversation,
    respondToQuestion,
    respondToApproval,
    canvasSurfaces,
    canvasPanel,
    onCanvasInteraction: respondToCanvas,
    planPendingApproval,
    approvePlan,
    requestPlanChanges,
    switchConversation,
    startNewChat,
    switchProvider,
    resumeSession,
    continueSessionInChat,
    setOnModeChanged,
    setOnPlanReady,
    setOnArtifactEvent,
    addSystemMessage,
    viewSession,
    clearViewingSession,
    mainSessionMeta,
    viewingSessionId,
    viewingSessionMeta,
    isContinuingSession,
    observeSession: attachToSession,
    attachToViewed,
    detachFromSession,
    attachedSessionId,
    attachedSessionMeta,
    sessionInteractionMode,
    proxyDeliveryNotice,
    wsRef,
    handleVoiceMessageRef,
    handleBinaryMessageRef,
    setOnChatDeleted,
    setOnChatCleared,
    selectedProvider,
    setSelectedProvider,
  };
}
