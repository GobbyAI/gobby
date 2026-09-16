import { describe, expect, it } from "vitest";

import {
  EMPTY_WRITE_SETTLEMENT,
  createWriteSeqAllocator,
  retryableWrite,
  writeSettlementReducer,
  type TerminalWrite,
  type WriteSettlementAction,
  type WriteSettlementState,
} from "../terminalWriteSettlement";

const ATTACHMENT = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
const OTHER_ATTACHMENT = "cccccccc-cccc-4ccc-8ccc-cccccccccccc";
const TERMINAL = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";

function write(seq: number, overrides: Partial<TerminalWrite> = {}) {
  return {
    attachmentId: ATTACHMENT,
    terminalId: TERMINAL,
    seq,
    kind: "input",
    payload: "ls\n",
    ...overrides,
  } satisfies TerminalWrite;
}

function reduce(
  actions: readonly WriteSettlementAction[],
  initial: WriteSettlementState = EMPTY_WRITE_SETTLEMENT,
): WriteSettlementState {
  return actions.reduce(writeSettlementReducer, initial);
}

describe("createWriteSeqAllocator", () => {
  it("counts each attachment separately and never reuses a seq after a gap", () => {
    const allocator = createWriteSeqAllocator();

    expect(allocator.next(ATTACHMENT)).toBe(1);
    expect(allocator.next(OTHER_ATTACHMENT)).toBe(1);
    expect(allocator.next(ATTACHMENT)).toBe(2);
    expect(allocator.next(OTHER_ATTACHMENT)).toBe(2);
    expect(allocator.next(ATTACHMENT)).toBe(3);

    // A finalized attachment's counter is dropped; a later attachment that
    // somehow reused the id starts a fresh seq space rather than inheriting a
    // high-water mark the daemon no longer holds.
    allocator.forget(ATTACHMENT);
    expect(allocator.next(ATTACHMENT)).toBe(1);
    expect(allocator.next(OTHER_ATTACHMENT)).toBe(3);
  });
});

describe("writeSettlementReducer", () => {
  it("settles outcomes without auto-resend", () => {
    const delivered = reduce([
      { type: "sent", write: write(1) },
      {
        type: "outcome",
        attachmentId: ATTACHMENT,
        seq: 1,
        outcome: "delivered",
        reason: null,
      },
    ]);
    expect(delivered).toEqual(EMPTY_WRITE_SETTLEMENT);

    const refused = reduce([
      { type: "sent", write: write(2, { payload: "rm\n" }) },
      {
        type: "outcome",
        attachmentId: ATTACHMENT,
        seq: 2,
        outcome: "refused",
        reason: "held",
      },
    ]);
    expect(refused.inFlight).toEqual([]);
    expect(refused.settled).toEqual([
      {
        ...write(2, { payload: "rm\n" }),
        outcome: "refused",
        reason: "held",
        retryable: false,
      },
    ]);

    const indeterminate = reduce([
      { type: "sent", write: write(3, { payload: "make\n" }) },
      {
        type: "outcome",
        attachmentId: ATTACHMENT,
        seq: 3,
        outcome: "indeterminate",
        reason: "indeterminate_backend",
      },
    ]);
    expect(indeterminate.inFlight).toEqual([]);
    expect(indeterminate.settled).toEqual([
      {
        ...write(3, { payload: "make\n" }),
        outcome: "indeterminate",
        reason: "indeterminate_backend",
        retryable: true,
      },
    ]);

    // The retry resends the identical seq and payload, and it is the caller
    // that sends: the reducer only hands back what to resend.
    const resend = retryableWrite(indeterminate, ATTACHMENT, 3);
    expect(resend).toEqual(write(3, { payload: "make\n" }));

    const retried = reduce(
      [{ type: "retry", attachmentId: ATTACHMENT, seq: 3 }],
      indeterminate,
    );
    expect(retried.settled).toEqual([]);
    expect(retried.inFlight).toEqual([write(3, { payload: "make\n" })]);

    // Exactly one retry: a second indeterminate on the same seq settles with
    // no further offer, and nothing is resent on its own.
    const retriedAgain = reduce(
      [
        {
          type: "outcome",
          attachmentId: ATTACHMENT,
          seq: 3,
          outcome: "indeterminate",
          reason: "indeterminate_backend",
        },
      ],
      retried,
    );
    expect(retriedAgain.settled).toEqual([
      {
        ...write(3, { payload: "make\n" }),
        outcome: "indeterminate",
        reason: "indeterminate_backend",
        retryable: false,
      },
    ]);
    expect(retriedAgain.inFlight).toEqual([]);
    expect(retryableWrite(retriedAgain, ATTACHMENT, 3)).toBeNull();
    expect(
      reduce(
        [{ type: "retry", attachmentId: ATTACHMENT, seq: 3 }],
        retriedAgain,
      ),
    ).toEqual(retriedAgain);
  });

  it("keys settlement by attachment as well as seq", () => {
    const state = reduce([
      { type: "sent", write: write(1) },
      { type: "sent", write: write(1, { attachmentId: OTHER_ATTACHMENT }) },
      {
        type: "outcome",
        attachmentId: OTHER_ATTACHMENT,
        seq: 1,
        outcome: "refused",
        reason: "stale_attachment",
      },
    ]);

    expect(state.inFlight).toEqual([write(1)]);
    expect(state.settled).toEqual([
      {
        ...write(1, { attachmentId: OTHER_ATTACHMENT }),
        outcome: "refused",
        reason: "stale_attachment",
        retryable: false,
      },
    ]);
  });

  it("grants nothing to an outcome for a write it is no longer tracking", () => {
    const discarded = reduce([
      { type: "sent", write: write(4) },
      {
        type: "outcome",
        attachmentId: ATTACHMENT,
        seq: 4,
        outcome: "indeterminate",
        reason: "indeterminate_backend",
      },
      { type: "discard", attachmentId: ATTACHMENT, seq: 4 },
    ]);
    expect(discarded).toEqual(EMPTY_WRITE_SETTLEMENT);

    const lateOutcome = reduce(
      [
        {
          type: "outcome",
          attachmentId: ATTACHMENT,
          seq: 4,
          outcome: "delivered",
          reason: null,
        },
      ],
      discarded,
    );
    expect(lateOutcome).toEqual(EMPTY_WRITE_SETTLEMENT);

    const unknownSeq = reduce([
      {
        type: "outcome",
        attachmentId: ATTACHMENT,
        seq: 99,
        outcome: "refused",
        reason: "write_seq_expired",
      },
    ]);
    expect(unknownSeq).toEqual(EMPTY_WRITE_SETTLEMENT);
  });

  it("drops every write for an attachment that closed", () => {
    const state = reduce([
      { type: "sent", write: write(1) },
      { type: "sent", write: write(2) },
      {
        type: "outcome",
        attachmentId: ATTACHMENT,
        seq: 2,
        outcome: "indeterminate",
        reason: "indeterminate_backend",
      },
      { type: "sent", write: write(1, { attachmentId: OTHER_ATTACHMENT }) },
      { type: "attachmentClosed", attachmentId: ATTACHMENT },
    ]);

    expect(state.settled).toEqual([]);
    expect(state.inFlight).toEqual([
      write(1, { attachmentId: OTHER_ATTACHMENT }),
    ]);
  });

  it("returns the same state object when an action changes nothing", () => {
    const state = reduce([{ type: "sent", write: write(1) }]);

    expect(
      writeSettlementReducer(state, {
        type: "attachmentClosed",
        attachmentId: OTHER_ATTACHMENT,
      }),
    ).toBe(state);
    expect(
      writeSettlementReducer(state, {
        type: "discard",
        attachmentId: OTHER_ATTACHMENT,
        seq: 1,
      }),
    ).toBe(state);
  });
});
