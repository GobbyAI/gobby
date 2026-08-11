import { GhosttyCore } from '@wterm/ghostty'

import { withGobbyAnsiPalette } from './ghosttyAnsiPalette'

const WASM_PATH = '/wasm/ghostty-vt.wasm'

export function loadGhosttyCore(): Promise<GhosttyCore> {
  return GhosttyCore.load({ wasmPath: WASM_PATH }).then(withGobbyAnsiPalette)
}
