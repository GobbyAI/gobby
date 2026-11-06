import { render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { CommandBar } from "../CommandBar";

it("shows the reference and provider icon with no session title", () => {
  render(
    <CommandBar
      sessionRef="gobby#12"
      sessionSource="codex"
      onOpenPalette={vi.fn()}
    />,
  );
  expect(screen.getByTestId("chat-session-selector")).toHaveTextContent(
    "gobby#12",
  );
  expect(screen.queryByText("Codex")).toBeNull();
});
