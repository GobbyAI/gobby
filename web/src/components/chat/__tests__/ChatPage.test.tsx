import { beforeEach, afterEach, describe, expect, it, vi } from "vitest";
import { render, waitFor } from "@testing-library/react";

import type { ChatState, ConversationState, VoiceProps } from "../../../types/chat";
import { ChatPage } from "../ChatPage";

const { clearArtifactsSpy, scrollToBottomSpy } = vi.hoisted(() => ({
  clearArtifactsSpy: vi.fn(),
  scrollToBottomSpy: vi.fn(),
}));

vi.mock("../MessageList", async () => {
  const React = await import("react");
  return {
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
  };
});

vi.mock("../ChatInput", () => ({
  ChatInput: () => <div data-testid="chat-input" />,
}));

vi.mock("../CommandBar", () => ({
  CommandBar: () => <div data-testid="command-bar" />,
}));

vi.mock("../CommandPalette", () => ({
  CommandPalette: () => null,
}));

vi.mock("../ActiveSessionsModal", () => ({
  ActiveSessionsModal: () => null,
}));

vi.mock("../../activity/ActivityPanel", () => ({
  ActivityPanel: () => null,
}));

vi.mock("../VoiceStatusBar", () => ({
  VoiceStatusBar: () => null,
}));

vi.mock("../AgentStatusBar", () => ({
  AgentStatusBar: () => null,
}));

vi.mock("../../../hooks/useIsMobile", () => ({
  useIsMobile: () => false,
}));

vi.mock("../../../hooks/useArtifacts", () => ({
  useArtifacts: () => ({
    artifacts: [],
    activeArtifact: null,
    createArtifact: vi.fn(),
    updateArtifact: vi.fn(),
    openArtifact: vi.fn(),
    closePanel: vi.fn(),
    clearArtifacts: clearArtifactsSpy,
    setVersion: vi.fn(),
  }),
}));

vi.mock("../../activity/useActivityPanel", () => ({
  useActivityPanel: () => ({
    activeTab: "artifacts",
    closeIfAutoOpened: vi.fn(),
    isPinned: false,
    panelWidth: 320,
    setActiveTab: vi.fn(),
    setIsPinned: vi.fn(),
    setPanelWidth: vi.fn(),
    showTab: vi.fn(),
  }),
}));

vi.mock("../../canvas/hooks/useCanvasPanel", () => ({
  useCanvasPanel: () => ({
    openCanvas: vi.fn(),
    closeCanvas: vi.fn(),
    activeCanvas: null,
  }),
}));

vi.mock("../../../hooks/useFileChanges", () => ({
  useFileChanges: () => ({
    changedFiles: [],
    fetchDiff: vi.fn(),
  }),
}));

vi.mock("../../../hooks/useConfirmDialog", () => ({
  useConfirmDialog: () => ({
    confirm: vi.fn(async () => true),
    ConfirmDialogElement: null,
  }),
}));

function createChat(overrides: Partial<ChatState> = {}): ChatState {
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
    ...overrides,
  } as ChatState;
}

function createConversations(): ConversationState {
  return {
    sessions: [],
    activeSessionId: null,
    onNewChat: vi.fn(),
    onSelectSession: vi.fn(),
    agents: [],
  };
}

function createVoice(): VoiceProps {
  return {
    sttEnabled: false,
    voiceInputMode: "ptt",
    isRecording: false,
    startRecording: vi.fn(async () => {}),
    stopRecording: vi.fn(async () => {}),
    cancelRecording: vi.fn(),
  };
}

describe("ChatPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        new Response(JSON.stringify({ providers: [] }), {
          headers: { "Content-Type": "application/json" },
        }),
      ),
    );
    vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) => {
      callback(0);
      return 1;
    });
    vi.stubGlobal("cancelAnimationFrame", vi.fn());
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("scrolls to the bottom once after a main chat load completes", async () => {
    const message = {
      id: "msg-1",
      role: "assistant" as const,
      content: "Loaded message",
      timestamp: new Date("2026-04-13T12:00:00Z"),
    };

    const { rerender } = render(
      <ChatPage
        chat={createChat({
          isLoadingMessages: true,
          messages: [message],
        })}
        conversations={createConversations()}
        voice={createVoice()}
      />,
    );

    expect(scrollToBottomSpy).not.toHaveBeenCalled();

    rerender(
      <ChatPage
        chat={createChat({
          isLoadingMessages: false,
          messages: [message],
        })}
        conversations={createConversations()}
        voice={createVoice()}
      />,
    );

    await waitFor(() => {
      expect(scrollToBottomSpy).toHaveBeenCalledTimes(1);
    });

    rerender(
      <ChatPage
        chat={createChat({
          isLoadingMessages: false,
          messages: [message],
        })}
        conversations={createConversations()}
        voice={createVoice()}
      />,
    );

    expect(scrollToBottomSpy).toHaveBeenCalledTimes(1);
  });
});
