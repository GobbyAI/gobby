import { GhosttyCore } from '@wterm/ghostty'

const WASM_PATH = '/wasm/ghostty-vt.wasm'

export function loadGhosttyCore(): Promise<GhosttyCore> {
  return GhosttyCore.load({ wasmPath: WASM_PATH })
}
