import { renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { useSessionAttention } from "../hooks/useSessionAttention";

vi.mock("../hooks/useWebSocketEvent", () => ({
  useWebSocketEvent: () => {},
  useWebSocketConnected: () => true,
}));

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

describe("useSessionAttention roster labels", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("keeps each session's definition, task claim and status line from the roster", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        jsonResponse({
          epoch: "epoch-a",
          seq: 1,
          entries: [
            {
              entry_id: "session:session-1",
              session_id: "session-1",
              attention: null,
              definition: "dev-team-lead",
              status_line: "Reviewing diff",
              task: {
                id: "task-1",
                ref: "#25767",
                title: "Web UI: replace titles",
                claim_holder: true,
              },
            },
            {
              entry_id: "session:session-2",
              session_id: "session-2",
              attention: null,
              definition: "developer",
              status_line: null,
              task: {
                id: "task-1",
                ref: "#25767",
                title: "Web UI: replace titles",
                claim_holder: false,
              },
            },
            {
              entry_id: "run:run-3",
              session_id: null,
              attention: null,
              definition: "orphan",
              status_line: null,
              task: null,
            },
          ],
        }),
      ),
    );

    const { result } = renderHook(() => useSessionAttention());

    await waitFor(() => expect(result.current.rosterBySession.size).toBe(2));
    expect(result.current.rosterBySession.get("session-1")).toEqual({
      definition: "dev-team-lead",
      task: {
        ref: "#25767",
        title: "Web UI: replace titles",
        claimHolder: true,
      },
      statusLine: "Reviewing diff",
    });
    expect(
      result.current.rosterBySession.get("session-2")?.task?.claimHolder,
    ).toBe(false);
    expect(result.current.rosterBySession.has("run-3")).toBe(false);
  });
});
