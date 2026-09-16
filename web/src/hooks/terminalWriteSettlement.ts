/**
 * Write settlement for terminal attachments.
 *
 * Every operator write the daemon admits is answered by exactly one
 * `terminal_write_outcome` carrying the `client_write_seq` it was sent with, so
 * (attachment, seq) is the settlement key. `delivered` clears the write,
 * `refused` surfaces it and stops, and `indeterminate` offers one retry that
 * resends the identical seq and payload — the daemon fingerprints a write by its
 * seq and replays the recorded outcome, so an identical resend is idempotent
 * while a re-allocated one is a second write. Nothing here resends on its own.
 */

/** The seq space is per attachment, so each attachment counts on its own. */
export interface WriteSeqAllocator {
  next: (attachmentId: string) => number;
  forget: (attachmentId: string) => void;
}

export type WriteOutcome = "delivered" | "refused" | "indeterminate";

export type WriteKind = "input" | "paste";

export interface TerminalWrite {
  attachmentId: string;
  terminalId: string;
  seq: number;
  kind: WriteKind;
  payload: string;
}

export interface SettledWrite extends TerminalWrite {
  outcome: Exclude<WriteOutcome, "delivered">;
  reason: string | null;
  /** Only an indeterminate write that has not already been retried offers one. */
  retryable: boolean;
}

export interface WriteSettlementState {
  inFlight: readonly TerminalWrite[];
  settled: readonly SettledWrite[];
  /**
   * Keys whose single retry is spent. A retry takes its write out of `settled`,
   * so the fact that one was offered has to outlive the entry itself.
   */
  retried: readonly string[];
}

export type WriteSettlementAction =
  | { type: "sent"; write: TerminalWrite }
  | {
      type: "outcome";
      attachmentId: string;
      seq: number;
      outcome: WriteOutcome;
      reason: string | null;
    }
  | { type: "retry"; attachmentId: string; seq: number }
  | { type: "discard"; attachmentId: string; seq: number }
  | { type: "attachmentClosed"; attachmentId: string }
  | { type: "reset" };

export const EMPTY_WRITE_SETTLEMENT: WriteSettlementState = {
  inFlight: [],
  settled: [],
  retried: [],
};

export function createWriteSeqAllocator(): WriteSeqAllocator {
  const counters = new Map<string, number>();
  return {
    next: (attachmentId) => {
      const seq = (counters.get(attachmentId) ?? 0) + 1;
      counters.set(attachmentId, seq);
      return seq;
    },
    forget: (attachmentId) => {
      counters.delete(attachmentId);
    },
  };
}

/**
 * NUL separates the two halves because it is the one character an
 * attachment id cannot contain, which is what lets `attachmentClosed`
 * match a whole attachment by prefix without matching a longer id.
 */
function writeKey(attachmentId: string, seq: number): string {
  return `${attachmentId}\u0000${seq}`;
}

function isSame(
  write: TerminalWrite,
  attachmentId: string,
  seq: number,
): boolean {
  return write.attachmentId === attachmentId && write.seq === seq;
}

function applyOutcome(
  state: WriteSettlementState,
  action: Extract<WriteSettlementAction, { type: "outcome" }>,
): WriteSettlementState {
  const { attachmentId, seq, outcome, reason } = action;
  const pending = state.inFlight.find((write) =>
    isSame(write, attachmentId, seq),
  );
  // A write the reducer is no longer tracking was discarded or belonged to a
  // closed attachment; a late outcome for it grants nothing.
  if (pending === undefined) return state;
  const inFlight = state.inFlight.filter(
    (write) => !isSame(write, attachmentId, seq),
  );
  if (outcome === "delivered") {
    return {
      inFlight,
      settled: state.settled,
      retried: state.retried.filter(
        (key) => key !== writeKey(attachmentId, seq),
      ),
    };
  }
  return {
    inFlight,
    settled: [
      ...state.settled.filter((write) => !isSame(write, attachmentId, seq)),
      {
        ...pending,
        outcome,
        reason,
        retryable:
          outcome === "indeterminate" &&
          !state.retried.includes(writeKey(attachmentId, seq)),
      },
    ],
    retried: state.retried,
  };
}

export function writeSettlementReducer(
  state: WriteSettlementState,
  action: WriteSettlementAction,
): WriteSettlementState {
  switch (action.type) {
    case "sent": {
      const { attachmentId, seq } = action.write;
      return {
        inFlight: [
          ...state.inFlight.filter(
            (write) => !isSame(write, attachmentId, seq),
          ),
          action.write,
        ],
        settled: state.settled,
        retried: state.retried,
      };
    }

    case "outcome":
      return applyOutcome(state, action);

    case "retry": {
      const offer = retryableWrite(state, action.attachmentId, action.seq);
      if (offer === null) return state;
      const key = writeKey(action.attachmentId, action.seq);
      return {
        inFlight: [...state.inFlight, offer],
        settled: state.settled.filter(
          (write) => !isSame(write, action.attachmentId, action.seq),
        ),
        retried: [...state.retried, key],
      };
    }

    case "discard": {
      const key = writeKey(action.attachmentId, action.seq);
      const inFlight = state.inFlight.filter(
        (write) => !isSame(write, action.attachmentId, action.seq),
      );
      const settled = state.settled.filter(
        (write) => !isSame(write, action.attachmentId, action.seq),
      );
      const retried = state.retried.filter((entry) => entry !== key);
      if (
        inFlight.length === state.inFlight.length &&
        settled.length === state.settled.length &&
        retried.length === state.retried.length
      ) {
        return state;
      }
      return { inFlight, settled, retried };
    }

    case "reset":
      return state === EMPTY_WRITE_SETTLEMENT ||
        (state.inFlight.length === 0 &&
          state.settled.length === 0 &&
          state.retried.length === 0)
        ? state
        : EMPTY_WRITE_SETTLEMENT;

    case "attachmentClosed": {
      const prefix = `${action.attachmentId}\u0000`;
      const inFlight = state.inFlight.filter(
        (write) => write.attachmentId !== action.attachmentId,
      );
      const settled = state.settled.filter(
        (write) => write.attachmentId !== action.attachmentId,
      );
      const retried = state.retried.filter((key) => !key.startsWith(prefix));
      if (
        inFlight.length === state.inFlight.length &&
        settled.length === state.settled.length &&
        retried.length === state.retried.length
      ) {
        return state;
      }
      return { inFlight, settled, retried };
    }
  }
}

/** The write a retry must resend verbatim, or null when nothing is retryable. */
export function retryableWrite(
  state: WriteSettlementState,
  attachmentId: string,
  seq: number,
): TerminalWrite | null {
  const settled = state.settled.find((write) =>
    isSame(write, attachmentId, seq),
  );
  if (settled === undefined || !settled.retryable) return null;
  const { outcome, reason, retryable, ...write } = settled;
  void outcome;
  void reason;
  void retryable;
  return write;
}
