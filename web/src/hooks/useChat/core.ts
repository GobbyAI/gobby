import type {
  ChatMessage,
  ToolCall,
  ChatMode,
  ContentBlock,
  SessionInteractionMode,
  SessionObservationMeta,
  TokenUsage,
  ToolResult,
} from "../../types/chat";
import { classifyTool } from "../../types/chat";
import { normalizeChatRole } from "../../lib/chatMessageMapping";
import { AUTO_REASONING_EFFORT } from "../../lib/providerModels";

export interface ContextUsage {
  totalInputTokens: number;
  outputTokens: number;
  contextWindow: number | null;
  uncachedInputTokens: number;
  cacheReadTokens: number;
  cacheCreationTokens: number;
}

const CONVERSATION_ID_KEY = "gobby-conversation-id";
const DB_SESSION_ID_KEY = "gobby-db-session-id";
const VIEWING_SESSION_ID_KEY = "gobby-viewing-session-id";
const VIEWING_SESSION_MODE_KEY = "gobby-viewing-session-mode";
const CHAT_PROVIDERS = new Set(["claude", "gemini", "qwen", "codex"]);

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

export interface PendingProxyMessage {
  clientMessageId: string;
  currentMessageId: string;
  sessionId: string;
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
  text: string;
  request_id: string;
}

export interface SessionUsageUpdatedMessage {
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

/** crypto.randomUUID() requires a secure context (HTTPS/localhost). Fall back for HTTP access (e.g. Tailscale IP). */
export function uuid(): string {
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

export function loadConversationId(): string {
  return (
    localStorage.getItem(CONVERSATION_ID_KEY) ||
    localStorage.getItem(DB_SESSION_ID_KEY) ||
    ""
  );
}

export function saveConversationId(id: string): void {
  if (!id) {
    localStorage.removeItem(CONVERSATION_ID_KEY);
    return;
  }
  localStorage.setItem(CONVERSATION_ID_KEY, id);
}

export function loadDbSessionId(): string | null {
  return localStorage.getItem(DB_SESSION_ID_KEY);
}

export function saveDbSessionId(id: string | null): void {
  if (id) {
    localStorage.setItem(DB_SESSION_ID_KEY, id);
  } else {
    localStorage.removeItem(DB_SESSION_ID_KEY);
  }
}

export function loadViewingSessionId(): string | null {
  return localStorage.getItem(VIEWING_SESSION_ID_KEY);
}

export function saveViewingSessionId(id: string | null): void {
  if (id) {
    localStorage.setItem(VIEWING_SESSION_ID_KEY, id);
  } else {
    localStorage.removeItem(VIEWING_SESSION_ID_KEY);
  }
}

export function loadViewingSessionMode(): "none" | "observe" {
  const persisted = localStorage.getItem(VIEWING_SESSION_MODE_KEY);
  return persisted === "observe" ? "observe" : "none";
}

export function saveViewingSessionMode(mode: "none" | "observe"): void {
  if (mode === "observe") {
    localStorage.setItem(VIEWING_SESSION_MODE_KEY, mode);
  } else {
    localStorage.removeItem(VIEWING_SESSION_MODE_KEY);
  }
}

export function isChatProvider(value: unknown): value is string {
  return typeof value === "string" && CHAT_PROVIDERS.has(value);
}

export function isValidSessionType(value: unknown): value is "terminal" | "web_chat" {
  return value === "terminal" || value === "web_chat";
}

export function enqueuePendingProxyMessage(
  pendingQueues: Map<string, string[]>,
  entry: PendingProxyMessage,
): void {
  const queue = pendingQueues.get(entry.sessionId) ?? [];
  queue.push(entry.clientMessageId);
  pendingQueues.set(entry.sessionId, queue);
}

export function consumePendingProxyMessage(
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

export function removePendingProxyMessageFromQueue(
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

export function clearPendingProxyMessages(
  pending: Map<string, PendingProxyMessage>,
  pendingQueues: Map<string, string[]>,
): void {
  pending.clear();
  pendingQueues.clear();
}

export function normalizeSessionType(value: unknown): "terminal" | "web_chat" | null {
  return isValidSessionType(value) ? value : null;
}

export function normalizeReasoningEffort(value: string | null | undefined): string | null {
  if (!value) {
    return null;
  }
  const normalized = value.trim().toLowerCase();
  if (!normalized || normalized === AUTO_REASONING_EFFORT) {
    return null;
  }
  return normalized;
}

export interface CreatedWebChatSession extends Record<string, unknown> {
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

export interface ContinuationRollbackSnapshot {
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

export async function createWebChatSession(params?: {
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

export function computeContextUsageFromSessionData(
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

export function hasSessionUsage(session: Record<string, unknown> | null): boolean {
  if (!session) return false;
  return (
    (typeof session.usage_input_tokens === "number" &&
      session.usage_input_tokens > 0) ||
    (typeof session.usage_output_tokens === "number" &&
      session.usage_output_tokens > 0) ||
    typeof session.context_window === "number"
  );
}

export function isWebChatSessionRecord(
  session: Record<string, unknown> | null | undefined,
): boolean {
  return normalizeSessionType(session?.session_type) === "web_chat";
}

const NON_RESTORABLE_SESSION_STATUSES = new Set([
  "expired",
  "archived",
  "closed",
  "ended",
]);

export function isRestorableSessionRecord(
  session: Record<string, unknown> | null | undefined,
): boolean {
  if (!isWebChatSessionRecord(session)) {
    return false;
  }
  const status =
    typeof session?.status === "string" ? session.status.toLowerCase() : null;
  // Treat unknown/missing status as restorable so transient backend hiccups
  // don't drop a working session; only refuse to restore explicit terminal
  // states (expired / closed / ended / etc).
  if (!status) return true;
  return !NON_RESTORABLE_SESSION_STATUSES.has(status);
}

export function buildContextUsageFromTotals(params: {
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

export function toSessionObservationMeta(
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

export interface ApiMessage {
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

export function mapStoredChatMessage(m: {
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

export function tryParseJSON(value: unknown): unknown {
  if (value === undefined || value === null) return undefined;
  if (typeof value !== "string") return value;
  try {
    return JSON.parse(value);
  } catch {
    return value;
  }
}

/** Helper: append to or create a text content block on the current assistant message. */
export function appendTextBlock(msg: ChatMessage, text: string) {
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
export function appendToolBlock(msg: ChatMessage, tc: ToolCall) {
  if (!msg.contentBlocks) msg.contentBlocks = [];
  const last = msg.contentBlocks[msg.contentBlocks.length - 1];
  if (last?.type === "tool_chain") {
    last.tool_calls.push(tc);
  } else {
    msg.contentBlocks.push({ type: "tool_chain", tool_calls: [tc] });
  }
}

/** Find a tool call by its tool_use_id across contentBlocks and flat toolCalls. */
export function findToolCallById(
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
export function findPendingToolCall(msg: ChatMessage): ToolCall | undefined {
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

export function extractServerName(toolName: string): string {
  const parts = toolName.split("__");
  if (parts.length >= 3 && parts[0] === "mcp") return parts[1];
  return "builtin";
}

export function isHookFeedback(content: string): boolean {
  return (
    /^Stop hook feedback:/.test(content) ||
    /^(Pre|Post)ToolUse hook/.test(content) ||
    /^UserPromptSubmit hook/.test(content)
  );
}

export function extractUserText(content: string): string | null {
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

export function mapApiMessages(messages: ApiMessage[]): ChatMessage[] {
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
