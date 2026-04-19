import {
  useState,
  useCallback,
  useMemo,
  useEffect,
  useRef,
  lazy,
  Suspense,
  Component,
  type ReactNode,
} from "react";
import { useAuth } from "./hooks/useAuth";
import { useChat } from "./hooks/useChat";
import { useVoice } from "./hooks/useVoice";
import { useSettings } from "./hooks/useSettings";
import { useMcp } from "./hooks/useMcp";
import { useSkills } from "./hooks/useSkills";
import { useColonAutocomplete } from "./hooks/useColonAutocomplete";
import type { PaletteItem } from "./hooks/useColonAutocomplete";
import { useAgentDefinitions } from "./hooks/useAgentDefinitions";
import { useProjects } from "./hooks/useProjects";
import { useSessionCatalog } from "./hooks/useSessionCatalog";
import { normalizeChatMode } from "./types/chat";
import type { QueuedFile } from "./types/chat";
import type { GobbySession } from "./types/sessions";
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
import type { CommandPaletteAction } from "./components/chat/CommandPalette";
import { FilesProvider } from "./contexts/FilesContext";
import {
  buildReasoningPreferenceKey,
  fetchProviderModelCatalog,
  getPreferredModelForProvider,
  getPreferredReasoningEffort,
  resolveModelValueForProvider,
  type ProviderModelEntry,
} from "./lib/providerModels";
import { cn } from "./lib/utils";

const CONVERSATION_ID_STORAGE_KEY = "gobby-conversation-id";
const DB_SESSION_ID_STORAGE_KEY = "gobby-db-session-id";
const REASONING_PREFERENCES_STORAGE_KEY = "gobby-reasoning-preferences";


function loadPersistedConversationId(): string | null {
  try {
    return (
      localStorage.getItem(DB_SESSION_ID_STORAGE_KEY) ||
      localStorage.getItem(CONVERSATION_ID_STORAGE_KEY)
    );
  } catch {
    return null;
  }
}


function loadPersistedDbSessionId(): string | null {
  try {
    return localStorage.getItem(DB_SESSION_ID_STORAGE_KEY);
  } catch {
    return null;
  }
}

function loadReasoningPreferences(): Record<string, string> {
  try {
    const raw = localStorage.getItem(REASONING_PREFERENCES_STORAGE_KEY);
    if (!raw) {
      return {};
    }
    const parsed = JSON.parse(raw);
    return parsed && typeof parsed === "object" ? (parsed as Record<string, string>) : {};
  } catch {
    return {};
  }
}
const MemoryPage = lazy(() =>
  import("./components/memory/MemoryPage").then((m) => ({
    default: m.MemoryPage,
  })),
);
const ProjectsPage = lazy(() =>
  import("./components/projects/ProjectsPage").then((m) => ({
    default: m.ProjectsPage,
  })),
);
const TasksPage = lazy(() =>
  import("./components/tasks/TasksPage").then((m) => ({
    default: m.TasksPage,
  })),
);
const SkillsPage = lazy(() =>
  import("./components/skills/SkillsPage").then((m) => ({
    default: m.SkillsPage,
  })),
);
const McpPage = lazy(() =>
  import("./components/mcp/McpPage").then((m) => ({ default: m.McpPage })),
);
const IntegrationsPage = lazy(() =>
  import("./components/integrations/IntegrationsPage").then((m) => ({
    default: m.IntegrationsPage,
  })),
);
const CronJobsPage = lazy(() =>
  import("./components/CronJobsPage").then((m) => ({
    default: m.CronJobsPage,
  })),
);
const ConfigurationPage = lazy(() =>
  import("./components/ConfigurationPage").then((m) => ({
    default: m.ConfigurationPage,
  })),
);
const WorkflowsPage = lazy(() =>
  import("./components/workflows/WorkflowsPage").then((m) => ({
    default: m.WorkflowsPage,
  })),
);
const ReportsPage = lazy(() =>
  import("./components/workflows/ReportsPage").then((m) => ({
    default: m.ReportsPage,
  })),
);
const DashboardPage = lazy(() =>
  import("./components/dashboard/DashboardPage").then((m) => ({
    default: m.DashboardPage,
  })),
);
const TracesPage = lazy(() =>
  import("./components/traces/TracesPage").then((m) => ({
    default: m.TracesPage,
  })),
);

class AppErrorBoundary extends Component<
  { children: ReactNode; activeTab: string; onReturnToChat: () => void },
  { hasError: boolean; error: Error | null }
> {
  constructor(props: {
    children: ReactNode;
    activeTab: string;
    onReturnToChat: () => void;
  }) {
    super(props);
    this.state = { hasError: false, error: null };
  }
  static getDerivedStateFromError(error: Error) {
    return { hasError: true, error };
  }
  componentDidCatch(error: Error, info: React.ErrorInfo) {
    console.error(
      "[AppErrorBoundary] Caught error in tab:",
      this.props.activeTab,
      error,
      info,
    );
  }
  componentDidUpdate(prevProps: { activeTab: string }) {
    if (prevProps.activeTab !== this.props.activeTab && this.state.hasError) {
      this.setState({ hasError: false, error: null });
    }
  }
  render() {
    if (this.state.hasError) {
      return (
        <main
          style={{
            display: "flex",
            flexDirection: "column",
            alignItems: "center",
            justifyContent: "center",
            flex: 1,
            gap: "1rem",
            padding: "2rem",
            color: "var(--text-secondary)",
          }}
        >
          <div
            style={{
              fontSize: "1.25rem",
              color: "var(--text-primary)",
              fontWeight: 600,
            }}
          >
            Something went wrong
          </div>
          <div
            style={{
              fontSize: "0.85rem",
              maxWidth: 480,
              textAlign: "center",
              lineHeight: 1.5,
            }}
          >
            An error occurred in the <b>{this.props.activeTab}</b> tab. This is
            usually caused by a rendering failure in a third-party library.
          </div>
          {this.state.error && (
            <code
              style={{
                fontSize: "0.75rem",
                color: "var(--text-muted)",
                background: "var(--bg-secondary)",
                padding: "0.5rem 1rem",
                borderRadius: 4,
                maxWidth: 600,
                overflow: "auto",
                whiteSpace: "pre-wrap",
              }}
            >
              {this.state.error.message}
            </code>
          )}
          <div style={{ display: "flex", gap: "0.5rem", marginTop: "0.5rem" }}>
            <button
              onClick={() => this.setState({ hasError: false, error: null })}
              style={{
                padding: "0.4rem 1rem",
                borderRadius: 4,
                border: "1px solid var(--border)",
                background: "var(--bg-secondary)",
                color: "var(--text-primary)",
                cursor: "pointer",
                fontSize: "0.8rem",
              }}
            >
              Try Again
            </button>
            <button
              onClick={this.props.onReturnToChat}
              style={{
                padding: "0.4rem 1rem",
                borderRadius: 4,
                border: "none",
                background: "var(--accent)",
                color: "#fff",
                cursor: "pointer",
                fontSize: "0.8rem",
              }}
            >
              Return to Chat
            </button>
          </div>
        </main>
      );
    }
    return this.props.children;
  }
}

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
    const validTabs = new Set([
      "dashboard",
      "chat",
      "projects",
      "tasks",
      "workflows",
      "reports",
      "cron",
      "traces",
      "memory",
      "skills",
      "mcp",
      "configuration",
    ]);
    return validTabs.has(hash) ? hash : "chat";
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

  // Global keyboard: Cmd+K opens command palette (or chord Cmd+K → t for quick capture)
  const chordPendingRef = useRef(false);
  const chordTimeoutRef = useRef<number | null>(null);

  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === "k") {
        e.preventDefault();
        // If already in chord mode, cancel it
        if (chordPendingRef.current) {
          chordPendingRef.current = false;
          if (chordTimeoutRef.current)
            window.clearTimeout(chordTimeoutRef.current);
        }
        // Start chord timer — if no follow-up key, open palette
        chordPendingRef.current = true;
        if (chordTimeoutRef.current)
          window.clearTimeout(chordTimeoutRef.current);
        chordTimeoutRef.current = window.setTimeout(() => {
          chordPendingRef.current = false;
          if (activeTab === "chat") {
            window.dispatchEvent(new CustomEvent("gobby:open-command-palette"));
          }
        }, 300);
        return;
      }

      if (chordPendingRef.current && e.key === "t") {
        e.preventDefault();
        chordPendingRef.current = false;
        if (chordTimeoutRef.current)
          window.clearTimeout(chordTimeoutRef.current);
        setQuickCaptureOpen(true);
      } else if (chordPendingRef.current) {
        chordPendingRef.current = false;
        if (chordTimeoutRef.current)
          window.clearTimeout(chordTimeoutRef.current);
        // No recognized chord key — open palette immediately
        if (activeTab === "chat") {
          window.dispatchEvent(new CustomEvent("gobby:open-command-palette"));
        }
      }
    };

    window.addEventListener("keydown", handleKeyDown);
    return () => {
      window.removeEventListener("keydown", handleKeyDown);
      if (chordTimeoutRef.current) window.clearTimeout(chordTimeoutRef.current);
    };
  }, [activeTab]);

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
  const sessionCatalog = useSessionCatalog(effectiveProjectId);
  const confirmSessionDeleted = sessionCatalog.confirmSessionDeleted;
  const markSessionDeleting = sessionCatalog.markSessionDeleting;
  const restoreSession = sessionCatalog.restoreSession;
  const agentDefs = useAgentDefinitions(effectiveProjectId, selectedProvider ?? undefined);

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
      initialReconciliationDone.current = false;
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
  const initialReconciliationDone = useRef(false);

  useEffect(() => {
    if (!projectReady) return;
    if (initialReconciliationDone.current) return;
    if (!effectiveProjectId || sessionCatalog.isLoading) return;

    // Guard: ensure fetched sessions belong to the current project.
    // After a project switch, the fetch for the new project may still be in-flight
    // while webChatSessions contains stale data from the old project.
    const sessionsMatchProject =
      webChatSessions.length === 0 ||
      webChatSessions.some((s) => s.project_id === effectiveProjectId);
    if (!sessionsMatchProject) return;

    initialReconciliationDone.current = true;

    const persistedConversationId = loadPersistedConversationId();
    const persistedDbSessionId = loadPersistedDbSessionId();

    const match =
      webChatSessions.find((s) => s.id === dbSessionId) ||
      (persistedDbSessionId
        ? webChatSessions.find((s) => s.id === persistedDbSessionId)
        : undefined);

    if (match) {
      switchConversation(match.id, {
        preserveViewing: Boolean(viewingSessionId),
      });
    } else if (viewingSessionId && !persistedDbSessionId) {
      // Restored read-only session view with no parked main-chat session.
      return;
    } else if (persistedConversationId && !persistedDbSessionId) {
      // Preserve an explicit local fresh-chat ID so reloads do not jump back
      // to the most recent saved session before the user sends a first message.
      return;
    } else if (webChatSessions.length > 0) {
      // Unknown conversation_id — switch to most recent session
      const mostRecent = webChatSessions[0]; // sorted newest-first
      switchConversation(mostRecent.id);
    } else {
      // No sessions for this project — clear any stale messages from mount effect
      startNewChat();
    }
  }, [
    projectReady,
    effectiveProjectId,
    sessionCatalog.isLoading,
    webChatSessions,
    dbSessionId,
    viewingSessionId,
    switchConversation,
    startNewChat,
  ]);

  // Wrap sendMessage to include the selected model + colon command interception
  const handleSendMessage = useCallback(
    async (
      content: string,
      files?: QueuedFile[],
      options?: { reasoningEffort?: string | null },
    ) => {
      const reasoningEffort = options?.reasoningEffort ?? currentMainReasoning;
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
        );
      } else {
        sendMessage(
          content,
          settings.model,
          files,
          effectiveProjectId,
          undefined,
          reasoningEffort,
        );
      }
    },
    [
      currentMainReasoning,
      sendMessage,
      settings.model,
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
        } else {
          showToast("Failed to cancel agent");
        }
      } catch {
        showToast("Failed to cancel agent");
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
        }
      } catch {
        showToast("Failed to expire session");
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

  const handlePaletteSelect = useCallback(
    (item: PaletteItem) => {
      // Sub-items are handled inline by ChatInput (Tab-complete into input)
      if (item.kind !== "command") return;

      if (item.action === "open_skills") {
        setActiveModal("skills");
        return;
      }
      if (item.action === "open_gobby") {
        setActiveModal("gobby");
        return;
      }
      if (item.action === "open_mcp") {
        setActiveModal("mcp");
        return;
      }
      if (item.action === "open_settings") {
        setSettingsOpen(true);
        return;
      }
      if (item.action === "clear_history") {
        clearHistory();
        return;
      }
      if (item.action === "compact_chat") {
        sendMessage(
          "/compact",
          settings.model,
          undefined,
          effectiveProjectId,
          undefined,
          currentMainReasoning,
        );
        return;
      }
      if (item.action === "resume_session") {
        setResumeModalOpen(true);
        return;
      }
      if (item.action === "restart_daemon") {
        addSystemMessage("Restarting daemon...");
        const baseUrl = import.meta.env.VITE_API_BASE_URL || "";
        fetch(`${baseUrl}/api/admin/restart`, { method: "POST" }).catch((err) =>
          console.error("Restart request failed:", err),
        );
        return;
      }
      if (item.action === "exit_plan_mode") {
        if (settings.chatMode === "plan") {
          updateChatMode(settings.postPlanChatMode);
          sendMode(settings.postPlanChatMode);
        }
        return;
      }
      if (item.action === "show_plan") {
        if (settings.chatMode !== "plan") {
          updateChatMode("plan");
          sendMode("plan");
        }
        showPlanRef.current?.();
      }
    },
    [
      clearHistory,
      sendMessage,
      settings.model,
      settings.chatMode,
      settings.postPlanChatMode,
      effectiveProjectId,
      currentMainReasoning,
      updateChatMode,
      sendMode,
      addSystemMessage,
    ],
  );

  // Build command palette actions for ChatPage
  const commandPaletteActions = useMemo<CommandPaletteAction[]>(() => {
    const actions: CommandPaletteAction[] = [
      {
        id: "new-chat",
        label: "New Chat",
        icon: "+",
        category: "action",
        onSelect: () => startNewChat(),
      },
      {
        id: "resume",
        label: "Resume Session",
        icon: "\u21BA",
        category: "action",
        onSelect: () => setResumeModalOpen(true),
      },
      {
        id: "settings",
        label: "Settings",
        icon: "\u2699",
        category: "action",
        onSelect: () => setSettingsOpen(true),
      },
      {
        id: "clear",
        label: "Clear History",
        icon: "\u2715",
        category: "action",
        onSelect: () => clearHistory(),
      },
      {
        id: "compact",
        label: "Compact Conversation",
        icon: "\u2026",
        category: "action",
        onSelect: () =>
          sendMessage(
            "/compact",
            settings.model,
            undefined,
            effectiveProjectId,
            undefined,
            currentMainReasoning,
          ),
      },
      {
        id: "restart",
        label: "Restart Daemon",
        icon: "\u21BB",
        category: "action",
        onSelect: () => {
          addSystemMessage("Restarting daemon...");
          const baseUrl = import.meta.env.VITE_API_BASE_URL || "";
          fetch(`${baseUrl}/api/admin/restart`, { method: "POST" }).catch(
            (err) => {
              console.error("Restart request failed:", err);
              addSystemMessage("Failed to restart daemon");
            },
          );
        },
      },
    ];
    // Navigation items
    const navPages: Array<{ id: string; label: string }> = [
      { id: "dashboard", label: "Dashboard" },
      { id: "tasks", label: "Tasks" },
      { id: "workflows", label: "Workflows" },
      { id: "reports", label: "Reports" },
      { id: "cron", label: "Cron Jobs" },
      { id: "traces", label: "Traces" },
      { id: "memory", label: "Memory" },
      { id: "skills", label: "Skills" },
      { id: "mcp", label: "MCP" },
      { id: "configuration", label: "Configuration" },
    ];
    for (const page of navPages) {
      actions.push({
        id: `nav-${page.id}`,
        label: page.label,
        icon: "\u2192",
        category: "navigate",
        onSelect: () => setActiveTab(page.id),
      });
    }
    return actions;
  }, [
    startNewChat,
    clearHistory,
    sendMessage,
    settings.model,
    effectiveProjectId,
    currentMainReasoning,
    addSystemMessage,
  ]);

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

  const navItems = [
    { id: "chat", label: "Chat", icon: <ChatIcon /> },
    {
      id: "dashboard",
      label: "Dashboard",
      icon: <DashboardIcon />,
      separator: true,
    },
    { id: "projects", label: "Project", icon: <ProjectsIcon /> },
    { id: "tasks", label: "Tasks", icon: <TasksIcon /> },
    { id: "workflows", label: "Workflows", icon: <WorkflowsIcon /> },
    { id: "cron", label: "Cron Jobs", icon: <CronIcon /> },
    { id: "reports", label: "Reports", icon: <ReportsIcon /> },
    { id: "traces", label: "Traces", icon: <TracesIcon /> },
    { id: "memory", label: "Memory", icon: <MemoryIcon /> },
    { id: "skills", label: "Skills", icon: <SkillsIcon /> },
    { id: "mcp", label: "MCP", icon: <McpIcon /> },
    { id: "integrations", label: "Integrations", icon: <IntegrationsIcon /> },
    {
      id: "configuration",
      label: "Configuration",
      icon: <ConfigurationIcon />,
      separator: true,
    },
  ];

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

function HamburgerIcon() {
  return (
    <svg
      width="20"
      height="20"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <line x1="3" y1="6" x2="21" y2="6" />
      <line x1="3" y1="12" x2="21" y2="12" />
      <line x1="3" y1="18" x2="21" y2="18" />
    </svg>
  );
}

function DashboardIcon() {
  return (
    <svg
      width="18"
      height="18"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <rect x="3" y="3" width="7" height="9" />
      <rect x="14" y="3" width="7" height="5" />
      <rect x="14" y="12" width="7" height="9" />
      <rect x="3" y="16" width="7" height="5" />
    </svg>
  );
}

function ComingSoonPage({ title }: { title: string }) {
  return (
    <main className="coming-soon-page">
      <div className="coming-soon-content">
        <h2>{title}</h2>
        <p>Coming Soon</p>
      </div>
    </main>
  );
}

function TasksIcon() {
  return (
    <svg
      width="18"
      height="18"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d="M9 11l3 3L22 4" />
      <path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11" />
    </svg>
  );
}

function ProjectsIcon() {
  return (
    <svg
      width="18"
      height="18"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z" />
    </svg>
  );
}

function ReportsIcon() {
  return (
    <svg
      width="18"
      height="18"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d="M4 19h16" />
      <path d="M4 15h16" />
      <path d="M4 11h16" />
      <rect x="6" y="3" width="4" height="18" rx="1" opacity="0.3" />
      <rect x="6" y="7" width="4" height="14" rx="1" />
      <rect x="14" y="3" width="4" height="18" rx="1" opacity="0.3" />
      <rect x="14" y="11" width="4" height="10" rx="1" />
    </svg>
  );
}

function WorkflowsIcon() {
  return (
    <svg
      width="18"
      height="18"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <line x1="6" y1="3" x2="6" y2="15" />
      <circle cx="18" cy="6" r="3" />
      <circle cx="6" cy="18" r="3" />
      <path d="M18 9a9 9 0 0 1-9 9" />
    </svg>
  );
}

function MemoryIcon() {
  return (
    <svg
      width="18"
      height="18"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <ellipse cx="12" cy="5" rx="9" ry="3" />
      <path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3" />
      <path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5" />
    </svg>
  );
}

function SkillsIcon() {
  return (
    <svg
      width="18"
      height="18"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2" />
    </svg>
  );
}

function CronIcon() {
  return (
    <svg
      width="18"
      height="18"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <circle cx="12" cy="12" r="10" />
      <polyline points="12 6 12 12 16 14" />
    </svg>
  );
}

function IntegrationsIcon() {
  return (
    <svg
      width="18"
      height="18"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71" />
      <path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71" />
    </svg>
  );
}

function McpIcon() {
  return (
    <svg
      width="18"
      height="18"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <circle cx="12" cy="12" r="3" />
      <circle cx="4" cy="6" r="2" />
      <circle cx="20" cy="6" r="2" />
      <circle cx="4" cy="18" r="2" />
      <circle cx="20" cy="18" r="2" />
      <line x1="6" y1="6" x2="9.5" y2="10" />
      <line x1="18" y1="6" x2="14.5" y2="10" />
      <line x1="6" y1="18" x2="9.5" y2="14" />
      <line x1="18" y1="18" x2="14.5" y2="14" />
    </svg>
  );
}

function ChatIcon() {
  return (
    <svg
      width="18"
      height="18"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" />
      <path d="M8 10h8" />
      <path d="M8 14h4" />
    </svg>
  );
}

function TracesIcon() {
  return (
    <svg
      width="18"
      height="18"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d="M22 12h-4l-3 9L9 3l-3 9H2" />
    </svg>
  );
}

function ConfigurationIcon() {
  return (
    <svg
      width="18"
      height="18"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <circle cx="12" cy="12" r="3" />
      <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z" />
    </svg>
  );
}
