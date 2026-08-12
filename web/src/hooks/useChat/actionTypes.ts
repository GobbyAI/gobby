import type { Dispatch, MutableRefObject, SetStateAction } from "react";
import type {
  ApprovalOption,
  ChatMessage,
  ChatMode,
  ContextUsage,
  FallbackContextMode,
  QueuedFile,
  SessionInteractionMode,
  SessionObservationMeta,
} from "../../types/chat";
import type { PendingProxyMessage } from "./pendingProxyMessages";
import type { ContinuationRollbackSnapshot } from "./sessionRecords";

export type Setter<T> = Dispatch<SetStateAction<T>>;
type SetCurrentMode = (mode: ChatMode) => void;

export interface EnsureMainSessionOptions {
  projectId?: string | null;
  provider?: string | null;
  model?: string | null;
  reasoningEffort?: string | null;
  forceNew?: boolean;
}

interface PendingChatMessage {
  messageId: string;
  content: string;
  model?: string | null;
  files?: QueuedFile[];
  projectId?: string | null;
  injectContext?: string;
  reasoningEffort?: string | null;
  ttsEnabled?: boolean;
}

export interface UseChatActionsParams {
  activeRequestIdRef: MutableRefObject<string | null>;
  applyMainSessionMeta: (session: Record<string, unknown> | null) => void;
  attachedSessionId: string | null;
  attachedSessionIdRef: MutableRefObject<string | null>;
  attachedSessionMeta: SessionObservationMeta | null;
  attachedSessionMetaRef: MutableRefObject<SessionObservationMeta | null>;
  bindActiveSession: (sessionId: string | null) => void;
  clearPreAttachContextUsage: () => void;
  clearSessionObservationState: (options?: {
    preserveViewing?: boolean;
  }) => void;
  contextUsage: ContextUsage;
  continuingSessionIdRef: MutableRefObject<string | null>;
  continuationRollbackRef: MutableRefObject<ContinuationRollbackSnapshot | null>;
  conversationId: string;
  conversationIdRef: MutableRefObject<string>;
  currentBranch: string | null;
  currentModeRef: MutableRefObject<ChatMode>;
  dbSessionId: string | null;
  dbSessionIdRef: MutableRefObject<string | null>;
  ensureMainSession: (
    options: EnsureMainSessionOptions,
  ) => Promise<string | null>;
  isStreaming: boolean;
  lastSeqRef: MutableRefObject<number>;
  lastServerModeTimestampRef: MutableRefObject<number>;
  mainSessionMeta: SessionObservationMeta | null;
  messages: ChatMessage[];
  messagesRef: MutableRefObject<ChatMessage[]>;
  observedSessionId: string | null;
  observedSessionMetaRef: MutableRefObject<SessionObservationMeta | null>;
  onModeChangedRef: MutableRefObject<((mode: ChatMode) => void) | null>;
  pendingMessagesRef: MutableRefObject<PendingChatMessage[]>;
  pendingPlanFeedbackRef: MutableRefObject<string | null>;
  pendingProxyMessagesRef: MutableRefObject<Map<string, PendingProxyMessage>>;
  pendingProxySessionQueuesRef: MutableRefObject<Map<string, string[]>>;
  planContentRef: MutableRefObject<string | null>;
  planToolCallIdRef: MutableRefObject<string | null>;
  projectIdRef: MutableRefObject<string | null>;
  proxyDeliveryNotice: string | null;
  resetMainChatState: () => void;
  restoreContinuationState: (snapshot: ContinuationRollbackSnapshot) => void;
  selectedProvider: string | null;
  selectedProviderRef: MutableRefObject<string | null>;
  sendMessageRef: MutableRefObject<SendMessageAction | null>;
  sessionInteractionMode: SessionInteractionMode;
  sessionInteractionModeRef: MutableRefObject<SessionInteractionMode>;
  sessionRef: string | null;
  sessionTitle: string | null;
  setActiveAgent: Setter<string>;
  setContextUsage: Setter<ContextUsage>;
  setConversationId: Setter<string>;
  setConversationSwitchKey: Setter<number>;
  setCurrentMode: SetCurrentMode;
  setIsContinuingSession: Setter<boolean>;
  setIsLoadingMessages: Setter<boolean>;
  setIsStreaming: Setter<boolean>;
  setIsThinking: Setter<boolean>;
  setMessages: Setter<ChatMessage[]>;
  setPlanPendingApproval: Setter<boolean>;
  setProxyDeliveryNotice: Setter<string | null>;
  setSelectedProvider: Setter<string | null>;
  viewingSessionId: string | null;
  viewingSessionIdRef: MutableRefObject<string | null>;
  viewingSessionMeta: SessionObservationMeta | null;
  worktreePath: string | null;
  wsRef: MutableRefObject<WebSocket | null>;
}

export interface SwitchConversationOptions {
  preserveViewing?: boolean;
}

export interface SwitchProviderOptions {
  model?: string | null;
  reasoningEffort?: string | null;
}

export interface ContinueSessionOptions {
  provider?: string | null;
  model?: string | null;
  reasoningEffort?: string | null;
  chatMode?: ChatMode | null;
  fallbackContext?: FallbackContextMode;
}

export type StartNewChatAction = (agentName?: string) => void;

export type SwitchConversationAction = (
  id: string,
  options?: SwitchConversationOptions,
) => void;

export type SwitchProviderAction = (
  newProvider: string,
  options?: SwitchProviderOptions,
) => Promise<void>;

export type ResumeSessionAction = (externalId: string) => void;

export type ContinueSessionInChatAction = (
  sourceDbSessionId: string,
  projectId?: string,
  options?: ContinueSessionOptions,
) => Promise<string>;

export type DeleteConversationAction = (
  id: string,
  sessionId?: string,
) => boolean;

export type SendModeAction = (mode: ChatMode) => boolean;

export type SendAttachedSessionModeAction = (
  targetSessionId: string,
  mode: ChatMode,
) => void;

export type SendProjectChangeAction = (projectId: string) => void;

export type SendAgentChangeAction = (agentName: string) => void;

export type SendWorktreeChangeAction = (
  worktreePath: string,
  worktreeId?: string,
) => void;

export type SendMessageAction = (
  content: string,
  model?: string | null,
  files?: QueuedFile[],
  projectId?: string | null,
  injectContext?: string,
  reasoningEffort?: string | null,
  ttsEnabled?: boolean,
  optimisticMessageId?: string,
) => boolean;

export type RespondToQuestionAction = (
  toolCallId: string,
  answers: Record<string, string>,
) => boolean;

export type ApprovalDecision = "approve" | "reject" | "approve_always";

export type RespondToApprovalAction = (
  toolCallId: string,
  decision: ApprovalDecision,
) => boolean;

export type ApprovePlanAction = (option?: ApprovalOption) => void;

export type RequestPlanChangesAction = (feedback: string) => void;

export interface ConversationActions {
  switchConversation: SwitchConversationAction;
  startNewChat: StartNewChatAction;
  switchProvider: SwitchProviderAction;
  resumeSession: ResumeSessionAction;
  continueSessionInChat: ContinueSessionInChatAction;
}

export interface ChatControlActions {
  clearHistory: () => boolean;
  deleteConversation: DeleteConversationAction;
  stopStreaming: () => void;
  sendMode: SendModeAction;
  sendAttachedSessionMode: SendAttachedSessionModeAction;
  sendProjectChange: SendProjectChangeAction;
  sendAgentChange: SendAgentChangeAction;
  sendWorktreeChange: SendWorktreeChangeAction;
  respondToQuestion: RespondToQuestionAction;
  respondToApproval: RespondToApprovalAction;
}

export interface PlanApprovalActions {
  approvePlan: ApprovePlanAction;
  requestPlanChanges: RequestPlanChangesAction;
}

export interface UseChatActionsResult
  extends ConversationActions, ChatControlActions, PlanApprovalActions {
  sendMessage: SendMessageAction;
}

export type UseChatActions = UseChatActionsResult;
