import { describe, expect, it } from "vitest";

import {
  createTerminalWsReducer,
  TERMINAL_FRAGMENT_BUDGET_BYTES,
  TERMINAL_FRAGMENT_OVERHEAD_BYTES,
} from "../terminalWsFragments";

const slice = (
  attachment: string,
  seq: number,
  index: number,
  more: boolean,
  text: string,
) => ({
  type: "terminal_ws_fragment",
  event: "terminal_output",
  terminal_id: "t1",
  attachment_id: attachment,
  message_seq: seq,
  fragment_index: index,
  more,
  encoding: "utf8-b64",
  payload: btoa(text),
});

describe("terminalWsFragments", () => {
  it("charges overhead and refreshes over budget", () => {
    // A fragment costs its payload plus a fixed per-fragment charge, so a
    // flood of tiny fragments exhausts the budget on fragment count alone —
    // which is the shape that actually starves the client.
    expect(TERMINAL_FRAGMENT_OVERHEAD_BYTES).toBe(256);
    expect(TERMINAL_FRAGMENT_BUDGET_BYTES).toBe(256 * 1024);

    const reducer = createTerminalWsReducer({
      now: () => 0,
      budgetBytes: 1024,
      fragmentOverheadBytes: 256,
    });
    reducer.markLive("att-a");

    // 256 payload + 256 overhead = 512 per fragment: two fit exactly.
    const payload = "x".repeat(256);
    reducer.push(slice("att-a", 1, 0, true, payload));
    reducer.push(slice("att-a", 1, 1, true, payload));
    expect(reducer.refreshRequests).toEqual([]);
    expect(reducer.isPinned("att-a")).toBe(false);

    // The third crosses it.
    reducer.push(slice("att-a", 1, 2, true, payload));
    expect(reducer.refreshRequests).toEqual(["att-a"]);
    expect(reducer.isPinned("att-a")).toBe(true);

    // Exactly one refresh: the partial message is already abandoned, so more
    // fragments of it must not each ask for their own snapshot.
    reducer.push(slice("att-a", 1, 3, true, payload));
    reducer.push(slice("att-a", 2, 0, false, payload));
    expect(reducer.refreshRequests).toEqual(["att-a"]);

    // Pinned means the stream is not trusted until the snapshot lands: output
    // from before the refresh would paint a torn screen.
    expect(reducer.applied).toEqual([]);

    // The snapshot itself clears the pin and is delivered.
    reducer.push({
      type: "terminal_attach_history",
      attachment_id: "att-a",
      text: "fresh",
    });
    expect(reducer.isPinned("att-a")).toBe(false);
    expect(reducer.applied).toEqual([
      {
        type: "terminal_attach_history",
        attachment_id: "att-a",
        text: "fresh",
      },
    ]);

    // Draining the request is what lets the caller send exactly one frame.
    expect(reducer.takeRefreshRequests()).toEqual(["att-a"]);
    expect(reducer.takeRefreshRequests()).toEqual([]);

    // The budget is per attachment, so one noisy terminal cannot refresh
    // another.
    const shared = createTerminalWsReducer({
      now: () => 0,
      budgetBytes: 1024,
      fragmentOverheadBytes: 256,
    });
    shared.markLive("att-a");
    shared.markLive("att-b");
    shared.push(slice("att-a", 1, 0, true, payload));
    shared.push(slice("att-b", 1, 0, true, payload));
    shared.push(slice("att-a", 1, 1, true, payload));
    shared.push(slice("att-a", 1, 2, true, payload));
    expect(shared.takeRefreshRequests()).toEqual(["att-a"]);
    expect(shared.isPinned("att-b")).toBe(false);
  });

  it("flushes accumulated fragments once per tick", () => {
    // The reassembly timeout only exists if something drives it; an undriven
    // tick leaves a half-delivered message holding its bytes for the life of
    // the socket.
    let now = 0;
    const reducer = createTerminalWsReducer({
      now: () => now,
      timeoutMs: 5000,
    });
    reducer.markLive("att-a");
    reducer.push(slice("att-a", 1, 0, true, "abc"));
    expect(reducer.socketBytes).toBe(3);

    now = 4999;
    reducer.tick(now);
    expect(reducer.socketBytes).toBe(3);
    expect(reducer.errors).toEqual([]);

    now = 5000;
    reducer.tick(now);
    expect(reducer.socketBytes).toBe(0);
    expect(reducer.errors).toEqual([
      { code: "fragment_timeout", attachment_id: "att-a" },
    ]);

    // One sweep per tick: a second tick over the same empty buffers is inert.
    reducer.tick(now);
    expect(reducer.errors).toHaveLength(1);
  });
});
