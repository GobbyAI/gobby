import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, act } from "@testing-library/react";
import {
  createMockWebSocket,
  type MockWebSocketInstance,
} from "../../test/mocks/websocket";

// The module uses module-level singleton state, so we need to reset it between tests.
// We re-import after resetting modules.
let useWebSocketEvent: typeof import("../useWebSocketEvent").useWebSocketEvent;
let useWebSocketConnected: typeof import("../useWebSocketEvent").useWebSocketConnected;
let mockWs: {
  instances: MockWebSocketInstance[];
  MockWebSocket: typeof WebSocket;
  restore: () => void;
};

beforeEach(() => {
  mockWs = createMockWebSocket();
  vi.useFakeTimers();
});

afterEach(() => {
  mockWs.restore();
  vi.unstubAllGlobals();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

// Helper: dynamically import the module fresh each test to reset singleton state
async function loadModule() {
  vi.resetModules();
  const mod = await import("../useWebSocketEvent");
  useWebSocketEvent = mod.useWebSocketEvent;
  useWebSocketConnected = mod.useWebSocketConnected;
}

describe("useWebSocketEvent", () => {
  it("creates a WebSocket connection on mount", async () => {
    await loadModule();
    const handler = vi.fn();
    renderHook(() => useWebSocketEvent("task_event", handler));

    expect(mockWs.instances).toHaveLength(1);
    expect(mockWs.instances[0].url).toContain("/ws");
  });

  it("sends subscribe message on open", async () => {
    await loadModule();
    const handler = vi.fn();
    renderHook(() => useWebSocketEvent("task_event", handler));

    const ws = mockWs.instances[0];
    act(() => ws.simulateOpen());

    expect(ws.send).toHaveBeenCalledWith(
      expect.stringContaining('"type":"subscribe"'),
    );
    const payload = JSON.parse(ws.send.mock.calls[0][0]);
    expect(payload.events).toContain("task_event");
  });

  it("dispatches messages to matching handler", async () => {
    await loadModule();
    const handler = vi.fn();
    renderHook(() => useWebSocketEvent("task_event", handler));

    const ws = mockWs.instances[0];
    act(() => ws.simulateOpen());
    act(() => ws.simulateMessage({ type: "task_event", id: "1" }));

    expect(handler).toHaveBeenCalledWith({ type: "task_event", id: "1" });
  });

  it("does not dispatch messages to non-matching handler", async () => {
    await loadModule();
    const taskHandler = vi.fn();
    const sessionHandler = vi.fn();
    renderHook(() => {
      useWebSocketEvent("task_event", taskHandler);
      useWebSocketEvent("session_event", sessionHandler);
    });

    const ws = mockWs.instances[0];
    act(() => ws.simulateOpen());
    act(() => ws.simulateMessage({ type: "task_event", data: "hello" }));

    expect(taskHandler).toHaveBeenCalledTimes(1);
    expect(sessionHandler).not.toHaveBeenCalled();
  });

  it("multiple handlers for the same event type all fire", async () => {
    await loadModule();
    const handler1 = vi.fn();
    const handler2 = vi.fn();

    const { unmount: unmount1 } = renderHook(() =>
      useWebSocketEvent("task_event", handler1),
    );
    renderHook(() => useWebSocketEvent("task_event", handler2));

    const ws = mockWs.instances[0];
    act(() => ws.simulateOpen());
    act(() => ws.simulateMessage({ type: "task_event", id: "1" }));

    expect(handler1).toHaveBeenCalledTimes(1);
    expect(handler2).toHaveBeenCalledTimes(1);

    // After unmounting one handler, only the other fires
    unmount1();
    act(() => ws.simulateMessage({ type: "task_event", id: "2" }));

    expect(handler1).toHaveBeenCalledTimes(1);
    expect(handler2).toHaveBeenCalledTimes(2);
  });

  it("closes WebSocket when all handlers unmount", async () => {
    await loadModule();
    const handler = vi.fn();
    const { unmount } = renderHook(() =>
      useWebSocketEvent("task_event", handler),
    );

    const ws = mockWs.instances[0];
    act(() => ws.simulateOpen());

    unmount();
    expect(ws.close).toHaveBeenCalled();
  });

  it("reconnects on close with exponential backoff", async () => {
    await loadModule();
    vi.spyOn(Math, "random").mockReturnValue(0.5);
    const handler = vi.fn();
    renderHook(() => useWebSocketEvent("task_event", handler));

    const ws = mockWs.instances[0];
    act(() => ws.simulateOpen());

    // Simulate close
    act(() => ws.simulateClose());

    // Should not reconnect immediately
    expect(mockWs.instances).toHaveLength(1);

    // Advance timer past the base delay (1000ms + jitter)
    act(() => vi.advanceTimersByTime(2000));

    expect(mockWs.instances).toHaveLength(2);
  });

  it("does not reset backoff when a socket opens then closes immediately", async () => {
    await loadModule();
    vi.spyOn(Math, "random").mockReturnValue(0.5);
    renderHook(() => useWebSocketEvent("task_event", vi.fn()));

    act(() => mockWs.instances[0].simulateOpen());
    act(() => mockWs.instances[0].simulateClose());
    act(() => vi.advanceTimersByTime(1000));
    expect(mockWs.instances).toHaveLength(2);

    act(() => mockWs.instances[1].simulateOpen());
    act(() => mockWs.instances[1].simulateClose());
    act(() => vi.advanceTimersByTime(1000));
    expect(mockWs.instances).toHaveLength(2);
    act(() => vi.advanceTimersByTime(1000));
    expect(mockWs.instances).toHaveLength(3);
  });

  it("does not tight-loop reconnects while /ws never stays open", async () => {
    await loadModule();
    vi.spyOn(Math, "random").mockReturnValue(0.5);
    renderHook(() => useWebSocketEvent("task_event", vi.fn()));

    const closeLatest = () => {
      const socket = mockWs.instances[mockWs.instances.length - 1];
      if (socket && socket.readyState !== WebSocket.CLOSED) {
        socket.simulateClose(4401, "unauthorized");
      }
    };

    act(() => {
      mockWs.instances[0].simulateOpen();
      closeLatest();
    });
    for (let second = 0; second < 60; second += 1) {
      act(() => {
        vi.advanceTimersByTime(1000);
        closeLatest();
      });
    }

    expect(mockWs.instances.length).toBeLessThan(12);
  });

  it("ignores callbacks from a socket retired by an event type change", async () => {
    await loadModule();
    const handler = vi.fn();
    const { rerender, result } = renderHook(
      ({ eventType }) => {
        useWebSocketEvent(eventType, handler);
        return useWebSocketConnected();
      },
      { initialProps: { eventType: "task_event" } },
    );

    const retired = mockWs.instances[0];
    act(() => retired.simulateOpen());

    rerender({ eventType: "session_event" });
    const current = mockWs.instances[1];
    act(() => current.simulateOpen());
    // Stable-open backoff reset is not a reconnect timer.
    act(() => vi.advanceTimersByTime(1000));

    act(() => {
      retired.simulateError();
      retired.simulateClose();
    });

    expect(result.current).toBe(true);
    expect(vi.getTimerCount()).toBe(0);
    expect(mockWs.instances).toHaveLength(2);
  });

  it("does not reconnect after a late close callback following unmount", async () => {
    await loadModule();
    const { unmount } = renderHook(() =>
      useWebSocketEvent("task_event", vi.fn()),
    );
    const retired = mockWs.instances[0];

    act(() => retired.simulateOpen());
    unmount();
    act(() => retired.simulateClose());
    act(() => vi.advanceTimersByTime(2000));

    expect(vi.getTimerCount()).toBe(0);
    expect(mockWs.instances).toHaveLength(1);
  });

  it("ignores malformed JSON messages", async () => {
    await loadModule();
    const handler = vi.fn();
    renderHook(() => useWebSocketEvent("task_event", handler));

    const ws = mockWs.instances[0];
    act(() => ws.simulateOpen());
    act(() => ws.simulateMessage("not json {{{"));

    expect(handler).not.toHaveBeenCalled();
  });

  it("ignores messages without a type field", async () => {
    await loadModule();
    const handler = vi.fn();
    renderHook(() => useWebSocketEvent("task_event", handler));

    const ws = mockWs.instances[0];
    act(() => ws.simulateOpen());
    act(() => ws.simulateMessage({ data: "no type here" }));

    expect(handler).not.toHaveBeenCalled();
  });

  it("uses latest handler via ref (no stale closure)", async () => {
    await loadModule();
    const handler1 = vi.fn();
    const handler2 = vi.fn();

    const { rerender } = renderHook(
      ({ handler }) => useWebSocketEvent("task_event", handler),
      { initialProps: { handler: handler1 } },
    );

    const ws = mockWs.instances[0];
    act(() => ws.simulateOpen());

    // Switch to handler2
    rerender({ handler: handler2 });

    act(() => ws.simulateMessage({ type: "task_event", id: "1" }));

    expect(handler1).not.toHaveBeenCalled();
    expect(handler2).toHaveBeenCalledTimes(1);
  });

  it("resubscribes when event type changes", async () => {
    await loadModule();
    const handler = vi.fn();

    const { rerender } = renderHook(
      ({ eventType }) => useWebSocketEvent(eventType, handler),
      { initialProps: { eventType: "task_event" } },
    );

    const retired = mockWs.instances[0];
    act(() => retired.simulateOpen());

    // Change event type
    rerender({ eventType: "session_event" });
    const current = mockWs.instances[1];
    act(() => current.simulateOpen());

    // Old event should not trigger handler
    act(() => retired.simulateMessage({ type: "task_event", id: "1" }));
    expect(handler).not.toHaveBeenCalled();

    // New event should
    act(() => current.simulateMessage({ type: "session_event", id: "2" }));
    expect(handler).toHaveBeenCalledWith({ type: "session_event", id: "2" });
  });

  it("useSessionCatalog refetches after session_event messages", async () => {
    await loadModule();
    const { useSessionCatalog } = await import("../useSessionCatalog");
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          sessions: [
            {
              id: "session-1",
              title: "First session",
              status: "active",
              seq_num: 100,
              updated_at: "2026-04-28T10:00:00Z",
            },
          ],
        }),
      })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          sessions: [
            {
              id: "session-1",
              title: "First session",
              status: "active",
              seq_num: 100,
              updated_at: "2026-04-28T10:00:00Z",
            },
            {
              id: "session-2",
              title: "Second session",
              status: "active",
              seq_num: 101,
              updated_at: "2026-04-28T10:05:00Z",
            },
          ],
        }),
      });
    vi.stubGlobal("fetch", fetchMock);

    const { result } = renderHook(() => useSessionCatalog("proj-1"));

    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });

    expect(result.current.sessions.map((session) => session.id)).toEqual([
      "session-1",
    ]);

    const ws = mockWs.instances[0];
    act(() => ws.simulateOpen());
    act(() =>
      ws.simulateMessage({
        type: "session_event",
        event: "session_created",
        session_id: "session-2",
      }),
    );

    await act(async () => {
      await vi.advanceTimersByTimeAsync(499);
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1);
    });

    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(result.current.sessions.map((session) => session.id)).toEqual([
      "session-2",
      "session-1",
    ]);
    expect(fetchMock.mock.calls[1][0]).toContain(
      "/api/sessions?project_id=proj-1&limit=100",
    );
  });
});

describe("useWebSocketConnected", () => {
  it("tracks the singleton connection state across open and close", async () => {
    await loadModule();

    // An event consumer opens the singleton; the connected reader observes it.
    const { result } = renderHook(() => {
      useWebSocketEvent("pipeline_event", () => undefined);
      return useWebSocketConnected();
    });

    expect(result.current).toBe(false);
    const ws = mockWs.instances[0];

    act(() => ws.simulateOpen());
    expect(result.current).toBe(true);

    // Server-side drop: connected flips off until the reconnect succeeds.
    act(() => ws.simulateClose());
    expect(result.current).toBe(false);

    act(() => vi.advanceTimersByTime(2000));
    const reconnected = mockWs.instances[1];
    act(() => reconnected.simulateOpen());
    expect(result.current).toBe(true);
  });

  it("does not open the socket by itself", async () => {
    await loadModule();

    const { result } = renderHook(() => useWebSocketConnected());

    expect(mockWs.instances).toHaveLength(0);
    expect(result.current).toBe(false);
  });
});
