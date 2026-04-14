import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";

import { CommandBar } from "../CommandBar";

describe("CommandBar", () => {
  it("renders the active viewed-session title in the selector without inline session controls", () => {
    render(
      <CommandBar
        sessionRef="#42"
        title="Viewed web chat"
        onOpenPalette={vi.fn()}
        onOpenActiveSessions={vi.fn()}
        onNewChat={vi.fn()}
        onTogglePanel={vi.fn()}
        agents={[]}
        isPanelPinned={false}
      />,
    );

    expect(screen.getByTestId("chat-session-selector")).toHaveTextContent("#42");
    expect(screen.getByTestId("chat-session-selector")).toHaveTextContent(
      "Viewed web chat",
    );
    expect(screen.queryByText("Attach")).toBeNull();
    expect(screen.queryByText("Detach")).toBeNull();
  });

  it("renders the default title for a fresh chat", () => {
    render(
      <CommandBar
        sessionRef={null}
        title={null}
        onOpenPalette={vi.fn()}
        onOpenActiveSessions={vi.fn()}
        onNewChat={vi.fn()}
        onTogglePanel={vi.fn()}
        agents={[]}
        isPanelPinned={false}
      />,
    );

    expect(screen.getByTestId("chat-session-selector")).toHaveTextContent(
      "New Chat Session",
    );
  });
});
