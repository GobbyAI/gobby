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

# macOS SDK 27 ships a math.h that Zig 0.15's bundled libcxx cannot compile
# ("use of undeclared identifier 'INFINITY'" in __random/clamp_to_integral.h), so
# build against the newest installed SDK below 27 when the active one is 27+.
# Zig caches libc detection in its global cache, so the cache is keyed to the SDK
# too: without that a warm cache silently reuses the incompatible include paths.
sdk_version_of() {
  plutil -extract Version raw "$1/SDKSettings.plist" 2>/dev/null || true
}

select_macos_sdk() {
  [[ "$(uname -s)" == Darwin ]] || return 0
  [[ -z "${SDKROOT:-}" ]] || return 0

  local current current_ver current_major
  current=$(xcrun --show-sdk-path 2>/dev/null || true)
  [[ -n "$current" ]] || return 0
  current_ver=$(sdk_version_of "$current")
  current_major=${current_ver%%.*}
  [[ "$current_major" =~ ^[0-9]+$ ]] || return 0
  ((current_major >= 27)) || return 0

  # Command Line Tools first so an Xcode SDK wins ties: Xcode's default MacOSX.sdk
  # is often older than the Command Line Tools one, and pointing DEVELOPER_DIR at
  # Xcode is what actually redirects Zig's libc detection.
  local best="" best_ver="" dir ver major newest
  for dir in /Library/Developer/CommandLineTools/SDKs/MacOSX*.sdk \
             /Applications/Xcode.app/Contents/Developer/Platforms/MacOSX.platform/Developer/SDKs/MacOSX*.sdk; do
    [[ -d "$dir" ]] || continue
    ver=$(sdk_version_of "$dir")
    major=${ver%%.*}
    [[ "$major" =~ ^[0-9]+$ ]] || continue
    ((major < 27)) || continue
    if [[ -z "$best_ver" ]]; then
      best=$dir best_ver=$ver
    else
      newest=$(printf '%s\n%s\n' "$best_ver" "$ver" | sort -V | tail -1)
      # Explicit if, not `[[ ]] && assign`: a false test there would return 1 from
      # the loop body and trip `set -e`.
      if [[ "$newest" == "$ver" ]]; then
        best=$dir
        best_ver=$ver
      fi
    fi
  done

  if [[ -z "$best" ]]; then
    echo "warning: active macOS SDK is $current_ver and no SDK below 27 is installed;" >&2
    echo "         the libghostty-vt build will likely fail inside Zig's bundled libcxx." >&2
    return 0
  fi

  export SDKROOT="$best"
  case "$best" in
    */Contents/Developer/Platforms/MacOSX.platform/Developer/SDKs/*)
      export DEVELOPER_DIR="${best%%/Platforms/MacOSX.platform/Developer/SDKs/*}"
      ;;
  esac
  # Both caches memoize the detected libc include paths, so both must be keyed to
  # the SDK; keying only one lets the other reuse the incompatible paths.
  export ZIG_GLOBAL_CACHE_DIR="${ZIG_GLOBAL_CACHE_DIR:-$HOME/.cache/zig}/sdk-$best_ver"
  export ZIG_LOCAL_CACHE_DIR="${ZIG_LOCAL_CACHE_DIR:-$VENDORED_DIR/.zig-cache}/sdk-$best_ver"
  echo "note: macOS SDK $current_ver is incompatible with Zig 0.15's libcxx; building against SDK $best_ver" >&2
}

select_macos_sdk

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
