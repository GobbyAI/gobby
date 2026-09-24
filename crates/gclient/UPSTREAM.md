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
| `src/ui.rs` | accept, split | `ui/chrome.rs` (view state, tabs, geometry) + `ui/chrome_render.rs` (frame composition) |
| `src/ui/sidebar.rs` | accept, split | `ui/sidebar.rs` + `ui/sidebar_rows.rs` |
| `src/ui/sidebar/tokens.rs` | accept | `ui/sidebar_tokens.rs` |
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
| `src/config/keybinds.rs` | accept (chord parsing, formatting, matching) | `ui/keymap.rs` |
| `src/ui/menus.rs` | reject / drop | plugin and herdr menu entries |
| `src/ui/mobile.rs` | reject / drop | mobile layout |
| `src/ui/onboarding.rs` | reject / drop | onboarding |
| `src/ui/release_notes.rs` | reject / drop | release notes |

herdr's `src/app/` orchestration is dropped wholesale; Gobby's `src/app/` is
new code that talks to the daemon.

Keep-set count: 15 accepted upstream files carve into 18 modules under
`crates/gclient/src/ui/`, each headed `// upstream: herdr v0.8.0 <path>`
(split modules repeat their source's header). 4 upstream UI modules are
dropped. `tests/ui_carve_guard.rs` checks this table against the tree.

Modules written for Gobby under `src/ui/` carry `// upstream: none` instead,
optionally followed by what they were modelled on: `ui/hit.rs` (the hit map
over `ViewState`) and `ui/context_menu.rs` (the right-click popup, adapted
from herdr v0.9.0 `src/client/shell/overlays.rs::render_context_menu`, which
post-dates the fork; v0.8.0 has no context menu). The guard accepts either
header and still rejects a headerless module.

### Keymap provenance

- Default chords are transcribed from herdr `src/config/model.rs`
  `KeysConfig::default()` at v0.8.0 into `ui/keymap.rs::BINDINGS` (prefix
  `ctrl+b`). Chord parsing, formatting, normalisation, event matching, and
  `prefix+1..9` range expansion are ported from `src/config/keybinds.rs`.
- herdr workspace/agent action names become terminal/attention names; the
  worktree and mobile actions are not carried. `custom_command` stays as a
  `reserved` binding: it never dispatches and is hidden from keybind help, so
  its default chord is free for an override to reclaim.
- Overrides are client-local TOML at `~/.gobby/client/keymap.toml`, parsed
  with the `toml` crate; an override that collides with another active chord
  is rejected and the defaults stand.

### gobby-terminal links

`gclient` links five `gobby_terminal` modules and copies none of them:
`layout` (BSP tiles, `PaneId`, `ScrollMetrics`; `ui/chrome.rs`,
`ui/pane_layout.rs`, `ui/scrollbar.rs`), `raw_input` and `input`
(`src/key_input.rs` turns parsed host bytes into keymap events and pane
bytes under the negotiated keyboard protocol), `selection`
(`Chrome::selection`, painted per layout slot by `ui/panes.rs`), and
`terminal_theme` (`Theme::terminal_theme` applies the `.impeccable.md` map).
`gobby_terminal::protocol` carries the frame wire. No copied `layout.rs` or
`raw_input.rs` exists in this crate; `tests/source_size.rs` checks both.

## Cherry-picks

| Commit | Decision | Notes |
| --- | --- | --- |
| `952729ee` | **accept (applied)** | copy-mode logical lines for wrapped wide graphemes (herdr #2735) |

## Render-test parity (4.1)

The keep-set render tests of herdr `346411fa21afd297f5ed3b3fa56f9e3fbf7654b7` are ported
under `tests/parity/` with row-text expectations unchanged; `tests/parity/upstream_tests.txt`
is the pinned inventory (98 identities, SHA-256 `1f240fd9a9cc7e855ec76ced40b0b33ece3c57637eb8df8355bbe82b5830e0d8`).

<!-- parity-table:start -->
| herdr source | ported | not ported | gclient parity module |
| --- | --- | --- | --- |
| `src/ui.rs` | 26 | 9 | `tests/parity/chrome.rs` |
| `src/ui/dialogs.rs` | 4 | 2 | `tests/parity/dialogs.rs` |
| `src/ui/keybind_help.rs` | 2 | 0 | `tests/parity/chrome.rs` |
| `src/ui/navigator.rs` | 5 | 0 | `tests/parity/navigator.rs` |
| `src/ui/panes.rs` | 15 | 2 | `tests/parity/panes.rs` |
| `src/ui/sidebar.rs` | 27 | 14 | `tests/parity/sidebar.rs` |
| `src/ui/sidebar/tokens.rs` | 6 | 0 | `tests/parity/sidebar.rs` |
| `src/ui/status.rs` | 4 | 0 | `tests/parity/status.rs` |
| `src/ui/tab_surface.rs` | 2 | 1 | `tests/parity/chrome.rs` |
| `src/ui/tabs.rs` | 5 | 0 | `tests/parity/tabs.rs` |
| `src/ui/text.rs` | 2 | 0 | `tests/parity/chrome.rs` |
| `src/ui/menus.rs` | 0 | 0 | dropped module |
| `src/ui/mobile.rs` | 0 | 15 | dropped module |
| `src/ui/onboarding.rs` | 0 | 0 | dropped module |
| `src/ui/release_notes.rs` | 0 | 6 | dropped module |
<!-- parity-table:end -->

Not ported: pinned dropped-surface identities (28). These sit in kept modules but
exercise worktree, git-space, or mobile surfaces gclient dropped (3.1), or the three
surfaces gclient redesigned or omitted (D4): the toast stack is pinned to the
top-right corner of the pane area and there is no diagnostic bar to dodge, there is
no tab-bar position setting, sidebar rows are not drag-reorderable, and a roster row is named by its
terminal title, so the title outranks its trailing tokens instead of truncating to
keep them, and an agent row carries no tab token at all: its `node:workspace:tab:pane`
address names the tab, so herdr's tab-label visibility rule has nothing to show. The
3.2a chrome refresh retired nine more: the collapsed rail, the sidebar's collapse toggle
and the pane-borders pref are gone (the sidebar is hidden or pinned, and every pane
draws all four edges).

- `src/ui.rs::collapsed_sidebar_keeps_active_workspace_highlight_in_terminal_mode`
- `src/ui.rs::configured_mobile_width_threshold_controls_layout_switch`
- `src/ui.rs::desktop_tab_bar_position_controls_geometry_and_mode_bar_placement`
- `src/ui.rs::desktop_toast_hit_area_still_offsets_for_config_diagnostic`
- `src/ui.rs::desktop_toast_hit_area_uses_full_frame_not_terminal_area`
- `src/ui.rs::hidden_collapsed_sidebar_uses_full_width_terminal_area`
- `src/ui.rs::mobile_background_tabs_use_mobile_terminal_area`
- `src/ui.rs::mobile_config_diagnostic_keeps_command_visible`
- `src/ui.rs::mobile_width_uses_header_and_full_width_terminal`
- `src/ui/dialogs.rs::new_worktree_error_renders_fatal_stderr_line`
- `src/ui/dialogs.rs::new_worktree_hit_test_geometry_matches_modal_size`
- `src/ui/panes.rs::borderless_pane_gaps_add_one_empty_cell_between_panes`
- `src/ui/panes.rs::disabled_pane_borders_make_inner_rect_equal_visual_rect`
- `src/ui/sidebar.rs::agent_panel_tab_label_visibility_tracks_tab_identity`
- `src/ui/sidebar.rs::collapsed_sidebar_keeps_status_visible_for_two_digit_positions`
- `src/ui/sidebar.rs::collapsed_sidebar_numbers_grouped_agents_by_list_position`
- `src/ui/sidebar.rs::collapsed_sidebar_numbers_priority_agents_by_list_position`
- `src/ui/sidebar.rs::desktop_worktree_connector_uses_full_list_at_viewport_boundary`
- `src/ui/sidebar.rs::desktop_worktree_tree_aligns_parents_and_marks_children`
- `src/ui/sidebar.rs::expanded_sidebar_toggle_sits_inside_sidebar_content`
- `src/ui/sidebar.rs::linked_only_worktree_members_do_not_form_parentless_group`
- `src/ui/sidebar.rs::narrow_agent_rows_preserve_later_tab_tokens`
- `src/ui/sidebar.rs::packed_workspace_drag_indicator_overlays_an_internal_boundary`
- `src/ui/sidebar.rs::render_sidebar_toggle_draws_expanded_collapse_icon`
- `src/ui/sidebar.rs::space_row_gap_preserves_compact_worktree_children`
- `src/ui/sidebar.rs::workspace_list_entries_group_multiple_workspaces_in_same_git_space`
- `src/ui/sidebar.rs::workspace_list_entries_group_non_contiguous_explicit_members`
- `src/ui/tab_surface.rs::mobile_full_app_semantic_frame_is_characterized`

Not ported: dropped modules (21). `menus.rs` and `onboarding.rs` carry no tests at
the pinned commit; every `#[test]` in the other two is listed:

- `src/ui/mobile.rs::global_agent_counts_ignore_active_agent_view_filter`
- `src/ui/mobile.rs::agent_summary_leads_with_attention_states_in_priority_order`
- `src/ui/mobile.rs::agent_summary_hides_empty_categories`
- `src/ui/mobile.rs::agent_summary_collapses_to_all_idle_without_attention`
- `src/ui/mobile.rs::agent_summary_drops_least_urgent_segments_when_narrow`
- `src/ui/mobile.rs::agent_summary_keeps_all_segments_when_wide_enough`
- `src/ui/mobile.rs::agent_summary_reports_no_agents_when_empty`
- `src/ui/mobile.rs::switcher_leads_with_agents_and_shifts_spaces_below`
- `src/ui/mobile.rs::switcher_spaces_follow_grouped_worktree_order`
- `src/ui/mobile.rs::switcher_without_agents_keeps_spaces_first`
- `src/ui/mobile.rs::mobile_agent_detail_includes_tab_context_when_available`
- `src/ui/mobile.rs::mobile_agent_detail_keeps_existing_compact_detail_without_tab_context`
- `src/ui/mobile.rs::mobile_tab_status_uses_compact_tab_label_and_position`
- `src/ui/mobile.rs::mobile_switcher_uses_compact_tab_label_for_auto_tab_labels`
- `src/ui/mobile.rs::mobile_header_uses_live_root_runtime_cwd_for_workspace_label`
- `src/ui/release_notes.rs::release_notes_inline_code_spans_are_styled_without_backticks`
- `src/ui/release_notes.rs::release_notes_config_inline_code_uses_nonbreaking_spaces`
- `src/ui/release_notes.rs::release_notes_preview_lines_show_update_steps`
- `src/ui/release_notes.rs::release_notes_preview_display_is_part_of_the_scrollable_notes_body`
- `src/ui/release_notes.rs::release_notes_fenced_code_blocks_render_as_preformatted_lines`
- `src/ui/release_notes.rs::release_notes_fenced_code_blocks_preserve_blank_lines`

Deferred: ported with herdr's expectations verbatim and counted above, but marked
`#[deferred]` in `tests/parity/` (ignored by nextest, asserted still-red by
`deferred_cases_are_still_red`) until the named task lands the surface:

- `src/ui.rs::keybind_help_shows_custom_command_descriptions` — #20201 (custom
  command bindings and their help rows)
