/**
 * What one attachment may accumulate before the client stops reassembling and
 * asks for a fresh snapshot instead. Holding a partial message costs more than
 * its payload — each fragment carries its own frame, timer slot and array entry
 * — so the charge adds a flat per-fragment overhead. A flood of tiny fragments
 * is the shape that actually starves the client, and payload bytes alone would
 * not notice it.
 */
export const TERMINAL_FRAGMENT_BUDGET_BYTES = 256 * 1024;
export const TERMINAL_FRAGMENT_OVERHEAD_BYTES = 256;
export const TERMINAL_WS_FRAGMENT_MAX_SOCKET_REASSEMBLY_BYTES =
  64 * 1024 * 1024;
export const TERMINAL_WS_FRAGMENT_REASSEMBLY_TIMEOUT_MS = 5000;
export const TERMINAL_WS_SAFE_INTEGER_MAX = Number.MAX_SAFE_INTEGER;

export type FragmentErrorCode =
  | "fragment_sequence"
  | "fragment_timeout"
  | "fragment_too_large"
  | "fragment_socket_budget";

export interface TerminalWsReducerOptions {
  now?: () => number;
  timeoutMs?: number;
  budgetBytes?: number;
  fragmentOverheadBytes?: number;
  maxSocketBytes?: number;
}

interface BufferState {
  event: string;
  terminalId: string;
  messageSeq: number;
  nextIndex: number;
  chunks: string[];
  bytes: number;
  /** Payload bytes plus the per-fragment overhead charge. */
  charged: number;
  startedAt: number;
}

function decodePayload(payload: unknown): Uint8Array | null {
  if (typeof payload !== "string") return null;
  try {
    const binary = atob(payload);
    const bytes = new Uint8Array(binary.length);
    for (let index = 0; index < binary.length; index += 1) {
      bytes[index] = binary.charCodeAt(index);
    }
    return bytes;
  } catch {
    return null;
  }
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null
    ? (value as Record<string, unknown>)
    : null;
}

export function createTerminalWsReducer(
  options: TerminalWsReducerOptions = {},
) {
  const now = options.now ?? (() => Date.now());
  const timeoutMs =
    options.timeoutMs ?? TERMINAL_WS_FRAGMENT_REASSEMBLY_TIMEOUT_MS;
  const budget = options.budgetBytes ?? TERMINAL_FRAGMENT_BUDGET_BYTES;
  const overhead =
    options.fragmentOverheadBytes ?? TERMINAL_FRAGMENT_OVERHEAD_BYTES;
  const maxSocket =
    options.maxSocketBytes ?? TERMINAL_WS_FRAGMENT_MAX_SOCKET_REASSEMBLY_BYTES;
  const live = new Set<string>();
  const buffers = new Map<string, BufferState>();
  const applied: Record<string, unknown>[] = [];
  const errors: { code: FragmentErrorCode; attachment_id: string }[] = [];
  // Attachments whose stream was abandoned mid-message: nothing from them is
  // trusted until their replacement snapshot lands.
  const pinned = new Set<string>();
  const refreshRequests: string[] = [];
  let socketBytes = 0;

  /**
   * Abandon the attachment's stream and ask for a snapshot, once. A second
   * over-budget fragment of the same doomed message must not queue a second
   * refresh; the first one already covers everything that follows.
   */
  const requestRefresh = (attachmentId: string) => {
    if (pinned.has(attachmentId)) return;
    pinned.add(attachmentId);
    refreshRequests.push(attachmentId);
  };

  const dropBuffer = (attachmentId: string, code?: FragmentErrorCode) => {
    const current = buffers.get(attachmentId);
    if (current === undefined) return;
    socketBytes -= current.bytes;
    if (socketBytes < 0) socketBytes = 0;
    buffers.delete(attachmentId);
    if (code !== undefined) {
      errors.push({ code, attachment_id: attachmentId });
    }
  };

  const applyEvent = (event: Record<string, unknown>) => {
    applied.push(event);
    if (event.type === "terminal_attachment_finalized") {
      const id = event.attachment_id;
      if (typeof id === "string") {
        dropBuffer(id);
        live.delete(id);
      }
    }
  };

  const pushFragment = (message: Record<string, unknown>) => {
    const attachmentId = message.attachment_id;
    if (typeof attachmentId !== "string" || !live.has(attachmentId)) return;
    const seq = message.message_seq;
    if (typeof seq !== "number" || !Number.isSafeInteger(seq) || seq < 0)
      return;
    if (seq > TERMINAL_WS_SAFE_INTEGER_MAX) return;
    const index = message.fragment_index;
    if (typeof index !== "number" || index < 0 || !Number.isInteger(index)) {
      dropBuffer(attachmentId, "fragment_sequence");
      return;
    }
    const event = typeof message.event === "string" ? message.event : "";
    const terminalId =
      typeof message.terminal_id === "string" ? message.terminal_id : "";
    const decoded = decodePayload(message.payload);
    if (decoded === null) {
      dropBuffer(attachmentId, "fragment_sequence");
      return;
    }
    let current = buffers.get(attachmentId);
    if (current !== undefined && current.messageSeq !== seq) {
      dropBuffer(attachmentId, "fragment_sequence");
      return;
    }
    if (current === undefined) {
      if (index !== 0) {
        errors.push({ code: "fragment_sequence", attachment_id: attachmentId });
        return;
      }
      current = {
        event,
        terminalId,
        messageSeq: seq,
        nextIndex: 0,
        chunks: [],
        bytes: 0,
        charged: 0,
        startedAt: now(),
      };
      buffers.set(attachmentId, current);
    }
    if (current.event !== event || current.terminalId !== terminalId) {
      dropBuffer(attachmentId, "fragment_sequence");
      return;
    }
    if (index !== current.nextIndex) {
      dropBuffer(attachmentId, "fragment_sequence");
      return;
    }
    if (current.charged + decoded.length + overhead > budget) {
      // Over budget the partial message is worthless: it can never complete,
      // and the bytes already held are a torn prefix of a screen. Drop it and
      // let the snapshot re-establish the truth.
      dropBuffer(attachmentId, "fragment_too_large");
      requestRefresh(attachmentId);
      return;
    }
    if (socketBytes + decoded.length > maxSocket) {
      dropBuffer(attachmentId, "fragment_socket_budget");
      return;
    }
    current.chunks.push(new TextDecoder().decode(decoded));
    current.bytes += decoded.length;
    current.charged += decoded.length + overhead;
    socketBytes += decoded.length;
    current.nextIndex += 1;
    if (message.more === true) return;
    const reconstructed = current.chunks.join("");
    dropBuffer(attachmentId);
    try {
      const parsed: unknown = JSON.parse(reconstructed);
      const record = asRecord(parsed);
      if (record !== null) applyEvent(record);
    } catch {
      errors.push({ code: "fragment_sequence", attachment_id: attachmentId });
    }
  };

  return {
    get applied() {
      return applied;
    },
    get errors() {
      return errors;
    },
    get socketBytes() {
      return socketBytes;
    },
    get refreshRequests(): readonly string[] {
      return refreshRequests;
    },
    /** True while this attachment is waiting for its replacement snapshot. */
    isPinned(attachmentId: string) {
      return pinned.has(attachmentId);
    },
    /** Drain the pending refreshes so each one is sent exactly once. */
    takeRefreshRequests(): string[] {
      return refreshRequests.splice(0);
    },
    markLive(attachmentId: string) {
      live.add(attachmentId);
    },
    finalize(attachmentId: string) {
      dropBuffer(attachmentId);
      live.delete(attachmentId);
      pinned.delete(attachmentId);
    },
    disconnect() {
      for (const id of [...buffers.keys()]) dropBuffer(id);
      live.clear();
      pinned.clear();
      refreshRequests.splice(0);
    },
    tick(nowMs: number) {
      for (const [id, buffer] of [...buffers.entries()]) {
        if (nowMs - buffer.startedAt >= timeoutMs) {
          dropBuffer(id, "fragment_timeout");
        }
      }
    },
    push(message: Record<string, unknown>) {
      const pinnedId = message.attachment_id;
      if (typeof pinnedId === "string" && pinned.has(pinnedId)) {
        // The snapshot is the rendezvous: it replaces history wholesale, so it
        // is the first frame that can be trusted again. Everything else on a
        // pinned attachment is pre-refresh output that would paint a torn
        // screen, and finalization ends the attachment either way.
        if (message.type === "terminal_attach_history") {
          pinned.delete(pinnedId);
        } else if (message.type !== "terminal_attachment_finalized") {
          return;
        }
      }
      if (message.type === "terminal_ws_fragment") {
        pushFragment(message);
        return;
      }
      if (message.type === "terminal_attachment_finalized") {
        applyEvent(message);
        return;
      }
      const attachmentId = message.attachment_id;
      if (typeof attachmentId === "string" && !live.has(attachmentId)) return;
      applyEvent(message);
    },
  };
}

export type TerminalWsReducer = ReturnType<typeof createTerminalWsReducer>;
