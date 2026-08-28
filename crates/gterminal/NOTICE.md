# Notice

gobby-terminal
Copyright 2026 Josh Wilhelmi

Licensed under the Apache License, Version 2.0 (the "License"); you may not use
this file except in compliance with the License. You may obtain a copy of the
License at:

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software distributed
under the License is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR
CONDITIONS OF ANY KIND, either express or implied. See the License for the
specific language governing permissions and limitations under the License.

This crate keeps Apache-2.0 identity inside the surrounding Gobby repository
(`FSL-1.1-ALv2`). See `LICENSE` in this directory and `license = "Apache-2.0"`
in `Cargo.toml`.

## Upstream

### herdr

- Source: https://github.com/herdrdev/herdr
- License: Apache License 2.0
- Fork point: release tag `v0.8.0` (2026-08-03), commit
  `346411fa21afd297f5ed3b3fa56f9e3fbf7654b7`
- Relicense: commit `cd5ea1be` (2026-07-22) recorded in the **released**
  `v0.8.0` changelog as "Relicensed Herdr from AGPL-3.0-or-later to
  Apache-2.0."
- Reference clone: `~/.gobby/clones/herdr`
- Upstream ships no `NOTICE` file.
- Caveat: the AGPL→Apache contributor-consent mechanism for outside
  contributors is not documented in the upstream repository.

The keep-set imported from herdr is the terminal core (ghostty FFI, pane
emulation, PTY, input, wire protocol, IPC, platform process ops, layout,
selection, theme, modes, kitty graphics, render profiling, and the small
runtime plumbing under herdr `src/terminal/`). See `UPSTREAM.md` for the
module map. This is a one-time fork; there is no re-pin procedure.

### libghostty-vt (Ghostty)

- Source: https://github.com/ghostty-org/ghostty
- License: MIT
- Copyright: 2024 Mitchell Hashimoto, Ghostty contributors
- Vendored pin: `c5a21edfcbc2d5b46540ad91b7980aca31f5f1f3`
  (`1.3.2-HEAD-+c5a21edfc`)
- Location: `vendor/libghostty-vt/`
- Built only with `-Demit-lib-vt` when this crate's `vt-engine` feature is
  enabled. The full Ghostty application, renderer, and font stack are not
  linked.

The MIT license text is at `vendor/libghostty-vt/LICENSE`.

### portable-pty

- Source: https://github.com/wezterm/wezterm (crate `portable-pty`)
- License: MIT
- Copyright: 2018 Wez Furlong
- Vendored version: `0.9.0`
- Location: `vendor/portable-pty/`
- Workspace `[patch.crates-io]` redirects the crates.io crate at this pin.

The MIT license text is at `vendor/portable-pty/LICENSE.md`.

## Modifications from upstream

### herdr keep-set

- Rebrand: `herdr` → `gterm` in module docs, identifiers, socket names, and
  test paths; `HERDR_*` environment variables → `GTERM_*`; wire
  `PROTOCOL_VERSION` restarts at `1` (herdr was `19`).
- Plan 1.3 de-agent-ify: `PaneRuntime` has no detection fields, tasks, or
  `crate::detect` / `crate::integration` wiring. OSC title/progress stay under
  neutral names (`osc_title()`, `osc_progress()`). Live-path process-table
  probes (`foreground_job`) are dropped. Rendering inverted from
  `&mut ratatui::Frame` as the primary API to `frame_data` / `dirty_patch`.
- Line-count splits via `include!` / `#[path]` so public APIs remain on the
  original module paths (see `UPSTREAM.md`).
- Three post-`v0.8.0` cherry-picks applied at import: `e9222d18`, `d277d2f8`,
  `36074530`. Remaining keep-set-touching commits are rejected in
  `UPSTREAM.md`. This is a fork record, not a tracking contract.

### libghostty-vt

Local patch `vendor/patches/libghostty-vt/0001-default-grapheme-cluster-mode.patch`
defaults lib-vt panes to grapheme clustering (DEC private mode 2027) so RIS
does not disable it. See `vendor/libghostty-vt.patches.md`.

### portable-pty

Two local patches, documented in `vendor/portable-pty.patches.md`:

- `0001-control-conpty-loading.patch` — hash-verified app-local ConPTY load
  instead of a bare `conpty.dll` PATH search.
- `0002-windows-raw-command-tail.patch` — Windows raw command-line tail for
  `cmd.exe /d /c`.

## Nested licenses in `vendor/libghostty-vt/pkg/`

Ghostty vendors in-tree Zig wrappers under `pkg/`. Per `pkg/README.md`, those
wrapper sources are MIT © 2024 Mitchell Hashimoto and Ghostty contributors,
except `pkg/afl++/LICENSE` (MIT © 2024 Loris Cro, zig-afl-kit).

The wrappers reference third-party tarballs through `build.zig.zon` fetch
URLs. Those nested projects are present in this source tree only as wrapper
code plus fetch metadata. **None of them are compiled into libghostty-vt
(`-Demit-lib-vt`) builds.** Inventory:

| `pkg/` directory | Nested project (fetch-only unless noted) |
| --- | --- |
| `afl++` | zig-afl-kit (in-tree MIT LICENSE); AFL++ |
| `android-ndk` | Android NDK bindings |
| `apple-sdk` | Apple SDK bindings |
| `breakpad` | Breakpad (`breakpad-b99f444ba5…`) |
| `dcimgui` | Dear ImGui / DearBindings |
| `fontconfig` | fontconfig 2.14.2 |
| `freetype` | FreeType |
| `glslang` | glslang |
| `gtk4-layer-shell` | gtk4-layer-shell 1.1.0; wayland-protocols |
| `harfbuzz` | HarfBuzz 11.0.0 |
| `highway` | Highway |
| `libintl` | gettext 0.24 |
| `libpng` | libpng |
| `libxml2` | libxml2 2.11.5 |
| `macos` | macOS Zig bindings |
| `oniguruma` | Oniguruma |
| `opengl` | OpenGL bindings |
| `sentry` | sentry-native (getsentry); also pulls `breakpad` |
| `simdutf` | simdutf |
| `spirv-cross` | SPIRV-Cross |
| `wuffs` | Wuffs; pixels |
| `zlib` | zlib |
