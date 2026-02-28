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
    description: "Auto-approve edits, prompt for dangerous commands",
    level: 1,
  },
  {
    id: "bypass",
    label: "Full Auto",
    description: "Auto-approve all tools",
    level: 2,
  },
];

export interface ToolCall {
  id: string;
  tool_name: string;
  server_name: string;
  status: "calling" | "completed" | "error" | "pending_approval";
  arguments?: Record<string, unknown>;
  result?: unknown;
  error?: string;
}

export type ContentBlock =
  | { type: "text"; content: string }
  | { type: "tool_chain"; calls: ToolCall[] }
  | { type: "image"; src: string; alt?: string };

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
}

export interface ChatState {
  messages: ChatMessage[];
  sessionRef: string | null;
  currentBranch: string | null;
  worktreePath: string | null;
  isStreaming: boolean;
  isThinking: boolean;
  isConnected: boolean;
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
  onWorktreeChange?: (worktreePath: string, worktreeId?: string) => void;
  activeAgent?: string;
  onAgentChange?: (agentName: string) => void;
  planPendingApproval: boolean;
  onApprovePlan: () => void;
  onRequestPlanChanges: (feedback: string) => void;
  setOnPlanReady?: (fn: (content: string | null) => void) => void;
  viewingSessionId?: string | null;
  viewingSessionMeta?: SessionObservationMeta | null;
  attachedSessionId?: string | null;
  attachedSessionMeta?: SessionObservationMeta | null;
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
  voiceMode?: boolean;
  voiceAvailable?: boolean;
  isListening?: boolean;
  isSpeechDetected?: boolean;
  isTranscribing?: boolean;
  voiceError?: string | null;
  onToggleVoice?: () => void;
}
