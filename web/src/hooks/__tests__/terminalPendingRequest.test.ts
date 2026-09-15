import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  createPendingRequestSlot,
  type PendingRequest,
} from "../terminalPendingRequest";

const attach = (requestId: string, generation: number): PendingRequest => ({
  kind: "attach",
  requestId,
  generation,
  target: { terminal_id: "t1" },
});

describe("terminalPendingRequest", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("holds one request and answers only its own result", () => {
    let generation = 1;
    const changes: boolean[] = [];
    const slot = createPendingRequestSlot({
      generation: () => generation,
      timeoutMs: 10_000,
      onChange: (pending) => changes.push(pending),
      onTimeout: () => {
        throw new Error("unexpected timeout");
      },
    });

    expect(slot.current()).toBeNull();
    const request = attach("attach-1-1", 1);
    slot.begin(request);
    expect(slot.current()).toBe(request);
    expect(changes).toEqual([true]);

    // A result for another request, or for this request id on an older
    // socket, answers nothing.
    expect(slot.match("attach-1-2")).toBeNull();
    generation = 2;
    expect(slot.match("attach-1-1")).toBeNull();
    generation = 1;
    expect(slot.match("attach-1-1")).toBe(request);

    slot.clear();
    expect(slot.current()).toBeNull();
    expect(changes).toEqual([true, false]);

    // Clearing cancelled the timeout: nothing fires later.
    vi.advanceTimersByTime(20_000);
    expect(changes).toEqual([true, false]);
  });

  it("empties the slot and names the request kind when it times out", () => {
    const changes: boolean[] = [];
    const errors: string[] = [];
    const slot = createPendingRequestSlot({
      generation: () => 1,
      timeoutMs: 10_000,
      onChange: (pending) => changes.push(pending),
      onTimeout: (error) => errors.push(error),
    });

    slot.begin(attach("attach-1-1", 1));
    vi.advanceTimersByTime(9_999);
    expect(errors).toEqual([]);
    vi.advanceTimersByTime(1);
    expect(slot.current()).toBeNull();
    expect(changes).toEqual([true, false]);
    // Rendered after a full stop ("Couldn't attach to this terminal. "), so
    // it has to stand on its own as a sentence.
    expect(errors).toEqual(["Attach request timed out."]);

    // A request begun while an earlier timer is armed owns the slot; the
    // stale timer must not empty it.
    slot.begin({ kind: "create", requestId: "create-1-2", generation: 1 });
    slot.begin({ kind: "create", requestId: "create-1-3", generation: 1 });
    vi.advanceTimersByTime(10_000);
    expect(errors).toEqual([
      "Attach request timed out.",
      "Create request timed out.",
    ]);
    expect(slot.current()).toBeNull();
  });

  it("disposes silently", () => {
    const changes: boolean[] = [];
    const slot = createPendingRequestSlot({
      generation: () => 1,
      timeoutMs: 10_000,
      onChange: (pending) => changes.push(pending),
      onTimeout: () => {
        throw new Error("unexpected timeout");
      },
    });
    slot.begin(attach("attach-1-1", 1));
    slot.dispose();
    expect(slot.current()).toBeNull();
    expect(changes).toEqual([true]);
    vi.advanceTimersByTime(20_000);
  });
});
