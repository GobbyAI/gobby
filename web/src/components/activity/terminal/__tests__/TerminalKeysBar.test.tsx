import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { TerminalKeysBar } from "../TerminalKeysBar";

describe("TerminalKeysBar", () => {
  it("exact key emissions", async () => {
    const user = userEvent.setup();
    const sendInput = vi.fn();
    render(<TerminalKeysBar sendInput={sendInput} />);

    // Regular typing goes straight into the focused terminal window; the bar
    // carries only the special keys an on-screen keyboard can't produce.
    expect(screen.queryByRole("textbox")).toBeNull();

    const quickKeys = [
      "Esc",
      "Tab",
      "Enter",
      "Up",
      "Down",
      "Ctrl+C",
      "1",
      "2",
      "3",
    ];
    for (const name of quickKeys) {
      await user.click(screen.getByRole("button", { name }));
    }

    expect(sendInput.mock.calls.map(([data]) => data)).toEqual([
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
  });
});

it("latches Shift for Tab and releases it after the key is sent", async () => {
  const user = userEvent.setup();
  const sendInput = vi.fn();
  render(<TerminalKeysBar sendInput={sendInput} />);
  const shift = screen.getByRole("button", { name: "Shift" });
  await user.click(shift);
  expect(shift).toHaveAttribute("aria-pressed", "true");
  await user.click(screen.getByRole("button", { name: "Tab" }));
  expect(sendInput).toHaveBeenLastCalledWith("\x1b[Z");
  expect(shift).toHaveAttribute("aria-pressed", "false");
  await user.click(screen.getByRole("button", { name: "Tab" }));
  expect(sendInput).toHaveBeenLastCalledWith("\t");
});
