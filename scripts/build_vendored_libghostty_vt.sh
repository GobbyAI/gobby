#!/usr/bin/env bash
# Build the vendored libghostty-vt tree with Zig 0.15.
# Triple map matches crates/gterminal/build.rs::zig_target.
# On macOS SDK 26.4+, use Homebrew zig@0.15 (LLVM backend); the official
# 0.15.2 tarball fails to link the build runner against libSystem.
set -euo pipefail

ROOT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
VENDORED_DIR=${VENDORED_GHOSTTY_DIR:-"$ROOT_DIR/crates/gterminal/vendor/libghostty-vt"}
OPTIMIZE=${LIBGHOSTTY_VT_OPTIMIZE:-ReleaseFast}
SIMD=${LIBGHOSTTY_VT_SIMD:-true}
ZIG_BIN=${ZIG:-zig}

zig_target() {
  case "$1" in
    x86_64-unknown-linux-gnu) echo x86_64-linux-gnu ;;
    aarch64-unknown-linux-gnu) echo aarch64-linux-gnu ;;
    x86_64-unknown-linux-musl) echo x86_64-linux-musl ;;
    aarch64-unknown-linux-musl) echo aarch64-linux-musl ;;
    x86_64-apple-darwin) echo x86_64-macos ;;
    aarch64-apple-darwin) echo aarch64-macos ;;
    x86_64-pc-windows-msvc) echo x86_64-windows-msvc ;;
    aarch64-pc-windows-msvc) echo aarch64-windows-msvc ;;
    *)
      echo "error: unsupported target for libghostty-vt build: $1" >&2
      exit 1
      ;;
  esac
}

if [[ ! -f "$VENDORED_DIR/build.zig" ]]; then
  echo "error: vendored libghostty-vt source not found at $VENDORED_DIR" >&2
  exit 1
fi

if [[ ! -x "$ZIG_BIN" ]] && ! command -v "$ZIG_BIN" >/dev/null 2>&1; then
  echo "error: zig not found (required Zig 0.15). Install Zig 0.15 and ensure it is on PATH, or set ZIG to the zig binary." >&2
  exit 1
fi

rust_triple=${GTERM_TARGET:-}
if [[ -z "$rust_triple" ]]; then
  if command -v rustc >/dev/null 2>&1; then
    rust_triple=$(rustc -vV | awk '/^host:/{print $2}')
  else
    echo "error: rustc not found; set GTERM_TARGET to a Rust target triple" >&2
    exit 1
  fi
fi
zig_tgt=$(zig_target "$rust_triple")
version_string=$(tr -d '[:space:]' < "$VENDORED_DIR/VERSION")

# Native builds omit -Dtarget so Zig links the host libSystem (herdr's helper
# does the same). Cross builds pass GTERM_TARGET and get the mapped Zig triple.
build_args=(
  build
  -Demit-lib-vt
  "-Doptimize=${OPTIMIZE}"
  "-Dsimd=${SIMD}"
  "-Dversion-string=${version_string}"
  -Demit-xcframework=false
)
if [[ -n "${GTERM_TARGET:-}" ]]; then
  build_args+=("-Dtarget=${zig_tgt}")
fi

cd "$VENDORED_DIR"
"$ZIG_BIN" "${build_args[@]}" "$@"

echo
printf 'built libghostty-vt in %s/zig-out\n' "$VENDORED_DIR"
