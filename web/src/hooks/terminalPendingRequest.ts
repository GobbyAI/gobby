import type { TmuxTarget } from "./terminalRosterSnapshot";

export type PendingRequest =
  | {
      kind: "attach";
      requestId: string;
      generation: number;
      target: TmuxTarget;
    }
  | {
      kind: "detach";
      requestId: string;
      generation: number;
      nextTarget: TmuxTarget | null;
    }
  | {
      kind: "create";
      requestId: string;
      generation: number;
    };

export interface PendingRequestSlotOptions {
  /** The live connection generation; a result from an older socket answers nothing. */
  generation: () => number;
  timeoutMs: number;
  /** Fires when the slot fills or empties, so the view can mirror it. */
  onChange: (pending: boolean) => void;
  /** Fires after the slot empties because its request outlived `timeoutMs`. */
  onTimeout: (error: string) => void;
}

export interface PendingRequestSlot {
  /** The request in flight, or null. */
  current(): PendingRequest | null;
  /** Fill the slot and start its timeout. Callers check `current()` first. */
  begin(request: PendingRequest): void;
  /** The outstanding request a result answers, or null when it answers none. */
  match(requestId: unknown): PendingRequest | null;
  /** Empty the slot and cancel its timeout. */
  clear(): void;
  /** Empty the slot without notifying; for teardown. */
  dispose(): void;
}

export function createPendingRequestSlot(
  options: PendingRequestSlotOptions,
): PendingRequestSlot {
  let slot: PendingRequest | null = null;
  let timer: number | null = null;

  const cancelTimer = () => {
    if (timer !== null) {
      clearTimeout(timer);
      timer = null;
    }
  };

  return {
    current: () => slot,
    begin(request) {
      cancelTimer();
      slot = request;
      options.onChange(true);
      timer = window.setTimeout(() => {
        timer = null;
        const pending = slot;
        if (
          pending?.requestId !== request.requestId ||
          pending.generation !== request.generation ||
          pending.kind !== request.kind
        ) {
          return;
        }
        slot = null;
        options.onChange(false);
        // Rendered after a full stop ("Couldn't attach to this terminal. "),
        // so it has to stand on its own as a sentence.
        options.onTimeout(
          `${request.kind[0].toUpperCase()}${request.kind.slice(1)} request timed out.`,
        );
      }, options.timeoutMs);
    },
    match(requestId) {
      if (
        !slot ||
        slot.requestId !== requestId ||
        slot.generation !== options.generation()
      ) {
        return null;
      }
      return slot;
    },
    clear() {
      cancelTimer();
      slot = null;
      options.onChange(false);
    },
    dispose() {
      cancelTimer();
      slot = null;
    },
  };
}
