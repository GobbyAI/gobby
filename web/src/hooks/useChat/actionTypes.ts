import type { Dispatch, SetStateAction } from "react";
import type {
  ApprovalOption,
  ChatMessage,
  ChatMode,
  FallbackContextMode,
  QueuedFile,
} from "../../types/chat";

export type Setter<T> = Dispatch<SetStateAction<T>>;

export interface EnsureMainSessionOptions {
  projectId?: string | null;
  provider?: string | null;
  model?: string | null;
  reasoningEffort?: string | null;
  forceNew?: boolean;
}

export interface UseChatActionsParams extends Record<string, any> {
  ensureMainSession: (
    options: EnsureMainSessionOptions,
  ) => Promise<string | null>;
  setConversationSwitchKey: Setter<number>;
  setMessages: Setter<ChatMessage[]>;
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
  chatMode?: string | null;
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

export type SendModeAction = (mode: ChatMode) => void;

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
