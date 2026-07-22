import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { TerminalKeysBar } from "../TerminalKeysBar";

describe("TerminalKeysBar", () => {
  it("exact key emissions", async () => {
    const user = userEvent.setup();
    const sendInput = vi.fn();
    render(<TerminalKeysBar sendInput={sendInput} />);

    const input = screen.getByRole("textbox", { name: "Terminal input" });
    await user.type(input, "status");
    await user.click(screen.getByRole("button", { name: "Send" }));

    const quickKeys = ["Esc", "Tab", "Enter", "Up", "Down", "Ctrl+C", "1", "2", "3"];
    for (const name of quickKeys) {
      await user.click(screen.getByRole("button", { name }));
    }

    expect(sendInput.mock.calls.map(([data]) => data)).toEqual([
      "status\r",
      "\x1b",
      "\t",
      "\r",
      "\x1b[A",
      "\x1b[B",
      "\x03",
      "1",
      "2",
      "3",
    ]);
    expect(input).toHaveValue("");
  });
});
