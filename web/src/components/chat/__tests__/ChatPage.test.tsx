import { beforeEach, afterEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";

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
  ChatInput: ({
    proxyDeliveryNotice,
    disabled,
    disabledPlaceholder,
    disabledAriaLabel,
    provider,
    currentModel,
  }: {
    proxyDeliveryNotice?: string | null;
    disabled?: boolean;
    disabledPlaceholder?: string;
    disabledAriaLabel?: string;
    provider?: string | null;
    currentModel?: string;
  }) => (
    <div data-testid="chat-input">
      <span data-testid="chat-input-disabled">{String(Boolean(disabled))}</span>
      <span data-testid="chat-input-placeholder">{disabledPlaceholder ?? ""}</span>
      <span data-testid="chat-input-aria-label">{disabledAriaLabel ?? ""}</span>
      <span data-testid="chat-input-notice">{proxyDeliveryNotice ?? ""}</span>
      <span data-testid="chat-input-provider">{provider ?? ""}</span>
      <span data-testid="chat-input-model">{currentModel ?? ""}</span>
    </div>
  ),
}));

vi.mock("../CommandBar", () => ({
  CommandBar: ({ onNewChat }: { onNewChat: () => void }) => (
    <div data-testid="command-bar">
      <button type="button" data-testid="new-chat-button" onClick={onNewChat}>
        New Chat
      </button>
    </div>
  ),
}));

vi.mock("../CommandPalette", () => ({
  CommandPalette: () => null,
}));

vi.mock("../ActiveSessionsModal", () => ({
  ActiveSessionsModal: () => null,
}));

vi.mock("../../activity/ActivityPanel", () => ({
  ActivityPanel: ({
    onSwapSession,
    chatSessionId,
  }: {
    onSwapSession?: (target: { sessionId: string; sessionType: "terminal" | "web_chat" | null; agentRunId: string | null }) => void
    chatSessionId?: string | null
  }) => (
    <div data-testid="activity-panel">
      <span data-testid="activity-panel-chat-session-id">
        {chatSessionId ?? ""}
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
    </div>
  ),
}));

vi.mock("../VoiceStatusBar", () => ({
  VoiceStatusBar: () => null,
}));

vi.mock("../AgentStatusBar", () => ({
  AgentStatusBar: ({
    viewingMeta,
    isAttached,
    onAttach,
    onResume,
    onDetach,
  }: {
    viewingMeta: { title?: string | null; source: string };
    isAttached?: boolean;
    onAttach?: () => void;
    onResume?: () => void;
    onDetach?: () => void;
  }) => (
    <div data-testid="agent-status-bar">
      <span>{viewingMeta.title ?? viewingMeta.source}</span>
      <span data-testid="agent-status-attached">{String(Boolean(isAttached))}</span>
      {onAttach && (
        <button type="button" data-testid="agent-status-attach" onClick={onAttach}>
          Attach
        </button>
      )}
      {onResume && (
        <button type="button" data-testid="agent-status-resume" onClick={onResume}>
          Resume
        </button>
      )}
      {isAttached && onDetach && (
        <button type="button" data-testid="agent-status-detach" onClick={onDetach}>
          Detach
        </button>
      )}
    </div>
  ),
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

function createConversations(): ConversationState {
  return {
    sessions: [],
    activeSessionId: null,
    onNewChat: vi.fn(),
    onSelectSession: vi.fn(),
    agents: [],
    onNavigateToAgent: vi.fn(),
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
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/api/providers/models")) {
          return new Response(
            JSON.stringify({
              providers: [
                {
                  provider: "claude",
                  available: true,
                  models: [{ value: "sonnet", label: "Sonnet", is_default: true }],
                  source: "static",
                },
                {
                  provider: "codex",
                  available: true,
                  models: [{ value: "gpt-5.4", label: "gpt-5.4", is_default: true }],
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

  it("renders the viewed-session status strip above the chat input for attached terminal sessions", async () => {
    render(
      <ChatPage
        chat={createChat({
          viewingSessionId: "terminal-1",
          attachedSessionId: "terminal-1",
          viewingSessionMeta: {
            ref: "#51",
            source: "claude",
            title: "Observed Terminal",
            status: "active",
            model: "sonnet",
            externalId: "term-51",
            sessionType: "terminal",
          },
          sessionInteractionMode: "proxy",
        })}
        conversations={createConversations()}
        voice={createVoice()}
      />,
    );

    await waitFor(() => {
      expect(screen.getByTestId("agent-status-bar")).toHaveTextContent(
        "Observed Terminal",
      );
      expect(screen.getByTestId("chat-input")).toBeInTheDocument();
      expect(screen.getByTestId("agent-status-attached")).toHaveTextContent(
        "true",
      );
    });

    const statusBar = screen.getByTestId("agent-status-bar");
    const messageList = screen.getByTestId("message-list");
    const chatInput = screen.getByTestId("chat-input");
    expect(
      messageList.compareDocumentPosition(statusBar) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(
      statusBar.compareDocumentPosition(chatInput) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });

  it("treats a read-only swapped terminal as the main session for the activity panel", async () => {
    await act(async () => {
      render(
        <ChatPage
          chat={createChat({
            dbSessionId: "web-main-1",
            viewingSessionId: "terminal-2",
            viewingSessionMeta: {
              ref: "#52",
              source: "claude",
              title: "Observed Terminal",
              status: "active",
              model: "claude-sonnet-4-6",
              externalId: "term-52",
              sessionType: "terminal",
            },
            sessionInteractionMode: "observe",
          })}
          conversations={createConversations()}
          voice={createVoice()}
        />,
      );
    });

    expect(
      screen.getByTestId("activity-panel-chat-session-id"),
    ).toHaveTextContent("terminal-2");
  });

  it("hides the entire chat input pane while watching a swapped terminal", async () => {
    await act(async () => {
      render(
        <ChatPage
          chat={createChat({
            viewingSessionId: "terminal-2",
            viewingSessionMeta: {
              ref: "#52",
              source: "claude",
              title: "Observed Terminal",
              status: "active",
              model: "claude-sonnet-4-6",
              externalId: "term-52",
              sessionType: "terminal",
            },
            sessionInteractionMode: "observe",
          })}
          conversations={createConversations()}
          voice={createVoice()}
        />,
      );
    });

    expect(screen.queryByTestId("chat-input")).not.toBeInTheDocument();
    expect(screen.getByTestId("agent-status-attach")).toBeInTheDocument();
    expect(screen.getByTestId("agent-status-resume")).toBeInTheDocument();
  });

  it("routes Attach to the viewed terminal attach handler", async () => {
    const onAttachToViewed = vi.fn();
    const continueSessionInChat = vi.fn(async () => "continued-session");

    await act(async () => {
      render(
        <ChatPage
          chat={createChat({
            viewingSessionId: "terminal-2",
            viewingSessionMeta: {
              ref: "#52",
              source: "claude",
              title: "Observed Terminal",
              status: "active",
              model: "claude-sonnet-4-6",
              externalId: "term-52",
              sessionType: "terminal",
            },
            sessionInteractionMode: "observe",
            onAttachToViewed,
            continueSessionInChat,
          })}
          conversations={createConversations()}
          voice={createVoice()}
          projectId="proj-1"
        />,
      );
    });

    fireEvent.click(screen.getByTestId("agent-status-attach"));

    expect(onAttachToViewed).toHaveBeenCalledTimes(1);
    expect(continueSessionInChat).not.toHaveBeenCalled();
  });

  it("routes Resume to the viewed terminal continuation flow", async () => {
    const continueSessionInChat = vi.fn(async () => "continued-session");

    await act(async () => {
      render(
        <ChatPage
          chat={createChat({
            viewingSessionId: "terminal-2",
            viewingSessionMeta: {
              ref: "#52",
              source: "claude",
              title: "Observed Terminal",
              status: "active",
              model: "claude-sonnet-4-6",
              externalId: "term-52",
              sessionType: "terminal",
            },
            sessionInteractionMode: "observe",
            continueSessionInChat,
          })}
          conversations={createConversations()}
          voice={createVoice()}
          projectId="proj-1"
        />,
      );
    });

    fireEvent.click(screen.getByTestId("agent-status-resume"));

    expect(continueSessionInChat).toHaveBeenCalledWith("terminal-2", "proj-1", {
      provider: "claude",
      model: "sonnet",
      reasoningEffort: "auto",
      chatMode: null,
    });
  });

  it("normalizes the input chip to a valid model for the active provider", async () => {
    render(
      <ChatPage
        chat={createChat({
          provider: "claude",
          mainSessionMeta: {
            ref: "#77",
            source: "claude",
            title: "Claude Session",
            status: "active",
            model: null,
            externalId: "session-77",
            sessionType: "web_chat",
          },
        })}
        conversations={createConversations()}
        voice={createVoice()}
        currentModel="gpt-5.4"
      />,
    );

    await waitFor(() => {
      expect(screen.getByTestId("chat-input-provider")).toHaveTextContent(
        "claude",
      );
      expect(screen.getByTestId("chat-input-model")).toHaveTextContent(
        "sonnet",
      );
    });
  });

  it("keeps non-autonomous terminal swaps in observe mode", async () => {
    const continueSessionInChat = vi.fn(async () => "continued-session");
    const viewSession = vi.fn();
    const observeSession = vi.fn();

    render(
      <ChatPage
        chat={createChat({
          continueSessionInChat,
          viewSession,
          observeSession,
        })}
        conversations={createConversations()}
        voice={createVoice()}
        projectId="proj-1"
      />,
    );

    await act(async () => {
      fireEvent.click(screen.getByTestId("swap-terminal-session"));
    });

    expect(continueSessionInChat).not.toHaveBeenCalled();
    expect(viewSession).toHaveBeenCalledWith("terminal-2");
    expect(observeSession).toHaveBeenCalledWith("terminal-2", "observe");
  });

  it("keeps autonomous terminal swaps in observe mode", async () => {
    const continueSessionInChat = vi.fn(async () => "continued-session");
    const viewSession = vi.fn();
    const observeSession = vi.fn();

    render(
      <ChatPage
        chat={createChat({
          continueSessionInChat,
          viewSession,
          observeSession,
        })}
        conversations={createConversations()}
        voice={createVoice()}
      />,
    );

    await act(async () => {
      fireEvent.click(screen.getByTestId("swap-autonomous-session"));
    });

    expect(continueSessionInChat).not.toHaveBeenCalled();
    expect(viewSession).toHaveBeenCalledWith("terminal-auto");
    expect(observeSession).toHaveBeenCalledWith("terminal-auto", "observe");
  });
});
