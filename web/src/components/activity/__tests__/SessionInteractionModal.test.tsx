import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { SessionInteractionModal } from "../SessionInteractionModal";

const ENTRY = {
  id: "target-session",
  type: "cli" as const,
  label: "Target session",
  hasTmux: false,
};

describe("SessionInteractionModal", () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    fetchMock.mockResolvedValue({
      ok: true,
      json: async () => ({ success: true }),
    });
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.clearAllMocks();
  });

  async function sendContext(fromSessionId?: string) {
    render(
      <SessionInteractionModal
        open
        onClose={vi.fn()}
        mode="context"
        entry={ENTRY}
        fromSessionId={fromSessionId}
      />,
    );

    fireEvent.change(screen.getByRole("textbox"), {
      target: { value: "Useful context" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledOnce());
    const request = fetchMock.mock.calls[0][1] as RequestInit;
    return JSON.parse(String(request.body));
  }

  it("omits from_session when no web chat session is active", async () => {
    const body = await sendContext();

    expect(body.arguments).toEqual({
      target: "session",
      target_id: "target-session",
      content: "Useful context",
    });
  });

  it("sends the active web chat session as from_session", async () => {
    const body = await sendContext("web-chat-session");

    expect(body.arguments).toEqual({
      from_session: "web-chat-session",
      target: "session",
      target_id: "target-session",
      content: "Useful context",
    });
  });
});
