import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ChatPage } from "../ChatPage";
import {
  createChat,
  createConversations,
  createVoice,
  isMobileState,
  isPinnedState,
  setIsPinnedSpy,
  showTabSpy,
  setupChatPageEnvironment,
  teardownChatPageEnvironment,
  togglePanelSpy,
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

describe("ChatPage – activity panel", () => {
  beforeEach(setupChatPageEnvironment);
  afterEach(teardownChatPageEnvironment);

  it("renders the activity-panel toggle in the status bar when the chat input is visible", async () => {
    render(
      <ChatPage
        chat={createChat()}
        conversations={createConversations()}
        voice={createVoice()}
      />,
    );

    await waitFor(() => {
      expect(screen.getByTestId("command-bar-panel-toggle")).toBeInTheDocument();
    });

    fireEvent.click(screen.getByTestId("command-bar-panel-toggle"));
    expect(togglePanelSpy).toHaveBeenCalledTimes(1);
  });


  it("does not flicker or disappear after repeated mobile user toggles", async () => {
    isMobileState.value = true;
    isPinnedState.value = false;
    const chat = createChat();
    const conversations = createConversations();
    const voice = createVoice();

    const { rerender } = render(
      <ChatPage
        chat={chat}
        conversations={conversations}
        voice={voice}
      />,
    );

    await waitFor(() => {
      expect(screen.getByTestId("command-bar-panel-toggle")).toBeInTheDocument();
    });

    setIsPinnedSpy.mockClear();
    fireEvent.click(screen.getByTestId("command-bar-panel-toggle"));
    expect(togglePanelSpy).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId("activity-panel")).toBeInTheDocument();

    isPinnedState.value = true;
    await act(async () => {
      rerender(
        <ChatPage
          chat={chat}
          conversations={conversations}
          voice={voice}
        />,
      );
    });
    expect(screen.getByTestId("activity-panel")).toBeInTheDocument();

    fireEvent.click(screen.getByTestId("command-bar-panel-toggle"));
    expect(togglePanelSpy).toHaveBeenCalledTimes(2);
    expect(screen.getByTestId("activity-panel")).toBeInTheDocument();

    isPinnedState.value = true;
    await act(async () => {
      rerender(
        <ChatPage
          chat={chat}
          conversations={conversations}
          voice={voice}
        />,
      );
    });
    expect(screen.getByTestId("activity-panel")).toBeInTheDocument();

    expect(setIsPinnedSpy).not.toHaveBeenCalledWith(false);
  });


  it("auto-closes the activity panel when a pinned desktop layout becomes mobile", async () => {
    isPinnedState.value = true;
    const chat = createChat();
    const conversations = createConversations();
    const voice = createVoice();

    const { rerender } = render(
      <ChatPage
        chat={chat}
        conversations={conversations}
        voice={voice}
      />,
    );

    await waitFor(() => {
      expect(screen.getByTestId("command-bar-panel-toggle")).toBeInTheDocument();
    });
    expect(setIsPinnedSpy).not.toHaveBeenCalledWith(false);

    isMobileState.value = true;
    await act(async () => {
      rerender(
        <ChatPage
          chat={chat}
          conversations={conversations}
          voice={voice}
        />,
      );
    });

    expect(setIsPinnedSpy).toHaveBeenCalledWith(false);
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


  it("still closes the activity panel after plan approval on mobile when pinned", async () => {
    isMobileState.value = true;
    isPinnedState.value = true;
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

    await waitFor(() => {
      expect(screen.getByTestId("approve-plan")).toBeInTheDocument();
    });
    setIsPinnedSpy.mockClear();

    await act(async () => {
      fireEvent.click(screen.getByTestId("approve-plan"));
    });

    expect(onApprovePlan).toHaveBeenCalledTimes(1);
    expect(setIsPinnedSpy).toHaveBeenCalledWith(false);
  });


  it("keeps the mobile attach-file callback routed while the panel is pinned", async () => {
    isMobileState.value = true;
    isPinnedState.value = true;
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
    setIsPinnedSpy.mockClear();

    fireEvent.click(screen.getByTestId("attach-file-to-chat"));

    expect(onSend).toHaveBeenCalledWith("Read and reference this file: /tmp/context.md");
    expect(screen.getByTestId("activity-panel")).toBeInTheDocument();
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

  it("keeps the parked web chat visible in Sessions while a terminal swap is active", async () => {
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
    expect(screen.getByTestId("activity-panel-focus-session-id")).toHaveTextContent(
      "db-session-1",
    );

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
          })}
          conversations={conversations}
          voice={voice}
        />,
      );
    });

    expect(screen.getByTestId("activity-panel-chat-session-id")).toHaveTextContent(
      "terminal-2",
    );
    expect(screen.getByTestId("activity-panel-focus-session-id")).toHaveTextContent(
      "db-session-1",
    );
  });

});
