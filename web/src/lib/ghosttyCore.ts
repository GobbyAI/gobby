import { GhosttyCore } from "@wterm/ghostty";

import { withGobbyAnsiPalette } from "./ghosttyAnsiPalette";

const WASM_PATH = "/wasm/ghostty-vt.wasm";

// libghostty's max_scrollback is denominated in BYTES (the wasm binding is
// init(cols, rows, max_scrollback)), and the wrapper's default of 10000 is
// therefore a 10 KB ring -- about 800 rows at 80 columns, evicted from the
// front. 10 MB is ghostty's own desktop default: it retains the full
// configurable attach-history window (tmux.attach_history_lines caps at
// 2000 lines) at any real pane width, and bounds a long-lived live session's
// scrollback memory at 10 MB per terminal.
const SCROLLBACK_LIMIT_BYTES = 10_000_000;

export function loadGhosttyCore(): Promise<GhosttyCore> {
  return GhosttyCore.load({
    wasmPath: WASM_PATH,
    scrollbackLimit: SCROLLBACK_LIMIT_BYTES,
  }).then(withGobbyAnsiPalette);
}
