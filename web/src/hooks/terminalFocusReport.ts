import { terminalFocusMessage } from "./tmuxSessionMessages";

/**
 * Reports web pane focus to the daemon so operator comms wakes hold while the
 * operator is at the pane (pane-focus-wake-gating, 3.1). The report rides on
 * the web lease: focused while this attachment holds it, and expired to
 * unfocused after the hold without operator activity, with the lease kept.
 */

export interface FocusReportDeps {
  socket: () => WebSocket | null;
  terminalId: () => string | undefined;
  holdSeconds: () => number;
}

export interface FocusReport {
  sync: (holder: string | null, attachmentId: string | null) => void;
  activity: () => void;
  listen: (target: Document) => () => void;
  dispose: () => void;
}

// Pointer motion is not activity: a cursor drifting over the pane is not the
// operator at it. Renderer replies arrive through onData, not these events.
const ACTIVITY_EVENTS = ["keydown", "paste", "pointerdown", "wheel"] as const;

export function createFocusReport(deps: FocusReportDeps): FocusReport {
  // The attachment this client holds the lease with, or null.
  let held: string | null = null;
  // The attachment last reported focused, or null after expiry or loss.
  let reported: string | null = null;
  let timer: number | null = null;

  // A report is a state signal: when the socket is not open it is dropped, and
  // the reconnect's new attachment or the daemon's finalize repairs it.
  function send(focused: boolean, attachmentId: string): void {
    const socket = deps.socket();
    const terminalId = deps.terminalId();
    if (socket?.readyState !== WebSocket.OPEN || terminalId === undefined) {
      return;
    }
    socket.send(terminalFocusMessage(terminalId, attachmentId, focused));
  }

  function clearTimer(): void {
    if (timer !== null) {
      window.clearTimeout(timer);
      timer = null;
    }
  }

  function expire(): void {
    timer = null;
    if (reported !== null) send(false, reported);
    reported = null;
  }

  function arm(): void {
    clearTimer();
    const seconds = deps.holdSeconds();
    if (seconds > 0) timer = window.setTimeout(expire, seconds * 1000);
  }

  function activity(): void {
    if (held === null) return;
    if (reported === null) {
      send(true, held);
      reported = held;
    }
    arm();
  }

  return {
    sync(holder, attachmentId) {
      held = holder !== null && holder === attachmentId ? holder : null;
      if (held === null) {
        // Release, takeover and finalize already cleared the daemon's flag.
        clearTimer();
        reported = null;
        return;
      }
      if (reported !== held) {
        // The grant follows the operator focusing the pane, so it is activity.
        send(true, held);
        reported = held;
        arm();
      }
    },
    activity,
    listen(target) {
      for (const type of ACTIVITY_EVENTS) {
        target.addEventListener(type, activity, {
          capture: true,
          passive: true,
        });
      }
      return () => {
        for (const type of ACTIVITY_EVENTS) {
          target.removeEventListener(type, activity, { capture: true });
        }
      };
    },
    dispose() {
      clearTimer();
      held = null;
      reported = null;
    },
  };
}
