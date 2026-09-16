/**
 * The rendezvous between an attachment and a measured grid.
 *
 * `terminal_set_viewport` is only meaningful once both facts exist: the daemon
 * keys the frame on `attachment_id` and drops one without it, and the rows and
 * cols come from the renderer, which measures itself asynchronously. The two
 * arrive in either order, so neither call site can send the frame alone.
 */

export interface AttachmentViewport {
  attachmentId: string;
  terminalId: string;
  rows: number;
  cols: number;
}

export interface AttachmentReadiness {
  /** An attach succeeded. A replacement re-arms the rendezvous. */
  attach: (attachmentId: string, terminalId: string) => void;
  /** The renderer measured its grid. */
  size: (rows: number, cols: number) => void;
  /** The viewport to send, exactly once per attachment, or null. */
  take: () => AttachmentViewport | null;
  /** Detach or disconnect: nothing pending may fire at a later attachment. */
  reset: () => void;
}

export function createAttachmentReadiness(): AttachmentReadiness {
  let attachmentId: string | null = null;
  let terminalId = "";
  let rows = 0;
  let cols = 0;
  // Armed means this attachment has not been told its viewport yet. The
  // measured size deliberately survives a re-arm: the pane did not move, so
  // remeasuring would only delay the replacement's first frame.
  let armed = false;

  return {
    attach: (nextAttachmentId, nextTerminalId) => {
      attachmentId = nextAttachmentId;
      terminalId = nextTerminalId;
      armed = true;
    },

    size: (nextRows, nextCols) => {
      // A grid mid-layout reports zero; that is not a viewport worth sending.
      if (nextRows <= 0 || nextCols <= 0) return;
      rows = nextRows;
      cols = nextCols;
    },

    take: () => {
      if (!armed || attachmentId === null || rows <= 0 || cols <= 0)
        return null;
      armed = false;
      return { attachmentId, terminalId, rows, cols };
    },

    reset: () => {
      attachmentId = null;
      terminalId = "";
      armed = false;
    },
  };
}
