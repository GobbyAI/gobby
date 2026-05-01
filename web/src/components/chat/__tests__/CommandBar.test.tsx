import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { CommandBar } from "../CommandBar";

describe("CommandBar", () => {
  it("renders the active viewed-session title in the selector without lower-bar state duplicated up top", () => {
    render(
      <CommandBar
        sessionRef="#42"
        title="Viewed web chat"
        sessionSource="codex"
        onOpenPalette={vi.fn()}
      />,
    );

    expect(screen.getByText("#42")).toHaveClass("command-bar-ref");
    expect(screen.getByText("Viewed web chat")).toHaveClass("command-bar-title");
    expect(screen.getByTestId("chat-session-selector")).toHaveTextContent("#42");
    expect(screen.getByTestId("chat-session-selector")).toHaveTextContent(
      "Viewed web chat",
    );
    expect(screen.queryByText("Watching live")).toBeNull();
    expect(screen.queryByText("Attach")).toBeNull();
    expect(screen.queryByText("Detach")).toBeNull();
  });

  it("renders the default title for a fresh chat", () => {
    render(
      <CommandBar
        sessionRef={null}
        title={null}
        onOpenPalette={vi.fn()}
      />,
    );

    expect(screen.getByTestId("chat-session-selector")).toHaveTextContent(
      "New Session",
    );
  });

  it("renders the activity-panel toggle when onTogglePanel is provided", async () => {
    const onTogglePanel = vi.fn();

    render(
      <CommandBar
        sessionRef="#42"
        title="Session"
        onOpenPalette={vi.fn()}
        onTogglePanel={onTogglePanel}
        isPanelPinned={true}
      />,
    );

    await userEvent.click(
      screen.getByRole("button", { name: "Hide activity panel" }),
    );
    expect(onTogglePanel).toHaveBeenCalledTimes(1);
  });
});
