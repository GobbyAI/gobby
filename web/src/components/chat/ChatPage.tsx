import "./styles.css";
import { useCallback, useEffect, useRef, useState } from "react";
import { useIsMobile } from "../../hooks/useIsMobile";
import type {
  ChatState,
  ConversationState,
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
import { ActivityPanel, useActivityPanel } from "../activity/ActivityPanel";
import { VoiceStatusBar } from "./VoiceStatusBar";
import { useCanvasPanel } from "../canvas/hooks/useCanvasPanel";
import { useFileChanges } from "../../hooks/useFileChanges";

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
  const activeSession = conversations.sessions.find(
    (s) => s.external_id === conversations.activeSessionId,
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

  // Session browsing via activity panel instead of Observing mode
  const [focusSessionId, setFocusSessionId] = useState<string | null>(null);
  const handleViewCliSession = useCallback(
    (session: { id: string }) => {
      setFocusSessionId(session.id);
      activity.showTab("sessions");
    },
    [activity],
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

  const handleAttachWatchedSession = useCallback(
    (sessionId: string) => {
      const targetSession = conversations.sessions.find(
        (session) => session.id === sessionId,
      );
      if (!targetSession) return;
      handleUnwatch(sessionId);
      conversations.onSelectSession(targetSession);
    },
    [conversations, handleUnwatch],
  );

  // Wrap onNewChat to park the current session as Watching
  const handleNewChat = useCallback(
    (agentName?: string) => {
      const currentDbId = chat.dbSessionId;
      if (currentDbId && chat.messages.length > 0) {
        setWatchingSessionIds((prev) => new Set(prev).add(currentDbId));
        // Auto-select the parked session in the Sessions tab
        setFocusSessionId(currentDbId);
        activity.showTab("sessions");
      }
      conversations.onNewChat(agentName);
    },
    [chat.dbSessionId, chat.messages.length, conversations, activity],
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
      canvas.openCanvas(chat.canvasPanel);
      // Auto-switch to canvas tab
      activity.showTab("canvas");
    } else {
      canvas.closeCanvas();
    }
  }, [chat.canvasPanel, canvas.openCanvas, canvas.closeCanvas]);

  const planArtifactIdRef = useRef<string | null>(null);
  // Local plan-pending state — kept in ChatPage (same scope as artifact
  // creation) so React batches both state updates into a single render.
  // The prop from useChat (chat.planPendingApproval) passes through App.tsx
  // and may arrive in a separate render cycle, causing the ArtifactPanel
  // to render the plan content WITHOUT the approval bar.
  const [planPendingLocal, setPlanPendingLocal] = useState(false);

  // Clear artifacts and plan state on session switch / new chat
  useEffect(() => {
    clearArtifacts();
    planArtifactIdRef.current = null;
    setPlanPendingLocal(false);
  }, [chat.conversationSwitchKey]); // eslint-disable-line react-hooks/exhaustive-deps

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

      if (
        planArtifactIdRef.current &&
        artifacts.has(planArtifactIdRef.current)
      ) {
        updateArtifact(planArtifactIdRef.current, content);
        openArtifact(planArtifactIdRef.current);
      } else {
        const id = createArtifact("text", content, "markdown", title, {
          isPlan: true,
        });
        planArtifactIdRef.current = id;
      }
      setPlanPendingLocal(true);
      activity.showTab("plans");
    },
    [createArtifact, updateArtifact, openArtifact, artifacts, activity.showTab],
  );

  useEffect(() => {
    chat.setOnPlanReady?.(onPlanReady);
  }, [chat.setOnPlanReady, onPlanReady]);

  // Wire artifact events (show_file) to artifact panel
  const validArtifactTypes = new Set<string>([
    "code",
    "text",
    "image",
    "sheet",
  ]);
  const onArtifactEvent = useCallback(
    (type: string, content: string, language?: string, title?: string) => {
      if (validArtifactTypes.has(type)) {
        createArtifact(type as ArtifactType, content, language, title);
      }
    },
    [createArtifact, activity.showTab],
  );

  useEffect(() => {
    chat.setOnArtifactEvent?.(onArtifactEvent);
  }, [chat.setOnArtifactEvent, onArtifactEvent]);

  // Intercept toggle_panel palette action before forwarding to App.tsx
  const handlePaletteSelect = useCallback(
    (item: PaletteItem) => {
      if (item.kind === "command" && item.action === "toggle_panel") {
        activity.togglePanel();
        return;
      }
      chat.onPaletteSelect?.(item);
    },
    [activity, chat],
  );

  // Add file to chat from Files tab (right-click "Add to chat")
  const handleAddFileToChat = useCallback(
    (filePath: string) => {
      chat.onSend(`Read and reference this file: ${filePath}`);
    },
    [chat.onSend],
  );

  const handleApprovePlan = useCallback(() => {
    setPlanPendingLocal(false);
    chat.onApprovePlan?.();
    // Direct local mode update — bypasses the ref-based callback bridge
    // in useChat.approvePlan which can silently fail if the ref isn't wired.
    chat.onModeChangeLocal?.("accept_edits");
    activity.setIsPinned(false);
  }, [chat.onApprovePlan, chat.onModeChangeLocal, activity.setIsPinned]);

  const handleRequestPlanChanges = useCallback(
    (feedback: string) => {
      setPlanPendingLocal(false);
      chat.onRequestPlanChanges?.(feedback);
      activity.setIsPinned(false);
    },
    [chat.onRequestPlanChanges, activity.setIsPinned],
  );

  // Close artifact and auto-close activity panel if it was opened programmatically
  const handleCloseArtifact = useCallback(() => {
    closeArtifactPanel();
    activity.closeIfAutoOpened();
  }, [closeArtifactPanel, activity.closeIfAutoOpened]);

  // Expose callback for /plan command to reopen plan artifact
  useEffect(() => {
    if (showPlanRef) {
      showPlanRef.current = () => {
        if (planArtifactIdRef.current) {
          openArtifact(planArtifactIdRef.current);
          activity.showTab("plans");
        }
      };
    }
    return () => {
      if (showPlanRef) showPlanRef.current = null;
    };
  }, [showPlanRef, openArtifact, activity.showTab]);

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
        activity.togglePanel();
        return;
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [activity.togglePanel]);

  return (
    <div className="relative flex h-full overflow-hidden bg-background text-foreground">
      {/* Main chat column */}
      <div className="flex flex-col flex-1 min-w-[400px]">
        {/* Command Bar */}
        <CommandBar
          sessionRef={effectiveSessionRef}
          title={
            chat.viewingSessionMeta?.title ??
            chat.attachedSessionMeta?.title ??
            activeTitle
          }
          viewingMeta={chat.viewingSessionMeta ?? chat.attachedSessionMeta}
          isAttached={!!chat.attachedSessionId}
          onAttach={chat.onAttachToViewed}
          onDetach={chat.onDetachFromSession}
          onOpenPalette={() => setShowCommandPalette(true)}
          onOpenActiveSessions={() => setShowActiveSessions(true)}
          onNewChat={handleNewChat}
          onTogglePanel={activity.togglePanel}
          agents={conversations.agents ?? []}
          agentDefinitions={agentDefinitions}
          agentGlobalDefs={agentGlobalDefs}
          agentProjectDefs={agentProjectDefs}
          agentShowScopeToggle={agentShowScopeToggle}
          agentHasGlobal={agentHasGlobal}
          agentHasProject={agentHasProject}
          isPanelPinned={activity.isPinned}
        />
        {voice.voiceMode && (
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

          {/* Chat input */}
          <ChatInput
            onSend={chat.onSend}
            onStop={chat.onStop}
            isStreaming={chat.isStreaming}
            disabled={
              !chat.isConnected ||
              (!!chat.viewingSessionId && !chat.attachedSessionId)
            }
            viewingSession={!!chat.viewingSessionId && !chat.attachedSessionId}
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
            voiceMode={voice.voiceMode}
            voiceAvailable={voice.voiceAvailable}
            voiceReady={voice.voiceReady}
            voiceLoading={voice.voiceLoading}
            onToggleVoice={voice.onToggleVoice}
            isMobile={isMobile}
            onScrollToBottom={() => messageListRef.current?.scrollToBottom()}
            provider={chat.provider}
            availableProviders={availableProviders}
            currentModel={currentModel}
            onModelChange={onModelChange}
            onProviderChange={chat.onProviderChange}
            onSwitchProvider={chat.onSwitchProvider}
            hasMessages={chat.messages.length > 0}
          />
        </ArtifactContext.Provider>
      </div>

      {/* Activity Panel */}
      <ActivityPanel
        isPinned={activity.isPinned}
        onPinnedChange={activity.setIsPinned}
        panelWidth={activity.panelWidth}
        onWidthChange={activity.setPanelWidth}
        activeTab={activity.activeTab}
        onTabChange={activity.setActiveTab}
        artifacts={artifacts}
        activeArtifact={activeArtifact}
        onOpenArtifact={openArtifact}
        onCloseArtifact={handleCloseArtifact}
        onUpdateArtifactContent={updateArtifact}
        onSetArtifactVersion={setVersion}
        planPendingApproval={
          (planPendingLocal || chat.planPendingApproval) &&
          activeArtifact?.id === planArtifactIdRef.current &&
          activeArtifact?.type === "text"
        }
        onApprovePlan={handleApprovePlan}
        onRequestPlanChanges={handleRequestPlanChanges}
        canvasState={canvas.activeCanvas}
        onCloseCanvas={canvas.closeCanvas}
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
        onAttachSession={handleAttachWatchedSession}
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
