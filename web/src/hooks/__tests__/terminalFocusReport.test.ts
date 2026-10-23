import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { createFocusReport } from "../terminalFocusReport";

type WireMessage = Record<string, unknown>;

interface FakeSocket {
  readyState: number;
  send: ReturnType<typeof vi.fn>;
}

let socket: FakeSocket;
let holdSeconds: number;

function sentFocus(): WireMessage[] {
  return socket.send.mock.calls.map(
    ([payload]) => JSON.parse(payload as string) as WireMessage,
  );
}

function focusFrame(focused: boolean, attachmentId = "att-1"): WireMessage {
  return {
    type: "terminal_focus",
    terminal_id: "term-1",
    attachment_id: attachmentId,
    focused,
  };
}

function makeReport() {
  return createFocusReport({
    socket: () => socket as unknown as WebSocket,
    terminalId: () => "term-1",
    holdSeconds: () => holdSeconds,
  });
}

beforeEach(() => {
  vi.useFakeTimers();
  socket = { readyState: WebSocket.OPEN, send: vi.fn() };
  holdSeconds = 300;
});

afterEach(() => {
  vi.useRealTimers();
});

describe("terminalFocusReport", () => {
  it("reportsFocusOnceWhenTheLeaseIsHeld", () => {
    const report = makeReport();

    report.sync("other-attachment", "att-1");
    expect(sentFocus()).toEqual([]);

    report.sync("att-1", "att-1");
    expect(sentFocus()).toEqual([focusFrame(true)]);

    report.sync("att-1", "att-1");
    expect(sentFocus()).toEqual([focusFrame(true)]);
    report.dispose();
  });

  it("idleExpiryReportsFocusLostAndActivityRestoresIt", () => {
    holdSeconds = 10;
    const report = makeReport();
    const stop = report.listen(document);
    report.sync("att-1", "att-1");

    vi.advanceTimersByTime(9_999);
    expect(sentFocus()).toEqual([focusFrame(true)]);

    vi.advanceTimersByTime(1);
    expect(sentFocus()).toEqual([focusFrame(true), focusFrame(false)]);

    document.dispatchEvent(new Event("pointermove"));
    expect(sentFocus()).toHaveLength(2);

    document.dispatchEvent(new KeyboardEvent("keydown", { key: "a" }));
    expect(sentFocus()).toEqual([
      focusFrame(true),
      focusFrame(false),
      focusFrame(true),
    ]);

    for (const type of ["paste", "pointerdown", "wheel"]) {
      vi.advanceTimersByTime(10_000);
      const before = sentFocus().length;
      document.dispatchEvent(new Event(type));
      expect(sentFocus().slice(before)).toEqual([focusFrame(true)]);
    }

    stop();
    report.dispose();
  });

  it("zeroHoldNeverExpiresAndLosingTheLeaseForgetsTheReport", () => {
    holdSeconds = 0;
    const report = makeReport();
    const stop = report.listen(document);
    report.sync("att-1", "att-1");

    vi.advanceTimersByTime(60 * 60 * 1000);
    expect(sentFocus()).toEqual([focusFrame(true)]);

    report.sync(null, "att-1");
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "a" }));
    expect(sentFocus()).toEqual([focusFrame(true)]);

    report.sync("att-1", "att-1");
    expect(sentFocus()).toEqual([focusFrame(true), focusFrame(true)]);

    stop();
    report.dispose();
  });
});
