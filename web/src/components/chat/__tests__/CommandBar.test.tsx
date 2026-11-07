import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { CommandBar } from "../CommandBar";

describe("CommandBar", () => {
  it("renders the viewed-session ref in the selector without lower-bar state duplicated up top", () => {
    render(
      <CommandBar
        sessionRef="#42"
        sessionSource="codex"
        onOpenPalette={vi.fn()}
      />,
    );

    expect(screen.getByText("#42")).toHaveClass("command-bar-ref");
    expect(screen.getByTestId("chat-session-selector")).toHaveTextContent(
      "#42",
    );
    expect(screen.queryByText("Watching live")).toBeNull();
    expect(screen.queryByText("Attach")).toBeNull();
    expect(screen.queryByText("Detach")).toBeNull();
  });

  it("renders no ref for a fresh chat", () => {
    const { container } = render(
      <CommandBar sessionRef={null} onOpenPalette={vi.fn()} />,
    );

    expect(container.querySelector(".command-bar-ref")).toBeNull();
    expect(screen.getByTestId("chat-session-selector")).toBeInTheDocument();
  });

  it("renders the activity-panel toggle when onTogglePanel is provided", async () => {
    const onTogglePanel = vi.fn();

    render(
      <CommandBar
        sessionRef="#42"
        onOpenPalette={vi.fn()}
        onTogglePanel={onTogglePanel}
        panelVisible={true}
      />,
    );

    await userEvent.click(
      screen.getByRole("button", { name: "Hide activity panel" }),
    );
    expect(onTogglePanel).toHaveBeenCalledTimes(1);
  });
});
