/**
 * Copy the Ghostty terminal core wasm into public/ for Vite and production builds.
 */
const fs = require('fs')
const path = require('path')

const SRC = path.join(
  __dirname,
  '..',
  'node_modules',
  '@wterm',
  'ghostty',
  'wasm',
  'ghostty-vt.wasm',
)
const DEST_DIR = path.join(__dirname, '..', 'public', 'wasm')
const DEST = path.join(DEST_DIR, 'ghostty-vt.wasm')

if (!fs.existsSync(SRC)) {
  console.warn(`[copy-ghostty-wasm] Source not found, skipping: ${SRC}`)
} else {
  try {
    fs.mkdirSync(DEST_DIR, { recursive: true })
    fs.copyFileSync(SRC, DEST)
    console.log('[copy-ghostty-wasm] Copied ghostty-vt.wasm')
  } catch (err) {
    console.error(`[copy-ghostty-wasm] Failed to copy ghostty-vt.wasm: ${err.message}`)
    process.exitCode = 1
  }
}
