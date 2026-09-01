import { useEffect, useRef } from "react";

import { useIsMobile } from "../../hooks/useIsMobile";
import { useConfirmDialog } from "../../hooks/useConfirmDialog";
import { useFileChanges } from "../../hooks/useFileChanges";
import { ActivityPanel } from "../activity/ActivityPanel";
import { useActivityPanel } from "../activity/useActivityPanel";
import { AppErrorBoundary } from "../app/AppErrorBoundary";
import { Heading } from "../shared/Heading";
import { CommandPalette } from "./CommandPalette";
import { ChatMainColumn } from "./ChatMainColumn";
import { type MessageListHandle } from "./MessageList";
import type { ChatPageProps } from "./ChatPage.types";
import { useChatPagePlans } from "./useChatPagePlans";
import { useChatPageCommandPalette } from "./useChatPageCommandPalette";
import { useChatPageProviderState } from "./useChatPageProviderState";
import { useChatPageSessionRouting } from "./useChatPageSessionRouting";
import { useChatPageVoiceStatus } from "./useChatPageVoiceStatus";

export function ChatPage({
  chat,
  conversations,
  voice,
  projectId,
  projectName,
  projectHasCheckout,
  showPlanRef,
  agentDefinitions = [],
  agentGlobalDefs = [],
  agentProjectDefs = [],
  agentShowScopeToggle = false,
  agentHasGlobal = false,
  agentHasProject = false,
  currentModel = "opus",
  onModelChange,
  reasoningPreferences = {},
  onReasoningPreferenceChange,
  paletteActions = [],
  allProjectSessions = [],
  allProjectSessionsLoading = false,
  activitySessions,
  activitySessionsLoading,
  sessionsFilters,
  onSessionsFiltersChange,
  mcp,
  requestedActivityTab,
  onActivityTabRequestHandled,
  onSttEnabledChange,
  onTtsEnabledChange,
  onVoiceInputModeChange,
  planPendingVariant,
}: ChatPageProps) {
  const messageListRef = useRef<MessageListHandle>(null);
  const lastAutoScrolledLoadRef = useRef<string | null>(null);
  const isMobile = useIsMobile();
  const activity = useActivityPanel(isMobile);
  const { confirm, ConfirmDialogElement } = useConfirmDialog();
  const {
    activeTab: activityTab,
    dirtyGuard,
    dismissOnMobile,
    effectiveMode,
    panelWidth,
    releasePanelOverride,
    requestPanelOverride,
    setActiveTab: setActivityTab,
    setPanelWidth,
    showTab,
    toggleFromChat,
    toggleFromPanel,
  } = activity;
  const panelVisible = effectiveMode !== "chat";
  const showChatColumn = isMobile || effectiveMode !== "panel";

  const routing = useChatPageSessionRouting({
    chat,
    conversations,
    projectId,
    showTab,
    dismissOnMobile,
  });
  // The Changes panel follows the session shown in the main chat window — live
  // web chat, or a watched/attached/resumed session. The message-scan overlay
  // only applies when that session is the active chat.
  const fileChanges = useFileChanges(
    routing.activityPanelChatSessionId ?? null,
    chat.messages,
    routing.activityPanelChatSessionId === chat.dbSessionId,
  );
  const providerState = useChatPageProviderState({
    chat,
    mainSessionMeta: routing.mainSessionMeta,
    currentModel,
    reasoningPreferences,
    onModelChange,
    onReasoningPreferenceChange,
    projectId,
    confirm,
  });
  const plans = useChatPagePlans({
    chat,
    showTab,
    dismissOnMobile,
    showPlanRef,
  });
  const commandPalette = useChatPageCommandPalette({
    activitySessions,
    allProjectSessions,
    activityPanelChatSessionId: routing.activityPanelChatSessionId,
    conversations,
    confirm,
    onPaletteSelect: chat.onPaletteSelect,
    handleSwapSession: routing.handleSwapSession,
    toggleFromChat,
    toggleFromPanel,
  });
  const voiceStatus = useChatPageVoiceStatus(voice);
  useEffect(() => {
    if (!requestedActivityTab) return;
    showTab(requestedActivityTab);
    onActivityTabRequestHandled?.();
  }, [onActivityTabRequestHandled, requestedActivityTab, showTab]);

  useEffect(() => {
    if (chat.isLoadingMessages || chat.messages.length === 0) {
      return;
    }

    const loadKey = [
      chat.conversationSwitchKey ?? 0,
      chat.viewingSessionId ?? chat.dbSessionId ?? "main-chat",
    ].join(":");
    if (lastAutoScrolledLoadRef.current === loadKey) {
      return;
    }
    lastAutoScrolledLoadRef.current = loadKey;

    const frameId = requestAnimationFrame(() => {
      messageListRef.current?.scrollToBottom();
    });
    return () => cancelAnimationFrame(frameId);
  }, [
    chat.conversationSwitchKey,
    chat.dbSessionId,
    chat.isLoadingMessages,
    chat.messages.length,
    chat.viewingSessionId,
  ]);

  return (
    <div className="flex h-full flex-col overflow-hidden bg-background text-foreground">
      <Heading level={1} className="sr-only">
        Chat
      </Heading>
      {ConfirmDialogElement}
      <div className="relative flex min-h-0 flex-1 overflow-hidden">
        {showChatColumn && (
          <ChatMainColumn
            chat={chat}
            voice={voice}
            projectId={projectId}
            projectHasCheckout={projectHasCheckout}
            panelVisible={panelVisible}
            effectiveSessionRef={routing.effectiveSessionRef}
            activeTitle={routing.activeTitle}
            mainSessionSource={
              routing.mainSessionMeta?.source ?? chat.provider ?? null
            }
            messageListRef={messageListRef}
            providerState={providerState}
            voiceStatus={voiceStatus}
            onOpenPalette={() => commandPalette.setShowCommandPalette(true)}
            onTogglePanel={toggleFromChat}
            onPaletteSelect={commandPalette.handlePaletteSelect}
            onNewChat={routing.handleNewChat}
            onModelChange={onModelChange}
            onSttEnabledChange={onSttEnabledChange}
            onTtsEnabledChange={onTtsEnabledChange}
            onVoiceInputModeChange={onVoiceInputModeChange}
            onViewPlan={() => showPlanRef?.current?.()}
            planPendingVariant={planPendingVariant}
            agentDefinitions={agentDefinitions}
            agentGlobalDefs={agentGlobalDefs}
            agentProjectDefs={agentProjectDefs}
            agentShowScopeToggle={agentShowScopeToggle}
            agentHasGlobal={agentHasGlobal}
            agentHasProject={agentHasProject}
          />
        )}

        <AppErrorBoundary
          activeTab="activity sidebar"
          onReturnToChat={toggleFromPanel}
        >
          <ActivityPanel
            mode={effectiveMode}
            onToggleChat={toggleFromPanel}
            panelWidth={panelWidth}
            onWidthChange={setPanelWidth}
            activeTab={activityTab}
            onTabChange={setActivityTab}
            plans={plans.plans}
            activePlan={plans.activePlan}
            onOpenPlan={plans.openPlan}
            onSetPlanVersion={plans.setPlanVersion}
            planPendingApproval={plans.planPendingApproval}
            planApproved={plans.planApproved}
            planApprovalOptions={plans.planApprovalOptions}
            onApprovePlan={plans.handleApprovePlan}
            onRequestPlanChanges={plans.handleRequestPlanChanges}
            planPendingVariant={planPendingVariant}
            changedFiles={fileChanges.changedFiles}
            fetchDiff={fileChanges.fetchDiff}
            changesLoading={fileChanges.loading}
            changesError={fileChanges.error}
            onRetryChanges={fileChanges.refresh}
            projectId={projectId}
            projectName={projectName}
            sessions={activitySessions ?? allProjectSessions}
            sessionsLoading={
              activitySessionsLoading ?? allProjectSessionsLoading
            }
            sessionsFilters={sessionsFilters}
            onSessionsFiltersChange={onSessionsFiltersChange}
            mcp={mcp}
            onKillAgent={conversations.onKillAgent}
            onExpireSession={conversations.onExpireSession}
            onAcpCloseSession={conversations.onAcpCloseSession}
            onAcpDeleteSession={conversations.onAcpDeleteSession}
            chatSessionId={routing.activityPanelChatSessionId}
            focusSessionId={routing.focusSessionId}
            dirtyGuard={dirtyGuard}
            onFocusSessionHandled={routing.handleFocusSessionHandled}
            onSwapSession={routing.handleSwapSession}
            onResumeSession={routing.handleResumeSessionFromActivity}
            onAddFileToChat={routing.handleAddFileToChat}
            terminalFocusSessionId={activity.terminalSessionRequest}
            onTerminalFocusHandled={activity.clearTerminalSessionRequest}
            isMobile={isMobile}
            requestPanelOverride={requestPanelOverride}
            releasePanelOverride={releasePanelOverride}
          />
        </AppErrorBoundary>
      </div>

      <CommandPalette
        isOpen={commandPalette.showCommandPalette}
        onClose={() => commandPalette.setShowCommandPalette(false)}
        sessions={commandPalette.commandPaletteSessions}
        activeSessionId={commandPalette.activeCommandPaletteSessionId}
        onSelectSession={commandPalette.handleCommandPaletteSelectSession}
        onDeleteSession={commandPalette.handleCommandPaletteDeleteSession}
        onRenameSession={conversations.onRenameSession}
        actions={paletteActions}
      />
    </div>
  );
}
