import { render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { CommandBar } from "../CommandBar";

it("shows the reference and provider icon without a redundant provider title", () => {
  const { rerender } = render(
    <CommandBar
      sessionRef="gobby#12"
      title="gobby#12: Codex"
      sessionSource="codex"
      onOpenPalette={vi.fn()}
    />,
  );
  expect(screen.getByTestId("chat-session-selector")).toHaveTextContent(
    "gobby#12",
  );
  expect(screen.queryByText("Codex")).toBeNull();
  rerender(
    <CommandBar
      sessionRef="gobby#12"
      title="gobby#12: Task #42 - Fix"
      sessionSource="codex"
      onOpenPalette={vi.fn()}
    />,
  );
  expect(screen.getByText("Task #42 - Fix")).toBeInTheDocument();
});
