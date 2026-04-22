import { beforeEach, afterEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";

import type { ChatState, ConversationState, VoiceProps } from "../../../types/chat";
import { ChatPage } from "../ChatPage";

const {
  clearArtifactsSpy,
  isPinnedState,
  scrollToBottomSpy,
  setIsPinnedSpy,
  togglePanelSpy,
  isMobileState,
} = vi.hoisted(() => ({
  clearArtifactsSpy: vi.fn(),
  isPinnedState: { value: false },
  scrollToBottomSpy: vi.fn(),
  setIsPinnedSpy: vi.fn(),
  togglePanelSpy: vi.fn(),
  isMobileState: { value: false },
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
    modeDisabled,
    attachmentsDisabled,
    agentPickerDisabled,
    worktreePickerDisabled,
    provider,
    currentModel,
    currentReasoning,
    providerPickerDisabledReason,
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
  }) => (
    <div data-testid="chat-input">
      <span data-testid="chat-input-disabled">{String(Boolean(disabled))}</span>
      <span data-testid="chat-input-placeholder">{disabledPlaceholder ?? ""}</span>
      <span data-testid="chat-input-aria-label">{disabledAriaLabel ?? ""}</span>
      <span data-testid="chat-input-notice">{proxyDeliveryNotice ?? ""}</span>
      <span data-testid="chat-input-mode-disabled">{String(Boolean(modeDisabled))}</span>
      <span data-testid="chat-input-attachments-disabled">{String(Boolean(attachmentsDisabled))}</span>
      <span data-testid="chat-input-agent-disabled">{String(Boolean(agentPickerDisabled))}</span>
      <span data-testid="chat-input-worktree-disabled">{String(Boolean(worktreePickerDisabled))}</span>
      <span data-testid="chat-input-provider">{provider ?? ""}</span>
      <span data-testid="chat-input-model">{currentModel ?? ""}</span>
      <span data-testid="chat-input-reasoning">{currentReasoning ?? ""}</span>
      <span data-testid="chat-input-provider-disabled-reason">{providerPickerDisabledReason ?? ""}</span>
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

vi.mock("../../activity/ActivityPanel", () => ({
  ActivityPanel: ({
    sessions,
    onSwapSession,
    onResumeSession,
    chatSessionId,
    onApprovePlan,
    onRequestPlanChanges,
  }: {
    sessions?: Array<{ id: string }>
    onSwapSession?: (target: { sessionId: string; sessionType: "terminal" | "web_chat" | null; agentRunId: string | null }) => void
    onResumeSession?: (sessionId: string) => void
    chatSessionId?: string | null
    onApprovePlan?: () => void
    onRequestPlanChanges?: (feedback: string) => void
  }) => (
    <div data-testid="activity-panel">
      <span data-testid="activity-panel-session-count">{sessions?.length ?? 0}</span>
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
    </div>
  ),
}));

vi.mock("../VoiceStatusBar", () => ({
  VoiceStatusBar: () => null,
}));

vi.mock("../AgentStatusBar", () => ({
  AgentStatusBar: ({
    isAttached,
    onAttach,
    onResume,
    onDetach,
    onTogglePanel,
  }: {
    isAttached?: boolean;
    onAttach?: () => void;
    onResume?: () => void;
    onDetach?: () => void;
    onTogglePanel?: () => void;
  }) => (
    <div data-testid="agent-status-bar">
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
      {onTogglePanel && (
        <button type="button" data-testid="agent-status-panel-toggle" onClick={onTogglePanel}>
          Toggle Panel
        </button>
      )}
    </div>
  ),
}));

vi.mock("../../../hooks/useIsMobile", () => ({
  useIsMobile: () => isMobileState.value,
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
    isPinned: isPinnedState.value,
    panelWidth: 320,
    setActiveTab: vi.fn(),
    setIsPinned: setIsPinnedSpy,
    setPanelWidth: vi.fn(),
    showTab: vi.fn(),
    togglePanel: togglePanelSpy,
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
      expect(screen.getByTestId("agent-status-bar")).toBeInTheDocument();
      expect(screen.getByTestId("chat-input")).toBeInTheDocument();
      expect(screen.getByTestId("agent-status-attached")).toHaveTextContent(
        "true",
      );
      expect(screen.getByTestId("agent-status-panel-toggle")).toBeInTheDocument();
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

  it("keeps the lower status bar visible for regular web chat sessions", async () => {
    render(
      <ChatPage
        chat={createChat()}
        conversations={createConversations()}
        voice={createVoice()}
      />,
    );

    await waitFor(() => {
      expect(screen.getByTestId("agent-status-bar")).toBeInTheDocument();
      expect(screen.getByTestId("chat-input")).toBeInTheDocument();
      expect(screen.getByTestId("agent-status-panel-toggle")).toBeInTheDocument();
    });
  });

  it("locks CLI-owned footer controls while proxy-attached and shows attached session settings", async () => {
    render(
      <ChatPage
        chat={createChat({
          provider: "claude",
          attachedSessionId: "terminal-9",
          sessionInteractionMode: "proxy",
          activeAgent: "default",
          currentBranch: "feature/local",
          viewingSessionMeta: {
            ref: "#59",
            source: "codex",
            title: "Attached Terminal",
            status: "active",
            model: "gpt-5.4",
            reasoningEffort: "high",
            externalId: "term-59",
            sessionType: "terminal",
            gitBranch: "feature/attached",
          },
        })}
        conversations={createConversations()}
        voice={createVoice()}
      />,
    );

    await waitFor(() => {
      expect(screen.getByTestId("chat-input")).toBeInTheDocument();
    });

    expect(screen.getByTestId("chat-input-disabled")).toHaveTextContent("false");
    expect(screen.getByTestId("chat-input-mode-disabled")).toHaveTextContent("true");
    expect(screen.getByTestId("chat-input-attachments-disabled")).toHaveTextContent("true");
    expect(screen.getByTestId("chat-input-agent-disabled")).toHaveTextContent("true");
    expect(screen.getByTestId("chat-input-worktree-disabled")).toHaveTextContent("true");
    expect(screen.getByTestId("chat-input-provider")).toHaveTextContent("codex");
    expect(screen.getByTestId("chat-input-model")).toHaveTextContent("gpt-5.4");
    expect(screen.getByTestId("chat-input-reasoning")).toHaveTextContent("high");
    expect(screen.getByTestId("chat-input-provider-disabled-reason")).toHaveTextContent(
      "Attached session owns provider, model, and reasoning",
    );
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

  it("threads the shared session catalog into the activity panel and resumes with auto fallback", async () => {
    const continueSessionInChat = vi.fn(async () => "continued-session");

    await act(async () => {
      render(
        <ChatPage
          chat={createChat({
            dbSessionId: "web-main-1",
            continueSessionInChat,
          })}
          conversations={createConversations()}
          voice={createVoice()}
          projectId="proj-1"
          allProjectSessions={[
            {
              id: "session-1",
              ref: "#11",
              external_id: "ext-11",
              source: "claude",
              project_id: "proj-1",
              title: "Session One",
              status: "active",
              model: "sonnet",
              message_count: 1,
              created_at: "2026-04-01T00:00:00Z",
              updated_at: "2026-04-01T00:00:00Z",
              seq_num: 11,
              summary_markdown: null,
              digest_markdown: null,
              git_branch: "main",
              usage_input_tokens: 0,
              usage_output_tokens: 0,
              had_edits: false,
              agent_depth: 0,
              chat_mode: null,
              agent_run_id: null,
              parent_session_id: null,
              session_type: "web_chat",
              terminal_context: null,
            },
            {
              id: "session-2",
              ref: "#12",
              external_id: "ext-12",
              source: "codex",
              project_id: "proj-1",
              title: "Session Two",
              status: "expired",
              model: "gpt-5.4",
              message_count: 2,
              created_at: "2026-04-02T00:00:00Z",
              updated_at: "2026-04-02T00:00:00Z",
              seq_num: 12,
              summary_markdown: null,
              digest_markdown: null,
              git_branch: "main",
              usage_input_tokens: 0,
              usage_output_tokens: 0,
              had_edits: false,
              agent_depth: 0,
              chat_mode: null,
              agent_run_id: null,
              parent_session_id: null,
              session_type: "terminal",
              terminal_context: null,
            },
          ]}
        />,
      );
    });

    expect(screen.getByTestId("activity-panel-session-count")).toHaveTextContent("2");

    fireEvent.click(screen.getByTestId("resume-activity-session"));

    await waitFor(() => {
      expect(continueSessionInChat).toHaveBeenCalledWith("resume-target", "proj-1", {
        fallbackContext: "auto",
      });
    });
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
    expect(screen.getByTestId("agent-status-panel-toggle")).toBeInTheDocument();

    fireEvent.click(screen.getByTestId("agent-status-panel-toggle"));
    expect(togglePanelSpy).toHaveBeenCalledTimes(1);
  });

  it("shows only Resume for swapped terminals that cannot proxy attach", async () => {
    await act(async () => {
      render(
        <ChatPage
          chat={createChat({
            viewingSessionId: "terminal-resume-only",
            viewingSessionMeta: {
              ref: "#153",
              source: "gemini",
              title: "Resume Only Terminal",
              status: "handoff_ready",
              canProxyAttach: false,
              model: "gemini-2.5-pro",
              externalId: "term-153",
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
    expect(screen.queryByTestId("agent-status-attach")).toBeNull();
    expect(screen.getByTestId("agent-status-resume")).toBeInTheDocument();
  });

  it("keeps Attach available for live handoff tmux sessions across providers", async () => {
    await act(async () => {
      render(
        <ChatPage
          chat={createChat({
            viewingSessionId: "terminal-live-handoff",
            viewingSessionMeta: {
              ref: "#154",
              source: "qwen",
              title: "Live Handoff Terminal",
              status: "handoff_ready",
              canProxyAttach: true,
              model: "qwen3-coder",
              externalId: "term-154",
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
      fallbackContext: "auto",
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

  it("renders the activity-panel toggle in the status bar when the chat input is visible", async () => {
    render(
      <ChatPage
        chat={createChat()}
        conversations={createConversations()}
        voice={createVoice()}
      />,
    );

    await waitFor(() => {
      expect(screen.getByTestId("agent-status-panel-toggle")).toBeInTheDocument();
    });

    fireEvent.click(screen.getByTestId("agent-status-panel-toggle"));
    expect(togglePanelSpy).toHaveBeenCalledTimes(1);
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

  it("keeps the activity panel open after plan approval on desktop", async () => {
    const onApprovePlan = vi.fn();

    render(
      <ChatPage
        chat={createChat({
          planPendingApproval: true,
          onApprovePlan,
        })}
        conversations={createConversations()}
        voice={createVoice()}
      />,
    );

    await act(async () => {
      fireEvent.click(screen.getByTestId("approve-plan"));
    });

    expect(onApprovePlan).toHaveBeenCalledTimes(1);
    expect(setIsPinnedSpy).not.toHaveBeenCalled();
  });

  it("does not unpin on mobile when the activity panel is already unpinned", async () => {
    isMobileState.value = true;

    render(
      <ChatPage
        chat={createChat()}
        conversations={createConversations()}
        voice={createVoice()}
      />,
    );

    await waitFor(() => {
      expect(screen.getByTestId("chat-input")).toBeInTheDocument();
    });

    expect(setIsPinnedSpy).not.toHaveBeenCalled();
  });

  it("closes the activity panel after plan changes are requested on mobile when pinned", async () => {
    isMobileState.value = true;
    isPinnedState.value = true;
    const onRequestPlanChanges = vi.fn();

    render(
      <ChatPage
        chat={createChat({
          planPendingApproval: true,
          onRequestPlanChanges,
        })}
        conversations={createConversations()}
        voice={createVoice()}
      />,
    );

    await act(async () => {
      fireEvent.click(screen.getByTestId("request-plan-changes"));
    });

    expect(onRequestPlanChanges).toHaveBeenCalledWith("Needs changes");
    expect(setIsPinnedSpy).toHaveBeenCalledWith(false);
  });
});
