import "./styles.css";

import { useCallback, useEffect, useRef } from "react";

import { useIsMobile } from "../../hooks/useIsMobile";
import { useConfirmDialog } from "../../hooks/useConfirmDialog";
import { useFileChanges } from "../../hooks/useFileChanges";
import { useCanvasPanel } from "../canvas/hooks/useCanvasPanel";
import { ActivityPanel } from "../activity/ActivityPanel";
import { useActivityPanel } from "../activity/useActivityPanel";
import { Heading } from "../shared/Heading";
import { CommandPalette } from "./CommandPalette";
import { ChatMainColumn } from "./ChatMainColumn";
import { type MessageListHandle } from "./MessageList";
import type { ChatPageProps } from "./ChatPage.types";
import { useChatPageArtifacts } from "./useChatPageArtifacts";
import { useChatPageCommandPalette } from "./useChatPageCommandPalette";
import { useChatPageProviderState } from "./useChatPageProviderState";
import { useChatPageSessionRouting } from "./useChatPageSessionRouting";
import { useChatPageVoiceStatus } from "./useChatPageVoiceStatus";

export function ChatPage({
  chat,
  conversations,
  voice,
  projectId,
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
}: ChatPageProps) {
  const messageListRef = useRef<MessageListHandle>(null);
  const lastAutoScrolledLoadRef = useRef<string | null>(null);
  const isMobile = useIsMobile();
  const canvas = useCanvasPanel();
  const activity = useActivityPanel(isMobile);
  const fileChanges = useFileChanges(chat.messages, projectId ?? null);
  const { confirm, ConfirmDialogElement } = useConfirmDialog();
  const {
    activeTab: activityTab,
    closeIfAutoOpened,
    dismissOnMobile,
    effectiveMode,
    panelWidth,
    refreshWikiTab,
    setActiveTab: setActivityTab,
    setPanelWidth,
    showTab,
    toggleFromChat,
    toggleFromPanel,
    wikiRefreshSignal,
  } = activity;
  const { openCanvas, closeCanvas, activeCanvas } = canvas;
  const panelVisible = effectiveMode !== "chat";
  const showChatColumn = isMobile || effectiveMode !== "panel";

  const routing = useChatPageSessionRouting({
    chat,
    conversations,
    projectId,
    showTab,
    dismissOnMobile,
  });
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
  const artifacts = useChatPageArtifacts({
    chat,
    showTab,
    dismissOnMobile,
    closeIfAutoOpened,
    showPlanRef,
  });
  const commandPalette = useChatPageCommandPalette({
    activitySessions,
    allProjectSessions,
    activityPanelChatSessionId: routing.activityPanelChatSessionId,
    conversations,
    onPaletteSelect: chat.onPaletteSelect,
    handleSwapSession: routing.handleSwapSession,
    toggleFromChat,
    toggleFromPanel,
  });
  const voiceStatus = useChatPageVoiceStatus(voice);
  const handleWikiActionComplete = useCallback(() => {
    showTab("wiki");
    refreshWikiTab();
  }, [refreshWikiTab, showTab]);

  useEffect(() => {
    if (chat.canvasPanel) {
      openCanvas(chat.canvasPanel);
      showTab("canvas");
    } else {
      closeCanvas();
    }
  }, [chat.canvasPanel, closeCanvas, openCanvas, showTab]);

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
    <div className="relative flex h-full overflow-hidden bg-background text-foreground">
      <Heading level={1} className="sr-only">
        Chat
      </Heading>
      {ConfirmDialogElement}
      {showChatColumn && (
        <ChatMainColumn
          chat={chat}
          voice={voice}
          projectId={projectId}
          isMobile={isMobile}
          panelVisible={panelVisible}
          effectiveSessionRef={routing.effectiveSessionRef}
          activeTitle={routing.activeTitle}
          mainSessionSource={routing.mainSessionMeta?.source ?? chat.provider ?? null}
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
          onWikiActionComplete={handleWikiActionComplete}
          openCodeAsArtifact={artifacts.openCodeAsArtifact}
          openFileAsArtifact={artifacts.openFileAsArtifact}
          agentDefinitions={agentDefinitions}
          agentGlobalDefs={agentGlobalDefs}
          agentProjectDefs={agentProjectDefs}
          agentShowScopeToggle={agentShowScopeToggle}
          agentHasGlobal={agentHasGlobal}
          agentHasProject={agentHasProject}
        />
      )}

      <ActivityPanel
        mode={effectiveMode}
        onToggleChat={toggleFromPanel}
        panelWidth={panelWidth}
        onWidthChange={setPanelWidth}
        activeTab={activityTab}
        onTabChange={setActivityTab}
        artifacts={artifacts.artifacts}
        activeArtifact={artifacts.activeArtifact}
        onOpenArtifact={artifacts.openArtifact}
        onCloseArtifact={artifacts.handleCloseArtifact}
        onUpdateArtifactContent={artifacts.updateArtifact}
        onSetArtifactVersion={artifacts.setVersion}
        planPendingApproval={artifacts.planPendingApproval}
        onApprovePlan={artifacts.handleApprovePlan}
        onRequestPlanChanges={artifacts.handleRequestPlanChanges}
        canvasState={activeCanvas}
        onCloseCanvas={closeCanvas}
        onClearCanvas={canvas.closeCanvas}
        changedFiles={fileChanges.changedFiles}
        fetchDiff={fileChanges.fetchDiff}
        projectId={projectId}
        sessions={activitySessions ?? allProjectSessions}
        sessionsLoading={activitySessionsLoading ?? allProjectSessionsLoading}
        sessionsFilters={sessionsFilters}
        onSessionsFiltersChange={onSessionsFiltersChange}
        mcp={mcp}
        onKillAgent={conversations.onKillAgent}
        onExpireSession={conversations.onExpireSession}
        chatSessionId={routing.activityPanelChatSessionId}
        focusSessionId={routing.focusSessionId}
        onFocusSessionHandled={routing.handleFocusSessionHandled}
        onSwapSession={routing.handleSwapSession}
        onResumeSession={routing.handleResumeSessionFromActivity}
        onAddFileToChat={routing.handleAddFileToChat}
        isMobile={isMobile}
        wikiRefreshSignal={wikiRefreshSignal}
      />

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
