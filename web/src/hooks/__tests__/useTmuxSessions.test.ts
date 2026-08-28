import { act, renderHook } from "@testing-library/react";
import { StrictMode, createElement, type ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  createMockWebSocket,
  type MockWebSocketInstance,
} from "../../test/mocks/websocket";
import {
  TMUX_REQUEST_TIMEOUT_MS,
  createTerminalWsReducer,
  TERMINAL_WS_FRAGMENT_MAX_REASSEMBLY_BYTES,
  TERMINAL_WS_FRAGMENT_MAX_SOCKET_REASSEMBLY_BYTES,
  TERMINAL_WS_SAFE_INTEGER_MAX,
  useTmuxSessions,
} from "../useTmuxSessions";

type WireMessage = Record<string, unknown>;

function lastOf<T>(items: readonly T[]): T | undefined {
  return items[items.length - 1];
}

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
  it("scopes every listing to the project picker and relists when it changes", () => {
    const mount = renderHook(
      ({ projectId }: { projectId: string | null }) =>
        useTmuxSessions(projectId),
      { initialProps: { projectId: "proj-a" as string | null } },
    );
    const ws = mockWs.instances[0];
    open(ws);

    expect(sentMessages(ws, "terminal_list")).toEqual([
      { type: "terminal_list", request_id: "init", project_id: "proj-a" },
    ]);

    act(() => {
      ws.simulateMessage({
        type: "terminal_list",
        request_id: "init",
        items: [{ terminal_id: "t-1" }],
        next_cursor: "2026-01-01T00:00:00|t-1",
      });
    });
    const page = lastOf(sentMessages(ws, "terminal_list"));
    expect(page).toMatchObject({
      project_id: "proj-a",
      cursor: "2026-01-01T00:00:00|t-1",
    });

    mount.rerender({ projectId: "proj-b" });
    const relist = lastOf(sentMessages(ws, "terminal_list"));
    expect(relist).toMatchObject({ project_id: "proj-b" });
    expect(String(relist?.request_id)).toMatch(/^refresh/);

    mount.rerender({ projectId: null });
    expect(lastOf(sentMessages(ws, "terminal_list"))).not.toHaveProperty(
      "project_id",
    );
  });

  it("a refresh replaces the list so vanished terminals drop out", () => {
    const mount = renderHook(() => useTmuxSessions());
    const ws = mockWs.instances[0];
    open(ws);
    act(() => {
      ws.simulateMessage({
        type: "terminal_list",
        request_id: "init",
        items: [{ terminal_id: "t-1" }, { terminal_id: "t-2" }],
        next_cursor: null,
      });
    });
    expect(mount.result.current.sessions.map((s) => s.terminal_id)).toEqual([
      "t-1",
      "t-2",
    ]);

    act(() => mount.result.current.refreshSessions());
    const refreshId = requestId(ws, "terminal_list");
    expect(refreshId).toMatch(/^refresh-/);
    act(() => {
      ws.simulateMessage({
        type: "terminal_list",
        request_id: refreshId,
        items: [{ terminal_id: "t-2" }],
        next_cursor: null,
      });
    });
    expect(mount.result.current.sessions.map((s) => s.terminal_id)).toEqual([
      "t-2",
    ]);
  });

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
    expect(lastOf(sentMessages(ws, "terminal_attach"))).toMatchObject({
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
    expect(lastOf(sentMessages(ws, "terminal_input"))).toMatchObject({
      type: "terminal_input",
      attachment_id: "stream-wire",
      data: "\u001b[A",
    });
    expect(lastOf(sentMessages(ws, "terminal_resize"))).toMatchObject({
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

  it("names the picker's project on create and omits it machine-wide", () => {
    const scoped = renderHook(() => useTmuxSessions("proj-1"));
    const scopedWs = mockWs.instances[0];
    open(scopedWs);
    scopedWs.send.mockClear();
    act(() => scoped.result.current.createSession());
    expect(sentMessages(scopedWs, "terminal_create")[0]).toMatchObject({
      project_id: "proj-1",
    });
    scoped.unmount();

    const machineWide = renderHook(() => useTmuxSessions());
    const wideWs = mockWs.instances[1];
    open(wideWs);
    wideWs.send.mockClear();
    act(() => machineWide.result.current.createSession());
    expect(sentMessages(wideWs, "terminal_create")[0]).not.toHaveProperty(
      "project_id",
    );
    machineWide.unmount();
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
    expect(result.current.attachError).toBe("Attach request timed out.");

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
    expect(result.current.attachError).toBe("Detach request timed out.");
    expect(result.current.streamingId).toBe("stream-timeout");

    act(() => {
      result.current.clearAttachError();
      result.current.createSession();
    });
    act(() => vi.advanceTimersByTime(TMUX_REQUEST_TIMEOUT_MS));
    expect(result.current.requestPending).toBe(false);
    expect(result.current.isLoading).toBe(false);
    expect(result.current.attachError).toBe("Create request timed out.");
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
  it("routes attach history to the registered consumer", () => {
    const mount = renderHook(() => useTmuxSessions());
    const [ws] = mockWs.instances;
    open(ws);

    const received: unknown[] = [];
    act(() =>
      mount.result.current.onAttachHistory((history) => received.push(history)),
    );

    act(() => {
      ws.simulateMessage({
        type: "terminal_attach_history",
        streaming_id: "stream-1",
        text: "older\r\nlines",
        truncated: true,
        unavailable: false,
        dropped_bytes: 128,
        total_bytes: 4096,
      });
    });

    expect(received).toEqual([
      {
        streamingId: "stream-1",
        text: "older\r\nlines",
        truncated: true,
        unavailable: false,
        droppedBytes: 128,
        totalBytes: 4096,
      },
    ]);

    // Missing optional fields degrade to safe defaults rather than undefined.
    act(() => {
      ws.simulateMessage({
        type: "terminal_attach_history",
        streaming_id: "stream-1",
      });
    });
    expect(received[1]).toEqual({
      streamingId: "stream-1",
      text: "",
      truncated: false,
      unavailable: false,
      droppedBytes: 0,
      totalBytes: 0,
    });
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
    expect(lastOf(ids)).toBe("t-100");
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
        client_write_seq: lastOf(sentMessages(ws, "terminal_input"))
          ?.client_write_seq,
        outcome: "refused",
        reason: "write_seq_conflict",
      });
    });
    expect(sentMessages(ws, "terminal_input").length).toBe(before + 1);
  });

  it("test_fragment_reassembly_and_cleanup", () => {
    const reducer = createTerminalWsReducer({ now: () => 0 });
    reducer.markLive("att-a");
    reducer.markLive("att-b");
    const history = JSON.stringify({
      type: "terminal_attach_history",
      attachment_id: "att-a",
      text: "hist",
    });
    const output = JSON.stringify({
      type: "terminal_output",
      attachment_id: "att-a",
      data: "kf",
    });
    const slice = (
      event: string,
      attachment: string,
      seq: number,
      index: number,
      more: boolean,
      text: string,
    ) => ({
      type: "terminal_ws_fragment",
      event,
      terminal_id: "t1",
      attachment_id: attachment,
      message_seq: seq,
      fragment_index: index,
      more,
      encoding: "utf8-b64",
      payload: btoa(text),
    });
    reducer.push(
      slice(
        "terminal_attach_history",
        "att-a",
        1,
        0,
        true,
        history.slice(0, 12),
      ),
    );
    reducer.push(
      slice(
        "terminal_output",
        "att-b",
        1,
        0,
        false,
        JSON.stringify({ type: "terminal_output", data: "b" }),
      ),
    );
    reducer.push(
      slice("terminal_attach_history", "att-a", 1, 1, false, history.slice(12)),
    );
    expect(reducer.applied[0]).toMatchObject({
      type: "terminal_output",
      data: "b",
    });
    expect(reducer.applied[1]).toMatchObject({
      type: "terminal_attach_history",
      text: "hist",
    });
    reducer.push(
      slice("terminal_output", "att-a", 2, 0, true, output.slice(0, 10)),
    );
    reducer.push(
      slice("terminal_output", "att-a", 2, 1, false, output.slice(10)),
    );
    expect(lastOf(reducer.applied)).toMatchObject({
      type: "terminal_output",
      data: "kf",
    });

    const gappy = createTerminalWsReducer({ now: () => 0 });
    gappy.markLive("att-a");
    gappy.push(slice("terminal_output", "att-a", 1, 0, true, "abc"));
    gappy.push(slice("terminal_output", "att-a", 1, 2, false, "def"));
    expect(gappy.applied).toEqual([]);
    expect(gappy.errors.some((item) => item.code === "fragment_sequence")).toBe(
      true,
    );

    const jumped = createTerminalWsReducer({ now: () => 0 });
    jumped.markLive("att-a");
    jumped.push(slice("terminal_output", "att-a", 1, 0, true, "abc"));
    jumped.push(slice("terminal_output", "att-a", 2, 0, false, output));
    expect(jumped.applied).toEqual([]);
    expect(
      jumped.errors.some((item) => item.code === "fragment_sequence"),
    ).toBe(true);

    let now = 0;
    const timed = createTerminalWsReducer({ now: () => now, timeoutMs: 5000 });
    timed.markLive("att-a");
    timed.push(slice("terminal_output", "att-a", 1, 0, true, "abc"));
    now = 5001;
    timed.tick(now);
    expect(timed.applied).toEqual([]);
    expect(timed.errors.some((item) => item.code === "fragment_timeout")).toBe(
      true,
    );

    expect(TERMINAL_WS_FRAGMENT_MAX_REASSEMBLY_BYTES).toBe(16 * 1024 * 1024);
    const huge = createTerminalWsReducer({
      now: () => 0,
      maxReassemblyBytes: 8,
    });
    huge.markLive("att-a");
    huge.push({
      ...slice("terminal_output", "att-a", 1, 0, false, "x"),
      payload: btoa("123456789"),
    });
    expect(huge.applied).toEqual([]);
    expect(huge.errors.some((item) => item.code === "fragment_too_large")).toBe(
      true,
    );

    const dropped = createTerminalWsReducer({ now: () => 0 });
    dropped.markLive("att-a");
    dropped.push(slice("terminal_output", "att-a", 1, 0, true, "abc"));
    dropped.disconnect();
    dropped.push(slice("terminal_output", "att-a", 1, 1, false, "def"));
    expect(dropped.applied).toEqual([]);
  });

  it("test_socket_reassembly_budget_and_stale_fragments", () => {
    expect(TERMINAL_WS_FRAGMENT_MAX_SOCKET_REASSEMBLY_BYTES).toBe(
      64 * 1024 * 1024,
    );
    expect(TERMINAL_WS_SAFE_INTEGER_MAX).toBe(2 ** 53 - 1);
    const reducer = createTerminalWsReducer({
      now: () => 0,
      maxSocketBytes: 24,
    });
    reducer.markLive("a");
    reducer.markLive("b");
    const chunk = (attachment: string, seq: number, text: string) => ({
      type: "terminal_ws_fragment",
      event: "terminal_output",
      terminal_id: "t",
      attachment_id: attachment,
      message_seq: seq,
      fragment_index: 0,
      more: true,
      encoding: "utf8-b64",
      payload: btoa(text),
    });
    reducer.push(chunk("a", 1, "1234567890123456"));
    reducer.push(chunk("b", 1, "1234567890123456"));
    expect(
      reducer.errors.some((item) => item.code === "fragment_socket_budget"),
    ).toBe(true);
    expect(reducer.applied).toEqual([]);
    const stale = createTerminalWsReducer({ now: () => 0 });
    stale.push(chunk("missing", 1, "hello"));
    expect(stale.applied).toEqual([]);
    expect(stale.socketBytes).toBe(0);
    const finished = createTerminalWsReducer({ now: () => 0 });
    finished.markLive("a");
    finished.push({
      ...chunk("a", 1, '{"type":"terminal_output","data":"z"}'),
      more: false,
    });
    expect(finished.socketBytes).toBe(0);
    const afterFinal = createTerminalWsReducer({ now: () => 0 });
    afterFinal.markLive("a");
    afterFinal.finalize("a");
    afterFinal.push(chunk("a", 1, "hello"));
    expect(afterFinal.applied).toEqual([]);
    expect(afterFinal.socketBytes).toBe(0);
    const seqs = createTerminalWsReducer({ now: () => 0 });
    seqs.markLive("a");
    seqs.push({
      ...chunk(
        "a",
        Number.MAX_SAFE_INTEGER - 1,
        '{"type":"terminal_output","data":"x"}',
      ),
      more: false,
    });
    seqs.push({
      ...chunk(
        "a",
        Number.MAX_SAFE_INTEGER,
        '{"type":"terminal_output","data":"y"}',
      ),
      more: false,
    });
    expect(seqs.applied).toHaveLength(2);
    const overflowSeq = createTerminalWsReducer({ now: () => 0 });
    overflowSeq.markLive("a");
    overflowSeq.push({
      ...chunk(
        "a",
        Number.MAX_SAFE_INTEGER + 1,
        '{"type":"terminal_output","data":"z"}',
      ),
      more: false,
    });
    expect(overflowSeq.applied).toEqual([]);
  });

  it("test_observe_only_finalized_drops_stale_fragments", () => {
    const reducer = createTerminalWsReducer({ now: () => 0 });
    reducer.markLive("obs");
    reducer.push({
      type: "terminal_ws_fragment",
      event: "terminal_output",
      terminal_id: "t",
      attachment_id: "obs",
      message_seq: 1,
      fragment_index: 0,
      more: true,
      encoding: "utf8-b64",
      payload: btoa("abc"),
    });
    reducer.push({
      type: "terminal_attachment_finalized",
      terminal_id: "t",
      attachment_id: "obs",
      reason: "proxy_frame_eof",
      lease_generation: 0,
    });
    expect(reducer.applied).toEqual([
      expect.objectContaining({
        type: "terminal_attachment_finalized",
        attachment_id: "obs",
        reason: "proxy_frame_eof",
      }),
    ]);
    expect(reducer.socketBytes).toBe(0);
    reducer.push({
      type: "terminal_ws_fragment",
      event: "terminal_output",
      terminal_id: "t",
      attachment_id: "obs",
      message_seq: 1,
      fragment_index: 1,
      more: false,
      encoding: "utf8-b64",
      payload: btoa("def"),
    });
    expect(reducer.applied).toHaveLength(1);
    expect(reducer.socketBytes).toBe(0);
  });
});
