import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ChatPage } from "../ChatPage";
import type { GobbySession } from "../../../types/sessions";
import {
  commandPalettePropsSpy,
  createChat,
  createConversations,
  createVoice,
  dismissOnMobileSpy,
  isMobileState,
  showTabSpy,
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

function makeSession(overrides: Partial<GobbySession>): GobbySession {
  return {
    id: "session-1",
    ref: "#1",
    external_id: "external-1",
    source: "codex",
    project_id: "proj-1",
    title: "Session",
    status: "active",
    model: "gpt-5.4",
    message_count: 1,
    created_at: "2026-05-04T12:00:00Z",
    updated_at: "2026-05-04T12:01:00Z",
    seq_num: 1,
    summary_markdown: null,
    digest_markdown: null,
    git_branch: "main",
    usage_input_tokens: 0,
    usage_output_tokens: 0,
    had_edits: false,
    agent_depth: 0,
    chat_mode: "plan",
    agent_run_id: null,
    parent_session_id: null,
    session_type: "web_chat",
    terminal_context: null,
    sandbox_enabled: false,
    sandbox_policy_hash: null,
    ...overrides,
  };
}

// The tri-state crossing logic (desktop<->mobile derivation, never both
// collapsed, localStorage migration) lives in `useActivityPanel` and is
// covered directly by useActivityPanel.test.tsx. These tests only assert
// ChatPage's wiring to the hook's command surface.
describe("ChatPage – activity panel wiring", () => {
  beforeEach(setupChatPageEnvironment);
  afterEach(teardownChatPageEnvironment);

  it("wires the command-bar panel toggle to toggleFromChat", async () => {
    render(
      <ChatPage
        chat={createChat()}
        conversations={createConversations()}
        voice={createVoice()}
      />,
    );

    await waitFor(() => {
      expect(
        screen.getByTestId("command-bar-panel-toggle"),
      ).toBeInTheDocument();
    });

    fireEvent.click(screen.getByTestId("command-bar-panel-toggle"));
    expect(toggleFromChatSpy).toHaveBeenCalledTimes(1);
  });

  it("keeps invoking toggleFromChat on repeated toggles without unmounting the panel", async () => {
    isMobileState.value = true;
    const chat = createChat();
    const conversations = createConversations();
    const voice = createVoice();

    render(
      <ChatPage chat={chat} conversations={conversations} voice={voice} />,
    );

    await waitFor(() => {
      expect(
        screen.getByTestId("command-bar-panel-toggle"),
      ).toBeInTheDocument();
    });

    fireEvent.click(screen.getByTestId("command-bar-panel-toggle"));
    fireEvent.click(screen.getByTestId("command-bar-panel-toggle"));

    expect(toggleFromChatSpy).toHaveBeenCalledTimes(2);
    expect(screen.getByTestId("activity-panel")).toBeInTheDocument();
  });

  it("calls onApprovePlan then dismissOnMobile after plan approval", async () => {
    const onApprovePlan = vi.fn();

    render(
      <ChatPage
        chat={createChat({ planPendingApproval: true, onApprovePlan })}
        conversations={createConversations()}
        voice={createVoice()}
      />,
    );

    await act(async () => {
      fireEvent.click(screen.getByTestId("approve-plan"));
    });

    expect(onApprovePlan).toHaveBeenCalledTimes(1);
    expect(dismissOnMobileSpy).toHaveBeenCalledTimes(1);
  });

  it("calls onRequestPlanChanges then dismissOnMobile after requesting changes", async () => {
    const onRequestPlanChanges = vi.fn();

    render(
      <ChatPage
        chat={createChat({ planPendingApproval: true, onRequestPlanChanges })}
        conversations={createConversations()}
        voice={createVoice()}
      />,
    );

    await act(async () => {
      fireEvent.click(screen.getByTestId("request-plan-changes"));
    });

    expect(onRequestPlanChanges).toHaveBeenCalledWith("Needs changes");
    expect(dismissOnMobileSpy).toHaveBeenCalledTimes(1);
  });

  it("routes the attach-file callback without dismissing the panel", async () => {
    isMobileState.value = true;
    const onSend = vi.fn();

    render(
      <ChatPage
        chat={createChat({ onSend })}
        conversations={createConversations()}
        voice={createVoice()}
      />,
    );

    await waitFor(() => {
      expect(screen.getByTestId("attach-file-to-chat")).toBeInTheDocument();
    });

    fireEvent.click(screen.getByTestId("attach-file-to-chat"));

    expect(onSend).toHaveBeenCalledWith(
      "Read and reference this file: /tmp/context.md",
    );
    expect(screen.getByTestId("activity-panel")).toBeInTheDocument();
    expect(dismissOnMobileSpy).not.toHaveBeenCalled();
  });

  it("feeds the command palette active and paused activity sessions", async () => {
    const activitySessions = [
      makeSession({ id: "db-session-1", ref: "#10", seq_num: 10 }),
      makeSession({
        id: "terminal-live",
        ref: "#14",
        seq_num: 14,
        session_type: "terminal",
      }),
      makeSession({
        id: "web-paused",
        ref: "#12",
        seq_num: 12,
        status: "paused",
      }),
      makeSession({
        id: "expired-session",
        ref: "#11",
        seq_num: 11,
        status: "expired",
      }),
      makeSession({
        id: "hidden-cron",
        ref: "#13",
        seq_num: 13,
        source: "cron",
      }),
    ];

    render(
      <ChatPage
        chat={createChat({ dbSessionId: "db-session-1" })}
        conversations={createConversations()}
        voice={createVoice()}
        activitySessions={activitySessions}
      />,
    );

    await act(async () => {
      fireEvent.click(screen.getByTestId("command-bar-open-palette"));
    });

    expect(screen.getByTestId("command-palette-session-ids")).toHaveTextContent(
      "terminal-live,web-paused",
    );
    expect(commandPalettePropsSpy).toHaveBeenLastCalledWith(
      expect.objectContaining({ activeSessionId: "db-session-1" }),
    );
  });

  it("routes command-palette non-web session selection through swap viewing", async () => {
    const viewSession = vi.fn();
    const observeSession = vi.fn();
    const onSelectSession = vi.fn();
    const terminalSession = makeSession({
      id: "terminal-live",
      ref: "#14",
      seq_num: 14,
      session_type: "terminal",
    });

    render(
      <ChatPage
        chat={createChat({
          dbSessionId: "db-session-1",
          viewSession,
          observeSession,
        })}
        conversations={{
          ...createConversations(),
          onSelectSession,
        }}
        voice={createVoice()}
        activitySessions={[terminalSession]}
      />,
    );

    await act(async () => {
      fireEvent.click(screen.getByTestId("command-bar-open-palette"));
    });
    await act(async () => {
      fireEvent.click(screen.getByTestId("command-palette-select-terminal-live"));
    });

    expect(showTabSpy).toHaveBeenCalledWith("sessions");
    expect(viewSession).toHaveBeenCalledWith("terminal-live", {
      forceRefresh: true,
    });
    expect(observeSession).toHaveBeenCalledWith("terminal-live", "observe");
    expect(onSelectSession).not.toHaveBeenCalled();
  });

  it("hides the swapped terminal while keeping the parked web chat focused", async () => {
    const viewSession = vi.fn();
    const observeSession = vi.fn();
    const conversations = createConversations();
    const voice = createVoice();
    const chat = createChat({
      dbSessionId: "db-session-1",
      messages: [
        {
          id: "main-msg-1",
          role: "user",
          content: "Park me",
          timestamp: new Date("2026-05-04T12:00:00Z"),
        },
      ],
      viewSession,
      observeSession,
    });

    const { rerender } = render(
      <ChatPage chat={chat} conversations={conversations} voice={voice} />,
    );

    await act(async () => {
      fireEvent.click(screen.getByTestId("swap-terminal-session"));
    });

    expect(showTabSpy).toHaveBeenCalledWith("sessions");
    expect(viewSession).toHaveBeenCalledWith("terminal-2", {
      forceRefresh: true,
    });
    expect(observeSession).toHaveBeenCalledWith("terminal-2", "observe");
    expect(
      screen.getByTestId("activity-panel-focus-session-id"),
    ).toHaveTextContent("db-session-1");

    await act(async () => {
      rerender(
        <ChatPage
          chat={createChat({
            ...chat,
            viewingSessionId: "terminal-2",
            viewingSessionMeta: {
              ref: "#220",
              source: "codex",
              title: "Terminal",
              status: "active",
              model: "gpt-5.4",
              externalId: "terminal-ext",
              chatMode: null,
              gitBranch: "main",
              contextWindow: null,
              agentRunId: null,
              workflowName: null,
              agentName: null,
              sessionType: "terminal",
            },
            viewSession,
            observeSession,
            sessionInteractionMode: "observe",
          })}
          conversations={conversations}
          voice={voice}
        />,
      );
    });

    expect(
      screen.getByTestId("activity-panel-chat-session-id"),
    ).toHaveTextContent("terminal-2");
    expect(
      screen.getByTestId("activity-panel-focus-session-id"),
    ).toHaveTextContent("db-session-1");
  });

  it("hides the swapped web chat from the activity panel main-session slot", async () => {
    const viewSession = vi.fn();
    const observeSession = vi.fn();
    const targetSession = {
      id: "web-chat-2",
      ref: "#221",
      external_id: "web-chat-ext",
      source: "codex",
      project_id: "proj-1",
      title: "Swapped Web Chat",
      status: "active",
      model: "gpt-5.5",
      message_count: 1,
      created_at: "2026-05-04T12:00:00Z",
      updated_at: "2026-05-04T12:01:00Z",
      seq_num: 221,
      summary_markdown: null,
      digest_markdown: null,
      git_branch: "main",
      usage_input_tokens: 0,
      usage_output_tokens: 0,
      had_edits: false,
      agent_depth: 0,
      chat_mode: "plan",
      agent_run_id: null,
      parent_session_id: null,
      session_type: "web_chat",
      terminal_context: null,
      sandbox_enabled: false,
      sandbox_policy_hash: null,
    } as const;
    const onSelectSession = vi.fn();
    const conversations = {
      ...createConversations(),
      sessions: [targetSession],
      onSelectSession,
    };
    const voice = createVoice();
    const chat = createChat({
      dbSessionId: "db-session-1",
      messages: [
        {
          id: "main-msg-1",
          role: "user",
          content: "Park me",
          timestamp: new Date("2026-05-04T12:00:00Z"),
        },
      ],
      viewSession,
      observeSession,
    });

    const { rerender } = render(
      <ChatPage chat={chat} conversations={conversations} voice={voice} />,
    );

    await act(async () => {
      fireEvent.click(screen.getByTestId("swap-web-chat-session"));
    });

    expect(showTabSpy).toHaveBeenCalledWith("sessions");
    expect(onSelectSession).toHaveBeenCalledWith(targetSession);
    expect(viewSession).not.toHaveBeenCalled();
    expect(observeSession).not.toHaveBeenCalled();

    await act(async () => {
      rerender(
        <ChatPage
          chat={createChat({
            ...chat,
            dbSessionId: "web-chat-2",
            viewSession,
            observeSession,
          })}
          conversations={conversations}
          voice={voice}
        />,
      );
    });

    expect(
      screen.getByTestId("activity-panel-chat-session-id"),
    ).toHaveTextContent("web-chat-2");
    expect(
      screen.getByTestId("activity-panel-focus-session-id"),
    ).toHaveTextContent("db-session-1");
  });

  it("parks the current web chat when starting a new chat even before messages are loaded", async () => {
    const onNewChat = vi.fn();
    const conversations = {
      ...createConversations(),
      onNewChat,
    };
    const voice = createVoice();
    const chat = createChat({
      dbSessionId: "web-chat-4993",
      messages: [],
    });

    const { rerender } = render(
      <ChatPage chat={chat} conversations={conversations} voice={voice} />,
    );

    await act(async () => {
      fireEvent.click(screen.getByTestId("new-chat-button"));
    });

    expect(showTabSpy).toHaveBeenCalledWith("sessions");
    expect(onNewChat).toHaveBeenCalledWith(undefined);
    expect(
      screen.getByTestId("activity-panel-focus-session-id"),
    ).toHaveTextContent("web-chat-4993");

    await act(async () => {
      rerender(
        <ChatPage
          chat={createChat({
            ...chat,
            dbSessionId: null,
            messages: [],
          })}
          conversations={conversations}
          voice={voice}
        />,
      );
    });

    expect(screen.getByTestId("activity-panel-chat-session-id").textContent).toBe(
      "",
    );
    expect(
      screen.getByTestId("activity-panel-focus-session-id"),
    ).toHaveTextContent("web-chat-4993");
  });
});
