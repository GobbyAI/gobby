import { act, fireEvent, render, screen } from "@testing-library/react";
import { createElement, forwardRef, useImperativeHandle } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  createMockWebSocket,
  type MockWebSocketInstance,
} from "../../../../test/mocks/websocket";
import { TerminalTab } from "../TerminalTab";
import type { TerminalViewHandle, TerminalViewProps } from "../TerminalView";

// The pane keeps the PTY on the renderer's grid: whatever size the renderer
// last measured is the size the daemon has to be told. These tests run the
// real hook under the real tab and stub only the renderer, which is driven
// through the two callbacks TerminalView uses (`onReady` once at mount,
// `onSizeChange` on every later grid change).
const view = vi.hoisted(() => ({
  props: null as TerminalViewProps | null,
  size: null as { rows: number; cols: number } | null,
}));

vi.mock("../TerminalView", () => ({
  TerminalView: forwardRef<TerminalViewHandle, TerminalViewProps>(
    (props, ref) => {
      view.props = props;
      useImperativeHandle(ref, () => ({
        write: () => undefined,
        getSize: () => view.size,
        setKeyboardOpen: () => undefined,
        applyAttachHistory: () => undefined,
      }));
      return createElement("div", { "data-testid": "terminal-view" });
    },
  ),
}));

type WireMessage = Record<string, unknown>;

let mockWs: ReturnType<typeof createMockWebSocket>;

function lastSent(ws: MockWebSocketInstance, type: string): WireMessage {
  const messages = ws.send.mock.calls
    .map(([payload]) => JSON.parse(payload as string) as WireMessage)
    .filter((message) => message.type === type);
  const last = messages[messages.length - 1];
  if (last === undefined) throw new Error(`no ${type} was sent`);
  return last;
}

function respondToAttach(
  ws: MockWebSocketInstance,
  terminalId: string,
  attachmentId: string,
): void {
  act(() =>
    ws.simulateMessage({
      type: "terminal_attach_result",
      request_id: lastSent(ws, "terminal_attach").request_id,
      success: true,
      attachment_id: attachmentId,
      terminal_id: terminalId,
    }),
  );
}

function mountTab(terminalIds: readonly string[]): MockWebSocketInstance {
  render(createElement(TerminalTab));
  const ws = mockWs.instances[0];
  act(() => ws.simulateOpen());
  act(() =>
    ws.simulateMessage({
      type: "terminal_list",
      request_id: "init",
      items: terminalIds.map((terminalId) => ({
        terminal_id: terminalId,
        backend: "tmux",
        ownership: "gobby",
        state: "live",
        title: terminalId,
        session_id: null,
        agent_run_id: null,
        dims: null,
      })),
      next_cursor: null,
    }),
  );
  // The tab defers its initial selection by a tick before it attaches.
  act(() => vi.advanceTimersByTime(1));
  return ws;
}

function mountedView(): TerminalViewProps {
  if (view.props === null) throw new Error("the terminal view never mounted");
  return view.props;
}

function measureGrid(rows: number, cols: number, ready = false): void {
  act(() => {
    view.size = { rows, cols };
    if (ready) mountedView().onReady?.(rows, cols);
    else mountedView().onSizeChange?.(rows, cols);
  });
}

beforeEach(() => {
  vi.useFakeTimers();
  mockWs = createMockWebSocket();
  view.props = null;
  view.size = null;
  window.sessionStorage.clear();
  // Idle shells are hidden under the default "agents" scope.
  window.localStorage.setItem("gobby:terminal:session-scope", "all");
});

afterEach(() => {
  mockWs.restore();
  vi.useRealTimers();
  vi.restoreAllMocks();
  window.localStorage.clear();
});

describe("TerminalTab keeps the PTY on the renderer's grid", () => {
  it("opens an attachment at the grid the renderer settled on while the attach was in flight", () => {
    const ws = mountTab(["t-a"]);
    // Fonts load and the panel settles after the first measurement; under load
    // the daemon is still answering the attach, so there is no attachment to
    // carry the new size yet.
    measureGrid(24, 80, true);
    measureGrid(40, 132);

    respondToAttach(ws, "t-a", "att-a");

    expect(lastSent(ws, "terminal_resize")).toMatchObject({
      attachment_id: "att-a",
      rows: 40,
      cols: 132,
    });
    expect(lastSent(ws, "terminal_set_viewport")).toMatchObject({
      attachment_id: "att-a",
      kind: "viewport",
      rows: 40,
      cols: 132,
    });
  });

  it("opens a replacement attachment at the grid the previous one was last resized to", () => {
    const ws = mountTab(["t-a", "t-b"]);
    measureGrid(24, 80, true);
    respondToAttach(ws, "t-a", "att-a");

    measureGrid(40, 132);
    // The attachment live at the time hears about the change.
    expect(lastSent(ws, "terminal_resize")).toMatchObject({
      attachment_id: "att-a",
      rows: 40,
      cols: 132,
    });

    // Switching terminals keeps the one renderer mounted: it is not
    // remeasured, so the replacement has to inherit the grid it is drawn on.
    const other = screen
      .getAllByRole("button", { name: /^Attach / })
      .find((button) => button.getAttribute("aria-pressed") === "false");
    if (other === undefined) throw new Error("no unselected terminal row");
    fireEvent.click(other);
    act(() =>
      ws.simulateMessage({
        type: "terminal_detach_result",
        request_id: lastSent(ws, "terminal_detach").request_id,
        success: true,
      }),
    );
    respondToAttach(ws, "t-b", "att-b");

    expect(lastSent(ws, "terminal_resize")).toMatchObject({
      attachment_id: "att-b",
      rows: 40,
      cols: 132,
    });
    expect(lastSent(ws, "terminal_set_viewport")).toMatchObject({
      attachment_id: "att-b",
      kind: "viewport",
      rows: 40,
      cols: 132,
    });
  });
});
