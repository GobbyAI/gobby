import { useState, useEffect, useCallback, useRef } from "react";
import type { ChatMessage, ToolCall, ChatMode } from "../types/chat";
import type { QueuedFile } from "../types/chat";
import type { A2UISurfaceState, UserAction } from "../components/canvas/types";
import type { CanvasPanelState } from "../components/canvas/hooks/useCanvasPanel";

const CONVERSATION_ID_KEY = "gobby-conversation-id";

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
  result?: unknown;
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
  return localStorage.getItem(CONVERSATION_ID_KEY) || uuid();
}

function saveConversationId(id: string): void {
  localStorage.setItem(CONVERSATION_ID_KEY, id);
}

const DB_SESSION_ID_KEY = "gobby-db-session-id";

function loadDbSessionId(): string | null {
  return localStorage.getItem(DB_SESSION_ID_KEY);
}

function saveDbSessionId(id: string | null): void {
  if (id) localStorage.setItem(DB_SESSION_ID_KEY, id);
  else localStorage.removeItem(DB_SESSION_ID_KEY);
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
    last.calls.push(tc);
  } else {
    msg.contentBlocks.push({ type: "tool_chain", calls: [tc] });
  }
}

/** Find a tool call by its tool_use_id across contentBlocks and flat toolCalls. */
function findToolCallById(msg: ChatMessage, toolUseId: string): ToolCall | undefined {
  if (msg.contentBlocks) {
    for (const block of msg.contentBlocks) {
      if (block.type === "tool_chain") {
        const found = block.calls.find((tc) => tc.id === toolUseId);
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
        const pending = block.calls.find((tc) => tc.status !== "completed");
        if (pending) return pending;
      }
    }
  }
  // Fallback to flat toolCalls
  return msg.toolCalls?.find((tc) => tc.status !== "completed");
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

    if (m.role === "user") {
      if (m.content_type === "tool_result") {
        // Tool result in a user message — prefer ID-based match, fall back to positional
        if (currentAssistant) {
          const match = m.tool_use_id
            ? findToolCallById(currentAssistant, m.tool_use_id)
            : findPendingToolCall(currentAssistant);
          if (match) {
            match.result = tryParseJSON(m.content);
            match.status = "completed";
          }
        }
        continue;
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
      if (m.content_type === "tool_use") {
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
        const toolCall: ToolCall = {
          id: m.tool_use_id || id,
          tool_name: m.tool_name || "unknown",
          server_name: "builtin",
          status: m.tool_result ? "completed" : "calling",
          arguments: tryParseJSON(m.tool_input) as
            | Record<string, unknown>
            | undefined,
          result: m.tool_result ? tryParseJSON(m.tool_result) : undefined,
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
      } else {
        // Regular assistant text
        if (currentAssistant) {
          if (m.content) {
            if (currentAssistant.content && !currentAssistant.content.endsWith("\n"))
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
            contentBlocks: m.content ? [{ type: "text", content: m.content }] : [],
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
          match.result = tryParseJSON(m.content);
          match.status = "completed";
        }
      }
    }
  }

  flushAssistant();
  return result;
}

export function useChat() {
  const conversationIdRef = useRef<string>(loadConversationId());
  const [conversationId, setConversationId] = useState<string>(
    conversationIdRef.current,
  );

  // Counter that increments only on intentional conversation switches (not SDK
  // session ID adoption).  Used by the mode-restore effect in App.tsx so that
  // adopting the SDK session ID doesn't reset the user's mode to the default.
  const [conversationSwitchKey, setConversationSwitchKey] = useState(0);

  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const messagesRef = useRef(messages);
  messagesRef.current = messages;
  const [isConnected, setIsConnected] = useState(false);
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

  // Branch/worktree tracking
  const [currentBranch, setCurrentBranch] = useState<string | null>(null);
  const [worktreePath, setWorktreePath] = useState<string | null>(null);

  // Active agent tracking
  const [activeAgent, setActiveAgent] = useState<string>("default-web-chat");

  // Session viewing tracking (read-only observation of CLI sessions via REST)
  const [viewingSessionId, setViewingSessionId] = useState<string | null>(null);
  const viewingSessionIdRef = useRef<string | null>(null);
  const [viewingSessionMeta, setViewingSessionMeta] =
    useState<import("../types/chat").SessionObservationMeta | null>(null);

  // Session attachment tracking (interactive observation via WS subscription)
  const [attachedSessionId, setAttachedSessionId] = useState<string | null>(
    null,
  );
  const attachedSessionIdRef = useRef<string | null>(null);
  const [attachedSessionMeta, setAttachedSessionMeta] =
    useState<import("../types/chat").SessionObservationMeta | null>(null);

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
    ((content: string) => boolean) | null
  >(null);

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

  // Connect to WebSocket
  const connect = useCallback(() => {
    if (wsRef.current?.readyState === WebSocket.OPEN) return;

    const wsProtocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    const wsUrl = `${wsProtocol}//${window.location.host}/ws`;

    console.log("Connecting to WebSocket:", wsUrl);
    const ws = new WebSocket(wsUrl);
    wsRef.current = ws;

    ws.onopen = () => {
      console.log("WebSocket connected");
      setIsConnected(true);

      ws.send(
        JSON.stringify({
          type: "subscribe",
          events: [
            "chat_stream",
            "chat_error",
            "tool_status",
            "chat_thinking",
            "canvas_event",
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
      }
    };

    ws.onclose = () => {
      console.log("WebSocket disconnected");
      setIsConnected(false);
      setIsStreaming(false);
      setIsThinking(false);
      activeRequestIdRef.current = null;

      reconnectTimeoutRef.current = window.setTimeout(() => {
        connect();
      }, 2000);
    };

    ws.onerror = (error) => {
      console.error("WebSocket error:", error);
    };

    ws.onmessage = (event) => {
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
          data.type === "voice_status"
        ) {
          // When STT transcription arrives, inject it as a user message and
          // register the request_id so the assistant's response stream is accepted.
          if (data.type === "voice_transcription") {
            const voiceMsg = data as unknown as VoiceTranscriptionMessage;
            const text = typeof voiceMsg.text === "string" ? voiceMsg.text : "";
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
        } else if (data.type === "plan_pending_approval") {
          const planContent = (data as Record<string, unknown>).plan_content as
            | string
            | undefined;
          if (planContent) {
            setPlanPendingApproval(true);
            planContentRef.current = planContent;
            onPlanReadyRef.current?.(planContent);
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
              // Always process plan approval/rejection regardless of mode equality
              if (
                reason === "plan_approved" ||
                reason === "plan_changes_requested"
              ) {
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
          if (dbSid && (!infoConvId || infoConvId === conversationIdRef.current)) {
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
        } else if (data.type === "attach_to_session_result") {
          const result = data as Record<string, unknown>;
          const sid = result.session_id as string;
          const meta = {
            ref: (result.ref as string) ?? null,
            source: (result.source as string) ?? "unknown",
            title: (result.title as string) ?? null,
            status: (result.status as string) ?? "unknown",
            model: (result.model as string) ?? null,
            externalId: (result.external_id as string) ?? "",
            chatMode: (result.chat_mode as string) ?? null,
            gitBranch: (result.git_branch as string) ?? null,
            contextWindow: (result.context_window as number) ?? null,
          };
          setAttachedSessionId(sid);
          setAttachedSessionMeta(meta);
          // Also set viewing state (attached implies viewing)
          setViewingSessionId(sid);
          setViewingSessionMeta(meta);
          // Map initial messages into chat format with proper tool call grouping
          const msgs = (result.messages as ApiMessage[]) || [];
          const mapped = mapApiMessages(msgs);
          // Preserve REST-loaded transcript when re-attaching to viewed session
          if (viewingSessionIdRef.current === sid && messagesRef.current.length > 0) {
            const mappedById = new Map(mapped.map(m => [m.id, m]));
            // Merge updates into existing messages, then append truly new ones
            const existingIds = new Set(messagesRef.current.map(m => m.id));
            const merged = messagesRef.current.map(m => mappedById.get(m.id) ?? m);
            const newMsgs = mapped.filter(m => !existingIds.has(m.id));
            if (newMsgs.length > 0 || mappedById.size > 0) {
              setMessages([...merged, ...newMsgs]);
            }
          } else {
            setMessages(mapped);
          }
          setIsStreaming(false);
          setIsThinking(false);
          setSessionRef((result.ref as string) ?? null);
          setDbSessionId(sid);
        } else if (data.type === "detach_from_session_result") {
          setAttachedSessionId(null);
          setAttachedSessionMeta(null);
          // Keep viewingSessionId/Meta — return to view-only mode
        } else if (
          data.type === "session_message" &&
          (data as Record<string, unknown>).session_id
        ) {
          // Real-time message from an attached CLI session
          const sm = data as Record<string, unknown>;
          const smSessionId = sm.session_id as string;
          // Only append if we're attached to this session
          if (smSessionId && smSessionId === attachedSessionIdRef.current) {
            const msg = sm.message as Record<string, unknown> | undefined;
            if (msg) {
              const role = msg.role as string;
              const contentType = msg.content_type as string | undefined;
              const idx = msg.index as number | undefined;
              const msgId = `cli-msg-${idx ?? Date.now()}`;

              if (
                role === "assistant" &&
                contentType === "tool_use"
              ) {
                // Tool invocation — append to last assistant message's toolCalls + contentBlocks
                const toolUseId = (msg.tool_use_id as string) || msgId;
                setMessages((prev) => {
                  if (idx !== undefined && prev.some((m) => m.id === msgId))
                    return prev;
                  const lastIdx = prev.length - 1;
                  const last = lastIdx >= 0 ? prev[lastIdx] : null;
                  const toolCall: ToolCall = {
                    id: toolUseId,
                    tool_name: (msg.tool_name as string) || "unknown",
                    server_name: "builtin",
                    status: "calling",
                    arguments: tryParseJSON(msg.tool_input) as
                      | Record<string, unknown>
                      | undefined,
                  };
                  if (last?.role === "assistant") {
                    const updated = [...prev];
                    const blocks = [...(last.contentBlocks || [])];
                    const lastBlock = blocks[blocks.length - 1];
                    if (lastBlock?.type === "tool_chain") {
                      blocks[blocks.length - 1] = { ...lastBlock, calls: [...lastBlock.calls, toolCall] };
                    } else {
                      blocks.push({ type: "tool_chain" as const, calls: [toolCall] });
                    }
                    updated[lastIdx] = {
                      ...last,
                      toolCalls: [...(last.toolCalls || []), toolCall],
                      contentBlocks: blocks,
                    };
                    return updated;
                  }
                  return [
                    ...prev,
                    {
                      id: msgId,
                      role: "assistant" as const,
                      content: "",
                      timestamp: new Date(),
                      toolCalls: [toolCall],
                      contentBlocks: [{ type: "tool_chain" as const, calls: [toolCall] }],
                    },
                  ];
                });
              } else if (
                contentType === "tool_result" ||
                role === "tool"
              ) {
                // Tool result — prefer ID-based match, fall back to positional
                const resultToolUseId = msg.tool_use_id as string | undefined;
                setMessages((prev) => {
                  for (let i = prev.length - 1; i >= 0; i--) {
                    const m = prev[i];
                    if (m.role !== "assistant" || !m.toolCalls) continue;
                    // Prefer ID-based match when tool_use_id is available
                    const pendingIdx = resultToolUseId
                      ? m.toolCalls.findIndex((tc) => tc.id === resultToolUseId)
                      : m.toolCalls.findIndex((tc) => tc.status !== "completed");
                    if (pendingIdx < 0) continue;
                    const updated = [...prev];
                    const updatedCalls = [...m.toolCalls];
                    const callRef = {
                      ...updatedCalls[pendingIdx],
                      result: tryParseJSON(
                        msg.tool_result ?? msg.content,
                      ),
                      status: "completed" as const,
                    };
                    updatedCalls[pendingIdx] = callRef;
                    // Also update the call in contentBlocks
                    const blocks = [...(m.contentBlocks || [])];
                    for (let bi = 0; bi < blocks.length; bi++) {
                      const block = blocks[bi];
                      if (block.type === "tool_chain") {
                        const tcIdx = block.calls.findIndex((c) => c.id === callRef.id);
                        if (tcIdx >= 0) {
                          const updatedBlockCalls = [...block.calls];
                          updatedBlockCalls[tcIdx] = callRef;
                          blocks[bi] = { ...block, calls: updatedBlockCalls };
                          break;
                        }
                      }
                    }
                    updated[i] = { ...m, toolCalls: updatedCalls, contentBlocks: blocks };
                    return updated;
                  }
                  return prev;
                });
              } else if (role === "user" && contentType !== "tool_result") {
                // Regular user message
                setMessages((prev) => {
                  if (idx !== undefined && prev.some((m) => m.id === msgId))
                    return prev;
                  return [
                    ...prev,
                    {
                      id: msgId,
                      role: "user" as const,
                      content: (msg.content as string) ?? "",
                      timestamp: new Date(
                        (msg.timestamp as string) ?? Date.now(),
                      ),
                    },
                  ];
                });
              } else if (role === "assistant") {
                // Regular assistant text or thinking
                setMessages((prev) => {
                  if (idx !== undefined && prev.some((m) => m.id === msgId))
                    return prev;
                  if (contentType === "thinking") {
                    const lastIdx = prev.length - 1;
                    const last = lastIdx >= 0 ? prev[lastIdx] : null;
                    if (last?.role === "assistant") {
                      const updated = [...prev];
                      updated[lastIdx] = {
                        ...last,
                        thinkingContent:
                          (last.thinkingContent || "") +
                          ((msg.content as string) || ""),
                      };
                      return updated;
                    }
                    return [
                      ...prev,
                      {
                        id: msgId,
                        role: "assistant" as const,
                        content: "",
                        timestamp: new Date(),
                        thinkingContent: (msg.content as string) || "",
                      },
                    ];
                  }
                  // Regular text — append to existing assistant msg or create new
                  const lastIdx = prev.length - 1;
                  const last = lastIdx >= 0 ? prev[lastIdx] : null;
                  if (
                    last?.role === "assistant" &&
                    !last.toolCalls?.length
                  ) {
                    const updated = [...prev];
                    updated[lastIdx] = {
                      ...last,
                      content:
                        last.content + ((msg.content as string) ?? ""),
                    };
                    return updated;
                  }
                  return [
                    ...prev,
                    {
                      id: msgId,
                      role: "assistant" as const,
                      content: (msg.content as string) ?? "",
                      timestamp: new Date(
                        (msg.timestamp as string) ?? Date.now(),
                      ),
                    },
                  ];
                });
              }
            }
          }
        } else if (data.type === "send_to_cli_session_result") {
          const result = data as Record<string, unknown>;
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
  }, []);

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
            blocks[blocks.length - 1] = { ...lastBlock, content: lastBlock.content + chunk.content };
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
            contentBlocks: chunk.content ? [{ type: "text" as const, content: chunk.content }] : [],
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
      // Adopt SDK session_id as the canonical conversation ID
      if (
        chunk.sdk_session_id &&
        chunk.sdk_session_id !== conversationIdRef.current
      ) {
        conversationIdRef.current = chunk.sdk_session_id;
        setConversationId(chunk.sdk_session_id);
        saveConversationId(chunk.sdk_session_id);
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
        const newCall: ToolCall = {
          id: status.tool_call_id,
          tool_name: status.tool_name || "unknown",
          server_name: status.server_name || "builtin",
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
            contentBlocks: [{ type: "tool_chain" as const, calls: [newCall] }],
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
        callRef = {
          id: status.tool_call_id,
          tool_name: status.tool_name || "unknown",
          server_name: status.server_name || "builtin",
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
            const tcIdx = block.calls.findIndex((c) => c.id === status.tool_call_id);
            if (tcIdx >= 0) {
              const updatedCalls = [...block.calls];
              updatedCalls[tcIdx] = callRef;
              blocks[bi] = { ...block, calls: updatedCalls };
              break;
            }
          }
        }
      } else {
        // New tool call — append to last tool_chain or create new one
        const lastBlock = blocks[blocks.length - 1];
        if (lastBlock?.type === "tool_chain") {
          blocks[blocks.length - 1] = { ...lastBlock, calls: [...lastBlock.calls, callRef] };
        } else {
          blocks.push({ type: "tool_chain" as const, calls: [callRef] });
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
    saveDbSessionId(dbSessionId);
  }, [dbSessionId]);

  // Keep refs in sync
  useEffect(() => {
    attachedSessionIdRef.current = attachedSessionId;
  }, [attachedSessionId]);
  useEffect(() => {
    viewingSessionIdRef.current = viewingSessionId;
  }, [viewingSessionId]);

  // Switch to a different conversation
  const switchConversation = useCallback((id: string, dbSessionId?: string) => {
    if (!id) return;
    // Skip if already on this conversation with messages loaded
    if (
      id === conversationIdRef.current &&
      messagesRef.current.length > 0 &&
      !dbSessionId
    )
      return;

    // Stop partial streaming first
    activeRequestIdRef.current = null;
    setIsStreaming(false);
    setIsThinking(false);
    setSessionRef(null);
    setDbSessionId(dbSessionId ?? null);
    setCurrentBranch(null);
    setWorktreePath(null);
    setCanvasSurfaces(new Map());
    setCanvasPanel(null);
    setContextUsage({
      totalInputTokens: 0,
      outputTokens: 0,
      contextWindow: null,
      uncachedInputTokens: 0,
      cacheReadTokens: 0,
      cacheCreationTokens: 0,
    });

    conversationIdRef.current = id;
    setConversationId(id);
    setConversationSwitchKey((k) => k + 1);
    saveConversationId(id);

    // Clear messages; DB fetch below will populate
    setMessages([]);

    // Fetch from server when dbSessionId is available
    if (dbSessionId) {
      setIsLoadingMessages(true);
      const baseUrl = import.meta.env.VITE_API_BASE_URL || "";
      fetch(`${baseUrl}/api/sessions/${dbSessionId}/messages?limit=100&offset=0`)
        .then((res) => (res.ok ? res.json() : null))
        .then((data) => {
          if (!data?.messages?.length || conversationIdRef.current !== id)
            return;
          const mapped = mapApiMessages(data.messages);
          if (mapped.length > 0) {
            setMessages(mapped);
          }
        })
        .catch((err) =>
          console.error("Failed to fetch session messages:", err),
        )
        .finally(() => setIsLoadingMessages(false));

      // Hydrate context usage and chat mode from persisted session data
      fetch(`${baseUrl}/api/sessions/${dbSessionId}`)
        .then((res) => (res.ok ? res.json() : null))
        .then((data) => {
          const s = data?.session;
          if (!s || conversationIdRef.current !== id) return;
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
          // Restore chat mode from DB (corrects stale sessions list data)
          if (s.chat_mode) {
            const restored = s.chat_mode as ChatMode;
            // Only apply if mode actually differs from current
            if (restored !== currentModeRef.current) {
              currentModeRef.current = restored;
              onModeChangedRef.current?.(restored);
            }
            // Sync restored mode to backend — but skip if the server
            // just sent an authoritative mode_changed (avoids loop)
            if (
              wsRef.current?.readyState === WebSocket.OPEN &&
              Date.now() - lastServerModeTimestampRef.current > 2000
            ) {
              wsRef.current.send(
                JSON.stringify({
                  type: "set_mode",
                  mode: restored,
                  conversation_id: conversationIdRef.current,
                }),
              );
            }
          }
        })
        .catch(() => {});
    }
  }, []);

  // Start a new chat conversation, optionally with a specific agent
  const startNewChat = useCallback((agentName?: string) => {
    const newId = uuid();
    conversationIdRef.current = newId;
    setConversationId(newId);
    setConversationSwitchKey((k) => k + 1);
    saveConversationId(newId);
    setMessages([]);
    setSessionRef(null);
    setDbSessionId(null);
    setCurrentBranch(null);
    setWorktreePath(null);
    setCanvasSurfaces(new Map());
    setCanvasPanel(null);
    setContextUsage({
      totalInputTokens: 0,
      outputTokens: 0,
      contextWindow: null,
      uncachedInputTokens: 0,
      cacheReadTokens: 0,
      cacheCreationTokens: 0,
    });

    activeRequestIdRef.current = null;
    setIsStreaming(false);
    setIsThinking(false);
    setIsLoadingMessages(false);

    // Set active agent and always send set_agent so the backend resolves
    // the agent definition (preamble, rules, skills) for the web chat session.
    const effectiveAgent = agentName || "default-web-chat";
    setActiveAgent(effectiveAgent);
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(
        JSON.stringify({
          type: "set_agent",
          conversation_id: newId,
          agent_name: effectiveAgent,
        }),
      );
    }
  }, []);

  // Resume a CLI session (e.g., Claude) — sets the conversation ID
  // so the next message triggers server-side resume
  const resumeSession = useCallback((externalId: string) => {
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
    async (sourceDbSessionId: string, projectId?: string): Promise<string> => {
      const newConversationId = uuid();

      // Switch to new conversation
      conversationIdRef.current = newConversationId;
      setConversationId(newConversationId);
      setConversationSwitchKey((k) => k + 1);
      saveConversationId(newConversationId);
      activeRequestIdRef.current = null;
      setIsStreaming(false);
      setIsThinking(false);
      setMessages([]);

      // Fetch source session's messages for display
      const baseUrl = import.meta.env.VITE_API_BASE_URL || "";
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
      try {
        const sessionRes = await fetch(
          `${baseUrl}/api/sessions/${sourceDbSessionId}`,
        );
        if (sessionRes.ok) {
          const sessionData = await sessionRes.json();
          const s = sessionData?.session;
          if (
            s &&
            (s.usage_input_tokens > 0 ||
              s.usage_output_tokens > 0 ||
              s.context_window)
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
          // Restore chat mode from source session
          if (s?.chat_mode) {
            onModeChangedRef.current?.(s.chat_mode as ChatMode);
          }
        }
      } catch {
        // Best-effort — don't block continuation
      }

      // Tell backend to prepare the continuation session
      if (wsRef.current?.readyState === WebSocket.OPEN) {
        wsRef.current.send(
          JSON.stringify({
            type: "continue_in_chat",
            conversation_id: newConversationId,
            source_session_id: sourceDbSessionId,
            project_id: projectId,
          }),
        );
      }

      return newConversationId;
    },
    [],
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
    // Reset frontend state
    setMessages([]);
    setCanvasSurfaces(new Map());
    setCanvasPanel(null);
    setDbSessionId(null);
    saveDbSessionId(null);
    setContextUsage({
      totalInputTokens: 0,
      outputTokens: 0,
      contextWindow: null,
      uncachedInputTokens: 0,
      cacheReadTokens: 0,
      cacheCreationTokens: 0,
    });
    activeRequestIdRef.current = null;
    // Start a fresh conversation
    const newId = uuid();
    conversationIdRef.current = newId;
    setConversationId(newId);
    setConversationSwitchKey((k) => k + 1);
    saveConversationId(newId);
    return true;
  }, []);

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

      // If deleting the active conversation, start a new one
      if (id === conversationIdRef.current) {
        const newId = uuid();
        conversationIdRef.current = newId;
        setConversationId(newId);
        setConversationSwitchKey((k) => k + 1);
        saveConversationId(newId);
        setMessages([]);
        setSessionRef(null);
        setDbSessionId(null);
        setCurrentBranch(null);
        setWorktreePath(null);
        setCanvasSurfaces(new Map());
        setCanvasPanel(null);
        setContextUsage({
          totalInputTokens: 0,
          outputTokens: 0,
          contextWindow: null,
          uncachedInputTokens: 0,
          cacheReadTokens: 0,
          cacheCreationTokens: 0,
        });
        activeRequestIdRef.current = null;
        setIsStreaming(false);
        setIsThinking(false);
      }
      return true;
    },
    [],
  );

  // Stop the current streaming response
  const stopStreaming = useCallback(() => {
    if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) return;
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
      if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) {
        console.error(
          "WebSocket not connected, state:",
          wsRef.current?.readyState,
        );
        return false;
      }

      // Route to CLI session if attached (bidirectional messaging)
      if (attachedSessionIdRef.current) {
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
    [],
  );

  // Update sendMessageRef with the latest sendMessage callback
  sendMessageRef.current = sendMessage;

  // Respond to an AskUserQuestion pending in the backend
  const respondToQuestion = useCallback(
    (toolCallId: string, answers: Record<string, string>) => {
      if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) return;
      wsRef.current.send(
        JSON.stringify({
          type: "ask_user_response",
          conversation_id: conversationIdRef.current,
          tool_call_id: toolCallId,
          answers,
        }),
      );
    },
    [],
  );

  // Respond to a tool approval request
  const respondToApproval = useCallback(
    (toolCallId: string, decision: "approve" | "reject" | "approve_always") => {
      if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) return;
      wsRef.current.send(
        JSON.stringify({
          type: "tool_approval_response",
          conversation_id: conversationIdRef.current,
          tool_call_id: toolCallId,
          decision,
        }),
      );
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

  // Approve the current plan — tells backend to unlock write tools,
  // then sends a follow-up message to prompt the agent to begin execution.
  const approvePlan = useCallback(() => {
    if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) return;
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
    if (!planContentRef.current) return;
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
    if (viewingSessionIdRef.current === sessionId || attachedSessionIdRef.current === sessionId) {
      return;
    }

    // Detach from any active WS subscription first
    if (attachedSessionIdRef.current) {
      if (wsRef.current?.readyState === WebSocket.OPEN) {
        wsRef.current.send(
          JSON.stringify({
            type: "detach_from_session",
            session_id: attachedSessionIdRef.current,
          }),
        );
      }
      setAttachedSessionId(null);
      setAttachedSessionMeta(null);
    }

    // Reset chat state
    activeRequestIdRef.current = null;
    setIsStreaming(false);
    setIsThinking(false);
    setMessages([]);

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
      .catch((err) =>
        console.error("Failed to fetch session messages:", err),
      );

    // Fetch session metadata
    fetch(`${baseUrl}/api/sessions/${sessionId}`)
      .then((res) => (res.ok ? res.json() : null))
      .then((data) => {
        const s = data?.session;
        if (!s || viewingSessionIdRef.current !== sessionId) return;
        const ref = s.seq_num ? `#${s.seq_num}` : null;
        setSessionRef(ref);
        setViewingSessionMeta({
          ref,
          source: s.source || "unknown",
          title: s.title || null,
          status: s.status || "unknown",
          model: s.model || null,
          externalId: s.external_id || "",
          chatMode: s.chat_mode || null,
          gitBranch: s.git_branch || null,
          contextWindow: s.context_window || null,
        });
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
      .catch((err) =>
        console.error("Failed to fetch session metadata:", err),
      );
  }, []);

  // Clear viewing state and restore previous web chat
  const clearViewingSession = useCallback(() => {
    // Detach from any active WS subscription
    if (attachedSessionIdRef.current) {
      if (wsRef.current?.readyState === WebSocket.OPEN) {
        wsRef.current.send(
          JSON.stringify({
            type: "detach_from_session",
            session_id: attachedSessionIdRef.current,
          }),
        );
      }
      setAttachedSessionId(null);
      setAttachedSessionMeta(null);
    }

    setViewingSessionId(null);
    setViewingSessionMeta(null);
    setMessages([]);
    setSessionRef(null);
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
      fetch(`${baseUrl}/api/sessions/${prevDbSid}/messages?limit=100&offset=0`)
        .then((res) => (res.ok ? res.json() : null))
        .then((data) => {
          if (!data?.messages?.length) return;
          const mapped = mapApiMessages(data.messages);
          if (mapped.length > 0) setMessages(mapped);
        })
        .catch((err) =>
          console.error("Failed to restore messages:", err),
        );

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
  }, []);

  // Attach to a CLI session (interactive mode with WS subscription)
  const attachToSession = useCallback((sessionId: string) => {
    if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) return;

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
  }, []);

  // Attach to the currently viewed session (button click from view-only mode)
  const attachToViewed = useCallback(() => {
    const sid = viewingSessionIdRef.current;
    if (sid) {
      attachToSession(sid);
    }
  }, [attachToSession]);

  // Detach from the attached session — returns to view-only mode
  const detachFromSession = useCallback(() => {
    const sid = attachedSessionIdRef.current;
    if (!sid) return;

    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(
        JSON.stringify({
          type: "detach_from_session",
          session_id: sid,
        }),
      );
    }

    setAttachedSessionId(null);
    setAttachedSessionMeta(null);
    // Keep viewingSessionId and viewingSessionMeta — return to view-only mode
    // Messages stay as-is (snapshot of what was loaded)
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

  // Connect on mount
  useEffect(() => {
    connect();

    return () => {
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
    dbSessionId,
    currentBranch,
    worktreePath,
    isConnected,
    isStreaming,
    isThinking,
    isLoadingMessages,
    contextUsage,
    sendMessage,
    sendMode,
    sendProjectChange,
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
    resumeSession,
    continueSessionInChat,
    setOnModeChanged,
    setOnPlanReady,
    addSystemMessage,
    viewSession,
    clearViewingSession,
    viewingSessionId,
    viewingSessionMeta,
    attachToSession,
    attachToViewed,
    detachFromSession,
    attachedSessionId,
    attachedSessionMeta,
    wsRef,
    handleVoiceMessageRef,
    setOnChatDeleted,
    setOnChatCleared,
  };
}
