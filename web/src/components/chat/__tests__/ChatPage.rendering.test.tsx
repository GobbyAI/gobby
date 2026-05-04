import { act, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ChatPage } from "../ChatPage";
import {
  DATA_URI,
  createArtifactSpy,
  createChat,
  createConversations,
  createVoice,
  scrollToBottomSpy,
  setupChatPageEnvironment,
  showTabSpy,
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

describe("ChatPage – rendering", () => {
  beforeEach(setupChatPageEnvironment);
  afterEach(teardownChatPageEnvironment);

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
      expect(screen.getByTestId("command-bar-panel-toggle")).toBeInTheDocument();
    });
  });


  it("opens the Artifacts tab when a show_file artifact event arrives", async () => {
    let artifactEvent:
      | ((
          type: string,
          content: string,
          language?: string,
          title?: string,
        ) => void)
      | null = null;
    const setOnArtifactEvent = vi.fn((fn) => {
      artifactEvent = fn;
    });

    render(
      <ChatPage
        chat={createChat({ setOnArtifactEvent })}
        conversations={createConversations()}
        voice={createVoice()}
      />,
    );

    await waitFor(() => {
      expect(setOnArtifactEvent).toHaveBeenCalled();
    });

    act(() => {
      artifactEvent?.("image", DATA_URI, "png", "Generated image");
    });

    expect(createArtifactSpy).toHaveBeenCalledWith(
      "image",
      DATA_URI,
      "png",
      "Generated image",
    );
    expect(showTabSpy).toHaveBeenCalledWith("artifacts");
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

});
