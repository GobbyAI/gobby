import type { GobbySession } from "./sessions";
import type { PaletteItem } from "../hooks/useColonAutocomplete";

export type ChatMode = "bypass" | "normal" | "plan";

export interface ChatModeInfo {
  id: ChatMode;
  label: string;
  description: string;
  level: number; // 0=plan, 1=act, 2=yolo
}

/**
 * One selectable plan-acceptance choice. Sourced from the backend registry
 * (gobby.adapters.plan_options) and emitted in the plan_pending_approval
 * payload; the frontend renders whatever it is given and echoes the id back as
 * option_id. The uniform set is Approve (YOLO) and Approve (Act); Reject is a
 * separate request-changes action, not an option. Mirrors the ChatModeInfo
 * static-metadata pattern.
 */
export interface ApprovalOption {
  id: string;
  label: string;
  description?: string;
  /** "approve" exits plan mode into the option's execution mode. */
  decision?: "approve";
  /**
   * Button-hierarchy hint: "primary" is the dominant solid CTA (YOLO),
   * "accent" is the quieter tinted action (Act). Absent -> tinted accent.
   */
  emphasis?: "primary" | "accent";
}

export const CHAT_MODES: ChatModeInfo[] = [
  {
    id: "plan",
    label: "Plan",
    description: "Read-only planning mode",
    level: 0,
  },
  {
    id: "normal",
    label: "Act",
    description: "Prompt for non-exempt tool use",
    level: 1,
  },
  {
    id: "bypass",
    label: "YOLO",
    description: "Run without approval prompts",
    level: 2,
  },
];

export const AUTONOMOUS_CHAT_MODES: ChatModeInfo[] = CHAT_MODES.filter(
  (mode) => mode.id !== "normal",
);

export function normalizeChatMode(mode: string | null | undefined): ChatMode {
  if (mode === "act") return "normal";
  if (mode === "accept_edits") return "normal";
  if (mode === "yolo") return "bypass";
  if (mode === "bypass" || mode === "normal" || mode === "plan") return mode;
  return "plan";
}

export type ToolResultKind = "text" | "json" | "image" | "error";

export interface ToolResult {
  content: unknown;
  kind: ToolResultKind;
  truncated: boolean;
  metadata?: Record<string, unknown>; // exit_code, line_count, etc.
}

export interface ToolCall {
  id: string;
  tool_name: string;
  server_name: string;
  tool_type: string; // NEW: 'bash', 'read', 'edit', 'mcp', etc.
  status: "calling" | "completed" | "error" | "pending" | "pending_approval";
  arguments?: Record<string, unknown>;
  result?: ToolResult; // NEW: typed result instead of unknown
  error?: string;
  tool_kind?: string;
  locations?: Record<string, unknown>[];
  content_blocks?: ContentBlock[];
  raw_output?: unknown;
}

/**
 * Classify a tool name into a canonical type (bash, read, edit, mcp, etc.)
 * matching the backend logic in transcript_renderer.py.
 */
export function classifyTool(toolName: string | null | undefined): string {
  if (!toolName) return "unknown";
  const name = toolName.toLowerCase();

  if (name === "protocol_context" || name === "protocol") return "protocol";

  // Built-in tools
  if (
    [
      "bash",
      "sh",
      "terminal",
      "shell",
      "run_command",
      "run_shell_command",
      "runshellcommand",
      "shelltool",
      "commandexecution",
      "exec_command",
    ].includes(name)
  ) {
    return "bash";
  }
  if (["read", "read_file", "cat"].includes(name)) return "read";
  if (["edit", "write", "multiedit", "patch", "sed"].includes(name))
    return "edit";
  if (["grep", "rg", "search"].includes(name)) return "grep";
  if (["glob", "ls", "list_files", "find"].includes(name)) return "glob";

  // MCP tools: mcp__server__tool
  if (name.startsWith("mcp__")) return "mcp";

  return "unknown";
}

export type ContentBlock =
  | { type: "text"; content: string }
  | { type: "thinking"; content: string }
  | { type: "compaction_summary"; content: string }
  | { type: "tool_chain"; tool_calls: ToolCall[] }
  | { type: "tool_reference"; tool_name: string; server_name: string }
  | { type: "attachment"; attachment: ChatAttachment }
  | {
      type: "image";
      source?: { media_type?: string; data?: string; [key: string]: unknown };
      image_url?: string | { url?: string };
      url?: string;
    }
  | { type: "document"; source: { name?: string } & Record<string, unknown> }
  | { type: "web_search_result"; content: Record<string, unknown> }
  | {
      type: "resource_link";
      uri: string;
      name?: string;
      description?: string;
      mime_type?: string;
    }
  | { type: "resource"; resource: Record<string, unknown> }
  | {
      type: "audio";
      data?: string;
      url?: string;
      mime_type?: string;
    }
  | {
      type: "diff";
      path?: string;
      old_text?: string;
      new_text?: string;
    }
  | { type: "terminal"; terminal_id?: string }
  | {
      type: "unknown";
      block_type: string;
      raw: Record<string, unknown>;
      source_line?: number;
    };

export interface TokenUsage {
  input_tokens: number;
  output_tokens: number;
  cache_creation_tokens?: number;
  cache_read_tokens?: number;
}

export interface ChatMessage {
  id: string;
  role: "user" | "assistant" | "system";
  content: string;
  timestamp: Date;
  toolCalls?: ToolCall[];
  thinkingContent?: string;
  contentBlocks?: ContentBlock[];
}

export interface ChatAttachment {
  id: string;
  project_id: string;
  filename: string;
  mime_type: string;
  size_bytes: number;
  content_url: string;
}

export interface QueuedFile {
  id: string;
  file: File;
  previewUrl: string | null;
  status: "uploading" | "uploaded" | "error";
  progress: number | null;
  attachment: ChatAttachment | null;
  error: string | null;
  uploadAbort: (() => void) | null;
}

export interface ChatSendOptions {
  reasoningEffort?: string | null;
  ttsEnabled?: boolean;
}

export interface AcpAvailableCommandInput {
  hint: string;
}

export interface AcpAvailableCommand {
  name: string;
  description: string;
  input?: AcpAvailableCommandInput | null;
}

export interface ProjectOption {
  id: string;
  name: string;
}

export interface ContextUsage {
  totalInputTokens: number;
  outputTokens: number;
  contextWindow: number | null;
  // Cache breakdown for tooltip
  uncachedInputTokens: number;
  cacheReadTokens: number;
  cacheCreationTokens: number;
  contextUsageRatio?: number | null;
  contextUsageSource?: string | null;
  contextUsageConfidence?: string | null;
}

export interface SessionObservationMeta {
  ref: string | null;
  source: string;
  title: string | null;
  status: string;
  canProxyAttach?: boolean;
  model: string | null;
  reasoningEffort?: string | null;
  externalId: string;
  chatMode?: ChatMode | null;
  gitBranch?: string | null;
  contextWindow?: number | null;
  updatedAt?: string | null;
  agentRunId?: string | null;
  workflowName?: string | null;
  agentName?: string | null;
  sessionType?: "terminal" | "web_chat" | null;
}

export type SessionInteractionMode = "none" | "observe" | "proxy";
export type FallbackContextMode = "auto" | "summary" | "handoff" | "none";

export interface SwappedSessionTarget {
  sessionId: string;
  sessionType?: string | null;
  agentRunId?: string | null;
}

export interface ChatState {
  messages: ChatMessage[];
  sessionRef: string | null;
  sessionTitle?: string | null;
  currentBranch: string | null;
  worktreePath: string | null;
  isStreaming: boolean;
  isThinking: boolean;
  isLoadingMessages?: boolean;
  isConnected: boolean;
  isReconnecting: boolean;
  contextUsage?: ContextUsage;
  contextUsageUpdatedAt?: number | null;
  onSend: (
    content: string,
    files?: QueuedFile[],
    options?: ChatSendOptions,
  ) => void;
  addSystemMessage: (content: string) => void;
  onStop: () => void;
  onRespondToQuestion: (
    toolCallId: string,
    answers: Record<string, string>,
  ) => void;
  onRespondToApproval: (
    toolCallId: string,
    decision: "approve" | "reject" | "approve_always",
  ) => void;
  onInputChange: (value: string) => void;
  paletteItems: PaletteItem[];
  onPaletteSelect: (item: PaletteItem) => void;
  acpAvailableCommands?: AcpAvailableCommand[];
  mode: ChatMode;
  onModeChange: (mode: ChatMode) => void;
  onModeChangeLocal?: (mode: ChatMode) => void;
  onWorktreeChange?: (worktreePath: string, worktreeId?: string) => void;
  activeAgent?: string;
  onAgentChange?: (agentName: string) => void;
  onSwitchProvider?: (
    provider: string,
    options?: { model?: string | null; reasoningEffort?: string | null },
  ) => void;
  continueSessionInChat?: (
    sourceDbSessionId: string,
    projectId?: string,
    options?: {
      provider?: string | null;
      model?: string | null;
      reasoningEffort?: string | null;
      chatMode?: ChatMode | null;
      fallbackContext?: FallbackContextMode;
    },
  ) => Promise<string>;
  planPendingApproval: boolean;
  /** Authoritative approval signal (backend plan_approved); distinguishes approve from reject (#15681). */
  planApproved: boolean;
  planApprovalOptions?: ApprovalOption[];
  onApprovePlan: (option?: ApprovalOption) => void;
  onRequestPlanChanges: (feedback: string) => void;
  setOnPlanReady?: (fn: (content: string | null) => void) => void;
  provider?: string | null;
  onProviderChange?: (provider: string | null) => void;
  dbSessionId?: string | null;
  conversationSwitchKey?: number;
  viewSession?: (
    sessionId: string,
    options?: { forceRefresh?: boolean },
  ) => void;
  clearViewingSession?: () => void;
  mainSessionMeta?: SessionObservationMeta | null;
  viewingSessionId?: string | null;
  viewingSessionMeta?: SessionObservationMeta | null;
  isContinuingSession?: boolean;
  attachedSessionId?: string | null;
  attachedSessionMeta?: SessionObservationMeta | null;
  sessionInteractionMode?: SessionInteractionMode;
  proxyDeliveryNotice?: string | null;
  observeSession?: (
    sessionId: string,
    mode?: Exclude<SessionInteractionMode, "none">,
  ) => void;
  onAttachToViewed?: () => void;
  onDetachFromSession?: () => void;
  onAttachedModeChange?: (mode: ChatMode) => void;
}

export interface ConversationState {
  sessions: GobbySession[];
  activeSessionId: string | null;
  deletingIds?: Set<string>;
  onNewChat: (agentName?: string) => void;
  onSelectSession: (session: GobbySession) => void;
  onDeleteSession?: (session: GobbySession) => void;
  onRenameSession?: (id: string, title: string) => void;
  onKillAgent?: (runId: string) => Promise<boolean | void> | boolean | void;
  onExpireSession?: (
    sessionId: string,
  ) => Promise<boolean | void> | boolean | void;
  // ACP-backed session lifecycle, distinct from `onDeleteSession` (which deletes
  // a chat conversation). Targets an ACP row by canonical session id.
  onAcpCloseSession?: (
    sessionId: string,
  ) => Promise<boolean | void> | boolean | void;
  onAcpDeleteSession?: (
    sessionId: string,
  ) => Promise<boolean | void> | boolean | void;
  viewingSessionId?: string | null;
  attachedSessionId?: string | null;
}

export interface VoiceProps {
  sttEnabled?: boolean;
  ttsEnabled?: boolean;
  voiceInputMode?: "ptt" | "vad";
  voiceAvailable?: boolean;
  voiceReady?: boolean;
  voiceLoading?: boolean;
  isListening?: boolean;
  isSpeechDetected?: boolean;
  isRecording?: boolean;
  isTranscribing?: boolean;
  isSpeaking?: boolean;
  voiceError?: string | null;
  prepareTTSPlayback?: () => void;
  startRecording?: () => Promise<void>;
  stopRecording?: () => Promise<void>;
  cancelRecording?: () => void;
  stopTTS?: () => void;
}
