import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ChatPage } from "../ChatPage";
import {
  createChat,
  createConversations,
  createVoice,
  setupChatPageEnvironment,
  teardownChatPageEnvironment,
  toggleFromChatSpy,
} from "./chatPageTestSetup";

vi.mock("../MessageList", async () =>
  (await import("./chatPageTestSetup")).messageListMockFactory(),
);
vi.mock("../ChatInput", async () =>
  (await import("./chatPageTestSetup")).chatInputMockFactory(),
);
vi.mock("../CommandBar", async () =>
  (await import("./chatPageTestSetup")).commandBarMockFactory(),
);
vi.mock("../CommandPalette", async () =>
  (await import("./chatPageTestSetup")).commandPaletteMockFactory(),
);
vi.mock("../../activity/ActivityPanel", async () =>
  (await import("./chatPageTestSetup")).activityPanelMockFactory(),
);
vi.mock("../VoiceStatusBar", async () =>
  (await import("./chatPageTestSetup")).voiceStatusBarMockFactory(),
);
vi.mock("../AgentStatusBar", async () =>
  (await import("./chatPageTestSetup")).agentStatusBarMockFactory(),
);
vi.mock("../../../hooks/useIsMobile", async () =>
  (await import("./chatPageTestSetup")).useIsMobileMockFactory(),
);
vi.mock("../../../hooks/useArtifacts", async () =>
  (await import("./chatPageTestSetup")).useArtifactsMockFactory(),
);
vi.mock("../../activity/useActivityPanel", async () =>
  (await import("./chatPageTestSetup")).useActivityPanelMockFactory(),
);
vi.mock("../../canvas/hooks/useCanvasPanel", async () =>
  (await import("./chatPageTestSetup")).useCanvasPanelMockFactory(),
);
vi.mock("../../../hooks/useFileChanges", async () =>
  (await import("./chatPageTestSetup")).useFileChangesMockFactory(),
);
vi.mock("../../../hooks/useConfirmDialog", async () =>
  (await import("./chatPageTestSetup")).useConfirmDialogMockFactory(),
);

describe("ChatPage – terminal attachment", () => {
  beforeEach(setupChatPageEnvironment);
  afterEach(teardownChatPageEnvironment);

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
      expect(
        screen.getByTestId("command-bar-panel-toggle"),
      ).toBeInTheDocument();
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

    expect(screen.getByTestId("chat-input-disabled")).toHaveTextContent(
      "false",
    );
    expect(screen.getByTestId("chat-input-mode-disabled")).toHaveTextContent(
      "false",
    );
    expect(
      screen.getByTestId("chat-input-attachments-disabled"),
    ).toHaveTextContent("false");
    expect(screen.getByTestId("chat-input-agent-disabled")).toHaveTextContent(
      "false",
    );
    expect(
      screen.getByTestId("chat-input-worktree-disabled"),
    ).toHaveTextContent("true");
    expect(screen.getByTestId("chat-input-provider")).toHaveTextContent(
      "codex",
    );
    expect(screen.getByTestId("chat-input-model")).toHaveTextContent("gpt-5.4");
    expect(screen.getByTestId("chat-input-reasoning")).toHaveTextContent(
      "high",
    );
    expect(
      screen.getByTestId("chat-input-provider-disabled-reason"),
    ).toHaveTextContent("Attached session owns provider, model, and reasoning");
  });

  it("uses the watched terminal as the hidden activity panel chat session", async () => {
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

    expect(
      screen.getByTestId("activity-panel-session-count"),
    ).toHaveTextContent("2");

    fireEvent.click(screen.getByTestId("resume-activity-session"));

    await waitFor(() => {
      expect(continueSessionInChat).toHaveBeenCalledWith(
        "resume-target",
        "proj-1",
        {
          fallbackContext: "auto",
        },
      );
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
    expect(screen.getByTestId("command-bar-panel-toggle")).toBeInTheDocument();

    fireEvent.click(screen.getByTestId("command-bar-panel-toggle"));
    expect(toggleFromChatSpy).toHaveBeenCalledTimes(1);
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
});
