# gobby-terminal upstream record

Fork point: herdr release tag `v0.8.0`, commit
`346411fa21afd297f5ed3b3fa56f9e3fbf7654b7` (annotated tag object
`857196dee1ce98df53efdd3f437aa2ac8a75b608`).
Reference clone: `~/.gobby/clones/herdr`.

This is a fork record, not a tracking contract. There is no re-pin
procedure. Post-fork upstream fixes are adopted only as deliberate
per-commit cherry-picks recorded below.

## Rebrand

- `herdr` → `gterm` in module docs, identifiers, socket names, and test paths
- `HERDR_*` environment variables → `GTERM_*` (`GTERM_ENV`, `GTERM_RENDER_PROF`, …)
- Wire `PROTOCOL_VERSION` restarts at `1` for the Gobby lineage (herdr was `19`)

## Imported module map (v0.8.0 paths → this crate)

Paths are herdr paths at the fork-point commit. Gobby-local `include!` /
`#[path]` splits keep public APIs on the original module path.

| Upstream (`v0.8.0`) | Gobby |
| --- | --- |
| `src/ghostty/mod.rs` | `crates/gterminal/src/ghostty/mod.rs` (+ `terminal_api.rs`, `terminal_ops.rs`, `render_pre.rs`, `render_state.rs`, `mod/tests.rs`) |
| `src/ghostty/bindings.rs` | `crates/gterminal/src/ghostty/bindings.rs` (+ `bindings/generated_01.rs` … `generated_05.rs`) |
| `src/pane.rs` | `crates/gterminal/src/pane.rs` plus `pane/{runtime,runtime_ops,shell,shutdown}.rs` (rewritten in plan 1.3) |
| `src/pane/cursor.rs` | `crates/gterminal/src/pane/cursor.rs` |
| `src/pane/input.rs` | `crates/gterminal/src/pane/input.rs` |
| `src/pane/kitty_keyboard.rs` | `crates/gterminal/src/pane/kitty_keyboard.rs` |
| `src/pane/osc.rs` | `crates/gterminal/src/pane/osc.rs` |
| `src/pane/state.rs` | `crates/gterminal/src/pane/state.rs` |
| `src/pane/terminal.rs` | `crates/gterminal/src/pane/terminal.rs` (+ `terminal_io.rs`, `terminal_render.rs`, `terminal_style.rs`) |
| `src/pane/terminal/windows_recent_fallback.rs` | `crates/gterminal/src/pane/terminal/windows_recent_fallback.rs` |
| `src/pane/xtgettcap.rs` | `crates/gterminal/src/pane/xtgettcap.rs` |
| `src/pty/mod.rs` | `crates/gterminal/src/pty/mod.rs` |
| `src/pty/actor.rs` | `crates/gterminal/src/pty/actor.rs` |
| `src/pty/actor/unix.rs` | `crates/gterminal/src/pty/actor/unix.rs` |
| `src/pty/backend.rs` | `crates/gterminal/src/pty/backend.rs` |
| `src/pty/backend/unix.rs` | `crates/gterminal/src/pty/backend/unix.rs` |
| `src/pty/fd.rs` | `crates/gterminal/src/pty/fd.rs` |
| `src/input/mod.rs` | `crates/gterminal/src/input/mod.rs` |
| `src/input/encode.rs` | `crates/gterminal/src/input/encode.rs` |
| `src/input/model.rs` | `crates/gterminal/src/input/model.rs` |
| `src/input/parse.rs` | `crates/gterminal/src/input/parse.rs` |
| `src/raw_input.rs` | `crates/gterminal/src/raw_input.rs` (+ `raw_input_framer.rs`) |
| `src/protocol/mod.rs` | `crates/gterminal/src/protocol/mod.rs` |
| `src/protocol/wire.rs` | `crates/gterminal/src/protocol/wire.rs` (+ `wire_types.rs`, `wire_codec.rs`) |
| `src/protocol/render_ansi.rs` | `crates/gterminal/src/protocol/render_ansi.rs` (+ `render_ansi_blit.rs`) |
| `src/ipc.rs` | `crates/gterminal/src/ipc.rs` |
| `src/platform/mod.rs` | `crates/gterminal/src/platform/mod.rs` |
| `src/platform/macos.rs` | `crates/gterminal/src/platform/macos.rs` |
| `src/platform/linux.rs` | `crates/gterminal/src/platform/linux.rs` |
| `src/platform/windows.rs` | `crates/gterminal/src/platform/windows.rs` (+ `windows_process.rs`) |
| `src/platform/fallback.rs` | `crates/gterminal/src/platform/fallback.rs` |
| `src/layout.rs` | `crates/gterminal/src/layout.rs` |
| `src/selection.rs` | `crates/gterminal/src/selection.rs` |
| `src/terminal_theme.rs` | `crates/gterminal/src/terminal_theme.rs` |
| `src/terminal_modes.rs` | `crates/gterminal/src/terminal_modes.rs` |
| `src/kitty_graphics.rs` | `crates/gterminal/src/kitty_graphics/` (`mod.rs`, `host_paint.rs`, `host_stream.rs`) |
| `src/render_prof.rs` | `crates/gterminal/src/render_prof.rs` |
| `src/terminal/id.rs` | `crates/gterminal/src/runtime/id.rs` |
| `src/terminal/runtime.rs` | `crates/gterminal/src/runtime/runtime.rs` |
| `src/terminal/runtime_registry.rs` | `crates/gterminal/src/runtime/runtime_registry.rs` |
| `src/terminal/title.rs` | `crates/gterminal/src/runtime/title.rs` |
| `src/terminal/history_read.rs` | `crates/gterminal/src/runtime/history_read.rs` |
| `src/terminal/mod.rs` | `crates/gterminal/src/runtime/mod.rs` (crate-local module root) |

`crates/gterminal/src/lib.rs` is Gobby-authored crate root (not an herdr
file). Sidecar `#[path]` unit-test files under `*/tests.rs` ride along with
the imported modules.

## Not imported

- `src/pane/agent_detection.rs`
- herdr `src/pane.rs` detection task and `crate::detect` / `crate::integration`
  wiring (plan 1.3 imported a de-agent-ified `PaneRuntime`)
- `src/terminal/state.rs`, `src/terminal/metadata.rs`
- `src/app/`, `src/api/`, `src/cli/`, `src/detect/`, `src/integration/`,
  `src/persist/`, `src/workspace/`, `src/remote/`, `src/server/`,
  `src/client/`, `src/ui/`, plugins, updater, sound, session

`src/ui/` chrome is deferred to `gobby-client` (plan 3.3), not this crate.

## Cherry-pick log (`v0.8.0..HEAD` keep-set paths)

Keep-set paths: `src/ghostty/`, `src/pane.rs`, `src/pane/`, `src/pty/`,
`src/input/`, `src/raw_input.rs`, `src/protocol/{mod,wire,render_ansi}.rs`,
`src/ipc.rs`, `src/platform/`, `src/layout.rs`, `src/selection.rs`,
`src/terminal_theme.rs`, `src/terminal_modes.rs`, `src/kitty_graphics.rs`,
`src/render_prof.rs`, `src/terminal/`.

Applied at import:

| Commit | Decision | Notes |
| --- | --- | --- |
| `e9222d18` | **accept (applied)** | restore keyboard reporting on detach (`terminal_modes.rs`) |
| `d277d2f8` | **accept (applied)** | preserve alt-prefixed control keys (`input/parse.rs`, `pane/terminal.rs`) |
| `36074530` | **accept (applied)** | compact large terminal redraws (`protocol/render_ansi.rs` only; `server/headless.rs` not imported) |

Remaining keep-set-touching commits, each with an accept/reject decision:

| Commit | Decision | Notes |
| --- | --- | --- |
| `adb50cba` | reject | pane automation / API encoding; host not imported |
| `ee8429fb` | reject | default mouse reports tied to herdr client input |
| `09cdd88d` | reject | layout focus-return coupled to workspace/API |
| `be1891ec` | reject | katakana marks also change unimported headless renderer |
| `2863b715` | reject | Windows remote attach / named-pipe product work |
| `ea047db8` | reject | legacy pane ctrl-tab; revisit with host input |
| `f83980db` | reject | OSC 4 palette forwarding; needs host theme path |
| `fd0e4ff4` | reject | copy-mode big-word motions owned by gclient later |
| `3825c0c3` | reject | host appearance refresh via app/theme_sync |
| `b0723b79` | reject | kitty shift reports also change app navigate + corpus |
| `00f04ac6` | reject | Windows recent-history snapshots; Windows runtime later |
| `7d77e927` | reject | Git Bash agent candidate scans |
| `6f311498` | reject | terminal bells wired through events/server |
| `10974c82` | reject | per-pane right-click routing (API/app) |
| `1777e9bb` | reject | pane graphics streaming (API/server; D1/host later) |
| `e7c38ab3` | reject | UI scrollbar mode reads |
| `e2aa86a9` | reject | pane-scaled render performance tests with UI |
| `e48d8306` | reject | tab bar status / platform status commands |
| `ccccda54` | reject | opencode TUI session tracking (`terminal/state.rs` not imported) |
| `1ac44afc` | reject | elevated Windows title decoration |
| `350f0013` | reject | outer window title sync with session |
| `7ae4b056` | reject | agent prompt readiness |
| `06ca0baa` | reject | Windows idle agent-detection CPU |
| `3f752a72` | reject | cancelled status-command termination (tab bar) |
| `49e333ae` | reject | Claude title spinner stripping |
| `a4d52ab6` | reject | qwen detection (`terminal/state.rs` not imported) |
| `952729ee` | reject | scrollback editor logical lines; app navigate |
