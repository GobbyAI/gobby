import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { TerminalDock } from "../TerminalDock";

const { terminalTabSpy } = vi.hoisted(() => ({
  terminalTabSpy: vi.fn(),
}));

vi.mock("../TerminalTab", () => ({
  TerminalTab: (props: {
    sessions: unknown[];
    focusSessionId?: string | null;
    onFocusHandled?: () => void;
  }) => {
    terminalTabSpy(props);
    return <div>Terminal Tab</div>;
  },
}));

describe("TerminalDock", () => {
  it("renders the terminal content with focus wiring", async () => {
    const onFocusHandled = vi.fn();

    render(
      <TerminalDock
        sessions={[]}
        focusSessionId="gobby-session-42"
        onFocusHandled={onFocusHandled}
        expanded={false}
        onToggleExpanded={vi.fn()}
        onClose={vi.fn()}
      />,
    );

    expect(await screen.findByText("Terminal Tab")).toBeInTheDocument();
    expect(terminalTabSpy.mock.lastCall?.[0]).toMatchObject({
      sessions: [],
      focusSessionId: "gobby-session-42",
      onFocusHandled,
    });
  });

  it("offers expand when collapsed and collapse when expanded", async () => {
    const user = userEvent.setup();
    const onToggleExpanded = vi.fn();

    const rendered = render(
      <TerminalDock
        sessions={[]}
        focusSessionId={null}
        onFocusHandled={vi.fn()}
        expanded={false}
        onToggleExpanded={onToggleExpanded}
        onClose={vi.fn()}
      />,
    );

    await user.click(screen.getByRole("button", { name: "Expand terminal panel" }));
    expect(onToggleExpanded).toHaveBeenCalledTimes(1);

    rendered.rerender(
      <TerminalDock
        sessions={[]}
        focusSessionId={null}
        onFocusHandled={vi.fn()}
        expanded={true}
        onToggleExpanded={onToggleExpanded}
        onClose={vi.fn()}
      />,
    );

    expect(screen.getByRole("button", { name: "Collapse terminal panel" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Expand terminal panel" })).toBeNull();
  });

  it("invokes onClose from the header close button", async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();

    render(
      <TerminalDock
        sessions={[]}
        focusSessionId={null}
        onFocusHandled={vi.fn()}
        expanded={false}
        onToggleExpanded={vi.fn()}
        onClose={onClose}
      />,
    );

    await user.click(screen.getByRole("button", { name: "Close terminal panel" }));
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});
