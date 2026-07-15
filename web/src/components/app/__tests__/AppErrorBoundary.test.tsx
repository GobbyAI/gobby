import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AppErrorBoundary } from "../AppErrorBoundary";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("AppErrorBoundary", () => {
  it("recovers when Return to Chat is clicked after a chat-tab crash", () => {
    vi.spyOn(console, "error").mockImplementation(() => undefined);
    const onReturnToChat = vi.fn();
    let shouldThrow = true;
    const suppressExpectedError = (event: ErrorEvent) => event.preventDefault();
    window.addEventListener("error", suppressExpectedError);

    function Chat() {
      if (shouldThrow) {
        throw new Error("chat crashed");
      }
      return <div>Chat recovered</div>;
    }

    try {
      render(
        <AppErrorBoundary activeTab="chat" onReturnToChat={onReturnToChat}>
          <Chat />
        </AppErrorBoundary>,
      );

      expect(screen.getByText("Something went wrong")).toBeTruthy();
      shouldThrow = false;
      fireEvent.click(screen.getByRole("button", { name: "Return to Chat" }));

      expect(onReturnToChat).toHaveBeenCalledOnce();
      expect(screen.getByText("Chat recovered")).toBeTruthy();
    } finally {
      window.removeEventListener("error", suppressExpectedError);
    }
  });
});
