import * as React from "react";
import { vi } from "vitest";

import type {
  ChatState,
  ConversationState,
  VoiceProps,
} from "../../../types/chat";

export const DATA_URI = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUg==";

export const isMobileState = { value: false };
export const isPinnedState = { value: false };

export const clearArtifactsSpy = vi.fn();
export const createArtifactSpy = vi.fn();
export const scrollToBottomSpy = vi.fn();
export const setIsPinnedSpy = vi.fn();
export const showTabSpy = vi.fn();
export const togglePanelSpy = vi.fn();

export const messageListMockFactory = () => ({
  MessageList: React.forwardRef((_props: unknown, ref) => {
    React.useImperativeHandle(
      ref,
      () => ({
        scrollToBottom: scrollToBottomSpy,
      }),
      [],
    );
    return <div data-testid="message-list" />;
  }),
});

export const chatInputMockFactory = () => ({
  ChatInput: ({
    proxyDeliveryNotice,
    disabled,
    disabledPlaceholder,
    disabledAriaLabel,
    modeDisabled,
    attachmentsDisabled,
    agentPickerDisabled,
    worktreePickerDisabled,
    provider,
    currentModel,
    currentReasoning,
    providerPickerDisabledReason,
    onNewChat,
  }: {
    proxyDeliveryNotice?: string | null;
    disabled?: boolean;
    disabledPlaceholder?: string;
    disabledAriaLabel?: string;
    modeDisabled?: boolean;
    attachmentsDisabled?: boolean;
    agentPickerDisabled?: boolean;
    worktreePickerDisabled?: boolean;
    provider?: string | null;
    currentModel?: string;
    currentReasoning?: string;
    providerPickerDisabledReason?: string | null;
    onNewChat?: () => void;
  }) => (
    <div data-testid="chat-input">
      <span data-testid="chat-input-disabled">{String(Boolean(disabled))}</span>
      <span data-testid="chat-input-placeholder">
        {disabledPlaceholder ?? ""}
      </span>
      <span data-testid="chat-input-aria-label">{disabledAriaLabel ?? ""}</span>
      <span data-testid="chat-input-notice">{proxyDeliveryNotice ?? ""}</span>
      <span data-testid="chat-input-mode-disabled">
        {String(Boolean(modeDisabled))}
      </span>
      <span data-testid="chat-input-attachments-disabled">
        {String(Boolean(attachmentsDisabled))}
      </span>
      <span data-testid="chat-input-agent-disabled">
        {String(Boolean(agentPickerDisabled))}
      </span>
      <span data-testid="chat-input-worktree-disabled">
        {String(Boolean(worktreePickerDisabled))}
      </span>
      <span data-testid="chat-input-provider">{provider ?? ""}</span>
      <span data-testid="chat-input-model">{currentModel ?? ""}</span>
      <span data-testid="chat-input-reasoning">{currentReasoning ?? ""}</span>
      <span data-testid="chat-input-provider-disabled-reason">
        {providerPickerDisabledReason ?? ""}
      </span>
      {onNewChat && (
        <button
          type="button"
          data-testid="new-chat-button"
          onClick={() => onNewChat()}
        >
          New Chat
        </button>
      )}
    </div>
  ),
});

export const commandBarMockFactory = () => ({
  CommandBar: ({ onTogglePanel }: { onTogglePanel?: () => void }) => (
    <div data-testid="command-bar">
      {onTogglePanel && (
        <button
          type="button"
          data-testid="command-bar-panel-toggle"
          onClick={onTogglePanel}
        >
          Toggle Panel
        </button>
      )}
    </div>
  ),
});

export const commandPaletteMockFactory = () => ({
  CommandPalette: () => null,
});

export const activityPanelMockFactory = () => ({
  ActivityPanel: ({
    sessions,
    onSwapSession,
    onResumeSession,
    chatSessionId,
    focusSessionId,
    onApprovePlan,
    onRequestPlanChanges,
    onAddFileToChat,
  }: {
    sessions?: Array<{ id: string }>;
    onSwapSession?: (target: {
      sessionId: string;
      sessionType: "terminal" | "web_chat" | null;
      agentRunId: string | null;
    }) => void;
    onResumeSession?: (sessionId: string) => void;
    chatSessionId?: string | null;
    focusSessionId?: string | null;
    onApprovePlan?: () => void;
    onRequestPlanChanges?: (feedback: string) => void;
    onAddFileToChat?: (filePath: string) => void;
  }) => (
    <div data-testid="activity-panel">
      <span data-testid="activity-panel-session-count">
        {sessions?.length ?? 0}
      </span>
      <span data-testid="activity-panel-chat-session-id">
        {chatSessionId ?? ""}
      </span>
      <span data-testid="activity-panel-focus-session-id">
        {focusSessionId ?? ""}
      </span>
      <button
        type="button"
        data-testid="swap-terminal-session"
        onClick={() =>
          onSwapSession?.({
            sessionId: "terminal-2",
            sessionType: "terminal",
            agentRunId: null,
          })
        }
      >
        Swap Terminal
      </button>
      <button
        type="button"
        data-testid="swap-autonomous-session"
        onClick={() =>
          onSwapSession?.({
            sessionId: "terminal-auto",
            sessionType: "terminal",
            agentRunId: "run-auto",
          })
        }
      >
        Swap Autonomous
      </button>
      <button
        type="button"
        data-testid="swap-web-chat-session"
        onClick={() =>
          onSwapSession?.({
            sessionId: "web-chat-2",
            sessionType: "web_chat",
            agentRunId: null,
          })
        }
      >
        Swap Web Chat
      </button>
      <button
        type="button"
        data-testid="resume-activity-session"
        onClick={() => onResumeSession?.("resume-target")}
      >
        Resume Session
      </button>
      <button
        type="button"
        data-testid="approve-plan"
        onClick={() => onApprovePlan?.()}
      >
        Approve Plan
      </button>
      <button
        type="button"
        data-testid="request-plan-changes"
        onClick={() => onRequestPlanChanges?.("Needs changes")}
      >
        Request Changes
      </button>
      <button
        type="button"
        data-testid="attach-file-to-chat"
        onClick={() => onAddFileToChat?.("/tmp/context.md")}
      >
        Attach File
      </button>
    </div>
  ),
});

export const voiceStatusBarMockFactory = () => ({
  VoiceStatusBar: ({
    voiceLoading,
    isListening,
    isTranscribing,
    voiceError,
  }: {
    voiceLoading?: boolean;
    isListening?: boolean;
    isTranscribing?: boolean;
    voiceError?: string | null;
  }) => (
    <div
      data-testid="voice-status-bar"
      data-loading={String(Boolean(voiceLoading))}
      data-listening={String(Boolean(isListening))}
      data-transcribing={String(Boolean(isTranscribing))}
    >
      {voiceLoading
        ? "Warming voice..."
        : isTranscribing
          ? "Transcribing..."
          : isListening
            ? "Listening..."
            : voiceError || ""}
    </div>
  ),
});

export const agentStatusBarMockFactory = () => ({
  AgentStatusBar: ({
    isAttached,
    onAttach,
    onResume,
    onDetach,
    onNewChat,
  }: {
    isAttached?: boolean;
    onAttach?: () => void;
    onResume?: () => void;
    onDetach?: () => void;
    onNewChat?: () => void;
  }) => (
    <div data-testid="agent-status-bar">
      <span data-testid="agent-status-attached">
        {String(Boolean(isAttached))}
      </span>
      {onAttach && (
        <button
          type="button"
          data-testid="agent-status-attach"
          onClick={onAttach}
        >
          Attach
        </button>
      )}
      {onResume && (
        <button
          type="button"
          data-testid="agent-status-resume"
          onClick={onResume}
        >
          Resume
        </button>
      )}
      {isAttached && onDetach && (
        <button
          type="button"
          data-testid="agent-status-detach"
          onClick={onDetach}
        >
          Detach
        </button>
      )}
      {onNewChat && (
        <button
          type="button"
          data-testid="new-chat-button"
          onClick={onNewChat}
        >
          New Chat
        </button>
      )}
    </div>
  ),
});

export const useIsMobileMockFactory = () => ({
  useIsMobile: () => isMobileState.value,
});

export const useArtifactsMockFactory = () => ({
  useArtifacts: () => ({
    artifacts: new Map(),
    activeArtifact: null,
    createArtifact: createArtifactSpy,
    updateArtifact: vi.fn(),
    openArtifact: vi.fn(),
    closePanel: vi.fn(),
    clearArtifacts: clearArtifactsSpy,
    setVersion: vi.fn(),
  }),
});

export const useActivityPanelMockFactory = () => ({
  useActivityPanel: () => ({
    activeTab: "artifacts",
    closeIfAutoOpened: vi.fn(),
    isPinned: isPinnedState.value,
    panelWidth: 320,
    setActiveTab: vi.fn(),
    setIsPinned: setIsPinnedSpy,
    setPanelWidth: vi.fn(),
    showTab: showTabSpy,
    togglePanel: togglePanelSpy,
  }),
});

export const useCanvasPanelMockFactory = () => ({
  useCanvasPanel: () => ({
    openCanvas: vi.fn(),
    closeCanvas: vi.fn(),
    activeCanvas: null,
  }),
});

export const useFileChangesMockFactory = () => ({
  useFileChanges: () => ({
    changedFiles: [],
    fetchDiff: vi.fn(),
  }),
});

export const useConfirmDialogMockFactory = () => ({
  useConfirmDialog: () => ({
    confirm: vi.fn(async () => true),
    ConfirmDialogElement: null,
  }),
});

export function createChat(overrides: Partial<ChatState> = {}): ChatState {
  return {
    messages: [],
    sessionRef: null,
    currentBranch: null,
    worktreePath: null,
    isStreaming: false,
    isThinking: false,
    isLoadingMessages: false,
    isConnected: true,
    isReconnecting: false,
    contextUsage: undefined,
    onSend: vi.fn(),
    onStop: vi.fn(),
    onRespondToQuestion: vi.fn(),
    onRespondToApproval: vi.fn(),
    paletteItems: [],
    onPaletteSelect: vi.fn(),
    canvasSurfaces: new Map(),
    canvasPanel: null,
    onCanvasInteraction: vi.fn(),
    mode: "plan",
    onModeChange: vi.fn(),
    planPendingApproval: false,
    onApprovePlan: vi.fn(),
    onRequestPlanChanges: vi.fn(),
    provider: "claude",
    dbSessionId: "db-session-1",
    conversationSwitchKey: 1,
    continueSessionInChat: vi.fn(async () => "continued-session"),
    viewSession: vi.fn(),
    observeSession: vi.fn(),
    clearViewingSession: vi.fn(),
    onAttachToViewed: vi.fn(),
    attachedSessionId: null,
    sessionInteractionMode: "none",
    proxyDeliveryNotice: null,
    ...overrides,
  } as ChatState;
}

export function createConversations(): ConversationState {
  return {
    sessions: [],
    activeSessionId: null,
    onNewChat: vi.fn(),
    onSelectSession: vi.fn(),
  };
}

export function createVoice(overrides: Partial<VoiceProps> = {}): VoiceProps {
  return {
    sttEnabled: false,
    ttsEnabled: false,
    voiceInputMode: "ptt",
    voiceAvailable: false,
    voiceReady: false,
    voiceLoading: false,
    isListening: false,
    isRecording: false,
    isTranscribing: false,
    voiceError: null,
    startRecording: vi.fn(async () => {}),
    stopRecording: vi.fn(async () => {}),
    cancelRecording: vi.fn(),
    ...overrides,
  };
}

export function setupChatPageEnvironment(): void {
  vi.clearAllMocks();
  isMobileState.value = false;
  isPinnedState.value = false;
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/providers/models")) {
        return new Response(
          JSON.stringify({
            providers: [
              {
                provider: "claude",
                available: true,
                models: [
                  { value: "sonnet", label: "Sonnet", is_default: true },
                ],
                source: "static",
              },
              {
                provider: "codex",
                available: true,
                models: [
                  { value: "gpt-5.4", label: "gpt-5.4", is_default: true },
                ],
                source: "static",
              },
            ],
          }),
          { headers: { "Content-Type": "application/json" } },
        );
      }
      return new Response(JSON.stringify({ providers: [] }), {
        headers: { "Content-Type": "application/json" },
      });
    }),
  );
  vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) => {
    callback(0);
    return 1;
  });
  vi.stubGlobal("cancelAnimationFrame", vi.fn());
}

export function teardownChatPageEnvironment(): void {
  vi.unstubAllGlobals();
}
