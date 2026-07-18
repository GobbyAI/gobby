import { render, screen, waitFor } from "@testing-library/react";
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
vi.mock("../../../hooks/usePlans", async () =>
  (await import("./chatPageTestSetup")).usePlansMockFactory(),
);
vi.mock("../../activity/useActivityPanel", async () =>
  (await import("./chatPageTestSetup")).useActivityPanelMockFactory(),
);
vi.mock("../../../hooks/useFileChanges", async () =>
  (await import("./chatPageTestSetup")).useFileChangesMockFactory(),
);
vi.mock("../../../hooks/useConfirmDialog", async () =>
  (await import("./chatPageTestSetup")).useConfirmDialogMockFactory(),
);

describe("ChatPage – plan mode radios (#18343)", () => {
  beforeEach(setupChatPageEnvironment);
  afterEach(teardownChatPageEnvironment);

  it("keeps mode radios enabled while a plan is pending approval", async () => {
    // A spurious (or genuine) plan_pending_approval frame must not remove the
    // user's escape hatch: switching modes releases the pending plan instead.
    render(
      <ChatPage
        chat={createChat({ planPendingApproval: true })}
        conversations={createConversations()}
        voice={createVoice()}
      />,
    );

    await waitFor(() => {
      expect(screen.getByTestId("chat-input")).toBeInTheDocument();
    });

    expect(screen.getByTestId("chat-input-mode-disabled")).toHaveTextContent(
      "false",
    );
  });

  it("still disables mode radios for autonomous sessions", async () => {
    render(
      <ChatPage
        chat={createChat({
          viewingSessionId: "terminal-7",
          attachedSessionId: "terminal-7",
          sessionInteractionMode: "proxy",
          viewingSessionMeta: {
            ref: "#77",
            source: "claude",
            title: "Autonomous Run",
            status: "active",
            model: "sonnet",
            externalId: "term-77",
            sessionType: "terminal",
            agentRunId: "run-77",
          },
        })}
        conversations={createConversations()}
        voice={createVoice()}
      />,
    );

    await waitFor(() => {
      expect(screen.getByTestId("chat-input")).toBeInTheDocument();
    });

    expect(screen.getByTestId("chat-input-mode-disabled")).toHaveTextContent(
      "true",
    );
  });
});
