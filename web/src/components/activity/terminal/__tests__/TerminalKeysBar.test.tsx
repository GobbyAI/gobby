import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";

import { TerminalKeysBar } from "../TerminalKeysBar";
import { applyCtrlModifier } from "../terminalKeys";

function Harness({ sendInput }: { sendInput: (data: string) => void }) {
  const [ctrlArmed, setCtrlArmed] = useState(false);
  return (
    <TerminalKeysBar
      sendInput={sendInput}
      ctrlArmed={ctrlArmed}
      onCtrlArmedChange={setCtrlArmed}
    />
  );
}

describe("TerminalKeysBar", () => {
  it("exact key emissions in bar order", async () => {
    const user = userEvent.setup();
    const sendInput = vi.fn();
    render(<Harness sendInput={sendInput} />);

    // Regular typing goes straight into the focused terminal window; the bar
    // carries only the special keys an on-screen keyboard can't produce.
    expect(screen.queryByRole("textbox")).toBeNull();

    const buttons = screen
      .getAllByRole("button")
      .map((button) => button.getAttribute("aria-label") ?? button.textContent);
    expect(buttons).toEqual([
      "1",
      "2",
      "3",
      "4",
      "Up",
      "Down",
      "Left",
      "Right",
      "Shift",
      "Ctrl",
      "Esc",
      "Tab",
      "Enter",
      "Ctrl+C",
    ]);

    for (const name of buttons) {
      if (name === "Shift" || name === "Ctrl") continue;
      await user.click(screen.getByRole("button", { name: name ?? "" }));
    }

    expect(sendInput.mock.calls.map(([data]) => data)).toEqual([
      "1",
      "2",
      "3",
      "4",
      "\x1b[A",
      "\x1b[B",
      "\x1b[D",
      "\x1b[C",
      "\x1b",
      "\t",
      "\r",
      "\x03",
    ]);
  });

  it("latches Shift for Tab and releases it after the key is sent", async () => {
    const user = userEvent.setup();
    const sendInput = vi.fn();
    render(<Harness sendInput={sendInput} />);
    const shift = screen.getByRole("button", { name: "Shift" });
    await user.click(shift);
    expect(shift).toHaveAttribute("aria-pressed", "true");
    await user.click(screen.getByRole("button", { name: "Tab" }));
    expect(sendInput).toHaveBeenLastCalledWith("\x1b[Z");
    expect(shift).toHaveAttribute("aria-pressed", "false");
    await user.click(screen.getByRole("button", { name: "Tab" }));
    expect(sendInput).toHaveBeenLastCalledWith("\t");
  });

  it("latches Ctrl for arrows and releases it after the key is sent", async () => {
    const user = userEvent.setup();
    const sendInput = vi.fn();
    render(<Harness sendInput={sendInput} />);
    const ctrl = screen.getByRole("button", { name: "Ctrl" });
    await user.click(ctrl);
    expect(ctrl).toHaveAttribute("aria-pressed", "true");
    await user.click(screen.getByRole("button", { name: "Left" }));
    expect(sendInput).toHaveBeenLastCalledWith("\x1b[1;5D");
    expect(ctrl).toHaveAttribute("aria-pressed", "false");

    // Shift + Ctrl compose into the xterm modifier 6.
    await user.click(screen.getByRole("button", { name: "Shift" }));
    await user.click(ctrl);
    await user.click(screen.getByRole("button", { name: "Up" }));
    expect(sendInput).toHaveBeenLastCalledWith("\x1b[1;6A");
  });
});

describe("applyCtrlModifier", () => {
  it("maps letters and C0 punctuation to control codes", () => {
    expect(applyCtrlModifier("a")).toBe("\x01");
    expect(applyCtrlModifier("Z")).toBe("\x1a");
    expect(applyCtrlModifier("[")).toBe("\x1b");
    expect(applyCtrlModifier("_")).toBe("\x1f");
    expect(applyCtrlModifier("?")).toBe("\x7f");
  });

  it("adds the Ctrl parameter to arrows and passes everything else through", () => {
    expect(applyCtrlModifier("\x1b[C")).toBe("\x1b[1;5C");
    expect(applyCtrlModifier("\x1b[1;2B")).toBe("\x1b[1;6B");
    expect(applyCtrlModifier("1")).toBe("1");
    expect(applyCtrlModifier("\x1b[24;80R")).toBe("\x1b[24;80R");
  });
});
