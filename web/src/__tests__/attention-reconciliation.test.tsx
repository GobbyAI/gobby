import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useAgentRuns } from "../hooks/useAgentRuns";

type AgentEventHandler = (data: Record<string, unknown>) => void;

const websocket = vi.hoisted(() => ({
  handler: null as AgentEventHandler | null,
  order: [] as string[],
}));

vi.mock("../hooks/useWebSocketEvent", () => ({
  useWebSocketEvent: (eventType: string, handler: AgentEventHandler) => {
    if (eventType === "agent_event") {
      websocket.handler = handler;
      if (!websocket.order.includes("subscribe")) websocket.order.push("subscribe");
    }
  },
}));

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

function deferredResponse() {
  let resolve!: (response: Response) => void;
  const promise = new Promise<Response>((next) => {
    resolve = next;
  });
  return { promise, resolve };
}

function attentionEvent(overrides: Record<string, unknown>): Record<string, unknown> {
  return {
    type: "agent_event",
    event: "attention_changed",
    epoch: "epoch-a",
    seq: 11,
    entry_id: "run:run-1",
    run_id: "run-1",
    session_id: "session-1",
    attention_id: "attention-1",
    state: "blocked",
    reason: "Approval required",
    kind: "approval",
    fingerprint: "approval:1",
    payload: {},
    since: "2026-07-22T09:00:00Z",
    seen_at: null,
    ...overrides,
  };
}

describe("useAgentRuns attention reconciliation", () => {
  beforeEach(() => {
    websocket.handler = null;
    websocket.order = [];
  });

  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("buffers subscribe-first transitions, applies them in cursor order, and refetches on epoch change", async () => {
    const initialRoster = deferredResponse();
    let rosterRequests = 0;
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.startsWith("/api/agents/runs?")) {
        return Promise.resolve(jsonResponse({ runs: [] }));
      }
      if (url === "/api/attention/roster") {
        rosterRequests += 1;
        websocket.order.push(`roster-${rosterRequests}`);
        if (rosterRequests === 1) return initialRoster.promise;
        return Promise.resolve(
          jsonResponse({
            epoch: "epoch-b",
            seq: 3,
            entries: [
              {
                entry_id: "run:run-1",
                run_id: "run-1",
                session_id: "session-1",
                attention: {
                  attention_id: "attention-1",
                  state: "blocked",
                  reason: "Approval required",
                },
              },
              {
                entry_id: "run:run-2",
                run_id: "run-2",
                session_id: "session-1",
                attention: {
                  attention_id: "attention-2",
                  state: "blocked",
                  reason: "Operator input required",
                },
              },
            ],
          }),
        );
      }
      throw new Error(`Unexpected request: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    const { result } = renderHook(() => useAgentRuns());

    expect(websocket.handler).not.toBeNull();
    await waitFor(() => expect(rosterRequests).toBe(1));
    expect(websocket.order.indexOf("subscribe")).toBeLessThan(websocket.order.indexOf("roster-1"));

    act(() => {
      websocket.handler?.(attentionEvent({ seq: 11 }));
      websocket.handler?.(attentionEvent({ seq: 9, state: null, reason: null }));
    });
    initialRoster.resolve(
      jsonResponse({
        epoch: "epoch-a",
        seq: 10,
        entries: [
          {
            entry_id: "run:run-1",
            run_id: "run-1",
            session_id: "session-1",
            attention: null,
          },
        ],
      }),
    );

    await waitFor(() => {
      expect(result.current.attentionBySession.get("session-1")).toMatchObject({
        count: 1,
        reasons: ["Approval required"],
      });
    });

    act(() => {
      websocket.handler?.(
        attentionEvent({
          epoch: "epoch-b",
          seq: 4,
          state: null,
          reason: null,
        }),
      );
    });

    await waitFor(() => expect(rosterRequests).toBe(2));
    await waitFor(() => {
      expect(result.current.attentionBySession.get("session-1")).toMatchObject({
        count: 1,
        reasons: ["Operator input required"],
      });
    });
  });
});
