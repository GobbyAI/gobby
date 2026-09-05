# Notice

gobby-client
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

The herdr-derived portions of this crate are the imported UI chrome under
`src/ui/` (chrome view state and frame composition, sidebar, tabs, navigator,
status, keybind help, dialogs, scrollbar, widgets, text, settings, pane
layout), the keymap chord grammar in `src/ui/keymap.rs` (from herdr
`src/config/keybinds.rs`), BSP layout reuse from `gobby-terminal`,
input-capture patterns, copy-mode logical-line extract (`952729ee` / herdr
#2735), paste_payload bracketing, and workspace-snapshot shapes. See
`UPSTREAM.md` for the UI-module accept/reject map. This is a one-time fork;
there is no re-pin procedure.

## Modifications from upstream

- Rebrand: herdr client chrome → `gclient`. Data sources are the Gobby daemon
  (roster, attention, tasks) rather than herdr app state.
- Dropped herdr agent detection, plugin menus, worktree/session-persistence
  surfaces, onboarding, release-notes, and mobile modules.
- `src/ui.rs` split: view state in `ui/chrome.rs`, frame composition in
  `ui/chrome_render.rs`; herdr app state is replaced by a `WorkspaceView`
  trait over the Gobby workspace.
- `ui/sidebar.rs` split: rows live in `ui/sidebar_rows.rs`; token stats in
  `ui/sidebar_tokens.rs` (from `src/ui/sidebar/tokens.rs`).
- `ui/panes.rs` split: layout helpers live in `ui/pane_layout.rs`.
- Keymap: herdr defaults transcribed into `ui/keymap.rs`; worktree and mobile
  actions dropped, plugin `custom_command` kept reserved and non-dispatching.
- Theme values come from `.impeccable.md` (hue 125 accent, deutan-safe state
  palette, dark and light). `src/theme.rs` maps them onto
  `gobby_terminal::terminal_theme::TerminalTheme`; herdr's catppuccin palette
  survives only as the `Palette` field names.
- Frame attach is read-only. Writes go through the daemon lease/input/paste
  surface. Settings edit client-local preferences only.
