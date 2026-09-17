/**
 * The terminal output plane: scrollback history once, then the live stream.
 *
 * Single consumer by design — the attached terminal view owns the stream — so
 * this is a pair of callback slots rather than a subscription list, and a
 * registration replaces whatever was registered before.
 */

import { boundAttachHistory } from "./terminalHistoryBounds";

/** A bounded scrollback window delivered once, just before streaming starts. */
export interface TerminalAttachHistory {
  streamingId: string;
  text: string;
  /** Older history existed and was cut, by the line bound or the byte bound. */
  truncated: boolean;
  /** Capture failed while the stream itself stayed healthy. */
  unavailable: boolean;
  droppedBytes: number;
  totalBytes: number;
}

/**
 * The daemon's answer to `terminal_set_scroll_offset`. It arrives twice for a
 * proxied native attachment — once as the daemon's own clamp against the
 * ceiling the client proposed, then once relayed from gterm, which owns the
 * real scrollback depth. Both are applied the same way; the later one wins.
 */
export interface TerminalScrollApplied {
  streamingId: string;
  appliedRows: number;
  maxRows: number;
}

export interface TerminalOutputSink {
  onOutput: (callback: (runId: string, data: string) => void) => void;
  onAttachHistory: (callback: (history: TerminalAttachHistory) => void) => void;
  onScrollOffsetApplied: (
    callback: (applied: TerminalScrollApplied) => void,
  ) => void;
  /** True when the frame belonged to the output plane. */
  handle: (message: Record<string, unknown>) => boolean;
}

export function createTerminalOutputSink(): TerminalOutputSink {
  let output: ((runId: string, data: string) => void) | null = null;
  let history: ((history: TerminalAttachHistory) => void) | null = null;
  let scrolled: ((applied: TerminalScrollApplied) => void) | null = null;

  const onHistoryFrame = (message: Record<string, unknown>): void => {
    // The host proxy keys history on the attachment; that id doubles as the
    // streaming id the scrollback consumer registered against.
    const attachmentId = message.attachment_id;
    if (typeof attachmentId !== "string") {
      console.warn("Ignoring terminal attach history without attachment_id", {
        terminal_id: message.terminal_id,
        type: message.type,
      });
      return;
    }
    // Bounded here, at the one point the window is built, so every consumer
    // below — the scrollback view and the fallback append alike — is handed a
    // window the renderer can afford.
    const { text, droppedLines } = boundAttachHistory(
      typeof message.text === "string" ? message.text : "",
    );
    if (history === null) {
      // No scrollback consumer: the window is still output, so it is better
      // appended to the stream than dropped.
      if (output !== null && text) output(attachmentId, text);
      return;
    }
    history({
      streamingId: attachmentId,
      text,
      // The daemon's own cut and this client's cut mean the same thing to the
      // reader: output existed above this window and is not shown.
      truncated: message.truncated === true || droppedLines > 0,
      unavailable: message.unavailable === true,
      droppedBytes:
        typeof message.dropped_bytes === "number" ? message.dropped_bytes : 0,
      totalBytes:
        typeof message.total_bytes === "number" ? message.total_bytes : 0,
    });
  };

  return {
    onOutput: (callback) => {
      output = callback;
    },

    onAttachHistory: (callback) => {
      history = callback;
    },

    onScrollOffsetApplied: (callback) => {
      scrolled = callback;
    },

    handle: (message) => {
      if (message.type === "terminal_attach_history") {
        onHistoryFrame(message);
        return true;
      }
      if (message.type === "terminal_scroll_offset_applied") {
        const attachmentId = message.attachment_id;
        // Wire input, so both counters are checked rather than asserted: a
        // frame missing either would otherwise unpin the pane at NaN rows.
        if (
          scrolled !== null &&
          typeof attachmentId === "string" &&
          typeof message.applied_rows === "number" &&
          typeof message.max_rows === "number"
        ) {
          scrolled({
            streamingId: attachmentId,
            appliedRows: message.applied_rows,
            maxRows: message.max_rows,
          });
        }
        return true;
      }
      if (message.type !== "terminal_output") return false;
      const attachmentId = message.attachment_id;
      // Both fields are checked rather than asserted: this is wire input, and
      // a frame missing `data` would otherwise reach the renderer as undefined.
      if (
        output !== null &&
        typeof attachmentId === "string" &&
        typeof message.data === "string"
      ) {
        output(attachmentId, message.data);
      }
      return true;
    },
  };
}
