import {
  useState,
  useCallback,
  useMemo,
  useEffect,
  useRef,
  lazy,
  Suspense,
} from "react";
import { cn } from "./lib/utils";
import { coarseHitAreaCls } from "./components/ui/controlStyles";
import { useAuth } from "./hooks/useAuth";
import { useChat } from "./hooks/useChat";
import { useVoice } from "./hooks/useVoice";
import { useSettings } from "./hooks/useSettings";
import { useMcp } from "./hooks/useMcp";
import { useSkills } from "./hooks/useSkills";
import { useColonAutocomplete } from "./hooks/useColonAutocomplete";
import { useAgentDefinitions } from "./hooks/useAgentDefinitions";
import { useProjects } from "./hooks/useProjects";
import { useSessionCatalog } from "./hooks/useSessionCatalog";
import { useIsMobile } from "./hooks/useIsMobile";
import { normalizeChatMode } from "./types/chat";
import type { QueuedFile } from "./types/chat";
import type { ActivityTab } from "./components/activity/ActivityPanelTabs";
import { ChatPage } from "./components/chat/ChatPage";
import { LoginPage } from "./components/auth/LoginPage";
import { ProjectSelector } from "./components/ProjectSelector";
import { Button } from "./components/ui/Button";
import { ThemeToggle } from "./components/ThemeToggle";
import { Badge } from "./components/ui/Badge";
import { AppErrorBoundary } from "./components/app/AppErrorBoundary";
import { GobbyLogo } from "./components/shared/GobbyLogo";
import { useAppCommandPalette } from "./components/app/useAppCommandPalette";
import { useAppKeyboardShortcuts } from "./components/app/useAppKeyboardShortcuts";
import { useAppProjectSelection } from "./components/app/useAppProjectSelection";
import { useAppSessionActions } from "./components/app/useAppSessionActions";
import { useAppToast } from "./components/app/useAppToast";
import { usePersistedSessionsFilters } from "./components/app/usePersistedSessionsFilters";
import { useReasoningPreferences } from "./components/app/useReasoningPreferences";
import { useSessionReconciliation } from "./components/app/useSessionReconciliation";
import { LogoutIcon, SettingsCogIcon } from "./components/icons";
import { useSettingsOverlay } from "./components/settings/useSettingsOverlay";

const SettingsOverlay = lazy(
  () => import("./components/settings/SettingsOverlay"),
);
const QuickCaptureTask = lazy(() =>
  import("./components/tasks/QuickCaptureTask").then((module) => ({
    default: module.QuickCaptureTask,
  })),
);
const ResumeSessionModal = lazy(() =>
  import("./components/chat/ResumeSessionModal").then((module) => ({
    default: module.ResumeSessionModal,
  })),
);
const SlashCommandModal = lazy(() =>
  import("./components/command-browser/SlashCommandModal").then((module) => ({
    default: module.SlashCommandModal,
  })),
);

export default function App() {
  const { authenticated, loading: authLoading, login, logout } = useAuth();
  const {
    messages,
    conversationId,
    conversationSwitchKey,
    sessionRef,
    sessionTitle,
    dbSessionId,
    currentBranch,
    worktreePath,
    isConnected,
    isReconnecting,
    isStreaming,
    isThinking,
    isLoadingMessages,
    acpAvailableCommands,
    transportError,
    checkoutRequired,
    contextUsage,
    sendMessage,
    ensureMainSession,
    sendMode,
    sendAttachedSessionMode,
    sendProjectChange,
    projectIdRef,
    setProjectIdRef,
    sendWorktreeChange,
    stopStreaming,
    clearHistory,
    deleteConversation,
    respondToQuestion,
    respondToApproval,
    planPendingApproval,
    planApproved,
    planApprovalOptions,
    approvePlan,
    requestPlanChanges,
    switchConversation,
    startNewChat,
    switchProvider,
    continueSessionInChat,
    setOnModeChanged,
    setOnPlanReady,
    addSystemMessage,
    viewSession,
    clearViewingSession,
    mainSessionMeta,
    observeSession,
    viewingSessionId,
    viewingSessionMeta,
    isContinuingSession,
    attachToViewed,
    detachFromSession,
    attachedSessionId,
    attachedSessionMeta,
    sessionInteractionMode,
    proxyDeliveryNotice,
    clearTransportError,
    wsRef,
    handleVoiceMessageRef,
    handleBinaryMessageRef,
    setOnChatDeleted,
    activeAgent,
    sendAgentChange,
    selectedProvider,
    setSelectedProvider,
  } = useChat({
    connectionEnabled: !authLoading && authenticated,
  });
  const clientSettings = useSettings();
  const {
    settings,
    updateModel,
    updateChatMode,
    updateTheme,
    updateSttEnabled,
    updateTtsEnabled,
    updateVoiceInputMode,
  } = clientSettings;
  const voiceConversationId =
    attachedSessionId && sessionInteractionMode === "proxy"
      ? attachedSessionId
      : conversationId;
  const ensureVoiceConversationId = useCallback(async () => {
    if (attachedSessionId && sessionInteractionMode === "proxy") {
      return attachedSessionId;
    }
    return ensureMainSession({
      projectId: projectIdRef.current,
      provider: selectedProvider,
      model: settings.model,
    });
  }, [
    attachedSessionId,
    ensureMainSession,
    projectIdRef,
    selectedProvider,
    sessionInteractionMode,
    settings.model,
  ]);
  const voice = useVoice(
    wsRef,
    voiceConversationId,
    conversationSwitchKey,
    projectIdRef,
    {
      sttEnabled: settings.sttEnabled,
      ttsEnabled: settings.ttsEnabled,
      voiceInputMode: settings.voiceInputMode,
    },
    isConnected,
    ensureVoiceConversationId,
  );
  const mcp = useMcp();
  const skillsHook = useSkills();
  const projectsHook = useProjects({ enabled: authenticated });
  const {
    paletteItems,
    filterInput: filterColonInput,
    parseColonCommand,
    resolveInjectContext,
  } = useColonAutocomplete(
    skillsHook.skills,
    mcp.servers,
    mcp.toolsByServer,
    mcp.fetchToolSchema,
    acpAvailableCommands,
  );
  const [activeModal, setActiveModal] = useState<"skills" | "gobby" | null>(
    null,
  );
  const settingsOverlay = useSettingsOverlay();
  const isMobile = useIsMobile();
  const [activityTabRequest, setActivityTabRequest] =
    useState<ActivityTab | null>(null);
  // Chat is the only page surface. Activity tabs (Tasks/Sessions/MCP) live
  // inside ChatPage and are driven by activityTabRequest, not the URL hash.
  const [activeTab, setActiveTab] = useState<string>("chat");
  const showPlanRef = useRef<(() => void) | null>(null);
  const [quickCaptureOpen, setQuickCaptureOpen] = useState(false);
  const [resumeModalOpen, setResumeModalOpen] = useState(false);
  const { toastMessage, setToastMessage, showToast } = useAppToast();

  const handleOpenActivityTab = useCallback((tab: ActivityTab) => {
    setActivityTabRequest(tab);
    setActiveTab("chat");
  }, []);
  const clearActivityTabRequest = useCallback(
    () => setActivityTabRequest(null),
    [],
  );

  useAppKeyboardShortcuts({ setQuickCaptureOpen });

  const {
    effectiveProjectId,
    initialReconciliationDoneRef,
    isPersonalProject,
    projectOptions,
    projectReady,
    projectSelection,
    selectProject,
    selectProvider,
  } = useAppProjectSelection({
    allProjects: projectsHook.allProjects,
    onProjectSelect: clearActivityTabRequest,
    selectedProvider,
    setSelectedProvider,
    startNewChat,
    setProjectIdRef,
    sendProjectChange,
  });
  // The Providers & Models settings section drives the same default-provider
  // state as the chat picker; App owns its persistence (see effects below).
  const providerSelection = useMemo(
    () => ({ selectedProvider, onSelectProvider: selectProvider }),
    [selectedProvider, selectProvider],
  );
  const { sessionsFilters, setSessionsFilters } =
    usePersistedSessionsFilters(effectiveProjectId);

  // Two catalog instances: the unfiltered one feeds the resume modal, web-chat
  // sidebar list, and session-reconciliation hook (consumers that must see
  // every session regardless of the activity panel's filter narrowing); the
  // filtered one feeds the activity panel's Sessions tab so date/provider/etc.
  // filters reach the historical tail via server-side predicates.
  const sessionCatalog = useSessionCatalog(effectiveProjectId);
  const activitySessionCatalog = useSessionCatalog(
    effectiveProjectId,
    sessionsFilters,
  );
  const confirmSessionDeleted = sessionCatalog.confirmSessionDeleted;
  const markSessionDeleting = sessionCatalog.markSessionDeleting;
  const restoreSession = sessionCatalog.restoreSession;
  const agentDefs = useAgentDefinitions(effectiveProjectId, {
    surfaceFilter: "persona",
  });

  const {
    reasoningPreferences,
    updateReasoningPreference,
    currentMainReasoning,
  } = useReasoningPreferences({
    mainSessionSource: mainSessionMeta?.source,
    selectedProvider,
    currentModel: settings.model,
    persistedSessionModel: mainSessionMeta?.model,
    updateModel,
  });

  const allProjectSessions = sessionCatalog.sessions;

  // Web-chat sessions for main conversation list
  const webChatSessions = useMemo(
    () =>
      allProjectSessions.filter(
        (session) => session.session_type === "web_chat",
      ),
    [allProjectSessions],
  );

  useSessionReconciliation({
    initialReconciliationDoneRef,
    projectReady,
    effectiveProjectId,
    isLoadingSessions: sessionCatalog.isLoading,
    webChatSessions,
    dbSessionId,
    viewingSessionId,
    switchConversation,
    startNewChat,
  });

  // Wrap sendMessage to include the selected model + colon command interception
  const handleSendMessage = useCallback(
    async (
      content: string,
      files?: QueuedFile[],
      options?: { reasoningEffort?: string | null; ttsEnabled?: boolean },
    ) => {
      const reasoningEffort = options?.reasoningEffort ?? currentMainReasoning;
      const ttsEnabled = options?.ttsEnabled ?? settings.ttsEnabled;
      const parsed = parseColonCommand(content);
      if (parsed) {
        const ctx = await resolveInjectContext(parsed);
        const visibleMessage =
          parsed.intent.trim() || `Use ${parsed.command}:${parsed.subItem}`;
        sendMessage(
          visibleMessage,
          settings.model,
          files,
          effectiveProjectId,
          ctx ?? undefined,
          reasoningEffort,
          ttsEnabled,
        );
      } else {
        sendMessage(
          content,
          settings.model,
          files,
          effectiveProjectId,
          undefined,
          reasoningEffort,
          ttsEnabled,
        );
      }
    },
    [
      currentMainReasoning,
      sendMessage,
      settings.model,
      settings.ttsEnabled,
      effectiveProjectId,
      parseColonCommand,
      resolveInjectContext,
    ],
  );

  const {
    handleCloseSession,
    handleContinueInChat,
    handleDeleteConversation,
    handleDeleteSession,
    handleExpireSession,
    handleKillAgent,
    handleSelectConversation,
  } = useAppSessionActions({
    attachedSessionId,
    clearViewingSession,
    confirmSessionDeleted,
    continueSessionInChat,
    deleteConversation,
    detachFromSession,
    markSessionDeleting,
    restoreSession,
    setActiveTab,
    setOnChatDeleted,
    showToast,
    switchConversation,
    viewingSessionId,
  });

  // Wire voice message handlers into useChat's WebSocket routing
  useEffect(() => {
    handleVoiceMessageRef.current = voice.handleVoiceMessage;
    handleBinaryMessageRef.current = voice.handleBinaryMessage;
  }, [
    voice.handleVoiceMessage,
    voice.handleBinaryMessage,
    handleVoiceMessageRef,
    handleBinaryMessageRef,
  ]);

  // Wire backend-initiated mode changes (e.g. agent EnterPlanMode/ExitPlanMode)
  // to update the settings slider
  useEffect(() => {
    setOnModeChanged(updateChatMode);
  }, [updateChatMode, setOnModeChanged]);

  const handleStartNewChat = useCallback(
    (agentName?: string) => {
      const defaultMode = normalizeChatMode(settings.defaultChatMode);
      // Reset BOTH the UI radio and the backend-facing currentModeRef. Resetting
      // only the radio leaves currentModeRef holding the prior conversation's
      // mode, which seeds the next session via createWebChatSession() — the
      // Plan-shown / session-created-in-bypass desync (#15703). Clearing the
      // conversation first means sendMode only updates currentModeRef.
      startNewChat(agentName);
      updateChatMode(defaultMode);
      sendMode(defaultMode);
    },
    [settings.defaultChatMode, startNewChat, updateChatMode, sendMode],
  );

  // Restore persisted mode only when we have an active durable web-chat session.
  // Drafts are seeded by handleStartNewChat; observed sessions sync mode through
  // useChat's authoritative session metadata and mode_changed events.
  // Restore at most once per conversation switch: this effect also re-runs on
  // session-catalog refreshes, and re-pushing the catalog's (possibly stale)
  // chat_mode would clobber a server-driven mode change — e.g. plan approval
  // flips the session to Act, then a catalog refresh reverted it to Plan.
  const restoredModeKeyRef = useRef<string | null>(null);
  useEffect(() => {
    if (sessionCatalog.isLoading) return;
    if (!dbSessionId) return;
    const session = webChatSessions.find((s) => s.id === dbSessionId);
    if (!session) return;
    const restoreKey = `${conversationSwitchKey}:${dbSessionId}`;
    if (restoredModeKeyRef.current === restoreKey) return;
    restoredModeKeyRef.current = restoreKey;
    const restoredMode =
      (session?.chat_mode ? normalizeChatMode(session.chat_mode) : null) ||
      normalizeChatMode(settings.defaultChatMode);
    updateChatMode(restoredMode);
    sendMode(restoredMode);
  }, [
    conversationSwitchKey,
    sessionCatalog.isLoading,
    dbSessionId,
    webChatSessions,
    settings.defaultChatMode,
    updateChatMode,
    sendMode,
  ]);

  const handleInputChange = useCallback(
    (value: string) => {
      filterColonInput(value);
    },
    [filterColonInput],
  );

  const { handlePaletteSelect, commandPaletteActions } = useAppCommandPalette({
    startNewChat: handleStartNewChat,
    clearHistory,
    sendMessage,
    settings,
    effectiveProjectId,
    currentMainReasoning,
    updateChatMode,
    sendMode,
    addSystemMessage,
    setActiveModal,
    settingsOverlay,
    setResumeModalOpen,
    showPlanRef,
    openActivityTab: handleOpenActivityTab,
  });

  // Auth guard — shown after all hooks (React rules)
  if (authLoading) {
    return (
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          minHeight: "100vh",
          background: "var(--bg-primary)",
          color: "var(--text-secondary)",
        }}
      >
        Loading...
      </div>
    );
  }
  if (!authenticated) {
    return <LoginPage onLogin={login} />;
  }

  const visibleToastMessage = toastMessage ?? transportError?.message ?? null;

  return (
    <div className="app">
      <AppErrorBoundary
        activeTab="header"
        onReturnToChat={() => setActiveTab("chat")}
      >
        <header
          data-testid="app-header"
          className="relative z-[100] flex items-center justify-between gap-3 border-b border-border px-4 py-3 mobile:gap-2 mobile:px-3 mobile:py-2.5"
        >
          <div className="flex min-w-0 items-center gap-1.5">
            <GobbyLogo
              className="[--app-brand-logo-size:2.75rem] mobile:[--app-brand-logo-size:1.875rem]"
              size="var(--app-brand-logo-size)"
            />
            <span className="min-w-0 overflow-hidden text-[length:var(--text-3xl)] leading-none font-semibold text-ellipsis whitespace-nowrap text-foreground mobile:text-[length:var(--text-2xl)]">
              Gobby
            </span>
          </div>
          <div className="flex shrink-0 flex-nowrap items-center justify-end gap-2 [--control-row-height:var(--status-bar-control-height)]">
            {!isConnected && (
              <Badge
                variant="error"
                style={{ height: "var(--control-row-height)" }}
                className="gap-2 tracking-[0.05em] uppercase"
              >
                <span
                  aria-hidden="true"
                  className="size-2 rounded-full bg-destructive-foreground"
                />
                <span>Down</span>
              </Badge>
            )}
            {projectOptions.length > 0 && (
              <ProjectSelector
                projects={projectOptions}
                selectedProjectId={effectiveProjectId}
                onProjectChange={selectProject}
                dropDirection="down"
              />
            )}
            {/* Mobile app-header pattern (.impeccable.md): theme toggle and
                logout collapse into the settings entry; the settings surface
                exposes both. Desktop keeps the three-button cluster. */}
            {!isMobile && (
              <ThemeToggle theme={settings.theme} onThemeChange={updateTheme} />
            )}
            <Button
              type="button"
              variant="accent"
              size="icon"
              dense
              className={cn("shrink-0", coarseHitAreaCls)}
              onClick={() => settingsOverlay.open()}
              aria-label="Open settings"
              aria-haspopup="dialog"
              aria-expanded={settingsOverlay.isOpen}
              title="Settings"
            >
              <SettingsCogIcon />
            </Button>
            {!isMobile && (
              <Button
                type="button"
                variant="accent"
                size="icon"
                dense
                className={cn("shrink-0", coarseHitAreaCls)}
                onClick={() => logout()}
                aria-label="Log out"
                title="Log out"
              >
                <LogoutIcon />
              </Button>
            )}
          </div>
        </header>
      </AppErrorBoundary>

      <AppErrorBoundary
        activeTab={activeTab}
        onReturnToChat={() => setActiveTab("chat")}
      >
        <Suspense
          fallback={
            <main className="flex flex-1 items-center justify-center text-muted-foreground">
              Loading...
            </main>
          }
        >
          <ChatPage
            projectId={effectiveProjectId}
            projectName={
              projectOptions.find(
                (project) => project.id === effectiveProjectId,
              )?.name ?? null
            }
            projectHasCheckout={
              projectOptions.find(
                (project) => project.id === effectiveProjectId,
              )?.hasCheckout !== false
            }
            showPlanRef={showPlanRef}
            planPendingVariant={settings.planPendingVariant}
            chat={{
              messages,
              sessionRef,
              sessionTitle,
              currentBranch,
              worktreePath,
              isStreaming,
              isThinking,
              isLoadingMessages,
              isConnected,
              isReconnecting,
              checkoutRequired,
              contextUsage,
              onSend: handleSendMessage,
              addSystemMessage,
              onStop: stopStreaming,
              onRespondToQuestion: respondToQuestion,
              onRespondToApproval: respondToApproval,
              onInputChange: handleInputChange,
              paletteItems,
              onPaletteSelect: handlePaletteSelect,
              acpAvailableCommands,
              mode: settings.chatMode,
              onModeChange: (mode) => {
                updateChatMode(mode);
                sendMode(mode);
              },
              onModeChangeLocal: updateChatMode,
              onWorktreeChange: isPersonalProject
                ? undefined
                : sendWorktreeChange,
              planPendingApproval,
              planApproved,
              planApprovalOptions,
              onApprovePlan: approvePlan,
              onRequestPlanChanges: requestPlanChanges,
              setOnPlanReady,
              continueSessionInChat,
              viewSession,
              clearViewingSession,
              mainSessionMeta,
              viewingSessionId,
              viewingSessionMeta,
              isContinuingSession,
              observeSession,
              attachedSessionId,
              attachedSessionMeta,
              sessionInteractionMode,
              proxyDeliveryNotice,
              onAttachToViewed: attachToViewed,
              onDetachFromSession: detachFromSession,
              onAttachedModeChange: attachedSessionId
                ? (mode) => {
                    updateChatMode(mode);
                    sendAttachedSessionMode(attachedSessionId, mode);
                  }
                : undefined,
              activeAgent,
              onAgentChange: sendAgentChange,
              provider: selectedProvider,
              onProviderChange: selectProvider,
              onSwitchProvider: switchProvider,
              dbSessionId,
              conversationSwitchKey,
            }}
            allProjectSessions={allProjectSessions}
            allProjectSessionsLoading={sessionCatalog.isLoading}
            activitySessions={activitySessionCatalog.sessions}
            activitySessionsLoading={activitySessionCatalog.isLoading}
            sessionsFilters={sessionsFilters}
            onSessionsFiltersChange={setSessionsFilters}
            mcp={mcp}
            requestedActivityTab={activityTabRequest}
            onActivityTabRequestHandled={() => setActivityTabRequest(null)}
            conversations={{
              sessions: webChatSessions,
              activeSessionId: dbSessionId,
              deletingIds: sessionCatalog.deletingIds,
              onNewChat: handleStartNewChat,
              onSelectSession: handleSelectConversation,
              onDeleteSession: handleDeleteConversation,
              onRenameSession: sessionCatalog.renameSession,
              onKillAgent: handleKillAgent,
              onExpireSession: handleExpireSession,
              onAcpCloseSession: handleCloseSession,
              onAcpDeleteSession: handleDeleteSession,
              viewingSessionId,
              attachedSessionId,
            }}
            currentModel={settings.model}
            onModelChange={updateModel}
            reasoningPreferences={reasoningPreferences}
            onReasoningPreferenceChange={updateReasoningPreference}
            agentDefinitions={agentDefs.definitions}
            agentGlobalDefs={agentDefs.globalDefs}
            agentProjectDefs={agentDefs.projectDefs}
            agentShowScopeToggle={agentDefs.showScopeToggle}
            agentHasGlobal={agentDefs.hasGlobal}
            agentHasProject={agentDefs.hasProject}
            paletteActions={commandPaletteActions}
            onSttEnabledChange={updateSttEnabled}
            onTtsEnabledChange={updateTtsEnabled}
            onVoiceInputModeChange={updateVoiceInputMode}
            voice={{
              sttEnabled: settings.sttEnabled,
              ttsEnabled: settings.ttsEnabled,
              voiceInputMode: settings.voiceInputMode,
              voiceAvailable: voice.voiceAvailable,
              voiceReady: voice.voiceReady,
              voiceLoading: voice.voiceLoading,
              isListening: voice.isListening,
              isSpeechDetected: voice.isSpeechDetected,
              isRecording: voice.isRecording,
              isTranscribing: voice.isTranscribing,
              isSpeaking: voice.isSpeaking,
              voiceError: voice.voiceError,
              prepareTTSPlayback: voice.prepareTTSPlayback,
              startRecording: voice.startRecording,
              stopRecording: voice.stopRecording,
              cancelRecording: voice.cancelRecording,
              stopTTS: voice.stopTTS,
            }}
          />
        </Suspense>
      </AppErrorBoundary>

      <AppErrorBoundary
        activeTab="modal"
        onReturnToChat={() => setActiveTab("chat")}
      >
        {settingsOverlay.isOpen && (
          <Suspense fallback={null}>
            <SettingsOverlay
              isOpen={settingsOverlay.isOpen}
              activeSection={settingsOverlay.activeSection}
              onClose={settingsOverlay.close}
              onSelectSection={settingsOverlay.selectSection}
              registerDirtyGuard={settingsOverlay.registerDirtyGuard}
              clientSettings={clientSettings}
              providerSelection={providerSelection}
              projectSelection={projectSelection}
              onLogout={logout}
            />
          </Suspense>
        )}
        {quickCaptureOpen && (
          <Suspense fallback={null}>
            <QuickCaptureTask
              isOpen
              onClose={() => setQuickCaptureOpen(false)}
            />
          </Suspense>
        )}
        {resumeModalOpen && (
          <Suspense fallback={null}>
            <ResumeSessionModal
              isOpen
              onClose={() => setResumeModalOpen(false)}
              sessions={allProjectSessions}
              onResume={handleContinueInChat}
            />
          </Suspense>
        )}
        {activeModal && (
          <Suspense fallback={null}>
            <SlashCommandModal
              modal={activeModal}
              onClose={() => setActiveModal(null)}
              onSendMessage={(content, context) => {
                sendMessage(
                  content,
                  settings.model,
                  undefined,
                  effectiveProjectId,
                  context,
                  currentMainReasoning,
                  settings.ttsEnabled,
                );
              }}
            />
          </Suspense>
        )}
      </AppErrorBoundary>
      {visibleToastMessage && (
        <Button
          type="button"
          variant="ghost"
          size="sm"
          dense
          className="app-toast"
          onClick={() => {
            setToastMessage(null);
            clearTransportError();
          }}
          aria-label={`Dismiss notification: ${visibleToastMessage}`}
        >
          {visibleToastMessage}
        </Button>
      )}
    </div>
  );
}
