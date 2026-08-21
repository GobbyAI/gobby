# gobby-client upstream record

Fork point: herdr release tag `v0.8.0`, commit
`346411fa21afd297f5ed3b3fa56f9e3fbf7654b7`.
Reference clone: `~/.gobby/clones/herdr`.

This is a fork record, not a tracking contract. There is no re-pin
procedure. Post-fork upstream fixes are adopted only as deliberate
per-commit cherry-picks recorded below.

## UI-module accept/reject map (`src/ui/` at v0.8.0)

| Upstream | Decision | Gobby |
| --- | --- | --- |
| `src/ui.rs` | accept (rewritten crate root) | `crates/gclient/src/ui/mod.rs` |
| `src/ui/sidebar.rs` | accept, split | `ui/sidebar.rs` + `ui/sidebar_rows.rs` |
| `src/ui/panes.rs` | accept, split | `ui/panes.rs` + `ui/pane_layout.rs` |
| `src/ui/tabs.rs` | accept | `ui/tabs.rs` |
| `src/ui/tab_surface.rs` | accept | `ui/tab_surface.rs` |
| `src/ui/navigator.rs` | accept | `ui/navigator.rs` |
| `src/ui/status.rs` | accept | `ui/status.rs` |
| `src/ui/keybind_help.rs` | accept | `ui/keybind_help.rs` |
| `src/ui/dialogs.rs` | accept (no worktree dialogs) | `ui/dialogs.rs` |
| `src/ui/scrollbar.rs` | accept | `ui/scrollbar.rs` |
| `src/ui/widgets.rs` | accept | `ui/widgets.rs` |
| `src/ui/text.rs` | accept | `ui/text.rs` |
| `src/ui/settings.rs` | accept (client-local only) | `ui/settings.rs` |
| `src/ui/menus.rs` | reject / drop | plugin and herdr menu entries |
| `src/ui/mobile.rs` | reject / drop | mobile layout |
| `src/ui/onboarding.rs` | reject / drop | onboarding |
| `src/ui/release_notes.rs` | reject / drop | release notes |
| `src/app/` | reject / drop | herdr orchestration; Gobby `src/app/` is new |

## Cherry-picks

| Commit | Decision | Notes |
| --- | --- | --- |
| `952729ee` | **accept (applied)** | copy-mode logical lines for wrapped wide graphemes (herdr #2735) |
