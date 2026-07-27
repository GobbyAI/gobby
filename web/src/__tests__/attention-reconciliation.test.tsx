import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useSessionAttention } from "../hooks/useSessionAttention";

type AgentEventHandler = (data: Record<string, unknown>) => void;

const websocket = vi.hoisted(() => ({
  handler: null as AgentEventHandler | null,
  connected: true,
  order: [] as string[],
}));

vi.mock("../hooks/useWebSocketEvent", () => ({
  useWebSocketEvent: (eventType: string, handler: AgentEventHandler) => {
    if (eventType === "agent_event") {
      websocket.handler = handler;
      if (!websocket.order.includes("subscribe")) websocket.order.push("subscribe");
    }
  },
  useWebSocketConnected: () => websocket.connected,
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

describe("useSessionAttention reconciliation", () => {
  beforeEach(() => {
    websocket.handler = null;
    websocket.connected = true;
    websocket.order = [];
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("buffers subscribe-first transitions, applies them in cursor order, and refetches on epoch change", async () => {
    const initialRoster = deferredResponse();
    let rosterRequests = 0;
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
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

    const { result } = renderHook(() => useSessionAttention());

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

  it("refetches the roster after a WebSocket reconnect", async () => {
    let rosterRequests = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(() => {
        rosterRequests += 1;
        return Promise.resolve(
          jsonResponse({
            epoch: `epoch-${rosterRequests}`,
            seq: 1,
            entries:
              rosterRequests === 1
                ? [
                    {
                      entry_id: "run:run-1",
                      session_id: "session-1",
                      attention: {
                        state: "blocked",
                        reason: "Approval required",
                      },
                    },
                  ]
                : [],
          }),
        );
      }),
    );

    const { result, rerender } = renderHook(() => useSessionAttention());
    await waitFor(() => {
      expect(result.current.attentionBySession.get("session-1")?.count).toBe(1);
    });

    websocket.connected = false;
    rerender();
    websocket.connected = true;
    rerender();

    await waitFor(() => expect(rosterRequests).toBe(2));
    await waitFor(() => {
      expect(result.current.attentionBySession.has("session-1")).toBe(false);
    });
  });

  it("drops stale-epoch events after one delayed resync", async () => {
    const initialRoster = deferredResponse();
    let rosterRequests = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(() => {
        rosterRequests += 1;
        if (rosterRequests === 1) return initialRoster.promise;
        return Promise.resolve(
          jsonResponse({
            epoch: "epoch-a",
            seq: 10,
            entries: [],
          }),
        );
      }),
    );

    const { result } = renderHook(() => useSessionAttention());
    await waitFor(() => expect(rosterRequests).toBe(1));
    act(() => {
      websocket.handler?.(
        attentionEvent({
          epoch: "stale-epoch",
          seq: 1,
        }),
      );
    });
    initialRoster.resolve(
      jsonResponse({
        epoch: "epoch-a",
        seq: 10,
        entries: [],
      }),
    );

    await waitFor(() => expect(rosterRequests).toBe(2));
    await new Promise((resolve) => window.setTimeout(resolve, 100));
    expect(rosterRequests).toBe(2);
    expect(result.current.attentionBySession.size).toBe(0);
  });

  it("polls the roster so passive TTL expiry removes stale attention", async () => {
    vi.useFakeTimers();
    let rosterRequests = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(() => {
        rosterRequests += 1;
        return Promise.resolve(
          jsonResponse({
            epoch: "epoch-a",
            seq: rosterRequests,
            entries:
              rosterRequests === 1
                ? [
                    {
                      entry_id: "run:run-1",
                      session_id: "session-1",
                      attention: {
                        state: "blocked",
                        reason: "Approval required",
                      },
                    },
                  ]
                : [],
          }),
        );
      }),
    );

    const { result } = renderHook(() => useSessionAttention());
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(result.current.attentionBySession.get("session-1")?.count).toBe(1);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(5_000);
    });

    expect(rosterRequests).toBe(2);
    expect(result.current.attentionBySession.has("session-1")).toBe(false);
  });
});
