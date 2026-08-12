/**
 * Copy VAD (Voice Activity Detection) assets from @ricky0123/vad-web
 * into the public/ directory so they can be served as static assets.
 */
const fs = require("fs");
const path = require("path");

const SRC_DIR = path.join(
  __dirname,
  "..",
  "node_modules",
  "@ricky0123",
  "vad-web",
  "dist",
);
const DEST_DIR = path.join(__dirname, "..", "public");

const FILES = ["silero_vad_legacy.onnx", "vad.worklet.bundle.min.js"];

for (const file of FILES) {
  const src = path.join(SRC_DIR, file);
  const dest = path.join(DEST_DIR, file);
  if (!fs.existsSync(src)) {
    console.warn(`[copy-vad-assets] Source not found, skipping: ${src}`);
    continue;
  }
  try {
    fs.copyFileSync(src, dest);
    console.log(`[copy-vad-assets] Copied ${file}`);
  } catch (err) {
    console.error(`[copy-vad-assets] Failed to copy ${file}: ${err.message}`);
  }
}
