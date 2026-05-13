import "./styles.css";
import { useCallback, useEffect, useRef, useState } from "react";
import { useIsMobile } from "../../hooks/useIsMobile";
import { AUTONOMOUS_CHAT_MODES } from "../../types/chat";
import type {
  ChatState,
  ConversationState,
  SwappedSessionTarget,
  VoiceProps,
} from "../../types/chat";
import type { AgentDefInfo } from "../../hooks/useAgentDefinitions";
import type { PaletteItem } from "../../hooks/useColonAutocomplete";
import type { VoiceInputMode } from "../../hooks/useSettings";
import type { ArtifactType } from "../../types/artifacts";
import type { GobbySession } from "../../types/sessions";
import { useArtifacts } from "../../hooks/useArtifacts";
import { ArtifactContext } from "./artifacts/ArtifactContext";
import { MessageList, type MessageListHandle } from "./MessageList";
import { ChatInput } from "./ChatInput";
import { CommandBar } from "./CommandBar";
import { CommandPalette, type CommandPaletteAction } from "./CommandPalette";
import { ActivityPanel } from "../activity/ActivityPanel";
import { useActivityPanel } from "../activity/useActivityPanel";
import type { SessionsFilters } from "../activity/sessionsFilters";
import { VoiceStatusBar } from "./VoiceStatusBar";
import { AgentStatusBar } from "./AgentStatusBar";
import { useCanvasPanel } from "../canvas/hooks/useCanvasPanel";
import { useFileChanges } from "../../hooks/useFileChanges";
import { useConfirmDialog } from "../../hooks/useConfirmDialog";
import {
  buildReasoningPreferenceKey,
  fetchProviderModelCatalog,
  getPreferredReasoningEffort,
  resolveProviderModelPair,
  type ProviderModelEntry,
} from "../../lib/providerModels";
import { canProxyAttachObservationMeta } from "../../lib/sessionProxyAttach";

const VALID_ARTIFACT_TYPES = new Set<string>([
  "code",
  "text",
  "image",
  "sheet",
]);

interface ChatPageProps {
  chat: ChatState;
  conversations: ConversationState;
  voice: VoiceProps;
  projectId?: string | null;
  showPlanRef?: React.MutableRefObject<(() => void) | null>;
  agentDefinitions?: AgentDefInfo[];
  agentGlobalDefs?: AgentDefInfo[];
  agentProjectDefs?: AgentDefInfo[];
  agentShowScopeToggle?: boolean;
  agentHasGlobal?: boolean;
  agentHasProject?: boolean;
  // Model selection
  currentModel?: string;
  onModelChange?: (model: string) => void;
  reasoningPreferences?: Record<string, string>;
  onReasoningPreferenceChange?: (
    provider: string,
    model: string,
    reasoningEffort: string,
  ) => void;
  // Command palette actions from App.tsx
  paletteActions?: CommandPaletteAction[];
  allProjectSessions?: GobbySession[];
  allProjectSessionsLoading?: boolean;
  activitySessions?: GobbySession[];
  activitySessionsLoading?: boolean;
  sessionsFilters?: SessionsFilters;
  onSessionsFiltersChange?: (filters: SessionsFilters) => void;
  onSttEnabledChange?: (enabled: boolean) => void;
  onTtsEnabledChange?: (enabled: boolean) => void;
  onVoiceInputModeChange?: (mode: VoiceInputMode) => void;
}

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
  onSttEnabledChange,
  onTtsEnabledChange,
  onVoiceInputModeChange,
}: ChatPageProps) {
  const messageListRef = useRef<MessageListHandle>(null);
  const lastAutoScrolledLoadRef = useRef<string | null>(null);
  const activeSession = conversations.sessions.find(
    (s) => s.id === conversations.activeSessionId,
  );
  const mainSessionMeta =
    chat.mainSessionMeta ??
    (activeSession
      ? {
          ref:
            activeSession.seq_num != null ? `#${activeSession.seq_num}` : null,
          source: activeSession.source,
          title: activeSession.title ?? null,
          status: activeSession.status,
          model: activeSession.model ?? null,
          externalId: activeSession.external_id,
          chatMode: activeSession.chat_mode ?? null,
          gitBranch: activeSession.git_branch ?? null,
          contextWindow: null,
          agentRunId: activeSession.agent_run_id ?? null,
          workflowName: null,
          agentName: null,
          sessionType: "web_chat" as const,
        }
      : null);
  const activeTitle = chat.sessionTitle ?? mainSessionMeta?.title ?? null;
  const effectiveSessionRef =
    chat.sessionRef ??
    mainSessionMeta?.ref ??
    (activeSession?.seq_num != null ? `#${activeSession.seq_num}` : null);

  const {
    artifacts,
    activeArtifact,
    createArtifact,
    updateArtifact,
    openArtifact,
    closePanel: closeArtifactPanel,
    clearArtifacts,
    setVersion,
  } = useArtifacts();

  const isMobile = useIsMobile();
  const canvas = useCanvasPanel();
  const activity = useActivityPanel();
  const fileChanges = useFileChanges(chat.messages, projectId ?? null);
  const { confirm, ConfirmDialogElement } = useConfirmDialog();
  const { openCanvas, closeCanvas, activeCanvas } = canvas;
  const {
    activeTab: activityTab,
    closeIfAutoOpened,
    isPinned,
    panelWidth,
    setActiveTab: setActivityTab,
    setIsPinned,
    setPanelWidth,
    showTab,
    togglePanel,
  } = activity;
  const {
    onApprovePlan,
    onPaletteSelect,
    onRequestPlanChanges,
    onSend,
    planPendingApproval,
    setOnArtifactEvent,
    setOnPlanReady,
  } = chat;

  // Session browsing via activity panel instead of Observing mode
  const [focusSessionId, setFocusSessionId] = useState<string | null>(null);
  const handleFocusSessionHandled = useCallback(() => {
    setFocusSessionId(null);
  }, []);
  const prevIsMobileRef = useRef(isMobile);
  const isPinnedRef = useRef(isPinned);
  const onSendRef = useRef(onSend);

  useEffect(() => {
    isPinnedRef.current = isPinned;
  }, [isPinned]);

  useEffect(() => {
    onSendRef.current = onSend;
  }, [onSend]);

  useEffect(() => {
    if (!prevIsMobileRef.current && isMobile && isPinnedRef.current) {
      setIsPinned(false);
    }
    prevIsMobileRef.current = isMobile;
  }, [isMobile, setIsPinned]);

  const parkCurrentSession = useCallback(
    (nextSessionId?: string) => {
      const currentSessionId = chat.dbSessionId;
      if (!currentSessionId || currentSessionId === nextSessionId) {
        return;
      }
      setFocusSessionId(currentSessionId);
      showTab("sessions");
    },
    [chat.dbSessionId, showTab],
  );

  const handleSwapSession = useCallback(
    (target: SwappedSessionTarget) => {
      parkCurrentSession(target.sessionId);

      if (target.sessionType === "web_chat") {
        const targetSession = conversations.sessions.find(
          (session) => session.id === target.sessionId,
        );
        if (targetSession) {
          conversations.onSelectSession(targetSession);
        }
        if (isMobile && isPinned) {
          setIsPinned(false);
        }
        return;
      }

      chat.viewSession?.(target.sessionId, { forceRefresh: true });
      chat.observeSession?.(target.sessionId, "observe");
      if (isMobile && isPinned) {
        setIsPinned(false);
      }
    },
    [chat, conversations, isMobile, isPinned, parkCurrentSession, setIsPinned],
  );

  const handleResumeSessionFromActivity = useCallback(
    async (sessionId: string) => {
      if (!chat.continueSessionInChat) {
        return "";
      }
      parkCurrentSession(sessionId);
      return chat.continueSessionInChat(sessionId, projectId ?? undefined, {
        fallbackContext: "auto",
      });
    },
    [chat, parkCurrentSession, projectId],
  );

  // Available LLM providers — fetched from daemon API
  const [availableProviders, setAvailableProviders] = useState<string[]>([]);
  const [providerModelCatalog, setProviderModelCatalog] = useState<
    ProviderModelEntry[]
  >([]);
  const viewingMeta =
    chat.viewingSessionMeta ?? chat.attachedSessionMeta ?? null;
  const isSwappedTerminal = viewingMeta?.sessionType === "terminal";
  const isAutonomousSession = Boolean(
    isSwappedTerminal && viewingMeta?.agentRunId,
  );
  const isProxyAttached =
    Boolean(chat.attachedSessionId) && chat.sessionInteractionMode === "proxy";
  const canAttachViewedSession =
    !isAutonomousSession && canProxyAttachObservationMeta(viewingMeta);
  const canControlViewedSession =
    viewingMeta?.sessionType === "terminal" && !isAutonomousSession;
  const providerPickerDisabledReason = isProxyAttached
    ? "Attached session owns provider, model, and reasoning"
    : isAutonomousSession
      ? chat.sessionInteractionMode === "proxy"
        ? "Cannot change provider on a pipeline-managed session"
        : "Observing autonomous session"
      : null;
  const mainInputSelection = resolveProviderModelPair(
    providerModelCatalog,
    {
      provider: mainSessionMeta?.source ?? null,
      model: mainSessionMeta?.model ?? null,
    },
    {
      provider: chat.provider ?? null,
      model: currentModel,
    },
  );
  const viewedInputSelection = resolveProviderModelPair(
    providerModelCatalog,
    {
      provider: viewingMeta?.source ?? null,
      model: viewingMeta?.model ?? null,
    },
    {
      provider: mainSessionMeta?.source ?? chat.provider ?? null,
      model: mainSessionMeta?.model ?? currentModel,
    },
  );
  const effectiveInputProvider = isSwappedTerminal
    ? viewedInputSelection.provider
    : mainInputSelection.provider;
  const effectiveInputModel = isSwappedTerminal
    ? (viewedInputSelection.model ?? "")
    : (mainInputSelection.model ?? "");
  const effectiveAgentName = isSwappedTerminal
    ? (viewingMeta?.agentName ?? chat.activeAgent)
    : chat.activeAgent;
  const effectiveBranch = viewingMeta?.gitBranch ?? chat.currentBranch;
  const effectiveReasoningPreferenceKey = buildReasoningPreferenceKey(
    effectiveInputProvider,
    effectiveInputModel,
  );
  const preferredReasoningEffort = effectiveReasoningPreferenceKey
    ? reasoningPreferences[effectiveReasoningPreferenceKey]
    : null;
  const effectiveInputReasoning =
    isSwappedTerminal && viewingMeta?.reasoningEffort
      ? viewingMeta.reasoningEffort
      : getPreferredReasoningEffort(
          providerModelCatalog,
          effectiveInputProvider,
          effectiveInputModel,
          preferredReasoningEffort,
        );
  const isReadOnlySession =
    isSwappedTerminal && chat.sessionInteractionMode !== "proxy";
  const showChatInput = !isReadOnlySession;
  const chatInputDisabled =
    !chat.isConnected || Boolean(chat.isContinuingSession);
  const chatInputDisabledPlaceholder = !chat.isConnected
    ? chat.isReconnecting
      ? "Reconnecting to server..."
      : "Connecting to server..."
    : chat.isContinuingSession
      ? "Resuming session in web chat..."
      : undefined;
  const chatInputDisabledAriaLabel = !chat.isConnected
    ? chat.isReconnecting
      ? "Message input — reconnecting"
      : "Message input — connecting"
    : chat.isContinuingSession
      ? "Message input — resuming session"
      : undefined;
  // Anchor the activity-panel session list to whichever session is currently
  // showing in the main area so a swapped/attached session cannot also appear
  // in the panel list.
  const activityPanelChatSessionId =
    chat.viewingSessionId ?? chat.attachedSessionId ?? chat.dbSessionId;

  const handleResumeViewedSession = useCallback(() => {
    if (
      !isSwappedTerminal ||
      isAutonomousSession ||
      !chat.viewingSessionId ||
      !chat.continueSessionInChat
    ) {
      return;
    }
    if (effectiveInputModel) {
      onModelChange?.(effectiveInputModel);
    }
    if (
      effectiveInputProvider &&
      effectiveInputModel &&
      effectiveInputReasoning
    ) {
      onReasoningPreferenceChange?.(
        effectiveInputProvider,
        effectiveInputModel,
        effectiveInputReasoning,
      );
    }
    void chat.continueSessionInChat(
      chat.viewingSessionId,
      projectId ?? undefined,
      {
        provider: effectiveInputProvider,
        model: effectiveInputModel,
        reasoningEffort: effectiveInputReasoning,
        chatMode: viewingMeta?.chatMode ?? null,
        fallbackContext: "auto",
      },
    );
  }, [
    chat,
    effectiveInputModel,
    effectiveInputProvider,
    effectiveInputReasoning,
    isAutonomousSession,
    isSwappedTerminal,
    onModelChange,
    onReasoningPreferenceChange,
    projectId,
    viewingMeta?.chatMode,
  ]);

  const handleSwappedSessionProviderSelection = useCallback(
    async (provider: string, model: string, reasoningEffort: string | null) => {
      if (
        !isSwappedTerminal ||
        isAutonomousSession ||
        !chat.viewingSessionId ||
        !chat.continueSessionInChat
      ) {
        return;
      }

      const confirmChange = canAttachViewedSession
        ? await confirm({
            title: "Change provider?",
            description: `This will end the terminal session and resume the conversation with ${provider} ${model}.`,
            confirmLabel: "Change Provider",
            destructive: true,
          })
        : true;
      if (!confirmChange) return;

      chat.onProviderChange?.(provider);
      onModelChange?.(model);
      if (reasoningEffort) {
        onReasoningPreferenceChange?.(provider, model, reasoningEffort);
      }
      await chat.continueSessionInChat(
        chat.viewingSessionId,
        projectId ?? undefined,
        {
          provider,
          model,
          reasoningEffort,
          chatMode: viewingMeta?.chatMode ?? null,
          fallbackContext: "auto",
        },
      );
    },
    [
      chat,
      confirm,
      isAutonomousSession,
      isSwappedTerminal,
      onModelChange,
      onReasoningPreferenceChange,
      projectId,
      viewingMeta?.chatMode,
      canAttachViewedSession,
    ],
  );

  const handleMainProviderSelection = useCallback(
    (provider: string, model: string, reasoningEffort: string | null) => {
      const providerChanged =
        provider !== (effectiveInputProvider ?? chat.provider ?? "claude");

      onModelChange?.(model);
      if (reasoningEffort) {
        onReasoningPreferenceChange?.(provider, model, reasoningEffort);
      }

      if (!providerChanged) {
        return;
      }

      if (chat.onSwitchProvider) {
        chat.onSwitchProvider(provider, {
          model,
          reasoningEffort,
        });
        return;
      }

      chat.onProviderChange?.(provider);
    },
    [chat, effectiveInputProvider, onModelChange, onReasoningPreferenceChange],
  );

  // Wrap onNewChat to park the current session as Watching
  const handleNewChat = useCallback(
    (agentName?: string) => {
      parkCurrentSession();
      conversations.onNewChat(agentName);
    },
    [conversations, parkCurrentSession],
  );

  useEffect(() => {
    fetch("/api/providers")
      .then((r) => {
        if (!r.ok) {
          throw new Error(`Provider fetch failed with ${r.status}`);
        }
        return r.json();
      })
      .then((data) => {
        const names = (Array.isArray(data?.providers) ? data.providers : [])
          .filter((p: { available: boolean }) => p.available)
          .map((p: { name: string }) => p.name);
        setAvailableProviders(names);
      })
      .catch(() => setAvailableProviders([effectiveInputProvider || "claude"]));
  }, [effectiveInputProvider]);
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

  // Modals
  const [showCommandPalette, setShowCommandPalette] = useState(false);

  useEffect(() => {
    if (chat.canvasPanel) {
      openCanvas(chat.canvasPanel);
      // Auto-switch to canvas tab
      showTab("canvas");
    } else {
      closeCanvas();
    }
  }, [chat.canvasPanel, closeCanvas, openCanvas, showTab]);

  const [planArtifactId, setPlanArtifactId] = useState<string | null>(null);
  const [pendingPlanArtifactId, setPendingPlanArtifactId] = useState<
    string | null
  >(null);
  const planArtifactIdRef = useRef<string | null>(null);
  const lastPlanArtifactContentRef = useRef<string | null>(null);

  useEffect(() => {
    planArtifactIdRef.current = planArtifactId;
  }, [planArtifactId]);

  // Reset plan state on session switch / new chat. Render-time comparison
  // avoids cascading setState-in-effect renders.
  const [prevSwitchKey, setPrevSwitchKey] = useState(
    chat.conversationSwitchKey,
  );
  if (prevSwitchKey !== chat.conversationSwitchKey) {
    setPrevSwitchKey(chat.conversationSwitchKey);
    setPlanArtifactId(null);
    setPendingPlanArtifactId(null);
  }
  useEffect(() => {
    clearArtifacts();
    planArtifactIdRef.current = null;
    lastPlanArtifactContentRef.current = null;
  }, [chat.conversationSwitchKey, clearArtifacts]);

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

  const openCodeAsArtifact = useCallback(
    (language: string, content: string, title?: string) => {
      createArtifact("code", content, language, title);
      showTab("artifacts");
    },
    [createArtifact, showTab],
  );

  const openFileAsArtifact = useCallback(
    (type: ArtifactType, language: string, content: string, title?: string) => {
      createArtifact(type, content, language, title);
      showTab("artifacts");
    },
    [createArtifact, showTab],
  );

  // Wire plan content to artifact panel when plan_pending_approval arrives.
  // If a plan artifact already exists (revision after rejection), add a new
  // version instead of creating a duplicate entry.
  const onPlanReady = useCallback(
    (content: string | null) => {
      if (!content) return;
      const existingArtifactId = planArtifactIdRef.current;
      if (
        existingArtifactId &&
        content === lastPlanArtifactContentRef.current
      ) {
        setPendingPlanArtifactId(existingArtifactId);
        openArtifact(existingArtifactId);
        showTab("plans");
        return;
      }

      const headingMatch = content.match(/^#\s+(.+)$/m);
      const title = headingMatch?.[1]?.trim() || "Implementation Plan";
      let nextArtifactId = existingArtifactId;

      if (existingArtifactId && artifacts.has(existingArtifactId)) {
        updateArtifact(existingArtifactId, content);
        openArtifact(existingArtifactId);
      } else {
        nextArtifactId = createArtifact("text", content, "markdown", title, {
          isPlan: true,
        });
      }
      planArtifactIdRef.current = nextArtifactId;
      lastPlanArtifactContentRef.current = content;
      setPlanArtifactId(nextArtifactId);
      setPendingPlanArtifactId(nextArtifactId);
      showTab("plans");
    },
    [artifacts, createArtifact, openArtifact, showTab, updateArtifact],
  );

  useEffect(() => {
    setOnPlanReady?.(onPlanReady);
  }, [onPlanReady, setOnPlanReady]);

  // Wire artifact events (show_file) to artifact panel
  const onArtifactEvent = useCallback(
    (type: string, content: string, language?: string, title?: string) => {
      if (VALID_ARTIFACT_TYPES.has(type)) {
        createArtifact(type as ArtifactType, content, language, title);
        showTab("artifacts");
      }
    },
    [createArtifact, showTab],
  );

  useEffect(() => {
    setOnArtifactEvent?.(onArtifactEvent);
  }, [onArtifactEvent, setOnArtifactEvent]);

  // Intercept toggle_panel palette action before forwarding to App.tsx
  const handlePaletteSelect = useCallback(
    (item: PaletteItem) => {
      if (item.kind === "command" && item.action === "toggle_panel") {
        togglePanel();
        return;
      }
      onPaletteSelect?.(item);
    },
    [onPaletteSelect, togglePanel],
  );

  // Add file to chat from Files tab (right-click "Add to chat")
  const handleAddFileToChat = useCallback((filePath: string) => {
    onSendRef.current?.(`Read and reference this file: ${filePath}`);
  }, []);

  const handleApprovePlan = useCallback(() => {
    setPendingPlanArtifactId(null);
    onApprovePlan?.();
    if (isMobile && isPinned) {
      setIsPinned(false);
    }
  }, [isMobile, isPinned, onApprovePlan, setIsPinned]);

  const handleRequestPlanChanges = useCallback(
    (feedback: string) => {
      setPendingPlanArtifactId(null);
      onRequestPlanChanges?.(feedback);
      if (isMobile && isPinned) {
        setIsPinned(false);
      }
    },
    [isMobile, isPinned, onRequestPlanChanges, setIsPinned],
  );

  // Close artifact and auto-close activity panel if it was opened programmatically
  const handleCloseArtifact = useCallback(() => {
    closeArtifactPanel();
    closeIfAutoOpened();
  }, [closeArtifactPanel, closeIfAutoOpened]);

  // Expose callback for /plan command to reopen plan artifact
  useEffect(() => {
    if (showPlanRef) {
      showPlanRef.current = () => {
        if (planArtifactId) {
          openArtifact(planArtifactId);
          showTab("plans");
        }
      };
    }
    return () => {
      if (showPlanRef) showPlanRef.current = null;
    };
  }, [openArtifact, planArtifactId, showPlanRef, showTab]);

  // Listen for palette open event from App.tsx Cmd+K handler
  useEffect(() => {
    const handler = () => setShowCommandPalette(true);
    window.addEventListener("gobby:open-command-palette", handler);
    return () =>
      window.removeEventListener("gobby:open-command-palette", handler);
  }, []);

  // Keyboard shortcuts
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      // Cmd+K — Command Palette (handled in App.tsx chord, but also direct)
      // Cmd+` — Toggle Activity Panel
      if ((e.metaKey || e.ctrlKey) && e.key === "`") {
        e.preventDefault();
        togglePanel();
        return;
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [togglePanel]);

  const wantsVoiceStatusSlot = Boolean(
    voice.ttsEnabled || (voice.sttEnabled && voice.voiceInputMode === "vad"),
  );
  const showVoiceStatusBar = Boolean(
    wantsVoiceStatusSlot ||
      voice.voiceLoading ||
      voice.isListening ||
      voice.isTranscribing ||
      voice.voiceError,
  );
  const voiceStatusWarming = Boolean(
    voice.voiceLoading ||
      (wantsVoiceStatusSlot &&
        voice.voiceAvailable &&
        !voice.voiceReady &&
        !voice.voiceError),
  );

  return (
    <div className="relative flex h-full overflow-hidden bg-background text-foreground">
      <h1 className="sr-only">Chat</h1>
      {ConfirmDialogElement}
      {/* Main chat column */}
      <div className="chat-column flex flex-col flex-1 min-w-[320px]">
        {/* Command Bar */}
        <CommandBar
          sessionRef={effectiveSessionRef}
          title={viewingMeta?.title ?? activeTitle}
          sessionSource={
            viewingMeta?.source ??
            mainSessionMeta?.source ??
            chat.provider ??
            null
          }
          onOpenPalette={() => setShowCommandPalette(true)}
          onTogglePanel={togglePanel}
          isPanelPinned={isPinned}
          agentDefinitions={agentDefinitions}
          agentGlobalDefs={agentGlobalDefs}
          agentProjectDefs={agentProjectDefs}
          agentShowScopeToggle={agentShowScopeToggle}
          agentHasGlobal={agentHasGlobal}
          agentHasProject={agentHasProject}
        />
        <ArtifactContext.Provider
          value={{ openCodeAsArtifact, openFileAsArtifact }}
        >
          {/* Reconnecting banner */}
          {chat.isReconnecting && (
            <div className="bg-warning/20 text-warning-foreground text-xs text-center py-1 shrink-0">
              Reconnecting...
            </div>
          )}

          {/* Messages */}
          <div className="grid flex-1 min-h-0">
            <MessageList
              ref={messageListRef}
              messages={chat.messages}
              isStreaming={chat.isStreaming}
              isThinking={chat.isThinking}
              isLoadingMessages={chat.isLoadingMessages}
              onRespondToQuestion={chat.onRespondToQuestion}
              onRespondToApproval={chat.onRespondToApproval}
              canvasSurfaces={chat.canvasSurfaces}
              onCanvasInteraction={chat.onCanvasInteraction}
            />
          </div>

          {showVoiceStatusBar && (
            <VoiceStatusBar
              voiceLoading={voiceStatusWarming}
              isListening={voice.isListening ?? false}
              isSpeechDetected={voice.isSpeechDetected ?? false}
              isTranscribing={voice.isTranscribing ?? false}
              voiceError={voice.voiceError}
            />
          )}

          <AgentStatusBar
            viewingMeta={viewingMeta}
            interactionMode={chat.sessionInteractionMode ?? "none"}
            contextUsage={chat.contextUsage}
            contextUsageUpdatedAt={chat.contextUsageUpdatedAt}
            isAttached={!!chat.attachedSessionId}
            isAutonomousSession={isAutonomousSession}
            onAttach={
              canAttachViewedSession ? chat.onAttachToViewed : undefined
            }
            onResume={
              canControlViewedSession ? handleResumeViewedSession : undefined
            }
            onDetach={
              chat.attachedSessionId ? chat.onDetachFromSession : undefined
            }
            onNewChat={() => handleNewChat()}
          />

          {/* Chat input */}
          {showChatInput && (
            <ChatInput
              onSend={onSend}
              onStop={chat.onStop}
              isStreaming={chat.isStreaming}
              disabled={chatInputDisabled}
              disabledPlaceholder={chatInputDisabledPlaceholder}
              disabledAriaLabel={chatInputDisabledAriaLabel}
              onInputChange={chat.onInputChange}
              paletteItems={chat.paletteItems}
              onPaletteSelect={handlePaletteSelect}
              mode={chat.mode}
              onModeChange={chat.onModeChange}
              modeDisabled={isProxyAttached}
              modeOptions={
                isAutonomousSession ? AUTONOMOUS_CHAT_MODES : undefined
              }
              currentBranch={effectiveBranch}
              worktreePath={chat.worktreePath}
              projectId={projectId ?? null}
              onWorktreeChange={chat.onWorktreeChange}
              worktreePickerDisabled={isProxyAttached}
              agentName={effectiveAgentName}
              onAgentChange={chat.onAgentChange}
              agentPickerDisabled={isProxyAttached}
              agentDefinitions={agentDefinitions}
              agentGlobalDefs={agentGlobalDefs}
              agentProjectDefs={agentProjectDefs}
              agentShowScopeToggle={agentShowScopeToggle}
              agentHasGlobal={agentHasGlobal}
              agentHasProject={agentHasProject}
              sttEnabled={voice.sttEnabled}
              ttsEnabled={voice.ttsEnabled}
              voiceInputMode={voice.voiceInputMode}
              isRecording={voice.isRecording}
              isSpeaking={voice.isSpeaking}
              voiceLoading={voice.voiceLoading}
              voiceReady={voice.voiceReady}
              prepareTTSPlayback={voice.prepareTTSPlayback}
              startRecording={voice.startRecording}
              stopRecording={voice.stopRecording}
              cancelRecording={voice.cancelRecording}
              stopTTS={voice.stopTTS}
              onSttEnabledChange={onSttEnabledChange}
              onTtsEnabledChange={onTtsEnabledChange}
              onVoiceInputModeChange={onVoiceInputModeChange}
              isMobile={isMobile}
              onScrollToBottom={() => messageListRef.current?.scrollToBottom()}
              provider={effectiveInputProvider}
              availableProviders={availableProviders}
              providerModelCatalog={providerModelCatalog}
              currentModel={effectiveInputModel}
              currentReasoning={effectiveInputReasoning}
              onModelChange={onModelChange}
              onReasoningChange={(reasoningEffort) => {
                if (effectiveInputProvider && effectiveInputModel) {
                  onReasoningPreferenceChange?.(
                    effectiveInputProvider,
                    effectiveInputModel,
                    reasoningEffort,
                  );
                }
              }}
              onProviderSelectionChange={
                isSwappedTerminal
                  ? handleSwappedSessionProviderSelection
                  : handleMainProviderSelection
              }
              providerPickerDisabledReason={providerPickerDisabledReason}
              hasMessages={chat.messages.length > 0}
              proxySlashMode={
                isSwappedTerminal && chat.sessionInteractionMode === "proxy"
              }
              proxyDeliveryNotice={chat.proxyDeliveryNotice}
              attachmentsDisabled={isProxyAttached}
              isAttached={isProxyAttached}
            />
          )}
        </ArtifactContext.Provider>
      </div>

      {/* Activity Panel */}
      <ActivityPanel
        isPinned={isPinned}
        onPinnedChange={setIsPinned}
        panelWidth={panelWidth}
        onWidthChange={setPanelWidth}
        activeTab={activityTab}
        onTabChange={setActivityTab}
        artifacts={artifacts}
        activeArtifact={activeArtifact}
        onOpenArtifact={openArtifact}
        onCloseArtifact={handleCloseArtifact}
        onUpdateArtifactContent={updateArtifact}
        onSetArtifactVersion={setVersion}
        planPendingApproval={
          (pendingPlanArtifactId === activeArtifact?.id ||
            planPendingApproval) &&
          activeArtifact?.id === planArtifactId &&
          activeArtifact?.type === "text"
        }
        onApprovePlan={handleApprovePlan}
        onRequestPlanChanges={handleRequestPlanChanges}
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
        onKillAgent={conversations.onKillAgent}
        onExpireSession={conversations.onExpireSession}
        chatSessionId={activityPanelChatSessionId}
        focusSessionId={focusSessionId}
        onFocusSessionHandled={handleFocusSessionHandled}
        onSwapSession={handleSwapSession}
        onResumeSession={handleResumeSessionFromActivity}
        onAddFileToChat={handleAddFileToChat}
        isMobile={isMobile}
      />

      {/* Command Palette Modal */}
      <CommandPalette
        isOpen={showCommandPalette}
        onClose={() => setShowCommandPalette(false)}
        sessions={conversations.sessions}
        activeSessionId={conversations.activeSessionId}
        onSelectSession={conversations.onSelectSession}
        onDeleteSession={conversations.onDeleteSession}
        onRenameSession={conversations.onRenameSession}
        actions={paletteActions}
      />
    </div>
  );
}
