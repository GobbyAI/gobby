import "./styles.css";
import { useCallback, useEffect, useRef, useState } from "react";
import { useIsMobile } from "../../hooks/useIsMobile";
import type {
  ChatState,
  ConversationState,
  SwappedSessionTarget,
  VoiceProps,
} from "../../types/chat";
import type { AgentDefInfo } from "../../hooks/useAgentDefinitions";
import type { PaletteItem } from "../../hooks/useColonAutocomplete";
import type { ArtifactType } from "../../types/artifacts";
import { useArtifacts } from "../../hooks/useArtifacts";
import { ArtifactContext } from "./artifacts/ArtifactContext";
import { MessageList, type MessageListHandle } from "./MessageList";
import { ChatInput } from "./ChatInput";
import { CommandBar } from "./CommandBar";
import { CommandPalette, type CommandPaletteAction } from "./CommandPalette";
import { ActiveSessionsModal } from "./ActiveSessionsModal";
import { ActivityPanel } from "../activity/ActivityPanel";
import { useActivityPanel } from "../activity/useActivityPanel";
import { VoiceStatusBar } from "./VoiceStatusBar";
import { AgentStatusBar } from "./AgentStatusBar";
import { useCanvasPanel } from "../canvas/hooks/useCanvasPanel";
import { useFileChanges } from "../../hooks/useFileChanges";
import { useConfirmDialog } from "../../hooks/useConfirmDialog";

const VALID_ARTIFACT_TYPES = new Set<string>(["code", "text", "image", "sheet"]);

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
  // Command palette actions from App.tsx
  paletteActions?: CommandPaletteAction[];
  // Active sessions modal
  onViewAgent?: (agent: {
    run_id: string;
    session_id?: string;
    mode?: string;
  }) => void;
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
  paletteActions = [],
  onViewAgent,
}: ChatPageProps) {
  const messageListRef = useRef<MessageListHandle>(null);
  const lastAutoScrolledLoadRef = useRef<string | null>(null);
  const activeSession = conversations.sessions.find(
    (s) => s.id === conversations.activeSessionId,
  );
  const activeTitle = activeSession?.title ?? null;
  const effectiveSessionRef =
    chat.sessionRef ??
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
  const {
    openCanvas,
    closeCanvas,
    activeCanvas,
  } = canvas;
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
    onModeChangeLocal,
    onPaletteSelect,
    onRequestPlanChanges,
    onSend,
    planPendingApproval,
    setOnArtifactEvent,
    setOnPlanReady,
  } = chat;

  // Session browsing via activity panel instead of Observing mode
  const [focusSessionId, setFocusSessionId] = useState<string | null>(null);
  const handleViewCliSession = useCallback(
    (session: { id: string }) => {
      setFocusSessionId(session.id);
      showTab("sessions");
    },
    [showTab],
  );
  const handleFocusSessionHandled = useCallback(() => {
    setFocusSessionId(null);
  }, []);

  // Watching: parked web chats stay selected in Sessions tab after starting a new chat
  const [watchingSessionIds, setWatchingSessionIds] = useState<Set<string>>(
    new Set(),
  );
  const handleUnwatch = useCallback((sessionId: string) => {
    setWatchingSessionIds((prev) => {
      const next = new Set(prev);
      next.delete(sessionId);
      return next;
    });
  }, []);

  const parkCurrentSession = useCallback(
    (nextSessionId?: string) => {
      const currentSessionId = chat.viewingSessionId ?? chat.dbSessionId;
      if (
        !currentSessionId ||
        chat.messages.length === 0 ||
        currentSessionId === nextSessionId
      ) {
        return;
      }
      setWatchingSessionIds((prev) => new Set(prev).add(currentSessionId));
      setFocusSessionId(currentSessionId);
      showTab("sessions");
    },
    [chat.dbSessionId, chat.messages.length, chat.viewingSessionId, showTab],
  );

  const handleSwapSession = useCallback(
    (target: SwappedSessionTarget) => {
      parkCurrentSession(target.sessionId);
      handleUnwatch(target.sessionId);

      if (target.sessionType === "web_chat") {
        const targetSession = conversations.sessions.find(
          (session) => session.id === target.sessionId,
        );
        if (targetSession) {
          conversations.onSelectSession(targetSession);
        }
        return;
      }

      if (!target.agentRunId && chat.continueSessionInChat) {
        void chat.continueSessionInChat(target.sessionId, projectId ?? undefined);
        return;
      }

      chat.viewSession?.(target.sessionId);
      chat.observeSession?.(target.sessionId, "observe");
    },
    [chat, conversations, handleUnwatch, parkCurrentSession, projectId],
  );

  const handleAutonomousDetach = useCallback(() => {
    if (chat.sessionInteractionMode === "proxy") {
      chat.onDetachFromSession?.();
      return;
    }
    if (chat.viewingSessionMeta?.sessionType === "terminal") {
      chat.clearViewingSession?.();
    }
  }, [chat]);

  const viewingMeta = chat.viewingSessionMeta ?? chat.attachedSessionMeta ?? null;
  const isSwappedTerminal = viewingMeta?.sessionType === "terminal";
  const isAutonomousSession = Boolean(
    isSwappedTerminal && viewingMeta?.agentRunId,
  );
  const showObserveOverlay =
    isAutonomousSession && chat.sessionInteractionMode !== "proxy";
  const canAttachViewedSession =
    viewingMeta?.sessionType === "terminal" && !isAutonomousSession;
  const providerPickerDisabledReason = isAutonomousSession
    ? chat.sessionInteractionMode === "proxy"
      ? "Cannot change provider on a pipeline-managed session"
      : "Observing autonomous session"
    : null;
  const effectiveInputProvider = isSwappedTerminal
    ? viewingMeta?.source ?? chat.provider
    : chat.provider;
  const effectiveInputModel = isSwappedTerminal
    ? viewingMeta?.model ?? currentModel
    : currentModel;

  const handleSwappedSessionProviderSelection = useCallback(
    async (provider: string, model: string) => {
      if (
        !isSwappedTerminal ||
        isAutonomousSession ||
        !chat.viewingSessionId ||
        !chat.continueSessionInChat
      ) {
        return;
      }

      const confirmChange =
        viewingMeta?.status === "active"
          ? await confirm({
              title: "Change provider?",
              description:
                `This will end the terminal session and resume the conversation with ${provider} ${model}.`,
              confirmLabel: "Change Provider",
              destructive: true,
            })
          : true;
      if (!confirmChange) return;

      chat.onProviderChange?.(provider);
      onModelChange?.(model);
      await chat.continueSessionInChat(chat.viewingSessionId, projectId ?? undefined, {
        provider,
        model,
      });
    },
    [
      chat,
      confirm,
      isAutonomousSession,
      isSwappedTerminal,
      onModelChange,
      projectId,
      viewingMeta?.status,
    ],
  );

  // Wrap onNewChat to park the current session as Watching
  const handleNewChat = useCallback(
    (agentName?: string) => {
      parkCurrentSession();
      conversations.onNewChat(agentName);
    },
    [conversations, parkCurrentSession],
  );

  // Available LLM providers — fetched from daemon API
  const [availableProviders, setAvailableProviders] = useState<string[]>([]);
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
      .catch(() => setAvailableProviders(["claude"]));
  }, []);

  // Modals
  const [showCommandPalette, setShowCommandPalette] = useState(false);
  const [showActiveSessions, setShowActiveSessions] = useState(false);

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

  // Clear artifacts and plan state on session switch / new chat
  useEffect(() => {
    clearArtifacts();
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
    },
    [createArtifact],
  );

  const openFileAsArtifact = useCallback(
    (type: ArtifactType, language: string, content: string, title?: string) => {
      createArtifact(type, content, language, title);
    },
    [createArtifact],
  );

  // Wire plan content to artifact panel when plan_pending_approval arrives.
  // If a plan artifact already exists (revision after rejection), add a new
  // version instead of creating a duplicate entry.
  const onPlanReady = useCallback(
    (content: string | null) => {
      if (!content) return;
      const headingMatch = content.match(/^#\s+(.+)$/m);
      const title = headingMatch?.[1]?.trim() || "Implementation Plan";
      let nextArtifactId = planArtifactId;

      if (planArtifactId && artifacts.has(planArtifactId)) {
        updateArtifact(planArtifactId, content);
        openArtifact(planArtifactId);
      } else {
        nextArtifactId = createArtifact("text", content, "markdown", title, {
          isPlan: true,
        });
      }
      setPlanArtifactId(nextArtifactId);
      setPendingPlanArtifactId(nextArtifactId);
      showTab("plans");
    },
    [artifacts, createArtifact, openArtifact, planArtifactId, showTab, updateArtifact],
  );

  useEffect(() => {
    setOnPlanReady?.(onPlanReady);
  }, [onPlanReady, setOnPlanReady]);

  // Wire artifact events (show_file) to artifact panel
  const onArtifactEvent = useCallback(
    (type: string, content: string, language?: string, title?: string) => {
      if (VALID_ARTIFACT_TYPES.has(type)) {
        createArtifact(type as ArtifactType, content, language, title);
      }
    },
    [createArtifact],
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
  const handleAddFileToChat = useCallback(
    (filePath: string) => {
      onSend?.(`Read and reference this file: ${filePath}`);
    },
    [onSend],
  );

  const handleApprovePlan = useCallback(() => {
    setPendingPlanArtifactId(null);
    onApprovePlan?.();
    // Direct local mode update — bypasses the ref-based callback bridge
    // in useChat.approvePlan which can silently fail if the ref isn't wired.
    onModeChangeLocal?.("accept_edits");
    setIsPinned(false);
  }, [onApprovePlan, onModeChangeLocal, setIsPinned]);

  const handleRequestPlanChanges = useCallback(
    (feedback: string) => {
      setPendingPlanArtifactId(null);
      onRequestPlanChanges?.(feedback);
      setIsPinned(false);
    },
    [onRequestPlanChanges, setIsPinned],
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
      // Cmd+Shift+A — Active Sessions
      if ((e.metaKey || e.ctrlKey) && e.shiftKey && e.key === "A") {
        e.preventDefault();
        setShowActiveSessions(true);
        return;
      }
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

  return (
    <div className="relative flex h-full overflow-hidden bg-background text-foreground">
      {ConfirmDialogElement}
      {/* Main chat column */}
      <div className="flex flex-col flex-1 min-w-[400px]">
        {/* Command Bar */}
        <CommandBar
          sessionRef={effectiveSessionRef}
          title={
            viewingMeta?.title ??
            activeTitle
          }
          onOpenPalette={() => setShowCommandPalette(true)}
          onOpenActiveSessions={() => setShowActiveSessions(true)}
          onNewChat={handleNewChat}
          onTogglePanel={togglePanel}
          agents={conversations.agents ?? []}
          agentDefinitions={agentDefinitions}
          agentGlobalDefs={agentGlobalDefs}
          agentProjectDefs={agentProjectDefs}
          agentShowScopeToggle={agentShowScopeToggle}
          agentHasGlobal={agentHasGlobal}
          agentHasProject={agentHasProject}
          isPanelPinned={isPinned}
        />
        {voice.sttEnabled &&
          voice.voiceInputMode === "vad" &&
          (voice.isListening || voice.isTranscribing || voice.voiceError) && (
          <VoiceStatusBar
            isListening={voice.isListening ?? false}
            isSpeechDetected={voice.isSpeechDetected ?? false}
            isTranscribing={voice.isTranscribing ?? false}
            voiceError={voice.voiceError}
          />
        )}

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

          {viewingMeta && (
            <AgentStatusBar
              viewingMeta={viewingMeta}
              interactionMode={chat.sessionInteractionMode ?? "none"}
              isAttached={!!chat.attachedSessionId}
              isAutonomousSession={isAutonomousSession}
              onAttach={canAttachViewedSession ? chat.onAttachToViewed : undefined}
              onDetach={
                isAutonomousSession
                  ? handleAutonomousDetach
                  : chat.attachedSessionId
                    ? chat.onDetachFromSession
                    : chat.clearViewingSession
              }
            />
          )}

          {/* Chat input */}
          <ChatInput
            onSend={onSend}
            onStop={chat.onStop}
            isStreaming={chat.isStreaming}
            disabled={
              !chat.isConnected ||
              (isSwappedTerminal && chat.sessionInteractionMode !== "proxy")
            }
            viewingSession={showObserveOverlay}
            onInputChange={chat.onInputChange}
            paletteItems={chat.paletteItems}
            onPaletteSelect={handlePaletteSelect}
            mode={chat.mode}
            onModeChange={chat.onModeChange}
            contextUsage={chat.contextUsage}
            currentBranch={chat.currentBranch}
            worktreePath={chat.worktreePath}
            projectId={projectId ?? null}
            onWorktreeChange={chat.onWorktreeChange}
            agentName={chat.activeAgent}
            onAgentChange={chat.onAgentChange}
            agentDefinitions={agentDefinitions}
            agentGlobalDefs={agentGlobalDefs}
            agentProjectDefs={agentProjectDefs}
            agentShowScopeToggle={agentShowScopeToggle}
            agentHasGlobal={agentHasGlobal}
            agentHasProject={agentHasProject}
            sttEnabled={voice.sttEnabled}
            voiceInputMode={voice.voiceInputMode}
            isRecording={voice.isRecording}
            startRecording={voice.startRecording}
            stopRecording={voice.stopRecording}
            cancelRecording={voice.cancelRecording}
            isMobile={isMobile}
            onScrollToBottom={() => messageListRef.current?.scrollToBottom()}
            provider={effectiveInputProvider}
            availableProviders={availableProviders}
            currentModel={effectiveInputModel}
            onModelChange={onModelChange}
            onProviderChange={chat.onProviderChange}
            onSwitchProvider={chat.onSwitchProvider}
            onProviderSelectionChange={
              isSwappedTerminal ? handleSwappedSessionProviderSelection : undefined
            }
            providerPickerDisabledReason={providerPickerDisabledReason}
            hasMessages={chat.messages.length > 0}
            proxySlashMode={isSwappedTerminal && chat.sessionInteractionMode === "proxy"}
            showObserveOverlay={showObserveOverlay}
            onAttachObservedSession={chat.onAttachToViewed}
            proxyDeliveryNotice={chat.proxyDeliveryNotice}
          />
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
          (pendingPlanArtifactId === activeArtifact?.id || planPendingApproval) &&
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
        onKillAgent={conversations.onKillAgent}
        onExpireSession={conversations.onExpireSession}
        chatSessionId={chat.dbSessionId}
        focusSessionId={focusSessionId}
        onFocusSessionHandled={handleFocusSessionHandled}
        watchingSessionIds={watchingSessionIds}
        onUnwatchSession={handleUnwatch}
        onSwapSession={handleSwapSession}
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

      {/* Active Sessions Modal */}
      <ActiveSessionsModal
        isOpen={showActiveSessions}
        onClose={() => setShowActiveSessions(false)}
        agents={conversations.agents ?? []}
        cliSessions={conversations.cliSessions}
        onViewAgent={(agent) => {
          onViewAgent?.(agent);
          setShowActiveSessions(false);
        }}
        onKillAgent={conversations.onKillAgent}
        onViewCliSession={handleViewCliSession}
      />
    </div>
  );
}
