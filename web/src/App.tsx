import {
  useState,
  useCallback,
  useMemo,
  useEffect,
  useRef,
  Suspense,
} from "react";
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
import { normalizeChatMode } from "./types/chat";
import type { QueuedFile } from "./types/chat";
import type { GobbySession } from "./types/sessions";
import {
  defaultSessionsFilters,
  deserializeFromStorage,
  serializeForStorage,
  type SessionsFilters,
} from "./components/activity/sessionsFilters";

const SESSIONS_FILTERS_STORAGE_KEY = "gobby-sessions-filters";
import { Settings } from "./components/Settings";
import { Sidebar } from "./components/Sidebar";
import { ChatPage } from "./components/chat/ChatPage";
import { LoginPage } from "./components/auth/LoginPage";
import { ProjectSelector } from "./components/ProjectSelector";
import { QuickCaptureTask } from "./components/tasks/QuickCaptureTask";
import { SlashCommandModal } from "./components/command-browser/SlashCommandModal";
import { ResumeSessionModal } from "./components/chat/ResumeSessionModal";
import { Badge } from "./components/chat/ui/Badge";
import { Button } from "./components/chat/ui/Button";
import { AppErrorBoundary } from "./components/app/AppErrorBoundary";
import {
  ComingSoonPage,
  ConfigurationPage,
  CronJobsPage,
  DashboardPage,
  IntegrationsPage,
  McpPage,
  MemoryPage,
  ProjectsPage,
  ReportsPage,
  SkillsPage,
  TasksPage,
  TracesPage,
  WorkflowsPage,
} from "./components/app/AppPages";
import { APP_VALID_TABS, createAppNavItems } from "./components/app/appNavigation";
import { useAppCommandPalette } from "./components/app/useAppCommandPalette";
import { useAppKeyboardShortcuts } from "./components/app/useAppKeyboardShortcuts";
import { useSessionReconciliation } from "./components/app/useSessionReconciliation";
import { HamburgerIcon } from "./components/icons";
import { FilesProvider } from "./contexts/FilesContext";
import {
  buildReasoningPreferenceKey,
  fetchProviderModelCatalog,
  getPreferredModelForProvider,
  getPreferredReasoningEffort,
  resolveModelValueForProvider,
  type ProviderModelEntry,
} from "./lib/providerModels";
import {
  loadReasoningPreferences,
  REASONING_PREFERENCES_STORAGE_KEY,
} from "./lib/sessionPersistence";
import { cn } from "./lib/utils";

const HIDDEN_PROJECTS = new Set(["_orphaned", "_migrated"]);

export default function App() {
  const {
    authRequired,
    authenticated,
    loading: authLoading,
    login,
    logout,
  } = useAuth();
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
    contextUsage,
    sendMessage,
    sendMode,
    sendProjectChange,
    setProjectIdRef,
    sendWorktreeChange,
    stopStreaming,
    clearHistory,
    deleteConversation,
    respondToQuestion,
    respondToApproval,
    planPendingApproval,
    approvePlan,
    requestPlanChanges,
    switchConversation,
    startNewChat,
    switchProvider,
    continueSessionInChat,
    setOnModeChanged,
    setOnPlanReady,
    setOnArtifactEvent,
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
    wsRef,
    handleVoiceMessageRef,
    handleBinaryMessageRef,
    canvasSurfaces,
    canvasPanel,
    onCanvasInteraction,
    setOnChatDeleted,
    activeAgent,
    sendAgentChange,
    selectedProvider,
    setSelectedProvider,
  } = useChat();
  const {
    settings,
    updateFontSize,
    updateModel,
    updateChatMode,
    updateTheme,
    updateDefaultChatMode,
    updatePostPlanChatMode,
    updateSttEnabled,
    updateTtsEnabled,
    updateVoiceInputMode,
    resetSettings,
  } = useSettings();
  const [providerModelCatalog, setProviderModelCatalog] = useState<
    ProviderModelEntry[]
  >([]);
  const [reasoningPreferences, setReasoningPreferences] = useState<
    Record<string, string>
  >(() => loadReasoningPreferences());
  const voice = useVoice(
    wsRef,
    conversationId,
    conversationSwitchKey,
    {
      sttEnabled: settings.sttEnabled,
      ttsEnabled: settings.ttsEnabled,
      voiceInputMode: settings.voiceInputMode,
    },
    isConnected,
  );
  const mcp = useMcp();
  const skillsHook = useSkills();
  const projectsHook = useProjects();
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
  );
  const [activeModal, setActiveModal] = useState<
    "skills" | "gobby" | "mcp" | null
  >(null);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [activeTab, setActiveTab] = useState<string>(() => {
    const hash = window.location.hash.slice(1);
    return APP_VALID_TABS.has(hash) ? hash : "chat";
  });

  useEffect(() => {
    window.location.hash = activeTab;
  }, [activeTab]);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [selectedProjectId, setSelectedProjectId] = useState<string | null>(
    null,
  );
  const [initialTraceId, setInitialTraceId] = useState<string | null>(null);
  const [uiSettingsLoaded, setUiSettingsLoaded] = useState(false);
  const showPlanRef = useRef<(() => void) | null>(null);
  const [quickCaptureOpen, setQuickCaptureOpen] = useState(false);
  const [resumeModalOpen, setResumeModalOpen] = useState(false);
  const [toastMessage, setToastMessage] = useState<string | null>(null);
  const toastTimerRef = useRef<number | null>(null);

  const handleNavigateToTrace = useCallback((traceId: string) => {
    setInitialTraceId(traceId);
    setActiveTab("traces");
  }, []);

  useAppKeyboardShortcuts({ activeTab, setQuickCaptureOpen });

  // Build project options for the selector (exclude internal system projects)
  const projectOptions = useMemo(
    () =>
      projectsHook.allProjects
        .filter((p) => !HIDDEN_PROJECTS.has(p.name))
        .map((p) => ({
          id: p.id,
          name:
            p.name === "_personal"
              ? "Personal"
              : p.display_name || p.name,
        })),
    [projectsHook.allProjects],
  );

  // Default to first repo-backed project, fall back to Personal
  const defaultProjectId = useMemo(() => {
    const repoProject = projectOptions.find((p) => p.name !== "Personal");
    return (
      repoProject?.id ??
      projectOptions.find((p) => p.name === "Personal")?.id ??
      projectOptions[0]?.id ??
      null
    );
  }, [projectOptions]);

  const projectReady = uiSettingsLoaded && projectOptions.length > 0;
  const resolvedSelectedProjectId =
    selectedProjectId &&
    projectOptions.some((project) => project.id === selectedProjectId)
      ? selectedProjectId
      : null;
  const effectiveProjectId = resolvedSelectedProjectId ?? defaultProjectId;
  const isPersonalProject =
    projectOptions.find((p) => p.id === effectiveProjectId)?.name ===
    "Personal";
  const [sessionsFilters, setSessionsFilters] = useState<SessionsFilters>(
    () => {
      try {
        return deserializeFromStorage(
          localStorage.getItem(SESSIONS_FILTERS_STORAGE_KEY),
        );
      } catch {
        return defaultSessionsFilters();
      }
    },
  );

  // Persist sessions-filter state so a reload restores the user's narrowed
  // view. The filter badge on the SessionsTab funnel button is the visible
  // cue that something is filtering.
  useEffect(() => {
    try {
      localStorage.setItem(
        SESSIONS_FILTERS_STORAGE_KEY,
        JSON.stringify(serializeForStorage(sessionsFilters)),
      );
    } catch {
      // Best-effort — disabled storage just means filters are per-tab-load.
    }
  }, [sessionsFilters]);

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

  useEffect(() => {
    let cancelled = false;
    fetchProviderModelCatalog()
      .then((catalog) => {
        if (!cancelled) {
          setProviderModelCatalog(catalog);
        }
      })
      .catch(() => {
        if (!cancelled) {
          setProviderModelCatalog([]);
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    try {
      localStorage.setItem(
        REASONING_PREFERENCES_STORAGE_KEY,
        JSON.stringify(reasoningPreferences),
      );
    } catch {
      // Best-effort local preference cache
    }
  }, [reasoningPreferences]);

  useEffect(() => {
    const activeProvider = mainSessionMeta?.source ?? selectedProvider ?? "claude";
    const selectedModelForProvider = resolveModelValueForProvider(
      providerModelCatalog,
      activeProvider,
      settings.model,
    );
    const persistedModelForProvider = resolveModelValueForProvider(
      providerModelCatalog,
      activeProvider,
      mainSessionMeta?.model ?? null,
    );

    const nextModel =
      selectedModelForProvider ??
      persistedModelForProvider ??
      getPreferredModelForProvider(providerModelCatalog, activeProvider, null);

    if (nextModel && nextModel !== settings.model) {
      updateModel(nextModel);
    }
  }, [
    mainSessionMeta?.model,
    mainSessionMeta?.source,
    providerModelCatalog,
    selectedProvider,
    settings.model,
    updateModel,
  ]);

  const updateReasoningPreference = useCallback(
    (
      provider: string | null | undefined,
      model: string | null | undefined,
      reasoningEffort: string | null | undefined,
    ) => {
      const key = buildReasoningPreferenceKey(provider, model);
      if (!key || !reasoningEffort) {
        return;
      }
      setReasoningPreferences((prev) => {
        if (prev[key] === reasoningEffort) {
          return prev;
        }
        return {
          ...prev,
          [key]: reasoningEffort,
        };
      });
    },
    [],
  );

  const currentMainReasoning = useMemo(() => {
    const provider = mainSessionMeta?.source ?? selectedProvider ?? "claude";
    const preferenceKey = buildReasoningPreferenceKey(provider, settings.model);
    return getPreferredReasoningEffort(
      providerModelCatalog,
      provider,
      settings.model,
      preferenceKey ? reasoningPreferences[preferenceKey] : null,
    );
  }, [
    mainSessionMeta?.source,
    providerModelCatalog,
    reasoningPreferences,
    selectedProvider,
    settings.model,
  ]);

  // On mount: fetch persisted project from API (DB is source of truth)
  useEffect(() => {
    let cancelled = false;
    const baseUrl = import.meta.env.VITE_API_BASE_URL || "";
    fetch(`${baseUrl}/api/config/ui-settings`)
      .then((res) => (res.ok ? res.json() : null))
      .then((data) => {
        if (cancelled) return;
        if (data?.selectedProjectId) setSelectedProjectId(data.selectedProjectId);
        setUiSettingsLoaded(true);
      })
      .catch(() => {
        if (!cancelled) setUiSettingsLoaded(true);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // Persist project selection to API (only after initial resolution)
  const isFirstProjectRender = useRef(true);
  useEffect(() => {
    if (!projectReady) return;
    if (isFirstProjectRender.current) {
      isFirstProjectRender.current = false;
      return;
    }
    const baseUrl = import.meta.env.VITE_API_BASE_URL || "";
    fetch(`${baseUrl}/api/config/ui-settings`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ selectedProjectId }),
    }).catch(() => {});
  }, [selectedProjectId, projectReady]);

  // When project changes, start fresh chat context for the new project.
  const prevProjectRef = useRef<string | null>(null);
  useEffect(() => {
    if (!projectReady) return;
    if (
      effectiveProjectId &&
      prevProjectRef.current !== null &&
      effectiveProjectId !== prevProjectRef.current
    ) {
      startNewChat();
      initialReconciliationDoneRef.current = false;
    }
    prevProjectRef.current = effectiveProjectId ?? null;
  }, [effectiveProjectId, startNewChat, projectReady]);

  // Keep useChat's projectIdRef in sync with App's effectiveProjectId
  useEffect(() => {
    setProjectIdRef(effectiveProjectId);
    if (effectiveProjectId) {
      sendProjectChange(effectiveProjectId);
    }
  }, [effectiveProjectId, setProjectIdRef, sendProjectChange]);

  const allProjectSessions = sessionCatalog.sessions;

  // Web-chat sessions for main conversation list
  const webChatSessions = useMemo(
    () => allProjectSessions.filter((session) => session.session_type === "web_chat"),
    [allProjectSessions],
  );

  // Auto-select most recent server session on initial load (cross-device sync)
  const initialReconciliationDoneRef = useRef(false);
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

  // Chat page: only web-chat sessions are selectable
  const handleSelectConversation = useCallback(
    (session: GobbySession) => {
      // Clear terminal session viewing state before switching
      if (viewingSessionId) {
        clearViewingSession();
      } else if (attachedSessionId) {
        detachFromSession();
      }
      switchConversation(session.id);
    },
    [
      switchConversation,
      viewingSessionId,
      attachedSessionId,
      clearViewingSession,
      detachFromSession,
    ],
  );

  const showToast = useCallback((msg: string, durationMs = 3000) => {
    if (toastTimerRef.current) clearTimeout(toastTimerRef.current);
    setToastMessage(msg);
    toastTimerRef.current = window.setTimeout(
      () => setToastMessage(null),
      durationMs,
    );
  }, []);

  // Track pending delete timeouts: sessionId → timerId
  const deleteTimeoutsRef = useRef<
    Map<string, { sessionId: string; timerId: number }>
  >(new Map());

  // Wire backend chat_deleted ACK to confirmed removal
  useEffect(() => {
    setOnChatDeleted((sessionId: string) => {
      const entry = deleteTimeoutsRef.current.get(sessionId);
      if (entry) {
        window.clearTimeout(entry.timerId);
        deleteTimeoutsRef.current.delete(sessionId);
      }
      confirmSessionDeleted(sessionId);
    });
  }, [confirmSessionDeleted, setOnChatDeleted]);

  const handleDeleteConversation = useCallback(
    (session: GobbySession) => {
      const sent = deleteConversation(session.id, session.id);
      if (!sent) {
        showToast("Cannot delete: disconnected from server");
        return;
      }
      // Mark as deleting (visually dimmed) while waiting for backend ACK
      markSessionDeleting(session.id);
      // Timeout: if backend doesn't confirm within 5s, restore and show error
      const timerId = window.setTimeout(() => {
        restoreSession(session.id);
        deleteTimeoutsRef.current.delete(session.id);
        showToast("Delete failed: server did not respond");
      }, 5000);
      deleteTimeoutsRef.current.set(session.id, {
        sessionId: session.id,
        timerId,
      });
    },
    [
      deleteConversation,
      markSessionDeleting,
      restoreSession,
      showToast,
    ],
  );

  /* Kill a running agent via the cancel endpoint */
  const handleKillAgent = useCallback(
    async (runId: string) => {
      try {
        const res = await fetch(
          `/api/agents/runs/${encodeURIComponent(runId)}/cancel`,
          { method: "POST" },
        );
        if (res.ok) {
          showToast("Agent cancelled");
          return true;
        } else {
          showToast("Failed to cancel agent");
          return false;
        }
      } catch {
        showToast("Failed to cancel agent");
        return false;
      }
    },
    [showToast],
  );

  /* Expire a session (CLI sessions — kills tmux + marks expired) */
  const handleExpireSession = useCallback(
    async (sessionId: string) => {
      try {
        const res = await fetch(
          `/api/sessions/${encodeURIComponent(sessionId)}/expire`,
          { method: "POST" },
        );
        if (!res.ok) {
          showToast("Failed to expire session");
          return false;
        }
        return true;
      } catch {
        showToast("Failed to expire session");
        return false;
      }
    },
    [showToast],
  );

  /* "Resume Session" from Sessions page — continue CLI session in web chat */
  const handleContinueInChat = useCallback(
    async (session: GobbySession) => {
      setActiveTab("chat");
      await continueSessionInChat(session.id, session.project_id, {
        fallbackContext: "auto",
      });
    },
    [continueSessionInChat],
  );

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
      updateChatMode(normalizeChatMode(settings.defaultChatMode));
      startNewChat(agentName);
    },
    [settings.defaultChatMode, startNewChat, updateChatMode],
  );

  // Restore persisted mode only when we have an active durable web-chat session.
  // Drafts are seeded by handleStartNewChat; observed sessions sync mode through
  // useChat's authoritative session metadata and mode_changed events.
  useEffect(() => {
    if (sessionCatalog.isLoading) return;
    if (!dbSessionId) return;
    const session = webChatSessions.find((s) => s.id === dbSessionId);
    if (!session) return;
    const restoredMode =
      (session?.chat_mode ? normalizeChatMode(session.chat_mode) : null) ||
      normalizeChatMode(settings.defaultChatMode);
    updateChatMode(restoredMode);
    sendMode(restoredMode);
  }, [conversationSwitchKey, sessionCatalog.isLoading, dbSessionId]); // eslint-disable-line react-hooks/exhaustive-deps

  const handleInputChange = useCallback(
    (value: string) => {
      filterColonInput(value);
    },
    [filterColonInput],
  );

  const { handlePaletteSelect, commandPaletteActions } = useAppCommandPalette({
    startNewChat,
    clearHistory,
    sendMessage,
    settings,
    effectiveProjectId,
    currentMainReasoning,
    updateChatMode,
    sendMode,
    addSystemMessage,
    setActiveTab,
    setActiveModal,
    setSettingsOpen,
    setResumeModalOpen,
    showPlanRef,
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
  if (authRequired && !authenticated) {
    return <LoginPage onLogin={login} />;
  }

  const navItems = createAppNavItems();

  return (
    <div className="app">
      <header className="relative z-[100] flex items-center justify-between gap-4 border-b border-border px-4 py-4">
        <div className="flex min-w-0 items-center gap-1">
          <Button
            variant="outline"
            size="icon"
            className="shrink-0 text-muted-foreground hover:text-foreground"
            onClick={() => setSidebarOpen(!sidebarOpen)}
            title="Toggle menu"
            aria-label="Toggle navigation menu"
          >
            <HamburgerIcon />
          </Button>
          <img src="/logo.png" alt="Gobby logo" className="h-9 w-auto" />
          <span className="truncate text-lg font-semibold text-foreground">Gobby</span>
        </div>
        <div className="flex flex-wrap items-center justify-end gap-3">
          {projectOptions.length > 0 && (
            <ProjectSelector
              projects={projectOptions}
              selectedProjectId={effectiveProjectId}
              onProjectChange={setSelectedProjectId}
              dropDirection="down"
            />
          )}
          <Badge
            variant={isConnected ? "success" : "error"}
            className="gap-2 px-3 py-1 uppercase tracking-[0.05em]"
          >
            <span
              aria-hidden="true"
              className={cn(
                "size-2 rounded-full",
                isConnected ? "bg-success-foreground" : "bg-destructive-foreground",
              )}
            />
            <span className="hidden sm:inline">
              {isConnected ? "Connected" : "Disconnected"}
            </span>
            <span className="sm:hidden">{isConnected ? "Up" : "Down"}</span>
          </Badge>
          {authRequired && authenticated && (
            <Button
              variant="outline"
              size="sm"
              className="text-muted-foreground hover:text-foreground"
              onClick={logout}
              title="Sign out"
            >
              Logout
            </Button>
          )}
        </div>
      </header>

      <Sidebar
        items={navItems}
        activeItem={activeTab}
        isOpen={sidebarOpen}
        onItemSelect={setActiveTab}
        onClose={() => setSidebarOpen(false)}
      />

      <FilesProvider>
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
            {activeTab === "chat" ? (
              <ChatPage
                projectId={effectiveProjectId}
                showPlanRef={showPlanRef}
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
                  contextUsage,
                  onSend: handleSendMessage,
                  onStop: stopStreaming,
                  onRespondToQuestion: respondToQuestion,
                  onRespondToApproval: respondToApproval,
                  onInputChange: handleInputChange,
                  paletteItems,
                  onPaletteSelect: handlePaletteSelect,
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
                  onApprovePlan: approvePlan,
                  onRequestPlanChanges: requestPlanChanges,
                  setOnPlanReady,
                  setOnArtifactEvent,
                  canvasSurfaces,
                  canvasPanel,
                  onCanvasInteraction,
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
                  activeAgent,
                  onAgentChange: sendAgentChange,
                  provider: selectedProvider,
                  onProviderChange: setSelectedProvider,
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
                onNavigateToPage={setActiveTab}
                onNavigateToTrace={handleNavigateToTrace}
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
            ) : activeTab === "projects" ? (
              <ProjectsPage projectId={effectiveProjectId} />
            ) : activeTab === "tasks" ? (
              <TasksPage projectFilter={effectiveProjectId} />
            ) : activeTab === "memory" ? (
              <MemoryPage projectId={effectiveProjectId} />
            ) : activeTab === "cron" ? (
              <CronJobsPage projectId={effectiveProjectId} />
            ) : activeTab === "traces" ? (
              <TracesPage
                projectId={effectiveProjectId || undefined}
                initialTraceId={initialTraceId}
              />
            ) : activeTab === "skills" ? (
              <SkillsPage />
            ) : activeTab === "workflows" ? (
              <WorkflowsPage projectId={effectiveProjectId} />
            ) : activeTab === "mcp" ? (
              <McpPage />
            ) : activeTab === "integrations" ? (
              <IntegrationsPage />
            ) : activeTab === "reports" ? (
              <ReportsPage
                projectId={effectiveProjectId}
                onNavigateToTrace={handleNavigateToTrace}
              />
            ) : activeTab === "configuration" ? (
              <ConfigurationPage />
            ) : activeTab === "dashboard" ? (
              <DashboardPage />
            ) : (
              <ComingSoonPage
                title={
                  navItems.find((i) => i.id === activeTab)?.label ?? activeTab
                }
              />
            )}
          </Suspense>
        </AppErrorBoundary>
      </FilesProvider>

      <Settings
        isOpen={settingsOpen}
        onClose={() => setSettingsOpen(false)}
        settings={settings}
        onFontSizeChange={updateFontSize}
        onThemeChange={updateTheme}
        onDefaultChatModeChange={updateDefaultChatMode}
        onPostPlanChatModeChange={updatePostPlanChatMode}
        onSttEnabledChange={updateSttEnabled}
        onTtsEnabledChange={updateTtsEnabled}
        onVoiceInputModeChange={updateVoiceInputMode}
        onReset={resetSettings}
      />

      <QuickCaptureTask
        isOpen={quickCaptureOpen}
        onClose={() => setQuickCaptureOpen(false)}
      />

      <ResumeSessionModal
        isOpen={resumeModalOpen}
        onClose={() => setResumeModalOpen(false)}
        sessions={allProjectSessions}
        onResume={handleContinueInChat}
      />

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

      {toastMessage && (
        <div className="app-toast" onClick={() => setToastMessage(null)}>
          {toastMessage}
        </div>
      )}
    </div>
  );
}
