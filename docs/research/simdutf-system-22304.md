# System simdutf experiment — #22304

Verified 2026-09-13 on arm64 macOS with the Command Line Tools macOS 27.0 SDK.

## Outcome

Our existing Ghostty pin works with system simdutf 9.1.2: **582 Gobby terminal
unit and frame-production tests passed**. A Ghostty upgrade is unnecessary for
this local configuration. This does not prove that 9.1.2 fixed a simdutf defect:
9.1.1 was not tested. No production build configuration or installed binary changed.

Current upstream Ghostty contains the exact SDK compatibility fix, but is not a
drop-in Gobby upgrade: its C API changed, and its Apple shared linker omits system
simdutf dependencies. Explicit linkage resolved the latter in this experiment.

## Versions and isolation

- Gobby baseline: `6f0ef09d502c05b7c9ca8ae90f93c649edb47df6`.
- Existing Ghostty: `c5a21edfcbc2d5b46540ad91b7980aca31f5f1f3`, with our patches.
- Upstream Ghostty: `7aab0a0392369613472bd5dcfd66bef58e78c3ec`.
- Baseline compiler: Homebrew Zig 0.15.2; candidate: official Zig 0.16.0 arm64 macOS.
- Downloaded Zig tarball SHA256: `b23d70deaa879b5c2d486ed3316f7eaa53e84acf6fc9cc747de152450d401489`.
- System simdutf: Homebrew 9.1.2, `/opt/homebrew/opt/simdutf`.
- Managed Gobby worktree: `/private/tmp/gobby-simdutf-22304`.
- Managed upstream clone: `/Users/josh/.gobby/clones/ghostty-simdutf-22304`.
- Scratch logs, compiler, artifacts: `/tmp/simdutf-22304`.

Initial baseline Rust checks used the managed shared Cargo target. The final
system-library check used a dedicated Cargo target after another session held
the shared build lock. No daemon was started or restarted.

## Results

| Configuration | Result |
| --- | --- |
| Existing Ghostty, Zig 0.15.2, bundled SIMD, static-only patch | Build passed; 580 terminal unit tests passed under nextest |
| Upstream Ghostty, Zig 0.16.0, bundled SIMD | Static and shared libraries built; shared library depends only on libSystem |
| Upstream Ghostty, system simdutf | Static archive built; native shared link failed on unresolved simdutf functions |
| Same archive, explicit Apple linkage | Shared library built; static/shared formatter examples ran with identical output |
| Upstream system build, base64/utf8 tests | 128 passed, 2 skipped across two test artifacts |
| Gobby bindings plus upstream bundled archive | Link failed on three removed C functions (listed below) |
| Existing Ghostty, Zig 0.15.2, system simdutf, static-only patch | 580 unit tests plus 2 frame-production tests passed |

Upstream no longer exports `ghostty_render_state_colors_get`,
`ghostty_terminal_mode_get`, and `ghostty_terminal_mode_set`, all referenced by
our current Rust bindings. Migrating that interface is separate from selecting
system SIMD. No compatibility shim was introduced to force this probe to pass.

## Reproduction

Use the pinned sources above. The following commands assume these scratch paths:

```sh
SIMD_SCRATCH=/tmp/simdutf-22304
ZIG16="$SIMD_SCRATCH/zig-aarch64-macos-0.16.0/zig"
```

In the Gobby worktree, establish the bundled baseline:

```sh
bash scripts/build_vendored_libghostty_vt.sh
cargo nextest run -p gobby-terminal --features vt-engine --lib
```

In the upstream clone, compare bundled and system builds and run focused tests:

```sh
"$ZIG16" build -Demit-lib-vt -Doptimize=ReleaseFast \
  -Demit-xcframework=false --prefix "$SIMD_SCRATCH/upstream-bundled"
"$ZIG16" build -Demit-lib-vt -Doptimize=ReleaseFast \
  -Demit-xcframework=false -fsys=simdutf \
  --search-prefix /opt/homebrew/opt/simdutf \
  --prefix "$SIMD_SCRATCH/upstream-system" --summary all
"$ZIG16" build test-lib-vt -Demit-lib-vt -Doptimize=ReleaseFast \
  -Dtest-filter=base64 -Dtest-filter=utf8 -fsys=simdutf \
  --search-prefix /opt/homebrew/opt/simdutf --summary all
```

The second build fails at the shared link; the emitted static archive is usable.
This explicit command resolves its missing link dependencies:

```sh
xcrun clang -dynamiclib -Wl,-all_load \
  "$SIMD_SCRATCH/upstream-system/lib/libghostty-vt.a" \
  -L/opt/homebrew/opt/simdutf/lib -lsimdutf -lc++ -Wl,-dead_strip \
  -install_name "$SIMD_SCRATCH/upstream-system/lib/libghostty-vt.dylib" \
  -o "$SIMD_SCRATCH/upstream-system/lib/libghostty-vt.dylib"
```

Compile `example/c-vt-formatter/src/main.c` using `xcrun clang`, `-Iinclude`,
and `-mmacosx-version-min=26.0`. For static linkage pass the archive above,
`-L/opt/homebrew/opt/simdutf/lib -lsimdutf -lc++`. For shared linkage pass
`-L/tmp/simdutf-22304/upstream-system/lib -lghostty-vt`. Run both binaries and
compare stdout with `cmp`: all returned zero, and outputs matched.

In the Gobby worktree, build the existing pin with system simdutf:

```sh
bash scripts/build_vendored_libghostty_vt.sh -fsys=simdutf \
  --search-prefix /opt/homebrew/opt/simdutf \
  --prefix "$SIMD_SCRATCH/pinned-system"
```

Create an executable scratch file `$SIMD_SCRATCH/use-prebuilt-zig` containing:

```sh
#!/bin/sh
set -eu
cp "$SIMD_EXPERIMENT_ARCHIVE" zig-out/lib/libghostty-vt.a
```

This adapter selects the already-built archive without editing Rust sources:

```sh
CARGO_TARGET_DIR="$SIMD_SCRATCH/cargo-target" \
MACOSX_DEPLOYMENT_TARGET=26.0 \
SIMD_EXPERIMENT_ARCHIVE="$SIMD_SCRATCH/pinned-system/lib/libghostty-vt.a" \
ZIG="$SIMD_SCRATCH/use-prebuilt-zig" \
RUSTFLAGS='-L native=/opt/homebrew/opt/simdutf/lib -l dylib=simdutf -l dylib=c++' \
cargo nextest run -p gobby-terminal --features vt-engine --lib --test frame_producer
```

`otool -L` on `cargo-target/debug/deps/gobby_terminal-39888e0cff2d915c`
confirmed `/opt/homebrew/opt/simdutf/lib/libsimdutf.35.dylib` and
`/usr/lib/libc++.1.dylib`. `nm` showed undefined simdutf references in the system
static archive, confirming externally supplied implementations.

## Implications and limits

- Ghostty's exact SDK fix is
  [1c861e3](https://github.com/ghostty-org/ghostty/commit/1c861e3c476f2489008c12fc0b75af72c1b8484d):
  a compatibility math.h supplies the definitions missing in Zig 0.16.
- Its native Apple link change is
  [d65cb512](https://github.com/ghostty-org/ghostty/commit/d65cb5128abfbee4dcb8a4ace3bcb35c1a7f0790).
  The experiment resolved its omitted system dependency through explicit linkage.
- Selecting system integration and propagating simdutf/libc++ linkage to Rust
  consumers is sufficient locally with our existing Ghostty pin.
- This Homebrew dylib targets macOS 26.0. Initial example links targeting 13.0
  warned; rerunning at 26.0 was clean. Distributed binaries must provide or
  require the external library and respect its deployment floor.
- Our successful Gobby build retains the static-only and grapheme patches.
  Their removal was not validated. Latest Ghostty needs an FFI migration first.
- Windows, Linux, older macOS, gclient live-daemon, and packaging behavior were
  not tested. No full workspace suite was run.
- Missing pkg-config was handled by `--search-prefix`, without installing it.
  An initial incorrect `-Dsystem-simdutf` flag was corrected to `-fsys=simdutf`.

The remaining choice is distribution: retain a self-contained SIMD build for
portable releases, or require/provide an external simdutf library. The observed
SDK error does not require bundled simdutf for local development.
