import { act, renderHook } from "@testing-library/react";
import { StrictMode, createElement, type ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  createMockWebSocket,
  type MockWebSocketInstance,
} from "../../test/mocks/websocket";
import { TMUX_REQUEST_TIMEOUT_MS, useTmuxSessions } from "../useTmuxSessions";

type WireMessage = Record<string, unknown>;

let mockWs: {
  instances: MockWebSocketInstance[];
  MockWebSocket: typeof WebSocket;
  restore: () => void;
};

function sentMessages(ws: MockWebSocketInstance, type?: string): WireMessage[] {
  const messages = ws.send.mock.calls.map(
    ([payload]) => JSON.parse(payload as string) as WireMessage,
  );
  return type ? messages.filter((message) => message.type === type) : messages;
}

function requestId(ws: MockWebSocketInstance, type: string): string {
  const messages = sentMessages(ws, type);
  return messages[messages.length - 1]?.request_id as string;
}

function open(ws: MockWebSocketInstance): void {
  act(() => ws.simulateOpen());
}

function respondToAttach(
  ws: MockWebSocketInstance,
  id: string,
  name: string,
  socket: string,
  streamingId: string,
): void {
  act(() => {
    ws.simulateMessage({
      type: "terminal_attach_result",
      request_id: id,
      success: true,
      attachment_id: streamingId,
      terminal_id: name || socket,
    });
  });
}

beforeEach(() => {
  vi.useFakeTimers();
  mockWs = createMockWebSocket();
});

afterEach(() => {
  mockWs.restore();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("useTmuxSessions", () => {
  it("reconnect generation guard", () => {
    const wrapper = ({ children }: { children: ReactNode }) =>
      createElement(StrictMode, null, children);
    const liveMount = renderHook(() => useTmuxSessions(), { wrapper });
    const [retired, live] = mockWs.instances;
    const retiredOnClose = retired.onclose;

    expect(liveMount.result.current.connected).toBe(false);
    expect(liveMount.result.current.sessionsLoaded).toBe(false);
    expect(retired.close).toHaveBeenCalledOnce();
    expect(
      mockWs.instances.filter(
        (socket) => socket.readyState !== WebSocket.CLOSED,
      ),
    ).toEqual([live]);

    open(live);
    expect(liveMount.result.current.connected).toBe(true);
    act(() => {
      live.simulateMessage({
        type: "terminal_list",
        sessions: [{ name: "worker", socket: "default" }],
      });
    });
    expect(liveMount.result.current.sessionsLoaded).toBe(true);

    act(() => liveMount.result.current.attachSession("worker", "default"));
    respondToAttach(
      live,
      requestId(live, "terminal_attach"),
      "worker",
      "default",
      "stream-1",
    );
    expect(liveMount.result.current.attachedTarget).toEqual({
      terminal_id: "worker",
    });

    act(() => {
      retiredOnClose?.(new CloseEvent("close"));
      vi.advanceTimersByTime(2_000);
    });
    expect(mockWs.instances).toHaveLength(2);

    act(() => live.simulateClose());
    expect(liveMount.result.current.connected).toBe(false);
    expect(liveMount.result.current.sessionsLoaded).toBe(false);
    expect(liveMount.result.current.attachedTarget).toBeNull();
    expect(liveMount.result.current.streamingId).toBeNull();

    act(() => vi.advanceTimersByTime(1_999));
    expect(mockWs.instances).toHaveLength(2);
    act(() => vi.advanceTimersByTime(1));
    expect(mockWs.instances).toHaveLength(3);

    const reconnect = mockWs.instances[2];
    const reconnectOnClose = reconnect.onclose;
    liveMount.unmount();
    act(() => {
      reconnectOnClose?.(new CloseEvent("close"));
      vi.advanceTimersByTime(2_000);
    });
    expect(mockWs.instances).toHaveLength(3);
  });

  it("socket qualified identity", () => {
    const { result, unmount } = renderHook(() => useTmuxSessions());
    const ws = mockWs.instances[0];
    open(ws);
    ws.send.mockClear();

    act(() => result.current.attachSession("term-default", "default"));
    respondToAttach(
      ws,
      requestId(ws, "terminal_attach"),
      "term-default",
      "default",
      "stream-default",
    );
    expect(result.current.attachedTarget).toEqual({
      terminal_id: "term-default",
    });

    ws.send.mockClear();
    act(() => result.current.attachSession("term-gobby", "gobby"));
    expect(sentMessages(ws, "terminal_detach")[0]).toMatchObject({
      type: "terminal_detach",
      attachment_id: "stream-default",
    });

    const detachId = requestId(ws, "terminal_detach");
    act(() =>
      ws.simulateMessage({
        type: "terminal_detach_result",
        request_id: detachId,
        success: true,
      }),
    );
    const attachMessages = sentMessages(ws, "terminal_attach");
    expect(attachMessages[attachMessages.length - 1]).toMatchObject({
      type: "terminal_attach",
      terminal_id: "term-gobby",
    });

    respondToAttach(
      ws,
      requestId(ws, "terminal_attach"),
      "term-gobby",
      "gobby",
      "stream-gobby",
    );
    expect(result.current.attachedTarget).toEqual({
      terminal_id: "term-gobby",
    });
    ws.send.mockClear();
    act(() => result.current.sendInput("pwd\r"));
    expect(sentMessages(ws, "terminal_input")[0]).toMatchObject({
      type: "terminal_input",
      attachment_id: "stream-gobby",
      data: "pwd\r",
    });
    unmount();
  });

  it("wire payloads", () => {
    const { result, unmount } = renderHook(() => useTmuxSessions());
    const ws = mockWs.instances[0];

    act(() => {
      result.current.attachSession("worker", "default");
      result.current.sendInput("\u001b");
      result.current.resizeTerminal(42, 120);
    });
    expect(ws.send).not.toHaveBeenCalled();

    open(ws);
    ws.send.mockClear();
    act(() => {
      result.current.sendInput("\u001b");
      result.current.resizeTerminal(42, 120);
      result.current.refreshTerminal("default-worker", "default");
      result.current.refreshTerminal("gobby-worker", "gobby");
    });
    expect(sentMessages(ws, "terminal_set_viewport")).toEqual([
      {
        type: "terminal_set_viewport",
        request_id: expect.any(String),
        terminal_id: "default-worker",
      },
      {
        type: "terminal_set_viewport",
        request_id: expect.any(String),
        terminal_id: "gobby-worker",
      },
    ]);

    ws.send.mockClear();
    act(() => result.current.attachSession("worker", "gobby"));
    expect(sentMessages(ws, "terminal_attach").at(-1)).toMatchObject({
      type: "terminal_attach",
      terminal_id: "worker",
    });
    respondToAttach(
      ws,
      requestId(ws, "terminal_attach"),
      "worker",
      "gobby",
      "stream-wire",
    );

    ws.send.mockClear();
    act(() => {
      result.current.sendInput("\u001b[A");
      result.current.resizeTerminal(42, 120);
    });
    expect(sentMessages(ws, "terminal_input").at(-1)).toMatchObject({
      type: "terminal_input",
      attachment_id: "stream-wire",
      data: "\u001b[A",
    });
    expect(sentMessages(ws, "terminal_resize").at(-1)).toMatchObject({
      type: "terminal_resize",
      attachment_id: "stream-wire",
      rows: 42,
      cols: 120,
    });

    act(() => ws.simulateClose());
    ws.send.mockClear();
    act(() => {
      result.current.sendInput("ignored");
      result.current.resizeTerminal(24, 80);
    });
    expect(ws.send).not.toHaveBeenCalled();
    unmount();
  });

  it("attach error handling", () => {
    const { result, unmount } = renderHook(() => useTmuxSessions());
    const ws = mockWs.instances[0];
    open(ws);
    ws.send.mockClear();

    act(() => result.current.attachSession("worker", "default"));
    const failedAttachId = requestId(ws, "terminal_attach");
    act(() =>
      ws.simulateMessage({
        type: "error",
        request_id: failedAttachId,
        message: "Attach failed",
      }),
    );
    expect(result.current.requestPending).toBe(false);
    expect(result.current.isLoading).toBe(false);
    expect(result.current.attachError).toBe("Attach failed");

    act(() => result.current.clearAttachError());
    expect(result.current.attachError).toBeNull();
    act(() => result.current.attachSession("worker", "default"));
    respondToAttach(
      ws,
      requestId(ws, "terminal_attach"),
      "worker",
      "default",
      "stream-retry",
    );
    expect(result.current.requestPending).toBe(false);

    act(() => result.current.detachSession());
    const failedDetachId = requestId(ws, "terminal_detach");
    act(() =>
      ws.simulateMessage({
        type: "error",
        request_id: failedDetachId,
        message: "Detach failed",
      }),
    );
    expect(result.current.requestPending).toBe(false);
    expect(result.current.attachError).toBe("Detach failed");
    expect(result.current.streamingId).toBe("stream-retry");

    act(() => result.current.detachSession());
    expect(result.current.attachError).toBeNull();
    const retryDetachId = requestId(ws, "terminal_detach");
    act(() =>
      ws.simulateMessage({
        type: "terminal_detach_result",
        request_id: retryDetachId,
        success: true,
      }),
    );
    expect(result.current.requestPending).toBe(false);
    expect(result.current.attachedTarget).toBeNull();
    expect(result.current.streamingId).toBeNull();
    unmount();
  });

  it("duplicate send suppression", () => {
    const { result, unmount } = renderHook(() => useTmuxSessions());
    const ws = mockWs.instances[0];
    open(ws);
    ws.send.mockClear();

    act(() => {
      result.current.attachSession("worker", "default");
      result.current.attachSession("worker", "default");
      result.current.attachSession("other", "gobby");
      result.current.detachSession();
    });
    expect(sentMessages(ws, "terminal_attach")).toHaveLength(1);
    expect(sentMessages(ws, "terminal_detach")).toHaveLength(0);
    expect(result.current.requestPending).toBe(true);

    respondToAttach(
      ws,
      requestId(ws, "terminal_attach"),
      "worker",
      "default",
      "stream-pending",
    );
    ws.send.mockClear();
    act(() => {
      result.current.detachSession();
      result.current.detachSession();
      result.current.attachSession("other", "gobby");
    });
    expect(sentMessages(ws, "terminal_detach")).toHaveLength(1);
    expect(sentMessages(ws, "terminal_attach")).toHaveLength(0);
    expect(result.current.requestPending).toBe(true);
    unmount();
  });

  it("correlates create results and suppresses duplicate create sends", () => {
    const { result, unmount } = renderHook(() => useTmuxSessions());
    const ws = mockWs.instances[0];
    open(ws);
    ws.send.mockClear();

    act(() => {
      result.current.createSession();
      result.current.createSession();
    });

    expect(sentMessages(ws, "terminal_create")[0]).toMatchObject({
      type: "terminal_create",
      rows: 24,
      cols: 80,
    });
    expect(result.current.requestPending).toBe(true);
    expect(result.current.createdSession).toBeNull();

    const createId = requestId(ws, "terminal_create");
    act(() =>
      ws.simulateMessage({
        type: "terminal_create_result",
        request_id: "stale-create",
        success: true,
        terminal_id: "web-stale",
      }),
    );
    expect(result.current.requestPending).toBe(true);
    expect(result.current.createdSession).toBeNull();

    act(() =>
      ws.simulateMessage({
        type: "terminal_create_result",
        request_id: createId,
        success: true,
        terminal_id: "web-new",
      }),
    );
    expect(result.current.requestPending).toBe(false);
    expect(result.current.isLoading).toBe(false);
    expect(result.current.createdSession).toEqual({
      terminal_id: "web-new",
    });
    expect(sentMessages(ws, "terminal_list")).toHaveLength(1);
    unmount();
  });

  it("routes a correlated create error through terminal request state", () => {
    const { result, unmount } = renderHook(() => useTmuxSessions());
    const ws = mockWs.instances[0];
    open(ws);
    ws.send.mockClear();

    act(() => result.current.createSession());
    const createId = requestId(ws, "terminal_create");
    act(() =>
      ws.simulateMessage({
        type: "error",
        request_id: createId,
        message: "Create failed",
      }),
    );

    expect(result.current.requestPending).toBe(false);
    expect(result.current.isLoading).toBe(false);
    expect(result.current.attachError).toBe("Create failed");
    expect(result.current.createdSession).toBeNull();
    unmount();
  });

  it("times out correlated attach, detach, and create requests", () => {
    const { result, unmount } = renderHook(() => useTmuxSessions());
    const ws = mockWs.instances[0];
    open(ws);
    ws.send.mockClear();

    act(() => result.current.attachSession("worker", "default"));
    act(() => vi.advanceTimersByTime(TMUX_REQUEST_TIMEOUT_MS));
    expect(result.current.requestPending).toBe(false);
    expect(result.current.isLoading).toBe(false);
    expect(result.current.attachError).toBe("attach request timed out");

    act(() => {
      result.current.clearAttachError();
      result.current.attachSession("worker", "default");
    });
    respondToAttach(
      ws,
      requestId(ws, "terminal_attach"),
      "worker",
      "default",
      "stream-timeout",
    );
    act(() => result.current.detachSession());
    act(() => vi.advanceTimersByTime(TMUX_REQUEST_TIMEOUT_MS));
    expect(result.current.requestPending).toBe(false);
    expect(result.current.isLoading).toBe(false);
    expect(result.current.attachError).toBe("detach request timed out");
    expect(result.current.streamingId).toBe("stream-timeout");

    act(() => {
      result.current.clearAttachError();
      result.current.createSession();
    });
    act(() => vi.advanceTimersByTime(TMUX_REQUEST_TIMEOUT_MS));
    expect(result.current.requestPending).toBe(false);
    expect(result.current.isLoading).toBe(false);
    expect(result.current.attachError).toBe("create request timed out");
    unmount();
  });

  it("keeps loading active when list and kill results arrive during a request", () => {
    const { result, unmount } = renderHook(() => useTmuxSessions());
    const ws = mockWs.instances[0];
    open(ws);

    act(() => result.current.attachSession("worker", "default"));
    const attachId = requestId(ws, "terminal_attach");
    expect(result.current.isLoading).toBe(true);

    act(() => {
      ws.simulateMessage({
        type: "terminal_list",
        sessions: [{ name: "worker", socket: "default" }],
      });
      ws.simulateMessage({
        type: "terminal_kill_result",
        request_id: "unrelated-kill",
        success: true,
      });
    });
    expect(result.current.isLoading).toBe(true);
    expect(result.current.requestPending).toBe(true);

    respondToAttach(ws, attachId, "worker", "default", "stream-loaded");
    expect(result.current.isLoading).toBe(false);
    unmount();
  });

  it("close clears pending request", () => {
    const { result, unmount } = renderHook(() => useTmuxSessions());
    const first = mockWs.instances[0];
    open(first);

    act(() => result.current.attachSession("worker", "default"));
    const staleAttachId = requestId(first, "terminal_attach");
    const staleAttachMessage = first.onmessage;
    expect(result.current.requestPending).toBe(true);

    act(() => first.simulateClose());
    expect(result.current.requestPending).toBe(false);
    expect(result.current.attachError).toBeNull();

    act(() => vi.advanceTimersByTime(2_000));
    const second = mockWs.instances[1];
    open(second);
    act(() => result.current.attachSession("worker", "default"));
    const liveAttachId = requestId(second, "terminal_attach");
    expect(result.current.requestPending).toBe(true);

    act(() => {
      staleAttachMessage?.(
        new MessageEvent("message", {
          data: JSON.stringify({
            type: "terminal_attach_result",
            request_id: staleAttachId,
            success: true,
            streaming_id: "stale-stream",
            session_name: "worker",
            socket: "default",
          }),
        }),
      );
      staleAttachMessage?.(
        new MessageEvent("message", {
          data: JSON.stringify({
            type: "error",
            request_id: staleAttachId,
            message: "Stale",
          }),
        }),
      );
    });
    expect(result.current.requestPending).toBe(true);
    expect(result.current.attachError).toBeNull();
    expect(result.current.streamingId).toBeNull();

    respondToAttach(second, liveAttachId, "worker", "default", "live-stream");
    act(() => result.current.detachSession());
    const staleDetachId = requestId(second, "terminal_detach");
    const staleDetachMessage = second.onmessage;
    expect(result.current.requestPending).toBe(true);

    act(() => second.simulateClose());
    expect(result.current.requestPending).toBe(false);
    expect(result.current.attachedTarget).toBeNull();

    act(() => vi.advanceTimersByTime(2_000));
    const third = mockWs.instances[2];
    open(third);
    act(() => result.current.attachSession("worker", "gobby"));
    const finalAttachId = requestId(third, "terminal_attach");

    act(() => {
      staleDetachMessage?.(
        new MessageEvent("message", {
          data: JSON.stringify({
            type: "terminal_detach_result",
            request_id: staleDetachId,
            success: true,
          }),
        }),
      );
      staleDetachMessage?.(
        new MessageEvent("message", {
          data: JSON.stringify({
            type: "error",
            request_id: staleDetachId,
            message: "Stale detach",
          }),
        }),
      );
    });
    expect(result.current.requestPending).toBe(true);
    expect(result.current.attachError).toBeNull();

    respondToAttach(third, finalAttachId, "worker", "gobby", "final-stream");
    expect(result.current.requestPending).toBe(false);
    expect(result.current.attachedTarget).toEqual({
      terminal_id: "worker",
    });
    expect(result.current.streamingId).toBe("final-stream");
    unmount();
  });

  it("test_terminal_list_follows_pages", () => {
    const { result } = renderHook(() => useTmuxSessions());
    const ws = mockWs.instances[0];
    open(ws);
    act(() => {
      ws.simulateMessage({
        type: "terminal_list",
        request_id: "init",
        items: Array.from({ length: 100 }, (_, index) => ({
          terminal_id: `t-${index.toString().padStart(3, "0")}`,
          backend: "tmux",
          ownership: "gobby",
          state: "live",
          title: `row-${index}`,
          session_id: null,
          agent_run_id: null,
          dims: null,
        })),
        next_cursor: "cursor-1",
      });
    });
    const pageRequest = sentMessages(ws, "terminal_list").find(
      (message) => message.cursor === "cursor-1",
    );
    expect(pageRequest).toBeDefined();
    act(() => {
      ws.simulateMessage({
        type: "terminal_list",
        request_id: "page-1",
        items: [
          {
            terminal_id: "t-100",
            backend: "tmux",
            ownership: "gobby",
            state: "live",
            title: "row-100",
            session_id: null,
            agent_run_id: null,
            dims: null,
          },
        ],
        next_cursor: null,
      });
    });
    const ids = result.current.sessions.map((session) => session.terminal_id);
    expect(ids[0]).toBe("t-000");
    expect(ids.at(-1)).toBe("t-100");
    expect(new Set(ids).size).toBe(ids.length);
  });

  it("test_write_seq_refusals_clear_inflight_and_do_not_resend", () => {
    const { result } = renderHook(() => useTmuxSessions());
    const ws = mockWs.instances[0];
    open(ws);
    act(() => {
      result.current.attachSession("term-1", "tmux");
    });
    const attachId = requestId(ws, "terminal_attach");
    respondToAttach(ws, attachId, "term-1", "tmux", "att-1");
    const before = sentMessages(ws, "terminal_input").length;
    act(() => {
      result.current.sendInput("x");
    });
    expect(sentMessages(ws, "terminal_input").length).toBe(before + 1);
    act(() => {
      ws.simulateMessage({
        type: "terminal_write_outcome",
        attachment_id: "att-1",
        client_write_seq: sentMessages(ws, "terminal_input").at(-1)
          ?.client_write_seq,
        outcome: "refused",
        reason: "write_seq_conflict",
      });
    });
    expect(sentMessages(ws, "terminal_input").length).toBe(before + 1);
  });
});
