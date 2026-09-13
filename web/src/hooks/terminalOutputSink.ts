/**
 * The terminal output plane: scrollback history once, then the live stream.
 *
 * Single consumer by design — the attached terminal view owns the stream — so
 * this is a pair of callback slots rather than a subscription list, and a
 * registration replaces whatever was registered before.
 */

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

export interface TerminalOutputSink {
  onOutput: (callback: (runId: string, data: string) => void) => void;
  onAttachHistory: (
    callback: (history: TerminalAttachHistory) => void,
  ) => void;
  /** True when the frame belonged to the output plane. */
  handle: (message: Record<string, unknown>) => boolean;
}

export function createTerminalOutputSink(): TerminalOutputSink {
  let output: ((runId: string, data: string) => void) | null = null;
  let history: ((history: TerminalAttachHistory) => void) | null = null;

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
    const text = typeof message.text === "string" ? message.text : "";
    if (history === null) {
      // No scrollback consumer: the window is still output, so it is better
      // appended to the stream than dropped.
      if (output !== null && text) output(attachmentId, text);
      return;
    }
    history({
      streamingId: attachmentId,
      text,
      truncated: message.truncated === true,
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

    handle: (message) => {
      if (message.type === "terminal_attach_history") {
        onHistoryFrame(message);
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
