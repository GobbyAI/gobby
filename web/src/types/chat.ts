import type { GobbySession } from "../hooks/useSessions";
import type { PaletteItem } from "../hooks/useColonAutocomplete";

export type ChatMode = "accept_edits" | "bypass" | "plan";

export interface ChatModeInfo {
  id: ChatMode;
  label: string;
  description: string;
  level: number; // 0=plan, 1=act, 2=full-auto
}

export const CHAT_MODES: ChatModeInfo[] = [
  {
    id: "plan",
    label: "Plan",
    description: "Read-only planning mode",
    level: 0,
  },
  {
    id: "accept_edits",
    label: "Act",
    description: "Auto-approve reads and edits, prompt for write operations",
    level: 1,
  },
  {
    id: "bypass",
    label: "Auto",
    description: "Auto-approve all tools",
    level: 2,
  },
];

export interface ToolResult {
  content: unknown;
  content_type: string; // 'text' | 'json' | 'image' | 'error'
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
}

/**
 * Classify a tool name into a canonical type (bash, read, edit, mcp, etc.)
 * matching the backend logic in transcript_renderer.py.
 */
export function classifyTool(toolName: string | null | undefined): string {
  if (!toolName) return "unknown";
  const name = toolName.toLowerCase();

  // Built-in tools
  if (["bash", "sh", "terminal", "shell"].includes(name)) return "bash";
  if (["read", "read_file", "cat"].includes(name)) return "read";
  if (["edit", "write", "multiedit", "patch", "sed"].includes(name))
    return "edit";
  if (["grep", "rg", "search"].includes(name)) return "grep";
  if (["glob", "ls", "list_files", "find"].includes(name)) return "glob";

  // MCP tools: mcp__server__tool
  if (toolName.startsWith("mcp__")) return "mcp";

  return "unknown";
}

export type ContentBlock =
  | { type: "text"; content: string }
  | { type: "thinking"; content: string }
  | { type: "tool_chain"; tool_calls: ToolCall[] }
  | { type: "tool_reference"; tool_name: string; server_name: string }
  | {
      type: "image";
      source: { media_type: string; data: string; [key: string]: unknown };
    }
  | { type: "document"; source: { name?: string } & Record<string, unknown> }
  | { type: "web_search_result"; content: Record<string, unknown> }
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

export interface RenderedMessage {
  id: string;
  role: "user" | "assistant" | "system";
  content: string;
  timestamp: string;
  content_blocks?: ContentBlock[];
  model?: string | null;
  usage?: TokenUsage | null;
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

export interface QueuedFile {
  id: string;
  file: File;
  previewUrl: string | null;
  base64: string | null;
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
}

import type { A2UISurfaceState, UserAction } from "../components/canvas/types";
import type { CanvasPanelState } from "../components/canvas/hooks/useCanvasPanel";

export interface SessionObservationMeta {
  ref: string | null;
  source: string;
  title: string | null;
  status: string;
  model: string | null;
  externalId: string;
  chatMode?: string | null;
  gitBranch?: string | null;
  contextWindow?: number | null;
  agentRunId?: string | null;
  workflowName?: string | null;
  agentName?: string | null;
  sessionType?: "terminal" | "web_chat" | null;
}

export type SessionInteractionMode = "none" | "observe" | "proxy";

export interface SwappedSessionTarget {
  sessionId: string;
  sessionType?: string | null;
  agentRunId?: string | null;
}

export interface ChatState {
  messages: ChatMessage[];
  sessionRef: string | null;
  currentBranch: string | null;
  worktreePath: string | null;
  isStreaming: boolean;
  isThinking: boolean;
  isLoadingMessages?: boolean;
  isConnected: boolean;
  isReconnecting: boolean;
  contextUsage?: ContextUsage;
  onSend: (content: string, files?: QueuedFile[]) => void;
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
  canvasSurfaces: Map<string, A2UISurfaceState>;
  canvasPanel: CanvasPanelState | null;
  onCanvasInteraction: (canvasId: string, action: UserAction) => void;
  mode: ChatMode;
  onModeChange: (mode: ChatMode) => void;
  onModeChangeLocal?: (mode: ChatMode) => void;
  onWorktreeChange?: (worktreePath: string, worktreeId?: string) => void;
  activeAgent?: string;
  onAgentChange?: (agentName: string) => void;
  onSwitchProvider?: (provider: string) => void;
  continueSessionInChat?: (
    sourceDbSessionId: string,
    projectId?: string,
    options?: { provider?: string | null; model?: string | null },
  ) => Promise<string>;
  planPendingApproval: boolean;
  onApprovePlan: () => void;
  onRequestPlanChanges: (feedback: string) => void;
  setOnPlanReady?: (fn: (content: string | null) => void) => void;
  setOnArtifactEvent?: (
    fn: (
      type: string,
      content: string,
      language?: string,
      title?: string,
    ) => void,
  ) => void;
  provider?: string | null;
  onProviderChange?: (provider: string | null) => void;
  dbSessionId?: string | null;
  conversationSwitchKey?: number;
  viewSession?: (sessionId: string) => void;
  clearViewingSession?: () => void;
  viewingSessionId?: string | null;
  viewingSessionMeta?: SessionObservationMeta | null;
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
}

export interface ConversationState {
  sessions: GobbySession[];
  activeSessionId: string | null;
  deletingIds?: Set<string>;
  onNewChat: (agentName?: string) => void;
  onSelectSession: (session: GobbySession) => void;
  onDeleteSession?: (session: GobbySession) => void;
  onRenameSession?: (id: string, title: string) => void;
  agents: Array<{
    run_id: string;
    provider: string;
    pid?: number;
    mode?: string;
    started_at?: string;
    tmux_session_name?: string;
  }>;
  onNavigateToAgent: (agent: {
    run_id: string;
    session_id?: string;
    mode?: string;
    tmux_session_name?: string;
  }) => void;
  onKillAgent?: (runId: string) => void;
  onExpireSession?: (sessionId: string) => void;
  cliSessions?: GobbySession[];
  viewingSessionId?: string | null;
  attachedSessionId?: string | null;
  onViewCliSession?: (session: GobbySession) => void;
  onDetachFromSession?: () => void;
}

export interface ProjectProps {
  projects: ProjectOption[];
  selectedProjectId: string | null;
  onProjectChange: (projectId: string) => void;
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
  startRecording?: () => Promise<void>;
  stopRecording?: () => Promise<void>;
  cancelRecording?: () => void;
  stopTTS?: () => void;
}
