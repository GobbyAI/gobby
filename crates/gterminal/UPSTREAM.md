# gobby-terminal upstream record

Fork point: herdr `v0.8.0` (`857196dee1ce98df53efdd3f437aa2ac8a75b608`).
Reference clone: `~/.gobby/clones/herdr`.
This is a fork record, not a tracking contract. There is no re-pin procedure.

## Rebrand

- `herdr` → `gterm` in module docs, identifiers, socket names, and test paths
- `HERDR_*` environment variables → `GTERM_*` (`GTERM_ENV`, `GTERM_RENDER_PROF`, …)
- Wire `PROTOCOL_VERSION` restarts at `1` for the Gobby lineage (herdr was `19`)

## Imported module map (v0.8.0 paths → this crate)

| Upstream | Gobby |
| --- | --- |
| `src/ghostty/` | `crates/gterminal/src/ghostty/` |
| `src/pane/{cursor,input,kitty_keyboard,osc,state,terminal,xtgettcap}.rs` | `crates/gterminal/src/pane/` |
| `src/pty/` | `crates/gterminal/src/pty/` |
| `src/input/` | `crates/gterminal/src/input/` |
| `src/raw_input.rs` | `crates/gterminal/src/raw_input.rs` (+ `raw_input_framer.rs`) |
| `src/protocol/{mod,wire,render_ansi}.rs` | `crates/gterminal/src/protocol/` |
| `src/ipc.rs` | `crates/gterminal/src/ipc.rs` |
| `src/platform/` | `crates/gterminal/src/platform/` |
| `src/layout.rs` | `crates/gterminal/src/layout.rs` |
| `src/selection.rs` | `crates/gterminal/src/selection.rs` |
| `src/terminal_theme.rs` | `crates/gterminal/src/terminal_theme.rs` |
| `src/terminal_modes.rs` | `crates/gterminal/src/terminal_modes.rs` |
| `src/kitty_graphics.rs` | `crates/gterminal/src/kitty_graphics/` |
| `src/render_prof.rs` | `crates/gterminal/src/render_prof.rs` |
| `src/terminal/{id,runtime,runtime_registry,title,history_read}.rs` | `crates/gterminal/src/runtime/` |

## Not imported

- `src/pane/agent_detection.rs`
- `src/pane.rs` herdr `PaneRuntime` agent-detection task and `crate::detect`/`crate::integration` wiring (plan 1.3 imported a de-agent-ified `PaneRuntime`)
- `src/terminal/state.rs`, `src/terminal/metadata.rs`
- `src/app/`, `src/api/`, `src/cli/`, `src/detect/`, `src/integration/`, `src/persist/`, `src/workspace/`, `src/remote/`, `src/server/`, `src/client/`, plugins, updater, sound, session

## Cherry-pick log (`v0.8.0..HEAD` keep-set paths)

Applied at import:

| Commit | Decision | Notes |
| --- | --- | --- |
| `e9222d18` | **accept (applied)** | restore keyboard reporting on detach (`terminal_modes.rs`) |
| `d277d2f8` | **accept (applied)** | preserve alt-prefixed control keys (`input/parse.rs`, `pane/terminal.rs`) |
| `36074530` | **accept (applied)** | compact large terminal redraws (`protocol/render_ansi.rs` only; `server/headless.rs` not imported) |

Remaining keep-set-touching commits:

| Commit | Decision | Notes |
| --- | --- | --- |
| `adb50cba` | reject | pane automation / API encoding; host not imported |
| `ee8429fb` | reject | default mouse reports tied to herdr client input |
| `09cdd88d` | reject | layout focus-return coupled to workspace/API |
| `be1891ec` | reject | katakana marks also change unimported headless renderer |
| `2863b715` | reject | Windows remote attach / named-pipe product work |
| `ea047db8` | reject | legacy pane ctrl-tab; revisit with host input |
| `f83980db` | reject | OSC 4 palette forwarding; needs host theme path |
| `e9222d18` | (applied above) | |
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
