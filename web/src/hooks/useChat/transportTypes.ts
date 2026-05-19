import type {
  Dispatch,
  MutableRefObject,
  SetStateAction,
} from "react";
import type { CanvasPanelState } from "../../components/canvas/hooks/useCanvasPanel";
import type { A2UISurfaceState } from "../../components/canvas/types";
import type {
  ChatMessage,
  ChatMode,
  QueuedFile,
  SessionInteractionMode,
  SessionObservationMeta,
} from "../../types/chat";
import type {
  ChatError,
  ChatStreamChunk,
  ChatThinkingMessage,
  ContextUsage,
  ContinuationRollbackSnapshot,
  ModelSwitchedMessage,
  PendingProxyMessage,
  ToolStatusMessage,
} from "./core";

export type Setter<T> = Dispatch<SetStateAction<T>>;

export interface QueuedTransportMessage {
  content: string;
  model?: string | null;
  files?: QueuedFile[];
  projectId?: string | null;
  reasoningEffort?: string | null;
  ttsEnabled?: boolean;
}

export type SendMessage = (
  content: string,
  model?: string | null,
  files?: QueuedFile[],
  projectId?: string | null,
  injectContext?: string,
  reasoningEffort?: string | null,
  ttsEnabled?: boolean,
) => boolean;

export type ArtifactEventCallback = (
  type: string,
  content: string,
  language?: string,
  title?: string,
) => void;

export interface UseChatTransportParams {
  activeRequestIdRef: MutableRefObject<string | null>;
  applyMainSessionMeta: (session: Record<string, unknown> | null) => void;
  attachedSessionIdRef: MutableRefObject<string | null>;
  attachedSessionMetaRef: MutableRefObject<SessionObservationMeta | null>;
  clearContinuationRollback: () => void;
  clearContinuingSession: () => void;
  conversationIdRef: MutableRefObject<string>;
  continuingSessionIdRef: MutableRefObject<string | null>;
  continuationRollbackRef: MutableRefObject<ContinuationRollbackSnapshot | null>;
  currentModeRef: MutableRefObject<ChatMode>;
  dbSessionIdRef: MutableRefObject<string | null>;
  handleBinaryMessageRef: MutableRefObject<(data: ArrayBuffer) => void>;
  handleChatErrorRef: MutableRefObject<(error: ChatError) => void>;
  handleChatStreamRef: MutableRefObject<(chunk: ChatStreamChunk) => void>;
  handleChatThinkingRef: MutableRefObject<(msg: ChatThinkingMessage) => void>;
  handleModelSwitchedRef: MutableRefObject<(msg: ModelSwitchedMessage) => void>;
  handleToolStatusRef: MutableRefObject<(status: ToolStatusMessage) => void>;
  handleVoiceMessageRef: MutableRefObject<(data: Record<string, unknown>) => void>;
  lastSeqRef: MutableRefObject<number>;
  lastServerModeTimestampRef: MutableRefObject<number>;
  markSessionUsageFresh: (sessionId: string, rawTimestamp?: string) => void;
  messagesRef: MutableRefObject<ChatMessage[]>;
  observedSessionIdRef: MutableRefObject<string | null>;
  observedSessionMetaRef: MutableRefObject<SessionObservationMeta | null>;
  onArtifactEventRef: MutableRefObject<ArtifactEventCallback | null>;
  onChatClearedRef: MutableRefObject<((conversationId: string) => void) | null>;
  onChatDeletedRef: MutableRefObject<((conversationId: string) => void) | null>;
  onModeChangedRef: MutableRefObject<((mode: ChatMode) => void) | null>;
  onPlanReadyRef: MutableRefObject<((content: string | null) => void) | null>;
  pendingMessagesRef: MutableRefObject<QueuedTransportMessage[]>;
  pendingPlanFeedbackRef: MutableRefObject<string | null>;
  pendingProxyMessagesRef: MutableRefObject<Map<string, PendingProxyMessage>>;
  pendingProxySessionQueuesRef: MutableRefObject<Map<string, string[]>>;
  pendingSessionInteractionModeRef: MutableRefObject<"observe" | "proxy">;
  planContentRef: MutableRefObject<string | null>;
  preAttachContextUsageRef: MutableRefObject<ContextUsage | null>;
  reconnectTimeoutRef: MutableRefObject<number | null>;
  reportTransportError: (message: string) => void;
  resolveAgentName: (agentRunId: string) => Promise<string | null>;
  restoreContinuationState: (snapshot: ContinuationRollbackSnapshot) => void;
  sendMessageRef: MutableRefObject<SendMessage | null>;
  sessionInteractionModeRef: MutableRefObject<SessionInteractionMode>;
  sessionRefRef: MutableRefObject<string | null>;
  setActiveAgent: Setter<string>;
  setAttachedSessionId: Setter<string | null>;
  setAttachedSessionMeta: Setter<SessionObservationMeta | null>;
  setCanvasPanel: Setter<CanvasPanelState | null>;
  setCanvasSurfaces: Setter<Map<string, A2UISurfaceState>>;
  setContextUsage: Setter<ContextUsage>;
  setConversationId: Setter<string>;
  setCurrentBranch: Setter<string | null>;
  setDbSessionId: Setter<string | null>;
  setIsConnected: Setter<boolean>;
  setIsReconnecting: Setter<boolean>;
  setIsStreaming: Setter<boolean>;
  setIsThinking: Setter<boolean>;
  setMainSessionMeta: Setter<SessionObservationMeta | null>;
  setMessages: Setter<ChatMessage[]>;
  setObservedSessionId: Setter<string | null>;
  setPlanPendingApproval: Setter<boolean>;
  setProxyDeliveryNotice: Setter<string | null>;
  setSelectedProvider: Setter<string | null>;
  setSessionInteractionMode: Setter<SessionInteractionMode>;
  setSessionRef: Setter<string | null>;
  setSessionTitle: Setter<string | null>;
  setViewingSessionId: Setter<string | null>;
  setViewingSessionMeta: Setter<SessionObservationMeta | null>;
  setWorktreePath: Setter<string | null>;
  viewingSessionIdRef: MutableRefObject<string | null>;
  viewingSessionMetaRef: MutableRefObject<SessionObservationMeta | null>;
  wsRef: MutableRefObject<WebSocket | null>;
}

export type TransportConnectRef = MutableRefObject<(() => void) | null>;
