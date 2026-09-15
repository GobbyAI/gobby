Plan artifact: `.gobby/plans/ghostty-zig-ffi-upgrade.md`

# Ghostty pin and Zig toolchain upgrade with C FFI migration

**Plan ID:** ghostty-zig-ffi-upgrade

## Overview
`kind: framing`

Leaf #22310 under epic #22076. The current vendored libghostty-vt pin is
`c5a21edfcbc2d5b46540ad91b7980aca31f5f1f3` (`1.3.2-HEAD-+c5a21edfc`) built
with Zig 0.15.2. Research #22304 (commit `c7f278b8c8`,
`docs/research/simdutf-system-22304.md`) built Ghostty
`7aab0a0392369613472bd5dcfd66bef58e78c3ec` with official Zig 0.16.0 against
the macOS 27 SDK: bundled static and shared libraries linked; the shared
dylib depended only on libSystem. Linking Gobby's current Rust bindings to
that archive failed on three removed C functions. This plan is the
FFI migration, not a version bump.

System simdutf 9.1.2 is context only: it passed 582 tests on the *existing*
pin and is rejected for portable native releases (Homebrew dylib, macOS 26
floor). Bundled, unmodified upstream simdutf/highway stay.

## Constraints
`kind: framing`

**Decision Record.**

1. Ghostty pin: `7aab0a0392369613472bd5dcfd66bef58e78c3ec`. It is the only
   commit research #22304 built as bundled static+shared against macOS 27
   with Zig 0.16.0. It contains SDK fix
   `1c861e3c476f2489008c12fc0b75af72c1b8484d` (compat `math.h` for
   `INFINITY`/`NAN`). No newer untested tip.
2. Zig: 0.16.0. The candidate pin's `build.zig.zon` declares
   `minimum_zig_version = "0.16.0"` (read from GitHub at `7aab0a03`).
   Developer path: Homebrew `zig` stable is 0.16.0 (bottled), so
   `brew install zig` / `brew upgrade zig`; the official tarball (SHA256
   `b23d70deaa879b5c2d486ed3316f7eaa53e84acf6fc9cc747de152450d401489`,
   `zig-aarch64-macos-0.16.0`) is the fallback via `ZIG=<path>`. CI uses
   `mlugg/setup-zig` `0.16.0`. Zig 0.15.2 cannot compile bundled libc++
   against the macOS 27 SDK and is retired for `vt-engine`.
3. SIMD: retain bundled, unmodified upstream simdutf and highway from the
   new pin. No `-fsys=simdutf`, no Homebrew/`libsimdutf` runtime, no
   macOS 26 deployment floor. `d65cb512` (Apple shared link omitting
   system SIMD) is irrelevant while SIMD stays bundled.
4. Local patches, against their remove-when text in
   `crates/gterminal/vendor/libghostty-vt.patches.md`:
   - **0001** (grapheme default): port onto the new tree in 1.1 so the
     vendor still resets to mode 2027; **drop in 2.1** after
     `GHOSTTY_TERMINAL_OPT_MODE_DEFAULT` plus the three grapheme tests
     pass. The C API now exists (candidate `terminal.h`: that option
     "unconditionally updates both the current value and the value
     restored by a full terminal reset (RIS)").
   - **0002** (`-Demit-lib-vt-shared`): **drop**. Zig 0.16.0 compiled the
     shared library against macOS 27 in #22304. `build.rs` and
     `scripts/build_vendored_libghostty_vt.sh` stop passing the
     non-upstream flag. Gobby still *links* only `libghostty-vt.a`.
   - **0003** (Darwin non-SIMD `CombineArchivesStep` / `ranlib`): **keep**,
     ported onto the new `GhosttyLibVt.zig`. Drop only if the extracted
     file already routes native Darwin static archives through
     `CombineArchivesStep` when `simd_libs` is empty **and**
     `darwin_nonsimd_archive_links_every_member` passes without the patch.
5. Wrapper signatures stay: `Terminal::new(cols, rows, max_scrollback)`,
   `mode_get(u16)`, `mode_set(u16, bool)`, `RenderState::colors()`. Pane
   callers are not edited. New search/snapshot/paste/clipboard-read C APIs
   are bindgen-only; do not wrap them.
6. Do not run `cargo clean`. Do not run `zig build` or cargo against
   `~/Projects/gobby`. Isolated `CARGO_TARGET_DIR` and Zig cache in the
   worktree or `/tmp`. Do not merge to `0.5.0`.
7. Native activation uses `promote_workspace_binary_set` for the stamped
   set (`gcode`, `gdaemon`, `ghook`) and
   `stage_and_promote_binary_file` for `gterm`/`gclient`. Never copy into
   `~/.gobby/bin` by hand. Coordinate with the install owner; announce
   restarts to active sessions.
8. Expansion: none. #22310 is itself the implementing leaf. Sections
   1.1, 1.2, 2.1 and 3.1 are its implementation specification and land on
   one branch in dependency order; `gobby-terminal` does not link between
   1.1 and 2.1, so no section closes as a separate task. The section
   acceptance items are #22310's close evidence.
9. Load the `rust` skill before Rust edits. Preserve Stage-0 triples
   (`aarch64-apple-darwin`, `x86_64-apple-darwin`,
   `x86_64-unknown-linux-gnu`, `aarch64-unknown-linux-gnu`) and the
   Windows cross clippy job with `LIBGHOSTTY_VT_SIMD=false`.

**Observed ABI audit** (current vendor headers vs
`/tmp/simdutf-22304/upstream-bundled/include` from the #22304 candidate
build). Literal sweep from this worktree (overlay #20664):
`gcode grep -w mode_get|mode_set crates/`,
`gcode grep -w ghostty_render_state_colors_get crates/`,
`gcode grep -F ffi::ghostty_ crates/gterminal/src/ghostty`.

| Current C symbol / type | Candidate | Gobby migration |
| --- | --- | --- |
| `ghostty_terminal_new(alloc, out, GhosttyTerminalOptions{cols,rows,max_scrollback})` | `ghostty_terminal_new(alloc, out, cols, rows)`; options struct **removed** | Call new arity; then `ghostty_terminal_set(GHOSTTY_TERMINAL_OPT_SCROLLBACK_MAX_LINES, &max_scrollback)`. For `max_scrollback == 0` also set `GHOSTTY_TERMINAL_OPT_SCROLLBACK_MAX_BYTES` to 0. |
| `ghostty_terminal_mode_get` / `_mode_set` | **removed**. `GhosttyTerminalModeConfig { mode, value }`; `GHOSTTY_TERMINAL_DATA_MODE` (37); `GHOSTTY_TERMINAL_OPT_MODE` (34); `GHOSTTY_TERMINAL_OPT_MODE_DEFAULT` (33) | Keep `u16` wrappers. DEC private numbers already equal packed `GhosttyMode` (bit 15 clear). Get: fill `mode`, `ghostty_terminal_get(DATA_MODE)`. Set current: `OPT_MODE`. Grapheme reset default: `OPT_MODE_DEFAULT` with `MODE_GRAPHEME_CLUSTER` (2027). |
| `ghostty_render_state_colors_get` + `GhosttyRenderStateColors` | Function **removed**. Struct layout **unchanged**. Query `GHOSTTY_RENDER_STATE_DATA_COLORS` (19) via `ghostty_render_state_get` after `GHOSTTY_INIT_SIZED` / `size = sizeof` | `RenderState::colors` switches the call; fields stay `background`, `foreground`, `cursor`, `cursor_has_value`, `palette[256]`. |
| `GhosttyTerminalClipboardWriteFn` returns `GhosttyClipboardWriteResult` | Returns **void**. Answer with `write->reply(write, &GhosttyClipboardWriteReply)` during the callback; returning without reply **denies**. Struct gains `name`, `granted`, `can_remember`, `ctx`, `reply` after `contents_len` | Trampoline becomes `void`; `capture_clipboard_write` calls `reply` with the same success/unsupported/invalid mapping. Do not register `GHOSTTY_TERMINAL_OPT_CLIPBOARD_READ`. |
| `GhosttyResult` | Adds `GHOSTTY_IO_ERROR=-5`, `GHOSTTY_LIMIT_EXCEEDED=-6`, `GHOSTTY_REJECTED=-7` | `GhosttyResultExt::into_result` already treats any non-success as `Error`. No new wrappers. |
| Additive only (bindgen, no wrappers) | `io.h` Reader/Writer; `search.h`; `snapshot.h`; `ghostty_terminal_paste`; continuation; `vt_write_until_ground`; `ghostty_formatter_format`; `ghostty_render_state_clean`; `row_iterator_next_dirty`; `GHOSTTY_RENDER_STATE_DATA_CURSOR` (18); new terminal option/data keys 27–40; mode macros `GHOSTTY_MODE_VISIBILITY_REPORT` (2033), `GHOSTTY_MODE_PASTE_EVENTS` (5522); OSC commands 23–26 | Do not expose in `ghostty::`. `ghostty_formatter_format_alloc` remains and is what `terminal_api.rs` calls. |
| `include/ghostty.h` (full app ABI) | Not in the lib-vt *install* prefix; still in the dist source tree | Do not bindgen `ghostty.h`. Current generated files have no `ghostty_app_*`. |
| Unchanged layouts used by wrappers | `GhosttyColorRgb`, `GhosttyStyle`, `GhosttyString`, `GhosttyAllocator`, key/mouse encoder APIs, kitty_graphics.h skeleton, `ghostty_terminal_set`/`get`/`vt_write`/`resize`/`free` | No migration. |

`mode_get` callers (no edit if wrappers hold):
`terminal_api.rs`, `pane/input.rs`, `pane/runtime_ops.rs`,
`pane/terminal.rs`, `pane/terminal_io.rs`, `ghostty/mod/tests.rs`.
`mode_set`: `terminal_api.rs`, `pane/terminal_io.rs`,
`pane/terminal/tests.rs`, `mod/tests.rs`.
`RenderState::colors`: `render_state.rs`, `pane/terminal_io.rs`,
`pane/terminal_render.rs`, `pane/terminal_style.rs`,
`pane/osc/tests.rs`, `pane/terminal/tests.rs`.
Do not target `pane/terminal.rs` (939 lines) or `pane/terminal_io.rs`
(944 lines).

**Rejected alternatives.** Stay on `c5a21edf` + Zig 0.15.2: fails macOS 27
libc++ without pinning Xcode 26.5 forever. System simdutf: rejected by
#22310. Compatibility shims for the three removed functions: rejected by
#22304 (no shim to force the probe). Hand-copy into `~/.gobby/bin`:
forbidden.

## P1: Pin, vendor, and Zig 0.16 toolchain
`kind: framing`

**Goal**: the worktree vendors `7aab0a0`, builds libghostty-vt with Zig
0.16.0 against the macOS 27 SDK using bundled SIMD, and CI/docs/helpers
name that toolchain.

### 1.1 Re-vendor libghostty-vt and retarget the Zig 0.16 build [category: code]
`kind: deliverable`

**Granularity:** more than six production files. One leaf: the pin, remaining
patches, and Zig invocation are one build configuration and cannot ship
apart.

Targets:
- `crates/gterminal/vendor/libghostty-vt.vendor.json`
- `crates/gterminal/vendor/libghostty-vt.patches.md`
- `crates/gterminal/vendor/patches/libghostty-vt/0002-optional-lib-vt-shared.patch` — operation: delete
- `crates/gterminal/vendor/patches/libghostty-vt/0003-normalize-darwin-nonsimd-archive.patch`
- `crates/gterminal/vendor/libghostty-vt/VERSION`
- `crates/gterminal/vendor/libghostty-vt/build.zig`
- `crates/gterminal/vendor/libghostty-vt/src/build/GhosttyLibVt.zig`
- `crates/gterminal/vendor/libghostty-vt/src/terminal/c/terminal.zig`
- `crates/gterminal/NOTICE.md`
- `crates/gterminal/build.rs::main`
- `crates/gterminal/build.rs::zig_target`
- `scripts/build_vendored_libghostty_vt.sh`
- `crates/gterminal/tests/build_env.rs::missing_zig_reports_requirement`
- `crates/gterminal/tests/build_env.rs::darwin_nonsimd_archive_links_every_member`
- `tests/gterminal/test_vendor_layer.py::*` — scope-reason: every pin, REQUIRED_ZIG, and patch-application assertion follows the new commit and Zig 0.16
- `src/gobby/cli/install_setup_gterm.py::*` — scope-reason: the module-level `GTERM_NO_ZIG_SKIP_REASON` string says "requires Zig 0.15"; it is a constant, not an indexed symbol

**Research context:** Vendoring follows herdr's dist workflow
(`~/.gobby/clones/herdr/scripts/vendor_libghostty_vt.py`): from a *clean*
Ghostty checkout at `7aab0a0392369613472bd5dcfd66bef58e78c3ec`, with Zig
0.16.0, `zig build dist -Demit-lib-vt -Doptimize=ReleaseFast`, extract the
`libghostty-vt-*+7aab0a039.tar.gz` over
`crates/gterminal/vendor/libghostty-vt/`, write `vendor.json`
(`source_commit`, `dist_archive`, `extracted_dir`). Do not add a new
Python vendor script. Concretely: obtain a managed clone of
`https://github.com/ghostty-org/ghostty` through `gobby-clones:create_clone`
(raw clone commands are blocked by rule), check out
`7aab0a0392369613472bd5dcfd66bef58e78c3ec`, run
`zig build dist -Demit-lib-vt -Doptimize=ReleaseFast` with Zig 0.16.0
(the pinned `build.zig` already defines the `dist` step at line 109 and
the archive lands in `zig-out/dist/`), `git rm -r crates/gterminal/vendor/libghostty-vt`,
extract the archive there, then re-apply the surviving patches with
`git apply` from `crates/gterminal`. GitHub is reachable from the build
shell (verified with a 200 on the raw `build.zig.zon`). Do not run that
`zig build dist` inside `~/Projects/gobby` or this worktree's Cargo target.

`build.rs::main` today runs `zig build -Demit-lib-vt -Doptimize=…
-Dsimd=… -Dtarget=… -Dversion-string=… -Demit-xcframework=false
-Demit-lib-vt-shared=false` and panics with "required Zig 0.15". After
this leaf: drop `-Demit-lib-vt-shared=false`; panic/help text and the
helper script say Zig 0.16; `REQUIRED_ZIG` in
`tests/gterminal/test_vendor_layer.py` becomes `"0.16"`;
`test_vendor_json_pins_ghostty_commit` asserts `7aab0a0392369613472bd5dcfd66bef58e78c3ec`
(or `startswith("7aab0a03")` matching today's `c5a21edf` style).
`test_vendor_patches_are_applied_to_copied_trees` uses `git apply
--check --reverse` from `crates/gterminal`; remaining patches must apply.

0002's files (`build.zig`, `src/build/Config.zig`) return to upstream
after extract. 0001's one-line `.default_modes = .{ .grapheme_cluster = true }`
in `src/terminal/c/terminal.zig` is ported if the hunk misses. 0003's
combine condition stays unless extract already has it; then delete 0003
and record that in `libghostty-vt.patches.md`.

`NOTICE.md` libghostty-vt pin, `-Demit-lib-vt-shared` sentence, and
nested `pkg/simdutf` version (today 9.0.0) update from the extracted
amalgamation. simdutf/highway remain the compiled-in exceptions.

Isolated validation (planned, not run in this planning pass): never
`cargo clean`. Use a scratch prefix and `CARGO_TARGET_DIR`.
`xcrun --sdk macosx --show-sdk-version` must be 27.x; do **not** set
`DEVELOPER_DIR` to the 26.5 Xcode. Build static+shared with bundled SIMD
(no `-fsys=simdutf`). `otool -L` on the dylib: libSystem only, no
`libsimdutf`. `nm` on the archive: simdutf symbols defined, not `U`.
`darwin_nonsimd_archive_links_every_member` with Zig 0.16.0. Windows
cross clippy keeps `LIBGHOSTTY_VT_SIMD=false`.

**Acceptance:**

- 1.1.1 - `libghostty-vt.vendor.json` `source_commit` is `7aab0a0392369613472bd5dcfd66bef58e78c3ec`. file: `crates/gterminal/vendor/libghostty-vt.vendor.json`. test: `tests/gterminal/test_vendor_layer.py::test_vendor_json_pins_ghostty_commit`.
- 1.1.2 - Bundled simdutf/highway stay in-tree; build.rs and the helper never pass `-fsys=simdutf`. file: `crates/gterminal/build.rs`. behavior: "bundled SIMD, no system simdutf" in `crates/gterminal/NOTICE.md`.
- 1.1.3 - Patch 0002 is gone; neither `build.rs` nor the helper passes `-Demit-lib-vt-shared`. file: `crates/gterminal/build.rs`.
- 1.1.4 - Patch 0003 remains applied, or `GhosttyLibVt.zig` already combines native Darwin static archives without SIMD and 0003 is deleted with that evidence in `libghostty-vt.patches.md`. file: `crates/gterminal/vendor/libghostty-vt.patches.md`. test: `crates/gterminal/tests/build_env.rs::darwin_nonsimd_archive_links_every_member`.
- 1.1.5 - Missing-zig errors name Zig 0.16, including the installer's `GTERM_NO_ZIG_SKIP_REASON` in `src/gobby/cli/install_setup_gterm.py`. symbol: `missing_zig_reports_requirement`. test: `tests/gterminal/test_vendor_layer.py::test_build_rs_names_zig_version_when_binary_missing`.
- 1.1.6 - Isolated `zig build -Demit-lib-vt` against the macOS 27 SDK emits static and shared libraries; `otool -L` on the dylib lists no `libsimdutf`; `nm` on the archive has no undefined simdutf. behavior: "isolated static/shared bundled SIMD build on macOS 27" in `crates/gterminal/vendor/libghostty-vt.patches.md`.
- 1.1.7 - NOTICE records the new Ghostty commit, drops the shared-skip rationale, and inventories the extracted simdutf/highway licenses. file: `crates/gterminal/NOTICE.md`.

Consumers unchanged:
- `crates/gterminal/build.rs::zig_target` — no-edit-reason: the eight Stage-0 triples are unchanged; only the Zig version string and shared-lib flag change in `main`.

### 1.2 Point GitHub Actions at Zig 0.16.0 [category: config] (depends: 1.1)
`kind: deliverable`

Targets:
- `.github/workflows/rust-ci.yml`
- `.github/workflows/release-gterminal.yml`
- `tests/cli/test_install_setup_gterm.py::test_release_workflows_gate_stage0_and_gclient_preflight`

**Research context:** Both workflows use `mlugg/setup-zig` with
`version: 0.15.2` (rust-ci.yml after the Zig-free `gobby-client` job;
release-gterminal.yml twice). `gclient` workflows must stay Zig-free.
`test_release_workflows_gate_stage0_and_gclient_preflight` asserts
`"0.15.2" in gterm_yaml`. Change both setup-zig versions to `0.16.0` and
the assertion to `"0.16.0"`. Stage-0 matrix triples stay. Do not add Zig
to `release-gclient.yml`.

**Acceptance:**

- 1.2.1 - `mlugg/setup-zig` in rust-ci and release-gterminal requests `0.16.0`, and the inventory test asserts that string. file: `.github/workflows/rust-ci.yml`. test: `tests/cli/test_install_setup_gterm.py::test_release_workflows_gate_stage0_and_gclient_preflight`.

## P2: C ABI binding and wrapper migration
`kind: framing`

**Goal**: checked-in bindgen matches the new `include/ghostty/vt/` headers,
safe wrappers compile and link, and grapheme default/reset behavior is
preserved without patch 0001.

### 2.1 Regenerate bindings and migrate Terminal, RenderState, and clipboard [category: code] (depends: 1.1)
`kind: deliverable`

**Granularity:** more than six production files and more than six
acceptance items. One leaf: generated bindings and wrappers are one
compile unit; splitting would not yield a green `vt-engine` build.

Targets:
- `crates/gterminal/src/ghostty/bindings.rs`
- `crates/gterminal/src/ghostty/bindings/generated_01.rs::*` — scope-reason: regenerate the entire lib-vt FFI surface for the new headers
- `crates/gterminal/src/ghostty/bindings/generated_02.rs::*` — scope-reason: regenerate the entire lib-vt FFI surface for the new headers
- `crates/gterminal/src/ghostty/bindings/generated_03.rs::*` — scope-reason: regenerate the entire lib-vt FFI surface for the new headers
- `crates/gterminal/src/ghostty/bindings/generated_04.rs::*` — scope-reason: regenerate the entire lib-vt FFI surface for the new headers
- `crates/gterminal/src/ghostty/bindings/generated_05.rs::*` — scope-reason: regenerate the entire lib-vt FFI surface for the new headers
- `crates/gterminal/src/ghostty/bindings/generated_06.rs`
- `crates/gterminal/src/ghostty/bindings/generated_07.rs`
- `crates/gterminal/src/ghostty/bindings/generated_08.rs`
- `crates/gterminal/src/ghostty/terminal_api.rs::Terminal::new`
- `crates/gterminal/src/ghostty/terminal_api.rs::Terminal::mode_get`
- `crates/gterminal/src/ghostty/terminal_api.rs::Terminal::mode_set`
- `crates/gterminal/src/ghostty/render_state.rs::RenderState::colors`
- `crates/gterminal/src/ghostty/mod.rs::*` — scope-reason: clipboard trampoline becomes void+reply; replace hardcoded terminal/kitty discriminants with regenerated bindgen names
- `crates/gterminal/vendor/patches/libghostty-vt/0001-default-grapheme-cluster-mode.patch` — operation: delete
- `crates/gterminal/vendor/libghostty-vt.patches.md`
- `crates/gterminal/src/ghostty/mod/tests.rs::*` — scope-reason: grapheme default/reset and any mode_get tests follow the wrapper internals

**Research context:** Bindings are rust-bindgen 0.72.1, split under
`bindings/generated_*.rs` (`source_size.rs` excludes `generated_*`).
`bindgen` is not installed on this machine and no regeneration script is
checked in: install with `cargo install bindgen-cli --version 0.72.1` and
point `LIBCLANG_PATH` at
`/Applications/Xcode.app/Contents/Developer/Toolchains/XcodeDefault.xctoolchain/usr/lib`
(present; Homebrew LLVM is not). Record the exact `bindgen` invocation
and version in a comment at the top of `bindings.rs` so the next re-pin
is reproducible. Regenerate from `vendor/libghostty-vt/include/ghostty/vt.h` with
`-I vendor/libghostty-vt/include`, const-style enums matching
`GhosttyResult_GHOSTTY_SUCCESS`, allowlist `ghostty_*` / `GHOSTTY_*` /
`Ghostty*`. Do not include `ghostty.h`. After regen, delete
`TERMINAL_DATA_COLOR_*` and `KITTY_PLACEMENT_DATA_*` numeric fallbacks in
`mod.rs` if bindgen now exports the names.

Split `generated_02.rs`, `generated_03.rs`, and `generated_04.rs` (each
already at or above 850 lines) so overflow moves into new files
`generated_06.rs`, `generated_07.rs`, and `generated_08.rs`; keep every
bindgen chunk under 850 lines and list the new modules in `bindings.rs`.
If `generated_01.rs` stays at the threshold after regen, split it the same
way into one of those new files.

`Terminal::new` today builds `GhosttyTerminalOptions` and calls
`ghostty_terminal_new(null, &mut raw, options)`, then sets userdata,
size, pwd, clipboard_write, color_scheme, glyph_protocol. Candidate
`ghostty_terminal_new` takes `(allocator, out, cols, rows)` only.
After success: set `GHOSTTY_TERMINAL_OPT_SCROLLBACK_MAX_LINES` to
`max_scrollback`; if `max_scrollback == 0` also set
`GHOSTTY_TERMINAL_OPT_SCROLLBACK_MAX_BYTES` to 0. Then
`GHOSTTY_TERMINAL_OPT_MODE_DEFAULT` with
`GhosttyTerminalModeConfig { mode: MODE_GRAPHEME_CLUSTER (2027), value: true }`.
If that returns `GHOSTTY_INVALID_VALUE`, keep/reapply 0001 and record it;
the planned path is drop. Existing effect registrations stay.

`mode_get`/`mode_set` swap to `DATA_MODE` / `OPT_MODE` with
`GhosttyTerminalModeConfig`. Public `u16` stays (DEC values already match
packed `GhosttyMode`).

`RenderState::colors` uses `ghostty_render_state_get(state,
GHOSTTY_RENDER_STATE_DATA_COLORS, &mut colors)` with
`colors.size = size_of::<GhosttyRenderStateColors>()`. Individual
`COLOR_BACKGROUND` keys remain valid; the sized struct is the
replacement for `colors_get`. Do not migrate cursor getters onto
`GHOSTTY_RENDER_STATE_DATA_CURSOR` in this leaf.

Clipboard: `clipboard_write_trampoline` is
`unsafe extern "C" fn(..., write: *const GhosttyClipboardWrite) -> GhosttyClipboardWriteResult`.
Candidate callback is `void`. Before return, call `write.reply(write, &GhosttyClipboardWriteReply { size, result, remember: false })`
using the same STANDARD / single text/plain / size-cap policy.
Guard `reply` with the sized-struct `size` field. Do not set
`CLIPBOARD_READ`. OSC 52 "?" stays ignored.

If `OPT_MODE_DEFAULT` plus the three grapheme tests pass, delete 0001 and
rewrite its `libghostty-vt.patches.md` section as removed, with the C API
as the replacement. The duplicate test in `pane/terminal.rs` stays as a
no-edit consumer.

TDD: existing grapheme/mode tests are red until wrappers link; then green;
then delete 0001.

**Acceptance:**

- 2.1.1 - Generated bindings compile against the new `vt.h` and contain `GHOSTTY_TERMINAL_OPT_MODE_DEFAULT`, `GHOSTTY_TERMINAL_DATA_MODE`, `GHOSTTY_RENDER_STATE_DATA_COLORS`, and no `ghostty_terminal_mode_get`, `ghostty_terminal_mode_set`, or `ghostty_render_state_colors_get`. file: `crates/gterminal/src/ghostty/bindings.rs`.
- 2.1.2 - `Terminal::new` uses the two-integer constructor, sets scrollback via `GHOSTTY_TERMINAL_OPT_SCROLLBACK_MAX_LINES` (and bytes=0 when max_scrollback is 0), and sets grapheme clustering as the RIS default via `GHOSTTY_TERMINAL_OPT_MODE_DEFAULT`. symbol: `Terminal::new`.
- 2.1.3 - `mode_get` / `mode_set` use `GhosttyTerminalModeConfig` and keep the `u16` public API. symbol: `Terminal::mode_get`.
- 2.1.4 - `RenderState::colors` reads `GHOSTTY_RENDER_STATE_DATA_COLORS`. symbol: `RenderState::colors`.
- 2.1.5 - Clipboard write trampoline is `void` and answers through `GhosttyClipboardWrite::reply` before return. symbol: `clipboard_write_trampoline`.
- 2.1.6 - Grapheme clustering is the default and survives `ESC c` without patch 0001. test: `crates/gterminal/src/ghostty/mod/tests.rs::grapheme_cluster_mode_is_default_and_survives_full_reset`.
- 2.1.7 - Flag emoji and ZWJ family still occupy one wide cell. test: `crates/gterminal/src/pane/terminal/tests.rs::grapheme_cluster_mode_renders_flag_emoji_in_single_wide_cell`.
- 2.1.8 - Patch 0001 is deleted once 2.1.6 and 2.1.7 pass; `libghostty-vt.patches.md` records the C API replacement. file: `crates/gterminal/vendor/libghostty-vt.patches.md`.
- 2.1.9 - `bindings.rs` carries the exact bindgen 0.72.1 command, include path and allowlist used for the regeneration. file: `crates/gterminal/src/ghostty/bindings.rs`.

Consumers unchanged:
- `crates/gterminal/src/pane/terminal.rs` — no-edit-reason: `grapheme_cluster_mode_is_default_and_survives_full_reset` already calls `mode_get`; production code is untouched (939 lines).
- `crates/gterminal/src/pane/input.rs` — no-edit-reason: still calls `mode_get(MODE_MOUSE_SGR_PIXELS)`.
- `crates/gterminal/src/pane/runtime_ops.rs` — no-edit-reason: still calls `mode_get` with DEC numbers 9/1000/1002/1003/1006.
- `crates/gterminal/src/pane/terminal_io.rs` — no-edit-reason: `mode_get`/`mode_set`/`colors()` signatures unchanged; file is 944 lines and must not grow.
- `crates/gterminal/src/pane/terminal_render.rs` — no-edit-reason: still calls `render_state.colors()`.
- `crates/gterminal/src/pane/terminal_style.rs` — no-edit-reason: still calls `render_state.colors()`.
- `crates/gterminal/src/ghostty/terminal_ops.rs` — no-edit-reason: kitty/grid helpers do not use the removed symbols.
- `crates/gterminal/src/ghostty/render_pre.rs` — no-edit-reason: kitty/grid/cell getters are unchanged in the candidate headers.

## P3: Toolchain docs and native cutover
`kind: framing`

**Goal**: humans and agents build with Zig 0.16.0 against macOS 27, and
installed `gterm`/`gclient` are activated only through the coordinated
new-inode path.

### 3.1 Update the terminal guide and cutover procedure [category: docs] (depends: 1.1, 2.1)
`kind: deliverable`

Targets:
- `docs/guides/gterminal-development-guide.md`

**Research context:** The guide currently requires Zig 0.15, tells
operators to pick Xcode with macOS 26.5 SDK because 0.15.2's libc++
breaks on 27, and documents `cp`/`mv` into `~/.gobby/bin`. After 1.1/2.1:
Zig 0.16.0 is the `vt-engine` build-time pin (official tarball SHA256
from Constraints); macOS 27 SDK is the validated SDK; `gobby-client`
stays Zig-free. Rebuild/reinstall must not teach hand copy.
`promote_workspace_binary_set`
(`src/gobby/install/bin_set_coherence.py`) stamps `gcode`/`gdaemon`/`ghook`
and ad-hoc signs. `gterm`/`gclient` are not SET_MEMBERS; they promote
through `stage_and_promote_binary_file`
(`src/gobby/install/bin_freshness_promotion.py`) — same new-inode
replace, never in-place overwrite. Coordinate with the install owner;
announce daemon/host restarts to active sessions; wait for a quiet
window. Do not edit those Python helpers in this leaf. Do not activate
binaries from an unreviewed worktree. `native-components` points at this
guide's rebuild section.

**Acceptance:**

- 3.1.1 - The guide names Zig 0.16.0, the macOS 27 SDK, bundled SIMD, and forbids `cp` into `~/.gobby/bin`; live activation is `promote_workspace_binary_set` / `stage_and_promote_binary_file`. behavior: "rebuild and reinstall" in `docs/guides/gterminal-development-guide.md`.

## V2 Isolated validation
`kind: verification`

All commands from this worktree with an isolated `CARGO_TARGET_DIR` and
Zig 0.16.0 on PATH. Never `cargo clean`. Never cargo/zig against
`~/Projects/gobby`. `xcrun --sdk macosx --show-sdk-version` is 27.x.

Planned (not run in this planning pass):

```sh
zig version                            # 0.16.0
bindgen --version                      # bindgen 0.72.1
xcrun --sdk macosx --show-sdk-version  # 27.x (observed 27.0)
# Static + shared bundled SIMD (no -fsys=simdutf, no -Demit-lib-vt-shared=false)
ZIG="$ZIG16" bash scripts/build_vendored_libghostty_vt.sh
otool -L crates/gterminal/vendor/libghostty-vt/zig-out/lib/libghostty-vt.dylib
nm crates/gterminal/vendor/libghostty-vt/zig-out/lib/libghostty-vt.a | grep simdutf

CARGO_TARGET_DIR=/tmp/gobby-22310-cargo \
  cargo fmt -p gobby-terminal -p gobby-client -- --check
CARGO_TARGET_DIR=/tmp/gobby-22310-cargo \
  cargo clippy -p gobby-terminal --features vt-engine --all-targets -- -D warnings
CARGO_TARGET_DIR=/tmp/gobby-22310-cargo \
  cargo clippy -p gobby-client --all-targets -- -D warnings

CARGO_TARGET_DIR=/tmp/gobby-22310-cargo \
  cargo nextest run -p gobby-terminal --features vt-engine --lib \
  grapheme_cluster_mode
CARGO_TARGET_DIR=/tmp/gobby-22310-cargo \
  cargo nextest run -p gobby-terminal --features vt-engine \
  --test frame_producer
CARGO_TARGET_DIR=/tmp/gobby-22310-cargo \
  cargo nextest run -p gobby-terminal --test build_env
CARGO_TARGET_DIR=/tmp/gobby-22310-cargo \
  cargo nextest run -p gobby-client --test frame_source_live --test frame_delivery

otool -L /tmp/gobby-22310-cargo/debug/gterm   # no libsimdutf
uv run pytest tests/gterminal/test_vendor_layer.py tests/cli/test_install_setup_gterm.py -q
uv run gobby plans validate .gobby/plans/ghostty-zig-ffi-upgrade.md -p .
```

Grapheme/emoji: `grapheme_cluster_mode_is_default_and_survives_full_reset`
(mod tests and pane/terminal.rs),
`grapheme_cluster_mode_renders_flag_emoji_in_single_wide_cell`,
`grapheme_cluster_mode_renders_zwj_family_in_single_wide_cell`.
Frame production: `crates/gterminal/tests/frame_producer.rs`
(`end_to_end_without_ratatui_frame`).
Live frame consumption: `crates/gclient/tests/frame_source_live.rs`
(`direct_frames_verify_epoch_and_render`) and
`crates/gclient/tests/frame_delivery.rs`.

Observed from #22304 only: upstream bundled static+shared built with Zig
0.16.0 on macOS 27; Gobby link failed on the three removed functions;
system simdutf rejected.

Native activation is not part of isolated validation. After review, the
install owner promotes via `promote_workspace_binary_set` /
`stage_and_promote_binary_file`.
