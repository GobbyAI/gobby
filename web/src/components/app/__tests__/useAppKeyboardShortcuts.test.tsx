import { renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useAppKeyboardShortcuts } from "../useAppKeyboardShortcuts";

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

function pressCmdK({ defaultPrevented = false } = {}) {
  const event = new KeyboardEvent("keydown", {
    key: "k",
    metaKey: true,
    cancelable: true,
    bubbles: true,
  });
  if (defaultPrevented) event.preventDefault();
  window.dispatchEvent(event);
}

describe("useAppKeyboardShortcuts", () => {
  it("opens the command palette on an unclaimed Cmd+K", () => {
    const opened = vi.fn();
    window.addEventListener("gobby:open-command-palette", opened);
    renderHook(() => useAppKeyboardShortcuts({ setQuickCaptureOpen: vi.fn() }));

    pressCmdK();
    vi.advanceTimersByTime(300);

    expect(opened).toHaveBeenCalledTimes(1);
    window.removeEventListener("gobby:open-command-palette", opened);
  });

  it("leaves Cmd+K alone when an inner surface already claimed it", () => {
    const opened = vi.fn();
    window.addEventListener("gobby:open-command-palette", opened);
    renderHook(() => useAppKeyboardShortcuts({ setQuickCaptureOpen: vi.fn() }));

    // A scoped palette (e.g. wiki quick-open) preventDefaults the event; the
    // app-level chord must not also fire and stack a second dialog on top.
    pressCmdK({ defaultPrevented: true });
    vi.advanceTimersByTime(300);

    expect(opened).not.toHaveBeenCalled();
    window.removeEventListener("gobby:open-command-palette", opened);
  });
});
