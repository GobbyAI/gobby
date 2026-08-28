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
`src/ui/` (sidebar, tabs, navigator, status, keybind help, dialogs, scrollbar,
widgets, text, settings, pane layout), BSP layout reuse from `gobby-terminal`,
input-capture patterns, copy-mode logical-line extract (`952729ee` / herdr
#2735), paste_payload bracketing, and workspace-snapshot shapes. See
`UPSTREAM.md` for the UI-module accept/reject map. This is a one-time fork;
there is no re-pin procedure.

## Modifications from upstream

- Rebrand: herdr client chrome → `gclient`. Data sources are the Gobby daemon
  (roster, attention, tasks) rather than herdr app state.
- Dropped herdr agent detection, plugin menus, worktree/session-persistence
  surfaces, onboarding, release-notes, and mobile modules.
- `ui/sidebar.rs` split: rows live in `ui/sidebar_rows.rs`.
- `ui/panes.rs` split: layout helpers live in `ui/pane_layout.rs`.
- Theme values come from `.impeccable.md` (hue 125 accent, deutan-safe state
  palette). Herdr's terminal_theme mechanism is reused via `gobby-terminal`.
- Frame attach is read-only. Writes go through the daemon lease/input/paste
  surface. Settings edit client-local preferences only.
