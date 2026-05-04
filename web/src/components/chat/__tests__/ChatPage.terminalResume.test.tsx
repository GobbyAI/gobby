import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ChatPage } from "../ChatPage";
import {
  createChat,
  createConversations,
  createVoice,
  setupChatPageEnvironment,
  teardownChatPageEnvironment,
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

describe("ChatPage – terminal resume", () => {
  beforeEach(setupChatPageEnvironment);
  afterEach(teardownChatPageEnvironment);

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
