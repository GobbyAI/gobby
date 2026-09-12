# libghostty-vt local patches

This file tracks intentional local changes applied on top of the vendored
`libghostty-vt` source. Remove a patch only when the vendored source commit
contains the upstream behavior and the listed verification still passes.

## 0001 default lib-vt panes to grapheme clustering

status: active

patch: `vendor/patches/libghostty-vt/0001-default-grapheme-cluster-mode.patch`

herdr issue: https://github.com/herdrdev/herdr/issues/243

upstream discussion: not opened; libghostty-vt currently exposes current mode mutation but no C API for configuring terminal default modes

upstream pr: not opened

vendored base: `c5a21edfcbc2d5b46540ad91b7980aca31f5f1f3`

local files:

- `vendor/libghostty-vt/src/terminal/c/terminal.zig`

reason: Herdr renders terminal cells directly and requires DEC private mode
2027 to store flags, ZWJ emoji, and other multi-codepoint grapheme clusters in
one cell. This patch makes clustering active for new terminals and keeps it as
the reset default so RIS (`ESC c`) does not disable it.

remove when: libghostty-vt exposes a C API for setting default mode 2027, or
upstream makes grapheme clustering the lib-vt default, and the reset-survival
regression passes without this patch.

verification:

```sh
cargo nextest run --locked grapheme_cluster_mode_is_default_and_survives_full_reset
cargo nextest run --locked grapheme_cluster_mode_renders_flag_emoji_in_single_wide_cell
cargo nextest run --locked grapheme_cluster_mode_renders_zwj_family_in_single_wide_cell
```

## 0002 allow skipping the libghostty-vt shared library

status: active

patch: `vendor/patches/libghostty-vt/0002-optional-lib-vt-shared.patch`

upstream discussion: not opened; the shared library is unconditionally part of
the `-Demit-lib-vt` install step and there is no option to emit only the
static archive

upstream pr: not opened

## 0003 normalize native Darwin static archives without SIMD

status: active

patch: `vendor/patches/libghostty-vt/0003-normalize-darwin-nonsimd-archive.patch`

upstream discussion/pr: not opened

vendored base: `c5a21edfcbc2d5b46540ad91b7980aca31f5f1f3`

local files:

- `vendor/libghostty-vt/build.zig`
- `vendor/libghostty-vt/src/build/Config.zig`

reason: `build.rs` links `zig-out/lib/libghostty-vt.a` on every platform and
never the shared library, but `zig build -Demit-lib-vt` built both. Linking a
C++ shared library makes zig compile its own bundled libc++, and zig 0.15.2
cannot compile that against the macOS 27.0 SDK: it builds libc++ with
`-std=c++23`, under which clang answers `__has_feature(modules)` with 1, so
the SDK's `math.h` hands `INFINITY` and `NAN` to `<float.h>` through
`__need_infinity_nan` and compiles out its own `#define INFINITY HUGE_VALF` —
and zig 0.15.2's bundled `float.h` has no `__need_infinity_nan` support.
Nothing then defines `INFINITY`, and `libcxx/src/random.cpp` fails to compile.
The static archive is never linked, so it is unaffected. The new
`-Demit-lib-vt-shared` option defaults to `true`, leaving upstream's behavior
unchanged for anyone who does not pass it.

Reproducible outside this repo with zig 0.15.2 on a macOS 27.0 SDK:

```sh
printf 'int f() { return 42; }\n' > t.cpp
zig c++ -shared -o libt.dylib t.cpp
```

remove when: the pinned zig toolchain can compile its bundled libc++ against
the current macOS SDK, or upstream gains an option to emit only the static
archive.

verification:

```sh
cargo build -p gobby-terminal --features vt-engine
cargo nextest run -p gobby-client

- `vendor/libghostty-vt/src/build/GhosttyLibVt.zig`

reason: Zig 0.15.2 can place `compiler_rt.o` at a four-byte rather than
eight-byte aligned offset. Apple `ld` rejects it, and `libtool` directly can
discard the member. Route native Darwin static archives through the existing
`CombineArchivesStep` even without SIMD; its `LibtoolStep` copies and normalizes
each input with Apple `ranlib` before combining archives.

remove when: upstream normalizes all native Darwin static archives and the
non-SIMD regression passes without this patch.

verification: build in temporary output/cache directories, retain the compiler
runtime member, and force-load all members through Apple's linker:

```sh
DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer cargo nextest run -p gobby-terminal --test build_env -E 'test(darwin_nonsimd_archive_links_every_member)'
```
