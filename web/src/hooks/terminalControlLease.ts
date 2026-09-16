/**
 * The terminal writer lease and the write plane, as one framework-free unit.
 *
 * The daemon grants write authority only through `terminal_take_control` —
 * every attach is observe-only — and `terminal_control_result` carries no
 * `request_id`, so the only correlation the wire supports is single-flight per
 * attachment: "the one request this attachment has outstanding". Clearing that
 * request is therefore what makes a late result grant nothing.
 *
 * Holding the lease, a write goes straight out. Without it the write becomes
 * the single pending slot behind one take-control request, and anything typed
 * after that is refused rather than queued — a queue would replay a burst of
 * keystrokes into a terminal the user may no longer be looking at.
 */

import {
  EMPTY_WRITE_SETTLEMENT,
  createWriteSeqAllocator,
  retryableWrite,
  writeSettlementReducer,
  type WriteKind,
  type WriteSettlementAction,
  type WriteSettlementState,
} from "./terminalWriteSettlement";
import {
  terminalReleaseControlMessage,
  terminalTakeControlMessage,
  terminalWriteMessage,
} from "./tmuxSessionMessages";

/** The single keystroke or paste held while a take-control request is out. */
export interface PendingWrite {
  kind: WriteKind;
  payload: string;
}

export interface ControlLeaseSnapshot {
  /** The attachment holding the daemon's writer lease, or null for none. */
  holder: string | null;
  /** A take or a release is on the wire. */
  pending: boolean;
  /**
   * Another session displaced this attachment. Distinct from simply not
   * holding the lease: it is what puts the pane visibly read-only, and it
   * stands until control is taken again.
   */
  lost: boolean;
  pendingWrite: PendingWrite | null;
  /** Why the last write was refused, rendered as a non-colour cue. */
  refusal: string | null;
  settlement: WriteSettlementState;
}

export const EMPTY_CONTROL_LEASE: ControlLeaseSnapshot = {
  holder: null,
  pending: false,
  lost: false,
  pendingWrite: null,
  refusal: null,
  settlement: EMPTY_WRITE_SETTLEMENT,
};

export interface ControlLeaseDeps {
  socket: () => WebSocket | null;
  attachmentId: () => string | null;
  terminalId: () => string | undefined;
  connectionGeneration: () => number;
  requestTimeoutMs: number;
  onChange: (snapshot: ControlLeaseSnapshot) => void;
}

export interface ControlLease {
  read: () => ControlLeaseSnapshot;
  take: (takeover: boolean) => void;
  release: () => void;
  write: (kind: WriteKind, payload: string) => void;
  retry: (attachmentId: string, seq: number) => void;
  discard: (attachmentId: string, seq: number) => void;
  dismissRefusal: () => void;
  /** True when the frame belonged to the lease or write plane. */
  handle: (message: Record<string, unknown>) => boolean;
  /** An attach succeeded: the new attachment starts observe-only. */
  observe: (attachmentId: string) => void;
  /** An attachment was finalized: its lease, slot and writes die with it. */
  forget: (attachmentId: string) => void;
  /** The socket went; `refusal` is shown only if a slot was actually held. */
  reset: (refusal: string) => void;
}

interface ControlRequest {
  kind: "take" | "release";
  attachmentId: string;
  generation: number;
}

export function createControlLease(deps: ControlLeaseDeps): ControlLease {
  let snapshot = EMPTY_CONTROL_LEASE;
  let request: ControlRequest | null = null;
  let timeout: number | null = null;
  let writeSeq = createWriteSeqAllocator();
  const leaseGenerations = new Map<string, number>();

  const publish = (next: Partial<ControlLeaseSnapshot>): void => {
    const merged = { ...snapshot, ...next };
    if (
      merged.holder === snapshot.holder &&
      merged.pending === snapshot.pending &&
      merged.lost === snapshot.lost &&
      merged.pendingWrite === snapshot.pendingWrite &&
      merged.refusal === snapshot.refusal &&
      merged.settlement === snapshot.settlement
    ) {
      return;
    }
    snapshot = merged;
    deps.onChange(snapshot);
  };

  const settle = (action: WriteSettlementAction): void => {
    publish({
      settlement: writeSettlementReducer(snapshot.settlement, action),
    });
  };

  const openSocket = (): WebSocket | null => {
    const ws = deps.socket();
    return ws !== null && ws.readyState === WebSocket.OPEN ? ws : null;
  };

  const clearTimer = (): void => {
    if (timeout === null) return;
    clearTimeout(timeout);
    timeout = null;
  };

  const clearRequest = (): void => {
    clearTimer();
    request = null;
    publish({ pending: false });
  };

  /** A slot that was never occupied has nothing to refuse. */
  const discardSlot = (refusal: string): void => {
    if (snapshot.pendingWrite === null) return;
    publish({ pendingWrite: null, refusal });
  };

  const sendWrite = (kind: WriteKind, payload: string): void => {
    const ws = openSocket();
    const attachmentId = deps.attachmentId();
    const terminalId = deps.terminalId();
    if (ws === null || attachmentId === null || terminalId === undefined)
      return;
    const seq = writeSeq.next(attachmentId);
    settle({
      type: "sent",
      write: { attachmentId, terminalId, seq, kind, payload },
    });
    ws.send(terminalWriteMessage(kind, terminalId, attachmentId, seq, payload));
  };

  const requestControl = (
    kind: "take" | "release",
    takeover: boolean,
  ): boolean => {
    const ws = openSocket();
    const attachmentId = deps.attachmentId();
    const terminalId = deps.terminalId();
    if (
      request !== null ||
      ws === null ||
      attachmentId === null ||
      terminalId === undefined
    ) {
      return false;
    }
    const generation = deps.connectionGeneration();
    request = { kind, attachmentId, generation };
    publish({ pending: true });
    ws.send(
      kind === "take"
        ? terminalTakeControlMessage(terminalId, attachmentId, takeover)
        : terminalReleaseControlMessage(terminalId, attachmentId),
    );
    timeout = window.setTimeout(() => {
      timeout = null;
      if (
        request === null ||
        request.attachmentId !== attachmentId ||
        request.generation !== generation ||
        request.kind !== kind
      ) {
        return;
      }
      request = null;
      publish({ pending: false });
      if (kind !== "take") return;
      publish({ holder: null });
      discardSlot("Control request timed out.");
    }, deps.requestTimeoutMs);
    return true;
  };

  const onControlResult = (
    attachmentId: string,
    message: Record<string, unknown>,
  ): void => {
    // No request outstanding means this attachment's request was already
    // cleared by a blur, a lease loss, a timeout or a reconnect, so the result
    // grants nothing however it answers.
    if (
      request === null ||
      request.attachmentId !== attachmentId ||
      request.generation !== deps.connectionGeneration()
    ) {
      return;
    }
    const { kind } = request;
    clearRequest();
    if (kind === "release") {
      publish({ holder: null });
      return;
    }
    if (message.granted !== true) {
      publish({ holder: null });
      discardSlot(
        typeof message.reason === "string" && message.reason
          ? `Control refused: ${message.reason}.`
          : "Control refused.",
      );
      return;
    }
    publish({ holder: attachmentId, lost: false });
    const held = snapshot.pendingWrite;
    if (held === null) return;
    // Delivered exactly once, under the generation just installed.
    publish({ pendingWrite: null });
    sendWrite(held.kind, held.payload);
  };

  const onWriteOutcome = (
    attachmentId: string,
    message: Record<string, unknown>,
  ): void => {
    const seq = message.client_write_seq;
    const outcome = message.outcome;
    if (
      typeof seq !== "number" ||
      (outcome !== "delivered" &&
        outcome !== "refused" &&
        outcome !== "indeterminate")
    ) {
      return;
    }
    const reason = typeof message.reason === "string" ? message.reason : null;
    settle({ type: "outcome", attachmentId, seq, outcome, reason });
    // The daemon refusing on authority is the authoritative statement that
    // this attachment is not the holder; keeping a granted flag after it would
    // leave the view silently swallowing keystrokes.
    if (
      outcome === "refused" &&
      (reason === "held" ||
        reason === "lease_lost" ||
        reason === "stale_attachment") &&
      snapshot.holder === attachmentId
    ) {
      publish({ holder: null });
    }
  };

  return {
    read: () => snapshot,

    take: (takeover) => {
      if (snapshot.holder === deps.attachmentId()) return;
      publish({ refusal: null });
      requestControl("take", takeover);
    },

    release: () => {
      // Blur discards the slot: a keystroke typed into a terminal the user has
      // left must never arrive after they look away.
      discardSlot("Control released before the keystroke was sent.");
      const held = snapshot.holder;
      clearRequest();
      if (held === null) return;
      if (!requestControl("release", false)) publish({ holder: null });
    },

    write: (kind, payload) => {
      const attachmentId = deps.attachmentId();
      if (openSocket() === null || attachmentId === null) return;
      if (snapshot.holder === attachmentId) {
        publish({ refusal: null });
        sendWrite(kind, payload);
        return;
      }
      if (snapshot.pendingWrite !== null) {
        publish({ refusal: "Waiting for control of this terminal." });
        return;
      }
      publish({ pendingWrite: { kind, payload }, refusal: null });
      if (request === null && !requestControl("take", false)) {
        discardSlot("Couldn't ask for control of this terminal.");
      }
    },

    retry: (attachmentId, seq) => {
      const ws = openSocket();
      const offer = retryableWrite(snapshot.settlement, attachmentId, seq);
      if (offer === null || ws === null) return;
      settle({ type: "retry", attachmentId, seq });
      // The same seq and the same payload: the daemon fingerprints by seq and
      // replays its recorded outcome, so an identical resend cannot double-write.
      ws.send(
        terminalWriteMessage(
          offer.kind,
          offer.terminalId,
          offer.attachmentId,
          offer.seq,
          offer.payload,
        ),
      );
    },

    discard: (attachmentId, seq) =>
      settle({ type: "discard", attachmentId, seq }),

    dismissRefusal: () => publish({ refusal: null }),

    handle: (message) => {
      const { type } = message;
      if (
        type !== "terminal_control_result" &&
        type !== "terminal_lease_lost" &&
        type !== "terminal_write_outcome"
      ) {
        return false;
      }
      const attachmentId = message.attachment_id;
      if (typeof attachmentId !== "string") return true;
      if (type === "terminal_write_outcome") {
        onWriteOutcome(attachmentId, message);
        return true;
      }
      // Both lease frames carry the lease generation; an older one is a
      // reordered frame and must not steer a lease a newer one already moved.
      const generation = message.lease_generation;
      if (typeof generation === "number") {
        const previous = leaseGenerations.get(attachmentId) ?? -1;
        if (generation < previous) return true;
        leaseGenerations.set(attachmentId, generation);
      }
      if (type === "terminal_control_result") {
        onControlResult(attachmentId, message);
        return true;
      }
      // The daemon fans `terminal_lease_lost` out to every client and names the
      // displaced attachment, so it is ours only when it names ours.
      if (attachmentId !== deps.attachmentId()) return true;
      publish({ holder: null, lost: true });
      clearRequest();
      discardSlot("Another session took control of this terminal.");
      return true;
    },

    observe: (attachmentId) => {
      publish({ holder: null, lost: false });
      clearRequest();
      writeSeq.forget(attachmentId);
    },

    forget: (attachmentId) => {
      settle({ type: "attachmentClosed", attachmentId });
      writeSeq.forget(attachmentId);
      leaseGenerations.delete(attachmentId);
      if (snapshot.holder !== attachmentId) return;
      publish({ holder: null });
      clearRequest();
      discardSlot("The terminal attachment ended.");
    },

    reset: (refusal) => {
      clearTimer();
      request = null;
      leaseGenerations.clear();
      // Fresh counters because every attachment id from the dead socket is
      // dead with it; the daemon holds no high-water mark for them any more.
      writeSeq = createWriteSeqAllocator();
      discardSlot(refusal);
      publish({
        holder: null,
        pending: false,
        lost: false,
        settlement: writeSettlementReducer(snapshot.settlement, {
          type: "reset",
        }),
      });
    },
  };
}
