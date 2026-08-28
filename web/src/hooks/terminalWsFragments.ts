export const TERMINAL_WS_FRAGMENT_MAX_REASSEMBLY_BYTES = 16 * 1024 * 1024;
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
  maxReassemblyBytes?: number;
  maxSocketBytes?: number;
}

interface BufferState {
  event: string;
  terminalId: string;
  messageSeq: number;
  nextIndex: number;
  chunks: string[];
  bytes: number;
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
  const maxReassembly =
    options.maxReassemblyBytes ?? TERMINAL_WS_FRAGMENT_MAX_REASSEMBLY_BYTES;
  const maxSocket =
    options.maxSocketBytes ?? TERMINAL_WS_FRAGMENT_MAX_SOCKET_REASSEMBLY_BYTES;
  const live = new Set<string>();
  const buffers = new Map<string, BufferState>();
  const applied: Record<string, unknown>[] = [];
  const errors: { code: FragmentErrorCode; attachment_id: string }[] = [];
  let socketBytes = 0;

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
    if (current.bytes + decoded.length > maxReassembly) {
      dropBuffer(attachmentId, "fragment_too_large");
      return;
    }
    if (socketBytes + decoded.length > maxSocket) {
      dropBuffer(attachmentId, "fragment_socket_budget");
      return;
    }
    current.chunks.push(new TextDecoder().decode(decoded));
    current.bytes += decoded.length;
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
    markLive(attachmentId: string) {
      live.add(attachmentId);
    },
    finalize(attachmentId: string) {
      dropBuffer(attachmentId);
      live.delete(attachmentId);
    },
    disconnect() {
      for (const id of [...buffers.keys()]) dropBuffer(id);
      live.clear();
    },
    tick(nowMs: number) {
      for (const [id, buffer] of [...buffers.entries()]) {
        if (nowMs - buffer.startedAt >= timeoutMs) {
          dropBuffer(id, "fragment_timeout");
        }
      }
    },
    push(message: Record<string, unknown>) {
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
