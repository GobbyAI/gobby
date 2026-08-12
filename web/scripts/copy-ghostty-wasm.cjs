/**
 * Copy the Ghostty terminal core wasm into public/ for Vite and production builds.
 */
const fs = require("fs");
const path = require("path");

const DEST_DIR = path.join(__dirname, "..", "public", "wasm");
const DEST = path.join(DEST_DIR, "ghostty-vt.wasm");

try {
  const packageEntry = require.resolve("@wterm/ghostty");
  const packageRoot = path.resolve(path.dirname(packageEntry), "..");
  const src = path.join(packageRoot, "wasm", "ghostty-vt.wasm");
  if (!fs.existsSync(src)) {
    throw new Error(`Ghostty WASM source not found: ${src}`);
  }
  fs.mkdirSync(DEST_DIR, { recursive: true });
  fs.copyFileSync(src, DEST);
  console.log("[copy-ghostty-wasm] Copied ghostty-vt.wasm");
} catch (err) {
  const message = err instanceof Error ? err.message : String(err);
  console.error(
    `[copy-ghostty-wasm] Failed to copy ghostty-vt.wasm: ${message}`,
  );
  process.exitCode = 1;
}
