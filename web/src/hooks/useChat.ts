import { useState, useEffect, useCallback, useRef } from "react";
import type {
  ChatMessage,
  ToolCall,
  ChatMode,
  ContentBlock,
  SessionInteractionMode,
  SessionObservationMeta,
  TokenUsage,
  ToolResult,
} from "../types/chat";
import { classifyTool } from "../types/chat";
import type { QueuedFile } from "../types/chat";
import type { A2UISurfaceState, UserAction } from "../components/canvas/types";
import type { CanvasPanelState } from "../components/canvas/hooks/useCanvasPanel";

const CONVERSATION_ID_KEY = "gobby-conversation-id";
const DB_SESSION_ID_KEY = "gobby-db-session-id";
const CHAT_PROVIDERS = new Set(["claude", "gemini", "codex"]);

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

function isChatProvider(value: unknown): value is string {
  return typeof value === "string" && CHAT_PROVIDERS.has(value);
}

interface CreatedWebChatSession {
  id: string;
  source: string;
  model: string | null;
  chat_mode: string | null;
  seq_num: number | null;
  title: string | null;
}

async function createWebChatSession(params?: {
  projectId?: string | null;
  provider?: string | null;
  model?: string | null;
  chatMode?: ChatMode | null;
}): Promise<CreatedWebChatSession> {
  const baseUrl = import.meta.env.VITE_API_BASE_URL || "";
  const response = await fetch(`${baseUrl}/api/sessions/web-chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      project_id: params?.projectId ?? null,
      provider: params?.provider ?? null,
      model: params?.model ?? null,
      chat_mode: params?.chatMode ?? null,
    }),
  });

  if (!response.ok) {
    throw new Error(`Failed to create web chat session: ${response.status}`);
  }

  const data = await response.json();
  return data.session as CreatedWebChatSession;
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
        role: (m.role as "user" | "assistant" | "system") || "assistant",
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

function mapRenderedMessageToChatMessage(message: Record<string, unknown>): ChatMessage {
  const chatMsg: ChatMessage = {
    id: String(message.id ?? `ws-${Date.now()}`),
    role: ((message.role as string) as "user" | "assistant" | "system") || "assistant",
    content: (message.content as string) ?? "",
    timestamp: new Date((message.timestamp as string) ?? Date.now()),
    contentBlocks: message.content_blocks as ContentBlock[] | undefined,
  };

  if (chatMsg.contentBlocks) {
    for (const block of chatMsg.contentBlocks) {
      if (block.type === "tool_chain" && block.tool_calls) {
        chatMsg.toolCalls = [...(chatMsg.toolCalls || []), ...block.tool_calls];
      } else if (block.type === "thinking") {
        chatMsg.thinkingContent =
          (chatMsg.thinkingContent || "") + block.content;
      }
    }
  }

  return chatMsg;
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
    () => localStorage.getItem(ACTIVE_AGENT_KEY) || "default-web-chat",
  );

  // Session title — stored from switchConversation to survive filtered list race
  const [sessionTitle, setSessionTitle] = useState<string | null>(null);

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
  const [viewingSessionId, setViewingSessionId] = useState<string | null>(null);
  const viewingSessionIdRef = useRef<string | null>(null);
  const [viewingSessionMeta, setViewingSessionMeta] =
    useState<SessionObservationMeta | null>(null);
  const viewingSessionMetaRef = useRef<SessionObservationMeta | null>(null);

  // Live terminal observation is distinct from interactive proxy mode.
  const [observedSessionId, setObservedSessionId] = useState<string | null>(null);
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
    ((
      content: string,
      model?: string | null,
      files?: QueuedFile[],
      projectId?: string | null,
      injectContext?: string,
    ) => boolean) | null
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

  const clearSessionObservationState = useCallback(
    ({ preserveViewing = false }: { preserveViewing?: boolean } = {}) => {
      if (observedSessionIdRef.current && wsRef.current?.readyState === WebSocket.OPEN) {
        wsRef.current.send(
          JSON.stringify({
            type: "detach_from_session",
            session_id: observedSessionIdRef.current,
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
        chatMode: currentModeRef.current,
      })
        .then((session) => {
          bindActiveSession(session.id);
          if (session.seq_num != null) {
            setSessionRef(`#${session.seq_num}`);
          }
          setSessionTitle(session.title ?? null);
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
    [bindActiveSession],
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
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectTimeoutRef = useRef<number | null>(null);
  // Timestamp of last server-authoritative mode_changed — used to suppress
  // redundant set_mode emissions on WS reconnect and session restore
  const lastServerModeTimestampRef = useRef<number>(0);

  // Track the active chat request to filter stale stream chunks from cancelled requests
  const activeRequestIdRef = useRef<string | null>(null);

  // Queue for messages sent while disconnected — flushed on reconnect
  const pendingMessagesRef = useRef<{ content: string; projectId?: string | null }[]>([]);

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
  const handleBinaryMessageRef = useRef<(data: ArrayBuffer) => void>(
    () => {},
  );

  // Connect to WebSocket
  const connect = useCallback(() => {
    if (wsRef.current?.readyState === WebSocket.OPEN) return;

    const wsProtocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    const wsUrl = `${wsProtocol}//${window.location.host}/ws`;

    console.log("Connecting to WebSocket:", wsUrl);
    const ws = new WebSocket(wsUrl);
    ws.binaryType = 'arraybuffer';  // For TTS audio binary frames
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
            sendMessageRef.current?.(msg.content, null, undefined, msg.projectId);
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
            if (!data?.messages?.length) return;
            const backfilled: ChatMessage[] = data.messages.map(
              (m: {
                id: string;
                role: string;
                content: string;
                tool_calls?: ToolCall[];
                seq: number;
                created_at: string;
              }) => ({
                id: m.id,
                role: m.role as "user" | "assistant" | "system",
                content: [{ type: "text" as const, content: m.content }],
                toolCalls: m.tool_calls ?? [],
                timestamp: new Date(m.created_at),
              }),
            );
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
          ],
        }),
      );

      // Sync current mode to backend on connect/reconnect — but skip if
      // the server just sent an authoritative mode_changed (avoids loop)
      if (
        conversationIdRef.current &&
        Date.now() - lastServerModeTimestampRef.current > 2000
      ) {
        ws.send(
          JSON.stringify({
            type: "set_mode",
            mode: currentModeRef.current,
            conversation_id: conversationIdRef.current,
          }),
        );

        // Re-sync current project on connect/reconnect
        if (projectIdRef.current) {
          ws.send(
            JSON.stringify({
              type: "set_project",
              conversation_id: conversationIdRef.current,
              project_id: projectIdRef.current,
            }),
          );
        }

        // Re-sync persisted agent on reconnect
        ws.send(
          JSON.stringify({
            type: "set_agent",
            conversation_id: conversationIdRef.current,
            agent_name: activeAgentRef.current,
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
          console.error('TTS binary message error:', err);
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
            const newMode = (data as Record<string, unknown>).mode as
              | ChatMode
              | undefined;
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
                reason === "plan_approved" &&
                pendingPlanExecutionRef.current
              ) {
                pendingPlanExecutionRef.current = false;
                setTimeout(() => {
                  sendMessageRef.current?.(
                    "Plan approved — proceed with implementation.",
                  );
                }, 200);
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
          setDbSessionId((continued.db_session_id as string) ?? null);
          console.log("Session continued:", data);
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
          const meta: SessionObservationMeta = {
            ref: (result.ref as string) ?? null,
            source: (result.source as string) ?? "unknown",
            title: (result.title as string) ?? null,
            status: (result.status as string) ?? "unknown",
            model: (result.model as string) ?? null,
            externalId: (result.external_id as string) ?? "",
            chatMode: (result.chat_mode as string) ?? null,
            gitBranch: (result.git_branch as string) ?? null,
            contextWindow: (result.context_window as number) ?? null,
            agentRunId: (result.agent_run_id as string) ?? null,
            workflowName: (result.workflow_name as string) ?? null,
            agentName: (result.agent_name as string) ?? null,
            sessionType:
              ((result.session_type as "terminal" | "web_chat" | null) ?? null),
          };
          setObservedSessionId(sid);
          observedSessionMetaRef.current = meta;
          // Also set viewing state (attached implies viewing)
          setViewingSessionId(sid);
          setViewingSessionMeta(meta);
          const requestedMode = pendingSessionInteractionModeRef.current;
          const proxyCapable =
            meta.sessionType === "terminal" && meta.status === "active";
          const nextMode =
            requestedMode === "proxy" && !proxyCapable
              ? "none"
              : requestedMode;
          setSessionInteractionMode(nextMode);
          if (nextMode === "proxy") {
            setAttachedSessionId(sid);
            setAttachedSessionMeta(meta);
            setProxyDeliveryNotice(null);
          } else {
            setAttachedSessionId(null);
            setAttachedSessionMeta(null);
            setProxyDeliveryNotice(
              requestedMode === "proxy" && meta.sessionType === "terminal"
                ? "This terminal session is paused. Use Resume Session to continue it in web chat."
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
          setObservedSessionId(null);
          observedSessionMetaRef.current = null;
          setAttachedSessionId(null);
          setAttachedSessionMeta(null);
          setSessionInteractionMode("none");
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
          setMessages((prev) => {
            const existingIdx = prev.findIndex(
              (message) => message.id === renderedMessage.id,
            );
            if (existingIdx >= 0) {
              const updated = [...prev];
              updated[existingIdx] = renderedMessage;
              return updated;
            }
            return [...prev, renderedMessage];
          });
        } else if (data.type === "send_to_cli_session_result") {
          const result = data as Record<string, unknown>;
          setProxyDeliveryNotice(
            result.delivered === false
              ? "Message queued until the session yields."
              : null,
          );
          console.log("Message sent to CLI session:", result.delivery_method);
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
  }, [resolveAgentName]);

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

      // Plan approval auto-send is handled in the mode_changed handler above,
      // since the agent's turn has already ended by the time the user approves.
      // Safety fallback: if somehow still pending here, consume it.
      if (pendingPlanExecutionRef.current) {
        pendingPlanExecutionRef.current = false;
        setTimeout(() => {
          sendMessageRef.current?.(
            "Plan approved — proceed with implementation.",
          );
        }, 200);
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
    if (!isActiveRequest(status.request_id)) {
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
    (id: string) => {
      if (!id) return;
      if (id === dbSessionIdRef.current && messagesRef.current.length > 0) {
        return;
      }

      clearSessionObservationState();
      resetMainChatState();
      bindActiveSession(id);
      setConversationSwitchKey((k) => k + 1);

      setIsLoadingMessages(true);
      const baseUrl = import.meta.env.VITE_API_BASE_URL || "";
      fetch(`${baseUrl}/api/chat/${id}/messages?limit=100&after_seq=0`)
        .then((res) => (res.ok ? res.json() : null))
        .then((data) => {
          if (!data?.messages?.length || conversationIdRef.current !== id) return;
          const mapped = data.messages.map(
            (m: Record<string, unknown>) =>
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

      fetch(`${baseUrl}/api/sessions/${id}`)
        .then((res) => (res.ok ? res.json() : null))
        .then((data) => {
          const s = data?.session;
          if (!s || conversationIdRef.current !== id) return;
          if (isChatProvider(s.source)) {
            setSelectedProvider(s.source);
          }
          if (s.title) setSessionTitle(s.title);
          if (s.seq_num != null) setSessionRef(`#${s.seq_num}`);
          if (
            s.usage_input_tokens > 0 ||
            s.usage_output_tokens > 0 ||
            s.context_window
          ) {
            const totalIn = s.usage_input_tokens ?? 0;
            const cacheRead = s.usage_cache_read_tokens ?? 0;
            const cacheCreation = s.usage_cache_creation_tokens ?? 0;
            setContextUsage({
              totalInputTokens: totalIn,
              outputTokens: s.usage_output_tokens ?? 0,
              contextWindow: s.context_window ?? null,
              uncachedInputTokens: totalIn - cacheRead - cacheCreation,
              cacheReadTokens: cacheRead,
              cacheCreationTokens: cacheCreation,
            });
          }
          if (s.chat_mode) {
            const restored = s.chat_mode as ChatMode;
            if (restored !== currentModeRef.current) {
              currentModeRef.current = restored;
              onModeChangedRef.current?.(restored);
            }
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
      bindActiveSession,
      clearSessionObservationState,
      resetMainChatState,
      setContextUsage,
      setSelectedProvider,
    ],
  );

  // Start a new chat conversation, optionally with a specific agent
  const startNewChat = useCallback(
    (agentName?: string) => {
      const effectiveAgent = agentName || "default-web-chat";
      setActiveAgent(effectiveAgent);
      clearSessionObservationState();
      resetMainChatState();
      bindActiveSession(null);
      setConversationSwitchKey((k) => k + 1);
      void ensureMainSession({
        projectId: projectIdRef.current,
        provider: selectedProviderRef.current,
        forceNew: true,
      });
    },
    [
      bindActiveSession,
      clearSessionObservationState,
      ensureMainSession,
      resetMainChatState,
    ],
  );

  // Switch provider by starting a new server-owned web-chat session.
  const switchProvider = useCallback(
    (newProvider: string) => {
      if (isStreaming && wsRef.current?.readyState === WebSocket.OPEN) {
        wsRef.current.send(
          JSON.stringify({
            type: "stop_chat",
            conversation_id: conversationIdRef.current,
          }),
        );
      }
      clearSessionObservationState();
      resetMainChatState();
      bindActiveSession(null);
      setConversationSwitchKey((k) => k + 1);
      void ensureMainSession({
        projectId: projectIdRef.current,
        provider: newProvider,
        forceNew: true,
      });
    },
    [
      bindActiveSession,
      clearSessionObservationState,
      ensureMainSession,
      isStreaming,
      resetMainChatState,
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
      options?: { provider?: string | null; model?: string | null },
    ): Promise<string> => {
      clearSessionObservationState();
      resetMainChatState();
      bindActiveSession(null);
      setConversationSwitchKey((k) => k + 1);

      const baseUrl = import.meta.env.VITE_API_BASE_URL || "";
      let sourceSession: Record<string, unknown> | null = null;
      try {
        const sessionRes = await fetch(
          `${baseUrl}/api/sessions/${sourceDbSessionId}`,
        );
        if (sessionRes.ok) {
          const sessionData = await sessionRes.json();
          sourceSession = (sessionData?.session as Record<string, unknown>) ?? null;
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

      if (continuationProvider) {
        setSelectedProvider(continuationProvider);
      }

      const newConversationId = await ensureMainSession({
        projectId:
          projectId ??
          (typeof sourceSession?.project_id === "string"
            ? sourceSession.project_id
            : projectIdRef.current),
        provider: continuationProvider,
        model: continuationModel,
        forceNew: true,
      });
      if (!newConversationId) {
        return "";
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
      if (
        s &&
        (((s.usage_input_tokens as number | undefined) ?? 0) > 0 ||
          ((s.usage_output_tokens as number | undefined) ?? 0) > 0 ||
          s.context_window)
      ) {
        const totalIn = (s.usage_input_tokens as number | undefined) ?? 0;
        const cacheRead = (s.usage_cache_read_tokens as number | undefined) ?? 0;
        const cacheCreation =
          (s.usage_cache_creation_tokens as number | undefined) ?? 0;
        setContextUsage({
          totalInputTokens: totalIn,
          outputTokens: (s.usage_output_tokens as number | undefined) ?? 0,
          contextWindow: (s.context_window as number | null | undefined) ?? null,
          uncachedInputTokens: totalIn - cacheRead - cacheCreation,
          cacheReadTokens: cacheRead,
          cacheCreationTokens: cacheCreation,
        });
      }
      if (s?.chat_mode) {
        onModeChangedRef.current?.(s.chat_mode as ChatMode);
      }

      // Tell backend to prepare the continuation session
      if (wsRef.current?.readyState === WebSocket.OPEN) {
        wsRef.current.send(
          JSON.stringify({
            type: "continue_in_chat",
            conversation_id: newConversationId,
            source_session_id: sourceDbSessionId,
            project_id:
              projectId ??
              (typeof sourceSession?.project_id === "string"
                ? sourceSession.project_id
                : undefined),
            provider: continuationProvider,
            model: continuationModel,
          }),
        );
      }

      return newConversationId;
    },
    [
      bindActiveSession,
      clearSessionObservationState,
      ensureMainSession,
      resetMainChatState,
      setContextUsage,
      setSelectedProvider,
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
    currentModeRef.current = mode; // Always track latest intended mode
    if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) return;
    if (!conversationIdRef.current) return;
    setPlanPendingApproval(false);
    wsRef.current.send(
      JSON.stringify({
        type: "set_mode",
        mode,
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
    ): boolean => {
      console.log(
        "sendMessage called:",
        content,
        "model:",
        model,
        "files:",
        files?.length,
      );

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
        }).then((sessionId) => {
          if (!sessionId) return;
          sendMessageRef.current?.(
            content,
            model,
            files,
            projectId,
            injectContext,
          );
        });
        return true;
      }

      if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) {
        // Queue the message to send on reconnect
        console.warn("WebSocket disconnected — queuing message for reconnect");
        pendingMessagesRef.current.push({ content, projectId });
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
        const messageId = `user-${uuid()}`;
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
            session_id: attachedSessionIdRef.current,
            content,
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

  // Track whether we're waiting for plan_approved mode_changed to auto-send
  const pendingPlanExecutionRef = useRef(false);
  const pendingPlanFeedbackRef = useRef<string | null>(null);

  // Approve the current plan — tells backend to unlock write tools,
  // then sends a follow-up message to prompt the agent to begin execution.
  const approvePlan = useCallback(() => {
    if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) return;
    if (!conversationIdRef.current) return;
    if (!planContentRef.current) return;
    pendingPlanExecutionRef.current = true;
    // Eagerly clear approval UI to prevent ghost flash when artifact panel closes
    setPlanPendingApproval(false);
    planContentRef.current = null;
    // Optimistically switch mode out of plan (WS mode_changed will confirm/correct)
    currentModeRef.current = "accept_edits";
    onModeChangedRef.current?.("accept_edits");
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
  const viewSession = useCallback((sessionId: string) => {
    // Skip if already viewing/attached to this session
    if (
      viewingSessionIdRef.current === sessionId ||
      observedSessionIdRef.current === sessionId
    ) {
      return;
    }

    // Detach from any active WS subscription first
    if (observedSessionIdRef.current) {
      if (wsRef.current?.readyState === WebSocket.OPEN) {
        wsRef.current.send(
          JSON.stringify({
            type: "detach_from_session",
            session_id: observedSessionIdRef.current,
          }),
        );
      }
      setObservedSessionId(null);
      observedSessionMetaRef.current = null;
      setAttachedSessionId(null);
      setAttachedSessionMeta(null);
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

    // Set viewing state
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
          model: s.model || null,
          externalId: s.external_id || "",
          chatMode: s.chat_mode || null,
          gitBranch: s.git_branch || null,
          contextWindow: s.context_window || null,
          agentRunId: s.agent_run_id || null,
          workflowName: s.workflow_name || null,
          agentName: null,
          sessionType: s.session_type || null,
        };
        setViewingSessionMeta(nextMeta);
        if (nextMeta.agentRunId) {
          void resolveAgentName(nextMeta.agentRunId).then((agentName) => {
            if (!agentName || viewingSessionIdRef.current !== sessionId) return;
            setViewingSessionMeta((prev) =>
              prev && viewingSessionIdRef.current === sessionId
                ? { ...prev, agentName }
                : prev,
            );
          });
        }
        // Populate context usage from session metadata
        if (
          s.usage_input_tokens > 0 ||
          s.usage_output_tokens > 0 ||
          s.context_window
        ) {
          const totalIn = s.usage_input_tokens ?? 0;
          const cacheRead = s.usage_cache_read_tokens ?? 0;
          const cacheCreation = s.usage_cache_creation_tokens ?? 0;
          setContextUsage({
            totalInputTokens: totalIn,
            outputTokens: s.usage_output_tokens ?? 0,
            contextWindow: s.context_window ?? null,
            uncachedInputTokens: totalIn - cacheRead - cacheCreation,
            cacheReadTokens: cacheRead,
            cacheCreationTokens: cacheCreation,
          });
        }
      })
      .catch((err) => console.error("Failed to fetch session metadata:", err));
    // resolveAgentName is a stable useCallback (its own deps are []) — safe
    // to reference here without re-creating the callback every render.
  }, [resolveAgentName, setContextUsage]);

  // Clear viewing state and restore previous web chat
  const clearViewingSession = useCallback(() => {
    // Detach from any active WS subscription
    if (observedSessionIdRef.current) {
      if (wsRef.current?.readyState === WebSocket.OPEN) {
        wsRef.current.send(
          JSON.stringify({
            type: "detach_from_session",
            session_id: observedSessionIdRef.current,
          }),
        );
      }
      setObservedSessionId(null);
      observedSessionMetaRef.current = null;
      setAttachedSessionId(null);
      setAttachedSessionMeta(null);
      setSessionInteractionMode("none");
    }

    setViewingSessionId(null);
    setViewingSessionMeta(null);
    setMessages([]);
    setSessionRef(null);
    setProxyDeliveryNotice(null);
    setContextUsage({
      totalInputTokens: 0,
      outputTokens: 0,
      contextWindow: null,
      uncachedInputTokens: 0,
      cacheReadTokens: 0,
      cacheCreationTokens: 0,
    });

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
            onModeChangedRef.current?.(s.chat_mode as ChatMode);
          }
        })
        .catch(() => {});
    }
  }, [setContextUsage]);

  // Attach to a CLI session (interactive mode with WS subscription)
  const attachToSession = useCallback(
    (sessionId: string, mode: "observe" | "proxy" = "proxy") => {
      if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) return;
      pendingSessionInteractionModeRef.current = mode;
      setProxyDeliveryNotice(null);

      // Don't reset messages if already viewing this session
      if (viewingSessionIdRef.current !== sessionId) {
        activeRequestIdRef.current = null;
        setIsStreaming(false);
        setIsThinking(false);
        setMessages([]);
        setContextUsage({
          totalInputTokens: 0,
          outputTokens: 0,
          contextWindow: null,
          uncachedInputTokens: 0,
          cacheReadTokens: 0,
          cacheCreationTokens: 0,
        });
      }

      wsRef.current.send(
        JSON.stringify({
          type: "attach_to_session",
          session_id: sessionId,
        }),
      );
    },
    [setContextUsage],
  );

  // Attach to the currently viewed session (button click from view-only mode)
  const attachToViewed = useCallback(() => {
    const sid = viewingSessionIdRef.current;
    if (!sid) {
      return;
    }
    const viewedMeta = viewingSessionMetaRef.current;
    if (viewedMeta?.sessionType && viewedMeta.sessionType !== "terminal") {
      return;
    }
    void continueSessionInChat(sid);
  }, [continueSessionInChat]);

  // Disable proxy mode but keep the live observation subscription.
  const detachFromSession = useCallback(() => {
    if (!attachedSessionIdRef.current) return;

    setAttachedSessionId(null);
    setAttachedSessionMeta(null);
    setSessionInteractionMode(observedSessionIdRef.current ? "observe" : "none");
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
    // Restore chat messages from server if we have an existing conversation
    const existingConvId = conversationIdRef.current;
    if (existingConvId) {
      const baseUrl = import.meta.env.VITE_API_BASE_URL || "";
      setIsLoadingMessages(true);
      fetch(`${baseUrl}/api/chat/${existingConvId}/messages`)
        .then((res) => (res.ok ? res.json() : null))
        .then((data) => {
          if (
            !data?.messages?.length ||
            conversationIdRef.current !== existingConvId
          )
            return;
          const restored: ChatMessage[] = data.messages.map(
            (m: {
              id: string;
              role: string;
              content: string;
              tool_calls?: ToolCall[];
              seq: number;
              created_at: string;
            }) => ({
              id: m.id,
              role: m.role as "user" | "assistant" | "system",
              content: [{ type: "text" as const, content: m.content }],
              toolCalls: m.tool_calls ?? [],
              timestamp: new Date(m.created_at),
            }),
          );
          setMessages(restored);
          if (data.max_seq) lastSeqRef.current = data.max_seq;
        })
        .catch((err) => console.error("Failed to restore chat messages:", err))
        .finally(() => setIsLoadingMessages(false));
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
      document.removeEventListener("visibilitychange", handleVisibilityChange);
      clearInterval(heartbeatInterval);
      if (reconnectTimeoutRef.current) {
        clearTimeout(reconnectTimeoutRef.current);
      }
      wsRef.current?.close();
    };
  }, [connect]);

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
    viewingSessionId,
    viewingSessionMeta,
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
