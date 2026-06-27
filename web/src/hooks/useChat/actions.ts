import { useChatControlActions } from "./actionControls";
import { useConversationActions } from "./actionConversations";
import { useMessageAction } from "./actionMessage";
import { usePlanApprovalActions } from "./actionPlanApproval";
import type { UseChatActions, UseChatActionsParams } from "./actionTypes";

export type { UseChatActions, UseChatActionsParams, UseChatActionsResult } from "./actionTypes";

export function useChatActions(params: UseChatActionsParams): UseChatActions {
  const conversationActions = useConversationActions(params);
  const controlActions = useChatControlActions(
    params,
    conversationActions.startNewChat,
  );
  const sendMessage = useMessageAction(params);
  const planActions = usePlanApprovalActions(params);

  return {
    switchConversation: conversationActions.switchConversation,
    startNewChat: conversationActions.startNewChat,
    switchProvider: conversationActions.switchProvider,
    resumeSession: conversationActions.resumeSession,
    continueSessionInChat: conversationActions.continueSessionInChat,
    clearHistory: controlActions.clearHistory,
    deleteConversation: controlActions.deleteConversation,
    stopStreaming: controlActions.stopStreaming,
    sendMode: controlActions.sendMode,
    sendSessionConfigOption: controlActions.sendSessionConfigOption,
    sendAcpAuthenticate: controlActions.sendAcpAuthenticate,
    sendAcpLogout: controlActions.sendAcpLogout,
    sendAttachedSessionMode: controlActions.sendAttachedSessionMode,
    sendProjectChange: controlActions.sendProjectChange,
    sendAgentChange: controlActions.sendAgentChange,
    sendWorktreeChange: controlActions.sendWorktreeChange,
    sendMessage,
    respondToQuestion: controlActions.respondToQuestion,
    respondToApproval: controlActions.respondToApproval,
    approvePlan: planActions.approvePlan,
    requestPlanChanges: planActions.requestPlanChanges,
  };
}
