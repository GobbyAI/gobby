# libghostty-vt local patches

This file tracks intentional local changes applied on top of the vendored
`libghostty-vt` source. Remove a patch only when the vendored source commit
contains the upstream behavior and the listed verification still passes.

Patch files use repository-root-relative paths (`crates/gterminal/vendor/...`).
`git apply` run from `crates/gterminal` skips any patch whose paths do not start
with that prefix, so a patch written with `vendor/...` paths would pass
`tests/gterminal/test_vendor_layer.py::test_vendor_patches_are_applied_to_copied_trees`
without ever being checked.

## 0001 default lib-vt panes to grapheme clustering

status: active

patch: `vendor/patches/libghostty-vt/0001-default-grapheme-cluster-mode.patch`

herdr issue: https://github.com/herdrdev/herdr/issues/243

upstream discussion: not opened

upstream pr: not opened

vendored base: `7aab0a0392369613472bd5dcfd66bef58e78c3ec`

local files:

- `vendor/libghostty-vt/src/terminal/c/terminal.zig`

reason: Gobby renders terminal cells directly and requires DEC private mode
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

status: removed at vendored base `7aab0a0392369613472bd5dcfd66bef58e78c3ec`

patch: deleted (`vendor/patches/libghostty-vt/0002-optional-lib-vt-shared.patch`)

removed because: the patch added a non-upstream `-Demit-lib-vt-shared` option
so `build.rs` could skip the shared library. It existed only because Zig 0.15.2
could not compile its bundled libc++ against the macOS 27 SDK: libc++ builds
with `-std=c++23`, under which clang answers `__has_feature(modules)` with 1,
so the SDK's `math.h` hands `INFINITY` and `NAN` to `<float.h>` through
`__need_infinity_nan` and compiles out its own `#define INFINITY HUGE_VALF` —
and Zig 0.15.2's bundled `float.h` has no `__need_infinity_nan` support. The
vendored toolchain is now Zig 0.16.0 and upstream `1c861e3c` adds
`pkg/apple-sdk/include/math.h` supplying those definitions, so the shared
library builds against the macOS 27 SDK from plain Command Line Tools with no
`DEVELOPER_DIR` override. `build.rs` and
`scripts/build_vendored_libghostty_vt.sh` no longer pass any shared-library
flag; Gobby still links only `libghostty-vt.a`.

evidence: isolated static/shared bundled SIMD build on macOS 27, run from this
worktree with Zig 0.16.0 and macOS SDK 27.0 (`xcrun --sdk macosx
--show-sdk-version`), no `DEVELOPER_DIR`:

```sh
ZIG=<zig-0.16.0> bash scripts/build_vendored_libghostty_vt.sh
# zig-out/lib: libghostty-vt.a and libghostty-vt.0.1.0.dylib both emitted
otool -L crates/gterminal/vendor/libghostty-vt/zig-out/lib/libghostty-vt.0.1.0.dylib
# @rpath/libghostty-vt.dylib and /usr/lib/libSystem.B.dylib only; no libsimdutf
nm crates/gterminal/vendor/libghostty-vt/zig-out/lib/libghostty-vt.a
# every simdutf:: reference is defined by the archive's own simdutf.o
```

## 0003 normalize native Darwin static archives without SIMD

status: active

patch: `vendor/patches/libghostty-vt/0003-normalize-darwin-nonsimd-archive.patch`

upstream discussion/pr: not opened

vendored base: `7aab0a0392369613472bd5dcfd66bef58e78c3ec`

local files:

- `vendor/libghostty-vt/src/build/GhosttyLibVt.zig`

reason: Zig can place `compiler_rt.o` at a four-byte rather than eight-byte
aligned offset. Apple `ld` rejects it, and `libtool` directly can discard the
member. Route native Darwin static archives through the existing
`CombineArchivesStep` even without SIMD; its `LibtoolStep` copies and
normalizes each input with Apple `ranlib` before combining archives. Upstream
at this base still combines only when `zig.simd_libs.items.len > 0`
(`src/build/GhosttyLibVt.zig`), so the patch is still required.

remove when: upstream normalizes all native Darwin static archives and the
non-SIMD regression passes without this patch.

verification: build in temporary output/cache directories, retain the compiler
runtime member, and force-load all members through Apple's linker:

```sh
cargo nextest run -p gobby-terminal --test build_env -E 'test(darwin_nonsimd_archive_links_every_member)'
```
