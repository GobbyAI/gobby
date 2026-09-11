# gclient mouse parity and pointer affordances

**Plan ID:** gclient-mouse-parity

## Overview
`kind: framing`

gclient (crates/gclient) never enables mouse capture: `TerminalGuard::arm` in
`crates/gclient/src/teardown.rs` sets raw mode, the alternate screen, bracketed
paste and hides the cursor, and stops there. The only mouse handler,
`route_mouse_selection` in `crates/gclient/src/copy_mode.rs`, acts in `Mode::Copy`
and is reached only by tests. herdr 0.8.0 (read-only reference at
`~/.gobby/clones/herdr`, never modified) is mouse-native: click panes, tabs,
workspaces and agent rows; drag tabs, rows, split borders and the sidebar
divider; right-click menus; drag-select and double-click copy; ctrl-click links;
wheel scrollback with forwarding to mouse-reporting apps; `ui.mouse_capture=false`
as the escape hatch. The user's decision for this work is "parity+": every herdr
mouse behavior, plus three gobby-native affordances (roster click attaches, an
attention click jumps to the session that raised it, the control indicator
takes or releases control by mouse), plus the keyboard and render gaps the parity
review found (30+ advertised chords are inert, keymap overrides never load,
non-focused panes render blank).

The review evidence lives in `~/.gobby/reviews/gclient-herdr-parity/`
(REVIEW-STATE.md, README.md, captures). Findings referenced below: F1 focus is the
daemon lease, F2 type-to-take-control, F3 blank non-focused panes, F4 visual
contract, F5 no mouse capture, F6 inert chords, F7 herdr mouse contract, F8 four
files at the 1,000-line ceiling, F9 dead keymap overrides.

Input already carries mouse events: `gobby_terminal::raw_input::spawn_input_reader`
parses SGR reports into `RawInputEvent::Mouse(crossterm::event::MouseEvent)`
(`crates/gterminal/src/raw_input.rs`). Hit geometry already exists: `ViewState`
in `crates/gclient/src/ui/chrome.rs` declares `tab_hit_areas`,
`roster_hit_areas`, `attention_hit_areas`, `new_tab_hit_area`, tab scroll hit
areas, `toast_hit_area`, `pane_infos` and `split_borders`, and `render_tab_bar` /
`render_sidebar` return `TabBarHits` / `SidebarHits`, but `render_workspace_with`
takes `&Chrome` so those hits are dropped and `compute_view` leaves the fields at
`ViewState::default()`. The work is to enable capture behind an escape hatch, keep
the hit map, and dispatch.

## Constraints
`kind: framing`

- Monolith ceiling: every hand-maintained production `.rs` file stays under 1,000
  lines; `crates/gclient/tests/source_size.rs::no_src_file_at_or_above_1000_lines`
  enforces it. Current sizes: `app/live_loop.rs` 997, `app/live.rs` 974,
  `ui/keymap.rs` 953, `app/mod.rs` 933. No mouse code lands in those four files;
  the split in 1.4 happens before any live-loop feature work, and every phase's
  Targets name new modules.
- New modules, all owned by this plan and declared from `live_loop.rs` (the
  loops' dispatch children; the scripted loop reaches them through `super::live_loop`
  with `pub(super)` visibility) so the near-ceiling parent module file gains no
  lines: `crates/gclient/src/app/live_loop/control.rs`,
  `crates/gclient/src/app/live_loop/actions.rs`, `crates/gclient/src/app/live_loop/mouse/mod.rs`,
  `crates/gclient/src/app/live_loop/mouse/pointer.rs`, `crates/gclient/src/app/live_loop/mouse/wheel.rs`,
  `crates/gclient/src/app/live_loop/mouse/select.rs`, `crates/gclient/src/app/live_loop/mouse/links.rs`,
  `crates/gclient/src/app/live_loop/mouse/forward.rs`,
  `crates/gclient/src/app/live_loop/menu.rs`, `crates/gclient/src/app/live_loop/modal_input.rs`,
  `crates/gclient/src/prefs.rs`, `crates/gclient/src/ui/hit.rs`,
  `crates/gclient/src/ui/context_menu.rs`.
- The lease model stays: focus is the daemon lease (`focus_live_pane` releases the
  previous pane's lease and takes the new one; bare keys in Observe take control
  before they are written). Mouse focus follows the same path; the observe-only
  variant is an explicit modifier, never the default.
- Mouse capture changes the host terminal for the user (native selection stops),
  so the escape hatch equivalent to herdr's `ui.mouse_capture=false` ships in P1
  as a prefs key, a CLI flag and a live settings toggle; capture is never enabled
  when the pref is off.
- Wire protocol: `ClientMessage` (`crates/gterminal/src/protocol/wire_types.rs`)
  has no input path by design; every byte a pane receives goes through the daemon
  WebSocket (`terminal_input` / `terminal_paste` in `send_live_write`). Mouse
  forwarding therefore encodes SGR reports as `terminal_input` bytes; it never
  adds a write path to the frame socket.
- Design contract: all chrome work reads `.impeccable.md` (CLI/TUI rules at
  lines 177-243) and loads the `impeccable` skill first. Deutan-safe state
  palette (info 250, warning 75, destructive 350, success 125 by lightness), never
  hue alone, no pure black/white, no red/green, no exclamation marks or emoji in
  client text. Menus, hover states and the control indicator use the existing
  `Palette` tokens in `crates/gclient/src/theme.rs`.
- herdr workspace surface. A herdr workspace is a repository checkout that owns
  tabs and is listed in the sidebar; gclient serves one daemon whose roster is
  flat (every terminal, whatever its project) and whose only grouping above a
  pane is the tab. gclient therefore has no workspace list: every herdr
  workspace mouse action lands on the tab bar, the roster or the settings
  dialog, or is disposed by name in the table at the end of this section.
- Non-goals: the nested-tmux prefix lockout is #21923 (open, needs-decision) and is
  not scheduled here; 2.5 gives a Held pane a mouse escape, which narrows #21923
  to the keyboard-only case. The scrollback editor (`edit_scrollback`) is removed
  from the action table in 4.1 rather than implemented (the scrollback lives on
  the host); `custom_command` stays reserved and hidden for the plugin-menu plan
  (#20201) and is not touched. Native (ghostty) pane pixel-mouse mapping (herdr
  `src/input/mouse.rs` `HostPixels`) is out of scope: gclient forwards cell
  coordinates only.
- UI work is done by the coordinator (session #12029); non-UI leaves (gterminal
  host and protocol changes in 1.5) go to codex agents and land via
  `merge_worktree`. Validation per leaf: `cargo fmt --check`,
  `cargo clippy -p gobby-client --all-targets -- -D warnings`,
  `cargo test -p gobby-client <filter>` (never bare `cargo test`), and for 1.5
  `cargo test -p gobby-terminal <filter>`.

herdr workspace surface, mapped action by action:

| herdr action | gclient surface | section |
| --- | --- | --- |
| sidebar workspace row click (`FocusWorkspace`) | tab click (`Hit::Tab`); in the collapsed rail herdr focuses a workspace glyph and gclient focuses the roster index glyph (`Hit::Roster`) | 2.2, 2.3 |
| workspace drag reorder (`MoveWorkspace`, `WORKSPACE_DRAG_THRESHOLD`) | tab drag reorder (`TAB_DRAG_THRESHOLD`); roster drag reorder for daemon rows | 2.2, 2.3 |
| worktree group drag (`MoveWorkspaceBlock`) | none: tabs and roster rows have no parent/child grouping, so there is no block to move; disposed | none |
| `[+ New]` sidebar button (`NewWorkspace`) | `[+]` new-tab button at the end of the tab bar (`Hit::NewTab`, `Spawn { placement: Placement::Tab }`); with `hide_tab_bar_when_single_tab` the button hides with the bar and the global menu `new tab` item plus the `NewTab` chord remain | 2.2, 5.1 |
| workspace list scrollbar thumb drag and track click (`workspace_list_scrollbar_target_at`) | roster and attention list scrollbars (`Hit::SidebarScrollbar`) | 1.3, 2.3 |
| workspace context menu (`ContextMenuKind::Workspace`: rename, close; `GitWorkspace` adds worktree items) | tab menu (`rename tab`, `close tab`, `new tab`); the worktree items have no client counterpart because gobby worktrees belong to the daemon's agent runs, not to a client checkout; disposed | 5.1 |
| settings overlay clicks (`handle_settings_mouse`) | settings dialog rows (`Hit::SettingsRow`) | 1.3, 4.2 |

## P1: Mouse foundation
`kind: framing`

**Goal**: mouse events reach a single dispatcher with a durable hit map, behind an
escape hatch, without any production file crossing the ceiling.

### 1.1 Add client prefs persistence with the `mouse_capture` escape hatch [category: code]
`kind: deliverable`

Targets:
- `crates/gclient/src/prefs.rs`
- `crates/gclient/src/ui/settings.rs::ClientPrefs`
- `crates/gclient/src/ui/settings.rs::ClientPrefs::default`
- `crates/gclient/src/ui/settings.rs::SettingsRow`
- `crates/gclient/src/ui/settings.rs::row_label`
- `crates/gclient/src/ui/settings.rs::row_value`
- `crates/gclient/src/startup.rs::CliArgs`
- `crates/gclient/src/startup.rs::parse_args`
- `crates/gclient/src/startup.rs::Ready`
- `crates/gclient/src/startup.rs::prepare_at`
- `crates/gclient/src/views/mod.rs::run_ready`
- `crates/gclient/src/lib.rs`
- `crates/gclient/src/ui/pane_layout.rs::*` — scope-reason: its tests build ClientPrefs::default and assert the new field
- `crates/gclient/tests/startup.rs::*` — scope-reason: new test functions appended; existing cases only change where the acceptance item names them
- `crates/gclient/tests/persist.rs::*` — scope-reason: new test functions appended; existing cases only change where the acceptance item names them

`ClientPrefs` (`crates/gclient/src/ui/settings.rs`) is built only by
`ClientPrefs::default()` in `Chrome::new`; nothing loads or saves it, so the
settings dialog shows values the user cannot change durably. Add
`mouse_capture: bool` (default `true`) to `ClientPrefs`, and create
`crates/gclient/src/prefs.rs` with:

```rust
pub const PREFS_FILE: &str = "client/prefs.toml"; // under ~/.gobby

pub fn prefs_path(gobby_home: &Path) -> PathBuf;
pub fn load_prefs(gobby_home: &Path) -> Result<ClientPrefs, PrefsError>; // missing file => default()
pub fn save_prefs(gobby_home: &Path, prefs: &ClientPrefs) -> Result<PathBuf, PrefsError>; // atomic: write .tmp, rename
```

File shape (TOML, every key optional, unknown keys rejected with the key name in
the error):

```toml
[ui]
theme = "dark"
mouse_capture = true
pane_borders = true
pane_scrollbars = true
pane_gaps = true
confirm_close = true
hide_tab_bar_when_single_tab = false
sidebar_width = 26
[keymap]
path = ""            # override file; empty = ~/.gobby/client/keymap.toml
```

`ClientPrefs` derives `serde::{Serialize, Deserialize}` with
`#[serde(default, deny_unknown_fields)]`; the `[ui]`/`[keymap]` grouping is a
`PrefsFile { ui: UiPrefs, keymap: KeymapPrefs }` wrapper in `prefs.rs` that maps
onto `ClientPrefs` (the `keybinds` field keeps its meaning: override path).
`PrefsError` is a `thiserror` enum `{ Io(std::io::Error), Parse(String) }`.

Startup: add `no_mouse: bool` to `CliArgs`, parsed from `--no-mouse` in
`parse_args` (documented in the `--help` text next to `--frame-delivery`). Add
`prefs: ClientPrefs` and `gobby_home: PathBuf` to `Ready`; `prepare_at` loads the
prefs from the resolved gobby home (the same root `persist::snapshot_path` uses)
and applies `no_mouse` as `prefs.mouse_capture = false`. A prefs parse error is a
`StartupError::Prefs(String)` that names the file and key and exits before the
alternate screen is entered. `run_ready` sets `chrome.prefs = ready.prefs` after
`Chrome::new` and before the loop.

Settings dialog: add `SettingsRow::MouseCapture` with label `mouse capture` and
value `on`/`off` via `row_value`; the row sits directly after `Theme`. Toggling
rows and persisting is 4.2's input router; this task only adds the row and the
pref. Export `prefs` from `lib.rs`.

**Acceptance:**

- 1.1.1 - `ClientPrefs` carries `mouse_capture` defaulting to `true`, serializes to and from the `[ui]`/`[keymap]` TOML shape, and rejects unknown keys by name. file: `crates/gclient/src/prefs.rs`. test: `crates/gclient/tests/persist.rs::prefs_round_trip_and_reject_unknown_keys`.
- 1.1.2 - `--no-mouse` parses into `CliArgs::no_mouse` and `prepare_at` yields `Ready::prefs` with `mouse_capture == false`; a missing prefs file yields defaults; a malformed file is a `StartupError::Prefs` naming the path. symbol: `crates/gclient/src/startup.rs::parse_args`. test: `crates/gclient/tests/startup.rs::no_mouse_flag_and_prefs_file_shape_ready`.
- 1.1.3 - The settings dialog renders a `mouse capture` row reflecting the pref. file: `crates/gclient/src/ui/settings.rs`. test: `crates/gclient/src/ui/settings.rs::row_values_follow_prefs`.

### 1.2 Enable and restore mouse capture in the terminal guard [category: code] (depends: 1.1)
`kind: deliverable`

Targets:
- `crates/gclient/src/teardown.rs::ModeBackend`
- `crates/gclient/src/teardown.rs::CrosstermBackend`
- `crates/gclient/src/teardown.rs::RecordingBackend`
- `crates/gclient/src/teardown.rs::Obligations`
- `crates/gclient/src/teardown.rs::arm`
- `crates/gclient/src/teardown.rs::restore`
- `crates/gclient/src/startup.rs::start_session`
- `crates/gclient/tests/teardown.rs::*` — scope-reason: new test functions appended; existing cases only change where the acceptance item names them

Add `enable_mouse_capture` / `disable_mouse_capture` to the `ModeBackend` trait
with default `Ok(())` bodies, implement them on `CrosstermBackend` with
`crossterm::execute!(stdout, EnableMouseCapture)` / `DisableMouseCapture`
(crossterm 0.29 emits `?1000h ?1002h ?1003h ?1006h ?1015h` and the matching `l`
sequence), and record them on `RecordingBackend`. `Obligations` gains
`mouse_capture: bool`. `arm` takes `mouse_capture: bool` (from
`ClientPrefs::mouse_capture`); when true it enables capture after the alternate
screen and sets the obligation. `restore` disables capture first, before
`show_cursor`, through `restore_obligation`, so a panic, SIGTERM or normal exit
never leaves the host terminal reporting mouse. `start_session` threads the pref
from `Ready` into `arm`. Add `TerminalGuard::set_mouse_capture(&mut self, on: bool)
-> io::Result<()>` for the live settings toggle (4.2): it enables or disables
capture immediately and updates the obligation; a no-op when the state already
matches.

**Acceptance:**

- 1.2.1 - With `mouse_capture == true` the recording backend sees enable-capture after enter-alternate-screen and disable-capture before show-cursor on restore; with `false` neither call is made. symbol: `crates/gclient/src/teardown.rs::arm`. test: `crates/gclient/tests/teardown.rs::mouse_capture_follows_the_pref_and_restores_first`.
- 1.2.2 - `TerminalGuard::set_mouse_capture` toggles the obligation and is idempotent. file: `crates/gclient/src/teardown.rs`. test: `crates/gclient/tests/teardown.rs::mouse_capture_toggle_is_idempotent`.
- 1.2.3 - `start_session` passes `Ready::prefs.mouse_capture` to `arm`. symbol: `crates/gclient/src/startup.rs::start_session`. test: `crates/gclient/tests/startup.rs::start_session_arms_mouse_capture_from_prefs`.

### 1.3 Keep the hit map: `ui/hit.rs` and hits written back into `ViewState` [category: code] (depends: 1.2)
`kind: deliverable`

Targets:
- `crates/gclient/src/ui/hit.rs`
- `crates/gclient/src/ui/chrome.rs::ViewState`
- `crates/gclient/src/ui/chrome.rs::Chrome::compute_view`
- `crates/gclient/src/ui/chrome_render.rs::render_workspace_with`
- `crates/gclient/src/ui/chrome_render.rs::render_workspace`
- `crates/gclient/src/ui/chrome_render.rs::render_navigation_chrome`
- `crates/gclient/src/ui/chrome_render.rs::render_content_column`
- `crates/gclient/src/ui/tab_surface.rs::render_tab_surface`
- `crates/gclient/src/ui/tabs.rs::render_tab_bar`
- `crates/gclient/src/ui/sidebar.rs::render_sidebar`
- `crates/gclient/src/ui/sidebar.rs::render_collapsed_sidebar`
- `crates/gclient/src/ui/sidebar.rs::render_roster`
- `crates/gclient/src/ui/sidebar.rs::render_attention`
- `crates/gclient/src/ui/settings.rs::render_settings`
- `crates/gclient/src/ui/status.rs::render_status_line`
- `crates/gclient/src/ui/mod.rs`
- `crates/gclient/src/app/live_loop.rs::render_live_workspace`
- `crates/gclient/src/app/run_loop.rs::render_workspace`
- `crates/gclient/tests/parity/chrome.rs::*` — scope-reason: new test functions appended; existing cases only change where the acceptance item names them

`render_workspace_with` returns a new `ChromeHits` value assembled from what the
renderers already compute: `render_tab_surface` returns the `TabBarHits` from
`render_tab_bar`, `render_navigation_chrome` returns the `SidebarHits` from either
sidebar renderer plus the toggle rect (`expanded_toggle_rect` /
`collapsed_toggle_rect`), and `render_status_line` returns the `Rect` of the
control indicator span (glyph plus label, from column 0 of the status row to the
end of the label). `render_live_workspace` and `render_workspace` apply the hits
inside the draw closure (`chrome` is `&mut` there): `chrome.view.apply_hits(hits)`.
`ViewState` gains `control_indicator_hit_area: Option<Rect>`,
`sidebar_toggle_hit_area: Option<Rect>`, `sidebar_divider_x: Option<u16>` (the
separator column between sidebar and content) and
`sidebar_section_divider_y: Option<u16>` (the row between roster and attention
sections, from `expanded_sections`). `render_roster` and `render_attention`
return the scrollbar track `Rect` (`scrollbar_track`, `None` when the list
fits) next to the row hits, `SidebarHits` carries them as `roster_scrollbar` and
`attention_scrollbar`, and `ViewState` stores them as
`roster_scrollbar_hit_area` and `attention_scrollbar_hit_area`. `render_settings`
in `crates/gclient/src/ui/settings.rs` returns `SettingsHits { dialog: Rect, rows: Vec<Rect> }`
(one rect per drawn `SettingsRow`) and the `Mode::Settings` arm of
`render_workspace_with` folds it into `ChromeHits`; `ViewState` keeps them as
`settings_dialog_area: Option<Rect>` and `settings_row_hit_areas: Vec<Rect>`,
cleared by every render outside settings mode. `compute_view` fills `split_borders`,
`pane_infos`, `sidebar_divider_x` and `sidebar_section_divider_y` itself; the
render pass fills the rest. Hits survive until the next render, so a click that
arrives between frames tests against the last drawn frame, which is what the
user saw. `render_live_workspace` in `crates/gclient/src/app/live_loop.rs` only
applies the hits; the hit test itself is split into the new
`crates/gclient/src/ui/hit.rs`, so the loop file gains one line.

`crates/gclient/src/ui/hit.rs` is the pure hit test over `ViewState`:

```rust
pub enum Hit {
    Tab(usize), TabScrollLeft, TabScrollRight, NewTab, TabBarEmpty,
    Roster(String), Attention(String), SidebarToggle, SidebarDivider, SidebarSectionDivider, SidebarEmpty,
    SidebarScrollbar { section: SidebarSection, row: u16 }, // roster or attention list scrollbar lane
    Pane { slot: layout::PaneId, col: u16, row: u16 },     // inner cell coordinates
    PaneBorder(layout::PaneId), PaneScrollbar { slot: layout::PaneId, row: u16 },
    SplitBorder(usize),                                    // index into view.split_borders
    SettingsRow(usize), SettingsDialog,                   // only while view.settings_dialog_area is set
    ControlIndicator, Status, Toast, Empty,
}
pub enum SidebarSection { Roster, Attention }
pub fn hit_test(view: &ViewState, column: u16, row: u16) -> Hit;
```

Order of tests mirrors herdr `handle_mouse` (settings dialog while
`settings_dialog_area` is set, toast, tab bar, sidebar with its scrollbar lanes,
split borders, panes by `inner_rect`, then `rect` for borders, then scrollbar lanes,
status row). `SplitBorder` matches a divider when the pointer is on `pos` (the
divider column or row) within `area`, same as herdr `find_border_at`.

**Acceptance:**

- 1.3.1 - After a render, `chrome.view` carries tab, roster, attention, new-tab, scroll-arrow, sidebar toggle, sidebar scrollbar, settings-row and control-indicator hit rects that match the drawn cells. symbol: `crates/gclient/src/ui/chrome_render.rs::render_workspace_with`. test: `crates/gclient/tests/parity/chrome.rs::rendered_hits_match_drawn_cells`.
- 1.3.2 - `hit_test` classifies every region of the `split_live` fixture layout (tabs, sidebar rows, both panes, the split border, scrollbar lane, status row, control indicator) and returns `Empty` elsewhere. file: `crates/gclient/src/ui/hit.rs`. test: `crates/gclient/src/ui/hit.rs::hit_test_covers_split_live_layout`.

### 1.4 Split `live_loop.rs` into control, actions and loop modules [category: refactor] (depends: 1.3)
`kind: deliverable`

Targets:
- `crates/gclient/src/app/live_loop/control.rs`
- `crates/gclient/src/app/live_loop/actions.rs`
- `crates/gclient/src/app/live_loop.rs::route_live_input`
- `crates/gclient/src/app/live_loop.rs::handle_live_action`
- `crates/gclient/src/app/live_loop.rs::focus_relative_live_pane`
- `crates/gclient/src/app/live_loop.rs::focus_live_pane`
- `crates/gclient/src/app/live_loop.rs::take_live_control`
- `crates/gclient/src/app/live_loop.rs::retire_live_control`
- `crates/gclient/src/app/live_loop.rs::release_live_control`
- `crates/gclient/src/app/live_loop.rs::send_live_input`
- `crates/gclient/src/app/live_loop.rs::send_live_write`
- `crates/gclient/src/app/live_loop.rs::apply_live_write_outcome`
- `crates/gclient/src/app/live_loop.rs::spawn_live_terminal`
- `crates/gclient/src/app/live_loop.rs::terminate_live_terminal`
- `crates/gclient/src/app/live_loop.rs::sync_live_chrome`

Pure move, no behavior change. `crates/gclient/src/app/live_loop/control.rs` receives `focus_live_pane`,
`take_live_control`, `retire_live_control`, `release_live_control`,
`send_live_input`, `send_live_write`, `apply_live_write_outcome` as
`pub(super)` free functions with the same signatures. `crates/gclient/src/app/live_loop/actions.rs` receives
`handle_live_action`, `focus_relative_live_pane`, `spawn_live_terminal`,
`terminate_live_terminal`, `sync_live_chrome`. `live_loop.rs` keeps
`run_live_loop`, signals, reconnect plumbing, `route_live_input`,
`recv_workspace_frame`, `render_live_workspace`, `resize_live_workspace` and ends
under 650 lines and declares the two child modules (`mod control; mod
actions;`), so the parent module file, already near the ceiling, is not touched. Existing tests in
`crates/gclient/tests/client_loop.rs` are the behavior pin.

**Acceptance:**

- 1.4.1 - `live_loop.rs` is under 650 lines and the two new modules exist with the moved functions; `cargo test -p gobby-client --test client_loop` passes unchanged. file: `crates/gclient/src/app/live_loop/control.rs`. test: `crates/gclient/tests/source_size.rs::no_src_file_at_or_above_1000_lines`.

### 1.5 Carry pane mouse modes on native frames and expose a tracking level [category: code]
`kind: deliverable`

Targets:
- `crates/gterminal/src/protocol/wire_types.rs::PaneModes`
- `crates/gterminal/src/pane/runtime_ops.rs::PaneRuntime::frame_data`
- `crates/gterminal/tests/wire_golden.rs::*` — scope-reason: new golden cases appended, no existing test changes
- `crates/gclient/tests/workspace.rs::*` — scope-reason: new test functions appended; existing cases only change where the acceptance item names them

The client must know whether the app inside a pane reports mouse (forward) or
not (scrollback, selection). The wire already carries this for tmux slots:
`FrameData::modes` is a `PaneModes` (`crates/gterminal/src/protocol/wire_types.rs`)
with `mouse_standard`, `mouse_button`, `mouse_any`, `mouse_all`, `mouse_sgr`,
`mouse_utf8` and `alternate_on`, filled by `parse_poll_batch` in
`crates/gterminal/src/host/poll.rs` from the tmux pane formats and published on
every tmux frame. Two gaps remain:

- Native slots publish `PaneModes::default()`: `PaneRuntime::frame_data` builds
  the frame with `FrameData::from_ratatui_buffer_with_hyperlinks` and never
  consults the terminal. Fill `modes` there from the ghostty core the way
  `wheel_routing` (`crates/gterminal/src/pane/terminal_io.rs`) already reads it:
  `mouse_standard` from `MODE_MOUSE_PRESS_RELEASE`, `mouse_button` from
  `MODE_MOUSE_BUTTON_MOTION`, `mouse_all` from `MODE_MOUSE_ANY_MOTION`,
  `mouse_any` from the X10 mode, `mouse_sgr` from `MODE_MOUSE_SGR`,
  `alternate_on` from `active_screen() == Alternate`; every other field keeps
  its default.
- Consumers need one answer, not six flags. Add to `PaneModes`:

```rust
pub enum MouseTracking { Off, X10, Normal, ButtonMotion, AnyMotion }
impl PaneModes {
    /// Highest tracking level the app enabled; tmux reports all lower flags too.
    pub fn mouse_tracking(&self) -> MouseTracking;
}
```

with `mouse_all => AnyMotion`, else `mouse_button => ButtonMotion`, else
`mouse_standard => Normal`, else `mouse_any => X10`, else `Off`. gclient reads
`pane.latest_frame().map(|frame| &frame.modes)`; `Pane` needs no new field.

**Acceptance:**

- 1.5.1 - `PaneModes::mouse_tracking` maps the flag combinations onto the five levels and the golden corpus pins a frame with `mouse_all` + `mouse_sgr` + `alternate_on` that decodes to `AnyMotion`. symbol: `crates/gterminal/src/protocol/wire_types.rs::PaneModes`. test: `crates/gterminal/tests/wire_golden.rs::frame_modes_expose_mouse_tracking`.
- 1.5.2 - A native frame carries the terminal's live mouse and alternate-screen flags: after the pane receives `\x1b[?1003h\x1b[?1006h` the next `frame_data` reports `AnyMotion` with `mouse_sgr`, and after `\x1b[?1003l` it reports `Off`. symbol: `crates/gterminal/src/pane/runtime_ops.rs::PaneRuntime::frame_data`. test: `crates/gterminal/src/pane/runtime_ops.rs::frame_data_reports_mouse_modes`.
- 1.5.3 - A gclient pane exposes the modes of its last frame through `latest_frame`, and a frame without the field decodes to `Off`. file: `crates/gclient/tests/workspace.rs`. test: `crates/gclient/tests/workspace.rs::frame_modes_follow_the_latest_frame`.

## P2: Pointer actions
`kind: framing`

**Goal**: every left-click, drag and wheel target herdr supports works in gclient,
plus the three gobby-native targets, through one dispatcher.

### 2.1 Add the mouse dispatcher and pane click-to-focus [category: code] (depends: 1.4, 1.5)
`kind: deliverable`

Targets:
- `crates/gclient/src/app/live_loop/mouse/mod.rs`
- `crates/gclient/src/app/live_loop/mouse/pointer.rs`
- `crates/gclient/src/app/live_loop.rs::route_live_input`
- `crates/gclient/src/app/run_loop.rs::route_scripted_input`
- `crates/gclient/src/app/live_loop/control.rs`
- `crates/gclient/src/ui/chrome.rs::Chrome`
- `crates/gclient/src/ui/chrome.rs::Chrome::new`
- `crates/gclient/tests/client_loop.rs::*` — scope-reason: new test functions appended; existing cases only change where the acceptance item names them

`crates/gclient/src/app/live_loop/mouse/mod.rs` owns `MouseGesture` (state carried on `Chrome.gesture:
Option<MouseGesture>`), `MouseOutcome`, and `route_mouse`:

```rust
pub enum MouseGesture {
    TabDrag { index: usize, origin_col: u16, moved: bool },
    RosterDrag { terminal_id: String, origin_row: u16, moved: bool },
    SplitDrag { border: usize },
    SidebarDrag,
    SectionDrag,
    SidebarScrollbarDrag { section: SidebarSection, grab_offset: u16 }, // 2.3
    ScrollbarDrag { slot: layout::PaneId, grab_offset: u16 },
    Select { slot: layout::PaneId },            // 3.1
    Forwarding { slot: layout::PaneId },        // 3.3 button held inside a reporting pane
}
pub enum MouseOutcome {
    Handled,
    Focus { pane: PaneId, observe_only: bool },
    Action(Action),                              // dispatched through handle_live_action
    Spawn { placement: Placement },              // new terminal: Tab | SplitRight | SplitDown
    Write { pane: PaneId, bytes: Vec<u8> },      // 3.3 forwarded SGR report
    Ignore,
}
pub fn route_mouse<W: WorkspaceView>(ws: &W, chrome: &mut Chrome, mouse: &MouseEvent) -> MouseOutcome;
```

`route_mouse` runs before key handling in both `route_live_input` and
`route_scripted_input`, ahead of `route_mouse_selection`. In
`crates/gclient/src/app/live_loop.rs` that is a single call: the dispatcher is
split into `crates/gclient/src/app/live_loop/mouse/mod.rs` and the loop file does not grow.
Modal modes: `Mode::Respond`,
`Mode::ConfirmClose`, `Mode::Rename`, `Mode::Settings`, `Mode::KeybindHelp`,
`Mode::Navigator` and `Mode::ContextMenu` (5.1) consume clicks on their own
surfaces (a click outside a modal closes it, matching herdr); otherwise the event
is classified with `hit::hit_test` and handed to `pointer.rs` (Down/Up/Drag),
`wheel.rs` (2.4) or `select.rs` (3.1). The live loop applies `MouseOutcome`:
`Focus` calls `chrome.focus_pane` then `focus_live_pane` (lease follows focus, F1)
or, with `observe_only`, only `workspace.focus` and no take; `Action` goes to
`handle_live_action`; `Spawn` to `spawn_live_terminal` with the placement (2.2
adds the parameter); `Write` to `send_live_write`.

Pane click (`Hit::Pane`): left Down focuses the pane's slot when it is not
already focused (herdr `FocusPane`). Default click = `Focus { observe_only: false }`;
alt+click = `observe_only: true` (the gobby lease escape: look without taking).
A click on the already-focused pane is `Ignore` here so 3.1 can start a
selection. Right Down opens the pane menu (5.1). Middle click pastes the last
finalized selection (`chrome.last_copy`, 3.1) through `paste_to_pty` when the pane
is writable.

**Acceptance:**

- 2.1.1 - A left click inside a non-focused pane focuses it and takes control through `focus_live_pane`; alt+click focuses without a `terminal_take_control` request. symbol: `crates/gclient/src/app/live_loop/mouse/pointer.rs`. test: `crates/gclient/tests/client_loop.rs::pane_click_focuses_and_takes_control_unless_alt`.
- 2.1.2 - Mouse events never reach a pane as key bytes; with capture off (`mouse_capture == false`) no `RawInputEvent::Mouse` is produced because capture was never armed (1.2), and if one arrives anyway it is `Ignore`. file: `crates/gclient/src/app/live_loop/mouse/mod.rs`. test: `crates/gclient/src/app/live_loop/mouse/mod.rs::route_mouse_ignores_when_capture_is_off`.

### 2.2 Tab bar: click, new-tab button, scroll arrows, drag reorder, wheel switch [category: code] (depends: 2.1)
`kind: deliverable`

Targets:
- `crates/gclient/src/app/live_loop/mouse/pointer.rs`
- `crates/gclient/src/ui/chrome.rs::Chrome::open_tab`
- `crates/gclient/src/ui/chrome.rs::Chrome::open_pane`
- `crates/gclient/src/app/live_loop/actions.rs`
- `crates/gclient/src/ui/tabs.rs::render_tab_bar`
- `crates/gclient/tests/parity/tabs.rs::*` — scope-reason: new test functions appended; existing cases only change where the acceptance item names them

`Hit::Tab(i)` Down: `chrome.active_tab = i`, focus the tab's focused slot pane
through `Focus`. Drag past `TAB_DRAG_THRESHOLD = 2` columns marks `moved`; Up over
another tab index swaps `chrome.tabs` order (`Vec::swap` walk, herdr `MoveTab`)
and keeps `active_tab` on the moved tab; Up without movement is the plain click.
`Hit::NewTab` = `Spawn { placement: Placement::Tab }`; `spawn_live_terminal`
takes `placement: Placement` and, after `fetch_roster` + `attach_ready_panes`,
places the created terminal with `open_tab` (Tab), `open_pane` (SplitRight, the
current horizontal split) or a new `Chrome::open_pane_below` (SplitDown, vertical
split) instead of relying on `sync_live_chrome` to open it. `TabScrollLeft/Right`
move `chrome.tab_scroll` by one and set `tab_scroll_follow_active = false`; wheel
over the tab bar switches tabs (up = previous, down = next, herdr
`handle_tab_bar_wheel`). `render_tab_bar` draws the dragged tab with the
`p.surface0` background and `Modifier::REVERSED` while `moved`.

The tab bar is where herdr's workspace surface lands (Constraints table): a
herdr workspace owns tabs the way a gclient tab owns panes, so `Hit::Tab` is
`FocusWorkspace`, the tab drag is `MoveWorkspace`, and `Hit::NewTab` is the
sidebar `NewWorkspace` button. `MoveWorkspaceBlock` has no counterpart because
tabs have no parent/child grouping. When `hide_tab_bar_when_single_tab` hides
the bar, the new-tab button hides with it; the global menu item `new tab` (5.1)
and the `NewTab` chord stay available, and the bar returns with the second tab.

**Acceptance:**

- 2.2.1 - Clicking a tab activates it and focuses its pane; the new-tab button spawns a terminal into a new tab; scroll arrows move `tab_scroll`. symbol: `crates/gclient/src/app/live_loop/mouse/pointer.rs`. test: `crates/gclient/tests/parity/tabs.rs::tab_bar_clicks_activate_spawn_and_scroll`.
- 2.2.2 - Dragging a tab past the threshold and releasing over another reorders `chrome.tabs` and keeps the moved tab active; a short drag is a click. symbol: `crates/gclient/src/app/live_loop/mouse/pointer.rs`. test: `crates/gclient/tests/parity/tabs.rs::tab_drag_reorders_or_clicks`.

### 2.3 Sidebar: roster attach, attention jump, toggle, divider and section drags, roster reorder, list wheel [category: code] (depends: 2.2)
`kind: deliverable`

Targets:
- `crates/gclient/src/app/live_loop/mouse/pointer.rs`
- `crates/gclient/src/app/live_loop/mouse/wheel.rs`
- `crates/gclient/src/ui/chrome.rs::attention_label`
- `crates/gclient/src/ui/chrome.rs::attention_pane`
- `crates/gclient/src/ui/sidebar_rows.rs::attention_rows`
- `crates/gclient/src/ui/navigator.rs::navigator_rows`
- `crates/gclient/src/ui/chrome.rs::SidebarState`
- `crates/gclient/src/app/persistence.rs::set_tab_order`
- `crates/gclient/src/app/live.rs::install_live_rows`
- `crates/gclient/tests/parity/sidebar.rs::*` — scope-reason: new test functions appended; existing cases only change where the acceptance item names them
- `crates/gclient/tests/attention_flow.rs::*` — scope-reason: new test functions appended; existing cases only change where the acceptance item names them

Gobby-native affordances:

- `Hit::Roster(terminal_id)` Down: `ws.pane_for_terminal` → `Focus` (this is
  "click a roster row to attach it": every roster pane is already attached by
  `attach_ready_panes`; the click focuses it in the active tab, opening a slot
  via `open_pane` if the pane is not shown, and takes the lease). The drag
  logic is split into `crates/gclient/src/app/live_loop/mouse/pointer.rs`; the only change
  in `crates/gclient/src/app/live.rs` is the sort in `install_live_rows`. Drag past
  `ROSTER_DRAG_THRESHOLD = 1` row and release over another row reorders the
  roster: `Workspace::set_tab_order` persists the new order in the workspace
  snapshot, and `install_live_rows` honors a persisted order (sort `ids` by the
  saved `tab_order`, unknown ids appended in daemon order) so the reorder survives
  the next roster page.
- `Hit::Attention(entry_id)` Down: `attention_pane(ws, entry_id)` → `Focus` on
  the pane that raised it; if the entry is a prompt (`parse_prompt` in
  `app/attention.rs` yields options), the Respond dialog opens for that entry
  after focusing (`open_response_dialog` keyed by entry id). Attention rows stop
  showing UUID prefixes: `attention_label` returns the mapped pane's
  `display_name` plus address (`15 %15`) when `attention_pane` resolves, and the
  entry kind, and only falls back to the 8-char id when no pane maps;
  `navigator_rows` shows the same label for attention rows.
- `Hit::SidebarToggle` toggles `sidebar.collapsed`. `SidebarDivider` drag resizes
  `sidebar.width` within `min_width..=max_width` (herdr sidebar divider drag);
  `SidebarSectionDivider` drag sets `sidebar.section_split` (herdr section
  divider). Both persist through 1.1's prefs (`sidebar_width`) on Up.
- Wheel over the roster or attention section scrolls `sidebar.scroll` /
  `sidebar.attention_scroll` by `MOUSE_SCROLL_LINES = 3` rows, clamped by
  `list_metrics`.
- The collapsed rail: `render_collapsed_sidebar` already returns roster hits for
  the index glyphs, so `Hit::Roster` behaves the same in both sidebar states
  (herdr `collapsed_workspace_at_row` focuses a workspace from the rail; gclient
  focuses the terminal).
- `Hit::SidebarScrollbar { section, row }` (herdr
  `workspace_list_scrollbar_target_at`): Down on the thumb starts
  `SidebarScrollbarDrag { section, grab_offset }` and each Drag moves
  `sidebar.scroll` or `sidebar.attention_scroll` so the thumb follows the
  pointer; Down on the track jumps the list to the offset that row represents.
  Both keep the offset-from-bottom form `list_metrics` clamps; Up ends the
  gesture.

**Acceptance:**

- 2.3.1 - Clicking a roster row focuses and takes control of that terminal, opening a slot when it is not shown. symbol: `crates/gclient/src/app/live_loop/mouse/pointer.rs`. test: `crates/gclient/tests/parity/sidebar.rs::roster_click_focuses_the_terminal`.
- 2.3.2 - Clicking an attention row focuses the pane that raised it and opens the Respond dialog for prompt entries; attention rows label the terminal, never a raw UUID, when a pane maps. symbol: `crates/gclient/src/ui/chrome.rs::attention_label`. test: `crates/gclient/tests/attention_flow.rs::attention_click_jumps_and_labels_the_terminal`.
- 2.3.3 - Roster drag reorders and persists; the toggle collapses; divider drags resize width and section split within bounds; wheel scrolls the lists; scrollbar thumb drags and track clicks scroll the list they belong to. symbol: `crates/gclient/src/app/persistence.rs::set_tab_order`. test: `crates/gclient/tests/parity/sidebar.rs::sidebar_drags_reorder_resize_and_scroll`.

### 2.4 Split-border drag, pane scrollbar, wheel scrollback [category: code] (depends: 2.3)
`kind: deliverable`

Targets:
- `crates/gclient/src/app/live_loop/mouse/pointer.rs`
- `crates/gclient/src/app/live_loop/mouse/wheel.rs`
- `crates/gclient/src/ui/scrollbar.rs::scrollbar_offset_from_row`
- `crates/gclient/src/ui/scrollbar.rs::scrollbar_offset_from_drag_row`
- `crates/gclient/src/ui/scrollbar.rs::scrollbar_thumb_grab_offset`
- `crates/gclient/src/app/mod.rs::Workspace::set_scroll_offset`
- `crates/gclient/src/app/live_loop/control.rs`
- `crates/gclient/tests/parity/panes.rs::*` — scope-reason: new test functions appended; existing cases only change where the acceptance item names them
- `crates/gclient/tests/client_loop.rs::*` — scope-reason: new test functions appended; existing cases only change where the acceptance item names them

`Hit::SplitBorder(i)` Down starts `SplitDrag`; each Drag computes the ratio from
the pointer position inside `split_borders[i].area` along the split direction,
clamped to `0.1..=0.9`, and applies `tab.layout.set_ratio_at(&border.path,
ratio)` (herdr `SetSplitRatio`); Up ends the gesture. `Hit::PaneScrollbar` Down on
the thumb starts `ScrollbarDrag` with `scrollbar_thumb_grab_offset`, elsewhere
jumps with `scrollbar_offset_from_row`; Drag uses `scrollbar_offset_from_drag_row`.
Wheel inside a pane whose `modes.mouse == Off` and `!alternate_screen` scrolls
scrollback by `MOUSE_SCROLL_LINES = 3` per tick: new offset = clamp(offset ±3,
0..=max_scroll), applied through a live `set_live_scroll_offset` in
`crates/gclient/src/app/live_loop/control.rs` that sends `ClientMessage::SetScrollOffset` on the pane's frame
source (both transports; `Workspace::set_scroll_offset` today only serves the
scripted native path, and keeps that body in `crates/gclient/src/app/mod.rs`;
the live variant is split into `crates/gclient/src/app/live_loop/control.rs`) and
mirrors `pane.scroll_offset`; wheel in an
`alternate_screen` pane with `mouse == Off` sends arrow keys (3 × `\x1b[A` /
`\x1b[B`, herdr alternate-scroll); wheel in a reporting pane is forwarded (3.3).
Horizontal wheel is forwarded only to reporting panes, otherwise ignored.

**Acceptance:**

- 2.4.1 - Dragging a split border changes the split ratio within bounds and re-lays out both panes. symbol: `crates/gclient/src/app/live_loop/mouse/pointer.rs`. test: `crates/gclient/tests/parity/panes.rs::split_border_drag_sets_ratio`.
- 2.4.2 - Wheel over a non-reporting pane sends `SetScrollOffset` steps of three and clamps; scrollbar click and drag map to offsets; wheel in an alternate-screen pane sends arrow keys. symbol: `crates/gclient/src/app/live_loop/mouse/wheel.rs`. test: `crates/gclient/tests/client_loop.rs::wheel_and_scrollbar_drive_scrollback`.

### 2.5 Control indicator click: take, release, take-back [category: code] (depends: 2.4)
`kind: deliverable`

Targets:
- `crates/gclient/src/app/live_loop/mouse/pointer.rs`
- `crates/gclient/src/ui/status.rs::control_indicator`
- `crates/gclient/src/ui/status.rs::render_status_line`
- `crates/gclient/tests/client_loop.rs::*` — scope-reason: new test functions appended; existing cases only change where the acceptance item names them
- `crates/gclient/tests/parity/status.rs::*` — scope-reason: new test functions appended; existing cases only change where the acceptance item names them

`Hit::ControlIndicator` Down on the focused pane maps by `ControlState`:
`Held` → `Action::ReleaseControl`; `Observe`, `LeaseLost`, `UncertainReadOnly` →
`Action::TakeControl`; `take_back` set → `Action::TakeBack`. This is the mouse
escape from Held that the keyboard lacks under a captured prefix (#21923). The
indicator renders as a button: the label gains brackets (`[● held]`) and hovering
(`MouseEventKind::Moved` over the hit rect, tracked as `chrome.hover:
Option<Hit>`) underlines it; colors stay the existing state tokens
(`p.accent` held, `p.subtext0` observe, `p.red` lease lost, `p.yellow` take-back)
because the glyph and label already carry the state without hue.

**Acceptance:**

- 2.5.1 - Clicking the indicator on a Held pane releases control; on an Observe pane it takes control; with take-back pending it accepts the take-back. symbol: `crates/gclient/src/app/live_loop/mouse/pointer.rs`. test: `crates/gclient/tests/client_loop.rs::control_indicator_click_toggles_control`.
- 2.5.2 - The status line draws the indicator as a bracketed button with a hover underline and no hue-only state. symbol: `crates/gclient/src/ui/status.rs::render_status_line`. test: `crates/gclient/tests/parity/status.rs::control_indicator_is_a_button`.

## P3: Selection, links, forwarding
`kind: framing`

**Goal**: the pointer inside a pane behaves like herdr: drag and double-click
copy without entering copy mode, ctrl-click opens links, and apps that asked for
mouse reports get them, with the same right-click escape hatches.

### 3.1 Drag-select and double-click copy in terminal mode [category: code] (depends: 2.5)
`kind: deliverable`

Targets:
- `crates/gclient/src/app/live_loop/mouse/select.rs`
- `crates/gclient/src/app/live_loop/mouse/mod.rs`
- `crates/gclient/src/copy_mode.rs::route_mouse_selection`
- `crates/gclient/src/copy_mode.rs::copy_finalized_selection`
- `crates/gclient/src/ui/chrome.rs::Chrome`
- `crates/gclient/tests/copy_paste.rs::*` — scope-reason: new test functions appended; existing cases only change where the acceptance item names them

Today a selection exists only in `Mode::Copy` (`route_mouse_selection`). herdr
selects in the normal mode: press, drag, release copies (`copy_on_select`), a
click without movement clears, double-click takes the word, triple-click the
line. `crates/gclient/src/app/live_loop/mouse/select.rs` owns that for gclient:

- Left Down on the focused pane (2.1 returns `Ignore` for that case so the event
  reaches here) starts `MouseGesture::Select { slot }` and anchors
  `chrome.selection` (`gobby_terminal::selection::Selection::anchor`) at the inner
  cell, when the pane's `mouse_tracking()` is `Off` or SHIFT is held (shift
  bypasses forwarding, herdr `shift_bypasses_mouse_reporting`). Drag extends with
  `Selection::drag`; Up calls `Selection::finish`: `is_just_click` clears the
  selection, otherwise `copy_finalized_selection` writes it as OSC 52 (unchanged
  function, now reachable from terminal mode) and the text is kept in
  `chrome.last_copy: Option<String>` for middle-click paste (2.1).
- Double-click (`DOUBLE_CLICK_MS = 400`, same cell, tracked on
  `chrome.last_click: Option<(Instant, u16, u16)>`) selects the token under the
  pointer from the frame row (`pane.latest_frame()` cells; token characters are
  alphanumeric plus `_-./~:@#%+=?&`), triple-click selects the whole row; both
  copy immediately.
- `route_mouse_selection` keeps serving `Mode::Copy` but delegates anchor, drag
  and finish to `select.rs` so there is one selection implementation;
  `highlight_selection` in `crates/gclient/src/ui/panes.rs` already draws
  `chrome.selection` in every mode.

**Acceptance:**

- 3.1.1 - In terminal mode a left drag inside the focused non-reporting pane highlights the range and, on release, copies it through OSC 52 and stores it as `last_copy`; a click without movement clears the selection. symbol: `crates/gclient/src/app/live_loop/mouse/select.rs`. test: `crates/gclient/tests/copy_paste.rs::terminal_mode_drag_selects_and_copies_on_release`.
- 3.1.2 - Double-click selects the token under the pointer and triple-click the row; shift-drag selects inside a mouse-reporting pane instead of forwarding. file: `crates/gclient/src/app/live_loop/mouse/select.rs`. test: `crates/gclient/tests/copy_paste.rs::double_and_triple_click_select_token_and_row`.

### 3.2 Ctrl-click opens OSC 8 and bare URLs [category: code] (depends: 3.1)
`kind: deliverable`

Targets:
- `crates/gclient/src/app/live_loop/mouse/links.rs`
- `crates/gclient/src/app/live_loop/mouse/mod.rs`
- `crates/gclient/src/app/live_loop/actions.rs`
- `crates/gclient/tests/terminal_links.rs::*` — scope-reason: new test functions appended; existing cases only change where the acceptance item names them
- `crates/gclient/tests/client_loop.rs::*` — scope-reason: new test functions appended; existing cases only change where the acceptance item names them

The wire already carries OSC 8 targets: `CellData::hyperlink` indexes
`FrameData::hyperlinks` (`crates/gterminal/src/protocol/wire_types.rs`), filled
by both the tmux capture and the native `frame_data`.
`crates/gclient/src/app/live_loop/mouse/links.rs` resolves a link for a ctrl+left Down over
`Hit::Pane`: the cell's OSC 8 URI when present, otherwise the maximal run of
URL characters around the column on that frame row that starts with `http://`
or `https://`, with trailing `.,;:)]}>'"` trimmed (herdr's bare-URL rule). A
resolved link is `MouseOutcome::OpenLink(String)`, added to the outcome enum;
link resolution runs before forwarding (3.3) so ctrl-click works inside
reporting panes too, and a ctrl-click with no link is the plain click. The live
loop (`crates/gclient/src/app/live_loop/actions.rs`) opens the link with the platform opener (`open` on
macOS, `xdg-open` elsewhere; `std::process::Command` spawned detached with null
stdio) through `open_link(opener: &str, url: &str) -> io::Result<()>`, and a
spawn failure becomes a `ToastKind::Warning` toast naming the opener.

**Acceptance:**

- 3.2.1 - ctrl+click on a cell with an OSC 8 target yields `OpenLink` with that URI; on a bare http(s) URL yields the trimmed URL; elsewhere it is a plain click. symbol: `crates/gclient/src/app/live_loop/mouse/links.rs`. test: `crates/gclient/tests/terminal_links.rs::ctrl_click_resolves_osc8_and_bare_urls`.
- 3.2.2 - The live loop launches the opener for `OpenLink` and a launch failure surfaces as a warning toast that names the opener. file: `crates/gclient/src/app/live_loop/actions.rs`. test: `crates/gclient/tests/client_loop.rs::open_link_failure_surfaces_a_toast`.

### 3.3 Forward mouse to reporting panes with right-click passthrough [category: code] (depends: 3.2)
`kind: deliverable`

Targets:
- `crates/gclient/src/app/live_loop/mouse/forward.rs`
- `crates/gclient/src/app/live_loop/mouse/mod.rs`
- `crates/gclient/src/app/live_loop/mouse/wheel.rs`
- `crates/gclient/src/app/pane.rs::Pane`
- `crates/gclient/src/prefs.rs`
- `crates/gclient/src/ui/settings.rs::ClientPrefs`
- `crates/gclient/src/ui/settings.rs::SettingsRow`
- `crates/gclient/src/ui/settings.rs::row_label`
- `crates/gclient/src/ui/settings.rs::row_value`
- `crates/gclient/src/app/live_loop/control.rs`
- `crates/gclient/tests/client_loop.rs::*` — scope-reason: new test functions appended; existing cases only change where the acceptance item names them

`crates/gclient/src/app/live_loop/mouse/forward.rs` turns a crossterm `MouseEvent` into
the bytes the app expects, relative to the pane's `inner_rect`:

```rust
pub fn encode_report(modes: &PaneModes, kind: MouseEventKind, modifiers: KeyModifiers, col: u16, row: u16) -> Option<Vec<u8>>;
```

SGR when `mouse_sgr`: `\x1b[<Cb;Cx;CyM` (press, motion, wheel) or `m`
(release), with `Cx = col + 1`, `Cy = row + 1`, `Cb` = button (0 left, 1 middle,
2 right, 3 release-none) + 4 shift + 8 alt + 16 ctrl + 32 motion + 64 for wheel
up (64), down (65), left (66), right (67). Without `mouse_sgr` the X10 form
`\x1b[M` + three bytes (`Cb + 32`, `Cx + 32`, `Cy + 32`, each clamped to 255,
`None` beyond column 223). Which events are encoded follows
`modes.mouse_tracking()`: `X10` presses only; `Normal` press, release and wheel;
`ButtonMotion` adds `Drag`; `AnyMotion` adds `Moved`; `Off` never encodes.

Dispatch (`mod.rs`): inside a pane whose tracking is not `Off`, and without
SHIFT, Down/Up/Drag/Moved/wheel become `MouseOutcome::Write { pane, bytes }`
when the pane is writable, and the gesture `Forwarding { slot }` keeps Drag and
Up on that pane while the button is held even when the pointer leaves it
(herdr's captured button). A left Down in a non-focused reporting pane focuses
first (2.1) and the same press is forwarded once `focus_live_pane` has left the
pane writable (herdr `captured_left_press_focuses_target_before_forwarding`).
An Observe pane forwards nothing: the mouse never takes control implicitly
(F2's type-to-take stays keyboard only), the click is the explicit focus and
the next click forwards. `crates/gclient/src/app/live_loop/control.rs` applies `Write` with
`send_live_write(workspace, pane, &bytes, false)`, so the bytes travel as
`terminal_input` like any key. `wheel.rs` (2.4) routes wheel ticks here for
reporting panes and horizontal wheel is forwarded only here.

Right-click passthrough, both herdr forms:

- `ClientPrefs::right_click_passthrough_modifier: PassthroughModifier`
  (`None | Shift | Alt | Ctrl`, default `None`, prefs key
  `[ui] right_click_passthrough_modifier = "none"`, settings row `right-click
  passthrough` cycling the four values): when a modifier is configured, right
  Down/Drag/Up with that modifier held is forwarded instead of opening the menu
  (herdr `handle_right_click_passthrough`).
- `Pane::right_click_passthrough: bool` (in-memory, default `false`, toggled from
  the pane menu in 5.1): when set, every right-click in that pane is forwarded.

Otherwise right Down opens the pane menu (5.1).

**Acceptance:**

- 3.3.1 - `encode_report` produces the SGR and X10 bytes for press, release, drag, motion and wheel with modifier bits relative to the inner rect, and encodes nothing the tracking level did not ask for. symbol: `crates/gclient/src/app/live_loop/mouse/forward.rs`. test: `crates/gclient/src/app/live_loop/mouse/forward.rs::report_encoding_follows_the_tracking_level`.
- 3.3.2 - A click in a writable pane whose frame reports `mouse_all` reaches the daemon as `terminal_input` SGR bytes; an Observe pane forwards nothing; a right-click is forwarded when the configured modifier is held or the pane's passthrough flag is set and otherwise opens the pane menu; shift-drag selects instead. symbol: `crates/gclient/src/app/live_loop/control.rs`. test: `crates/gclient/tests/client_loop.rs::mouse_forwarding_follows_pane_modes_and_passthrough`.

## P4: Keyboard and render parity gaps
`kind: framing`

**Goal**: the actions the keymap advertises work, every modal surface has an
input router, user keymap overrides load, and every attached pane shows its
frame. The context menus in P5 dispatch through the actions wired here.

### 4.1 Wire the inert keymap actions [category: code] (depends: 3.3)
`kind: deliverable`

Targets:
- `crates/gclient/src/app/live_loop/actions.rs`
- `crates/gclient/src/ui/chrome.rs::Chrome`
- `crates/gclient/src/ui/chrome.rs::Chrome::focus_pane`
- `crates/gclient/src/ui/keymap.rs::Action`
- `crates/gclient/src/ui/keymap.rs::Keymap::defaults`
- `crates/gclient/src/key_input.rs::*` — scope-reason: exhaustive Action matches drop the EditScrollback arm
- `crates/gclient/src/ui/keybind_help.rs::*` — scope-reason: help entries drop edit_scrollback wherever they are enumerated
- `crates/gclient/tests/keymap.rs::*` — scope-reason: new test functions appended; existing cases only change where the acceptance item names them
- `crates/gclient/tests/client_loop.rs::*` — scope-reason: new test functions appended; existing cases only change where the acceptance item names them

`handle_live_action` dispatches thirteen actions and swallows the rest with a
wildcard (F6). Replace the wildcard with an arm per action so a new `Action`
variant fails to compile until it is handled:

| Action | Behavior |
| --- | --- |
| `SplitVertical` (`split side by side`) | `spawn_live_terminal(Placement::SplitRight)` (2.2) |
| `SplitHorizontal` (`split stacked`) | `spawn_live_terminal(Placement::SplitDown)` |
| `NewTab` | `spawn_live_terminal(Placement::Tab)` |
| `Zoom` | toggle `tab.zoomed` on the active tab (`pane_layout` already honors it) |
| `CloseTab` | with `prefs.confirm_close`, `Dialog::ConfirmClose { target: CloseTarget::Tab, .. }` in `Mode::ConfirmClose`; on confirm (4.2) `terminate_live_terminal` for every pane in the tab, then `sync_live_chrome` drops the tab |
| `RenameTab`, `RenamePane`, `RenameTerminal` | `Dialog::Rename { kind }` in `Mode::Rename` seeded with the current title; commit (4.2) sets `tab.title` for `Tab`, and the pane's `title` for `Pane` / `Terminal` (client-side label only, the daemon keeps its own name) |
| `ToggleSidebar` | `sidebar.collapsed = !sidebar.collapsed` |
| `FocusPaneLeft/Down/Up/Right` | `tab.layout.find_in_direction(slot, direction)` then `Focus` (lease follows) |
| `SwapPaneLeft/Down/Up/Right` | neighbor via `find_in_direction`, `tab.layout.swap_panes(a, b)` and swap the two `tab.slots` entries |
| `LastPane` | focus `chrome.last_focused: Option<PaneId>`, recorded by `Chrome::focus_pane` on every change |
| `PreviousTab`, `NextTab`, `SwitchTab(n)` | move `active_tab` (wrapping, `n` is 1-based) and `Focus` the new tab's focused slot |
| `ResizeMode` | `Mode::Resize` (keys in 4.2) |
| `TerminalPicker`, `Goto` | `Mode::Navigator` with a cleared query; `Goto` starts with `search_focused = true` |
| `NavigateUp`, `NavigateDown` | `Mode::Navigate`: move `sidebar.selected` and clamp (these actions are bound bare in that mode) |
| `NavigatePaneLeft/Down/Up/Right` | same as the `FocusPane*` arms, leaving `Mode::Navigate` |
| `PreviousAttention`, `NextAttention`, `FocusAttention(n)` | pick the entry (previous, next, or the nth of `attention_entry_ids`), then the 2.3 jump: `attention_pane` -> `Focus`, Respond dialog for prompts |
| `OpenNotificationTarget` | `chrome.toast.target` -> `pane_for_terminal` -> `Focus`, then clear the toast |
| `ReloadConfig` | reload prefs (1.1) and the keymap (4.3); on error keep the current values and show a warning toast naming the file |
| `EditScrollback` | removed: the variant, its `edit_scrollback` spec and name mapping leave `Action` and `Keymap::defaults`; `crates/gclient/tests/keymap.rs` stops expecting it |

`CustomCommand`, `Detach`, `Quit` and the thirteen existing arms are unchanged.
The dispatch table lives in the split-out `crates/gclient/src/app/live_loop/actions.rs`
(1.4); `crates/gclient/src/ui/keymap.rs` only loses the `EditScrollback` variant
and its spec, so it shrinks.

**Acceptance:**

- 4.1.1 - Every `Action` variant has an explicit arm in `handle_live_action` (no wildcard) and `EditScrollback` no longer exists. symbol: `crates/gclient/src/ui/keymap.rs::Action`. test: `crates/gclient/tests/keymap.rs::default_bindings_cover_every_action_except_reserved`.
- 4.1.2 - Split, zoom, focus-direction, swap, last-pane, tab switching, attention jumps and the notification target drive the live workspace through the daemon: a split spawns into the named placement, focus-direction takes the lease of the neighbor, swap exchanges the two slots. file: `crates/gclient/src/app/live_loop/actions.rs`. test: `crates/gclient/tests/client_loop.rs::wired_actions_split_focus_swap_and_switch_tabs`.

### 4.2 Modal input routers and the live settings toggle [category: code] (depends: 4.1)
`kind: deliverable`

Targets:
- `crates/gclient/src/app/live_loop/modal_input.rs`
- `crates/gclient/src/app/live_loop/mouse/mod.rs`
- `crates/gclient/src/app/live_loop.rs::route_live_input`
- `crates/gclient/src/app/live_loop.rs::run_live_loop`
- `crates/gclient/src/app/run_loop.rs::route_scripted_input`
- `crates/gclient/src/ui/chrome.rs::Chrome`
- `crates/gclient/src/views/mod.rs::run_ready`
- `crates/gclient/src/startup.rs::start_session`
- `crates/gclient/tests/client_loop.rs::*` — scope-reason: new test functions appended; existing cases only change where the acceptance item names them
- `crates/gclient/tests/parity/dialogs.rs::*` — scope-reason: new test functions appended; existing cases only change where the acceptance item names them

`route_live_input` today special-cases `Mode::Respond` and sends every other
mode to `resolve_chord`, so the settings dialog, keybind help, navigator,
confirm-close, rename and resize modes render but take no keys.
`crates/gclient/src/app/live_loop/modal_input.rs` adds
`route_modal_key<W: WorkspaceView>(ws: &W, chrome: &mut Chrome, key: &KeyInput) -> ModalOutcome`
with `ModalOutcome { Consumed, Close, Focus(PaneId), Action(Action), Confirm(CloseTarget), Commit(RenameKind, String), Passthrough }`,
called from `route_live_input` and `route_scripted_input` before `resolve_chord`
for every mode except `Terminal`, `Prefix`, `Copy` and `Respond` (which keeps
`route_response_input`):

- `KeybindHelp`: printable keys edit `keybind_help.query`, up/down/page keys
  scroll, esc closes.
- `Navigator`: printable keys edit `navigator.query`, up/down move `selected`,
  tab cycles `filter`, enter yields `Focus` for a terminal row or the attention
  jump for an attention row, esc closes.
- `Settings`: up/down move `selected`; enter or space toggles a boolean row or
  cycles `theme` and `right_click_passthrough_modifier`; left/right step
  `sidebar_width` within the sidebar bounds; every change is applied to
  `chrome.prefs` at once and written with `save_prefs` (1.1), a write error
  becoming `status_message`; esc closes. Toggling `mouse capture` sets
  `chrome.pending_mouse_capture: Option<bool>`; `run_live_loop` takes a
  `&mut dyn MouseCaptureSwitch` (a one-method trait implemented by
  `TerminalGuard` over `set_mouse_capture`, and by a recording stub in tests)
  and applies the pending value after each input event, so `run_ready` passes
  the guard it already owns through `start_session`. Mouse in `Mode::Settings`
  (herdr `handle_settings_mouse`): the modal branch of `route_mouse` in
  `crates/gclient/src/app/live_loop/mouse/mod.rs` maps left Down on
  `Hit::SettingsRow(i)` to `settings.selected = i` followed by the activation
  enter performs (`activate_settings_row` in
  `crates/gclient/src/app/live_loop/modal_input.rs`, shared by the key and mouse
  paths), wheel over `Hit::SettingsDialog` moves `selected` by one, and a Down
  outside the dialog closes it like esc.
- `ConfirmClose`: `y`/enter yields `Confirm(target)` (terminal, pane or every
  pane of the tab, each through `terminate_live_terminal`), `n`/esc cancels.
- `Rename`: printable keys insert at `cursor`, backspace/delete/left/right/
  home/end edit, enter yields `Commit(kind, value)` (4.1 applies it), esc
  cancels.
- `Resize`: `h`/`j`/`k`/`l` and the arrows call `tab.layout.resize_focused`
  with a 0.05 step in that direction, enter/esc return to `Terminal`.
- `Navigate`: up/down move `sidebar.selected`, enter yields `Focus` for the
  selected roster row, esc returns to `Terminal`.

Closing a modal always restores `Mode::Terminal` and clears `chrome.dialog`.
The routers are split into `crates/gclient/src/app/live_loop/modal_input.rs`;
`crates/gclient/src/app/live_loop.rs` gains the one dispatch call and the
pending-capture check.

**Acceptance:**

- 4.2.1 - Each modal mode consumes its keys as listed: help and navigator filter on typing, the navigator's enter focuses the row, confirm-close accepts and cancels, rename commits the edited text, resize steps the ratio, navigate moves the roster selection. symbol: `crates/gclient/src/app/live_loop/modal_input.rs`. test: `crates/gclient/tests/parity/dialogs.rs::modal_keys_drive_every_mode`.
- 4.2.2 - Toggling `mouse capture` in settings flips capture on the guard immediately and persists `mouse_capture` in the prefs file; toggling it back re-enables capture. symbol: `crates/gclient/src/app/live_loop.rs::run_live_loop`. test: `crates/gclient/tests/client_loop.rs::settings_toggle_switches_mouse_capture_and_saves_prefs`.
- 4.2.3 - Clicking a settings row selects and activates it with the same effect as enter (a boolean flips, `theme` cycles) and a click outside the dialog closes it. symbol: `crates/gclient/src/app/live_loop/modal_input.rs`. test: `crates/gclient/tests/parity/dialogs.rs::settings_rows_respond_to_clicks`.

### 4.3 Load keymap overrides at startup and on reload [category: code] (depends: 4.2)
`kind: deliverable`

Targets:
- `crates/gclient/src/startup.rs::Ready`
- `crates/gclient/src/startup.rs::prepare_at`
- `crates/gclient/src/views/mod.rs::run_ready`
- `crates/gclient/tests/startup.rs::*` — scope-reason: new test functions appended; existing cases only change where the acceptance item names them
- `crates/gclient/tests/keymap.rs::*` — scope-reason: new test functions appended; existing cases only change where the acceptance item names them

`Keymap::load_overrides` and `default_override_path` exist and are covered by
tests, but `Chrome::new` always installs `Keymap::defaults()` and nothing calls
the loader (F9), so `~/.gobby/client/keymap.toml` is dead. `prepare_at` resolves
the override file (`prefs.keybinds` when non-empty, relative paths under the
gobby home, else `default_override_path()`), loads it with
`Keymap::load_overrides` into `Ready::keymap`, and turns a `KeymapError` into
`StartupError::Keymap(String)` naming the path and the parse error, failing
before the alternate screen like a bad prefs file (1.1); a missing file is the
default keymap. `run_ready` assigns `chrome.keymap = ready.keymap`.
`ReloadConfig` (4.1) reuses the same resolution and keeps the current keymap on
error. `switch_terminal` stays unbound by default; an override binds it.

**Acceptance:**

- 4.3.1 - With an override file that rebinds `help`, `Ready::keymap` carries the new chord and the live chrome resolves it; a malformed file is a `StartupError::Keymap` naming the path; a missing file yields the defaults. symbol: `crates/gclient/src/startup.rs::prepare_at`. test: `crates/gclient/tests/startup.rs::keymap_overrides_load_or_fail_loud`.
- 4.3.2 - `Keymap::load_overrides` on the resolved path is the only keymap source at startup: the override chord wins over the default chord for the same action. file: `crates/gclient/src/views/mod.rs`. test: `crates/gclient/tests/keymap.rs::override_chord_replaces_default_chord`.

### 4.4 Every attached pane renders its latest frame [category: code] (depends: 4.3)
`kind: deliverable`

Targets:
- `crates/gclient/src/app/live_loop.rs::run_live_loop`
- `crates/gclient/src/app/live_loop.rs::resize_live_workspace`
- `crates/gclient/src/app/live_loop/actions.rs`
- `crates/gclient/src/app/mod.rs::record_source_message`
- `crates/gclient/src/views/grid.rs::render`
- `crates/gclient/src/ui/panes.rs::render_panes`
- `crates/gclient/tests/client_loop.rs::*` — scope-reason: new test functions appended; existing cases only change where the acceptance item names them

Every live capture in the review shows only the focused pane painted while the
other attached panes stay blank (F3); herdr paints all panes. Static tracing
rules out the frame pump: both frame-source `recv` paths are cancellation-safe
(mpsc and broadcast), `recv_workspace_frame` records any pane's frame, and the
host's `publish_frame` sends to every attachment. It confirms one defect and
two silent failure modes, all fixed here:

- Initial geometry is never propagated: `resize_live_workspace` runs only from
  the SIGWINCH branch of `run_live_loop`, so a pane keeps the viewport the
  attach reply chose (24x80 defaults on the proxy path) until the user resizes
  the window. `run_live_loop` calls `resize_live_workspace` after the initial
  `sync_live_chrome` and again whenever `sync_live_chrome` or a spawn (2.2)
  opened a slot, so `SetViewport` always carries the pane's `inner_rect`.
- `grid::render` returns silently when `cells.len() != width * height` or when
  no frame has arrived. A size mismatch is a protocol error: the pane body shows
  `frame_size_mismatch WxH/N` (through `render_panes`, the same muted text style
  as `render_empty`) and `record_source_message` logs the pane id, dimensions
  and cell count at debug level on every frame. A pane with a live source and no
  frame yet shows `waiting for frames` in its body instead of nothing.
- The invariant gets a pin: two scripted sources feeding two panes in one tab
  through the shared render path, asserting both bodies carry their frame text
  after each pump.

The slot-change hook is split into `crates/gclient/src/app/live_loop/actions.rs`;
`crates/gclient/src/app/live_loop.rs` gains the startup call and
`crates/gclient/src/app/mod.rs` gains the one debug line.

If the live rows still show a blank pane after this lands, the body text and
the debug log name the failing side, and that fix is found work inside this
task (rung 1), not a new task.

**Acceptance:**

- 4.4.1 - After startup and after every slot change, each live pane has received `SetViewport` with its inner rect (the scripted daemon records the messages). symbol: `crates/gclient/src/app/live_loop.rs::run_live_loop`. test: `crates/gclient/tests/client_loop.rs::initial_geometry_reaches_every_pane`.
- 4.4.2 - With two panes fed by two frame sources, both pane bodies render their frame text; a mismatched frame renders the `frame_size_mismatch` line and a frameless live pane renders `waiting for frames`. symbol: `crates/gclient/src/views/grid.rs::render`. test: `crates/gclient/tests/client_loop.rs::every_attached_pane_renders_its_frame`.

## P5: Context menus
`kind: framing`

**Goal**: right-click menus for panes, tabs, roster rows, attention rows and the
empty chrome, matching herdr's item sets plus the gobby control items, rendered
inside the design contract.

### 5.1 Context menu state, items and dispatch [category: code] (depends: 4.4)
`kind: deliverable`

Targets:
- `crates/gclient/src/app/live_loop/menu.rs`
- `crates/gclient/src/app/live_loop/mouse/mod.rs`
- `crates/gclient/src/app/live_loop/mouse/pointer.rs`
- `crates/gclient/src/app/live_loop/modal_input.rs`
- `crates/gclient/src/ui/chrome.rs::Mode`
- `crates/gclient/src/ui/chrome.rs::Chrome`
- `crates/gclient/src/ui/status.rs::mode_name`
- `crates/gclient/tests/client_loop.rs::*` — scope-reason: new test functions appended; existing cases only change where the acceptance item names them

`crates/gclient/src/app/live_loop/menu.rs`:

```rust
pub enum ContextMenuKind { Pane(PaneId), Tab(usize), Roster(String), Attention(String), Global }
pub enum MenuAction { Act(Action), TogglePassthrough(PaneId), FocusTerminal(PaneId), OpenInNewTab(PaneId), JumpToAttention(String), MarkSeen(String) }
pub struct MenuItem { pub label: &'static str, pub action: MenuAction, pub enabled: bool }
pub struct ContextMenuState { pub kind: ContextMenuKind, pub anchor: (u16, u16), pub items: Vec<MenuItem>, pub selected: usize, pub item_rects: Vec<Rect> }
pub fn build_menu<W: WorkspaceView>(ws: &W, chrome: &Chrome, kind: ContextMenuKind, anchor: (u16, u16)) -> ContextMenuState;
pub fn menu_hit(state: &ContextMenuState, column: u16, row: u16) -> Option<usize>;
```

Items, in order (labels are lowercase verbs, no punctuation, per the contract):

- Pane: `take control` or `release control` (by `ControlState`), `respond` (only
  when an attention entry maps to the pane), `copy mode`, `split right`, `split
  down`, `zoom` / `unzoom`, `send right-clicks to pane` / `use gclient menu`
  (`TogglePassthrough`, 3.3), `close terminal`.
- Tab: `new tab`, `rename tab`, `close tab` (herdr's workspace menu is `Rename`,
  `Close`; its `GitWorkspace` worktree items have no client counterpart, see
  the Constraints table).
- Roster row: `focus`, `take control` / `release control`, `open in new tab`,
  `rename terminal`, `close terminal`.
- Attention row: `respond` (prompts only), `jump to terminal`, `mark seen`
  (`Daemon::mark_seen` with the entry and attention ids the roster carries).
- Global (empty tab-bar space, empty terminal area, empty sidebar): `new
  terminal`, `new tab`, `settings`, `keybinding help`, `toggle sidebar`.

Right Down on the matching `Hit` (unless 3.3 forwards it) opens the menu:
`chrome.menu = Some(build_menu(..))`, `chrome.mode = Mode::ContextMenu` (new
variant; `mode_name` shows `menu`). While open, `route_mouse` consults
`menu_hit` first: Moved highlights the item under the pointer, left Down on an
item activates it, any Down outside closes. `crates/gclient/src/app/live_loop/modal_input.rs` handles up/down,
enter (activate) and esc (close). Activation closes the menu and dispatches:
`Act(action)` through `handle_live_action`, `TogglePassthrough` flips
`Pane::right_click_passthrough`, `FocusTerminal` is the roster `Focus`,
`OpenInNewTab` opens a new tab holding that pane (`Chrome::open_tab`), and the
attention actions reuse 2.3's jump and the daemon call.

**Acceptance:**

- 5.1.1 - Right-clicking a pane, a tab, a roster row, an attention row and empty chrome opens the menu with exactly the listed items for that target and state (held vs observe, prompt vs plain entry, zoomed vs not). symbol: `crates/gclient/src/app/live_loop/menu.rs`. test: `crates/gclient/src/app/live_loop/menu.rs::menus_list_items_per_target_and_state`.
- 5.1.2 - Hover follows the pointer, clicking an item dispatches it (split right spawns into a split, take control requests the lease, mark seen calls the daemon), keys navigate and activate, and a click outside closes the menu. symbol: `crates/gclient/src/app/live_loop/mouse/pointer.rs`. test: `crates/gclient/tests/client_loop.rs::context_menu_dispatches_items_and_closes_outside`.

### 5.2 Context menu rendering [category: code] (depends: 5.1)
`kind: deliverable`

Targets:
- `crates/gclient/src/ui/context_menu.rs`
- `crates/gclient/src/ui/mod.rs`
- `crates/gclient/src/ui/chrome_render.rs::render_workspace_with`
- `crates/gclient/tests/parity/dialogs.rs::*` — scope-reason: new test functions appended; existing cases only change where the acceptance item names them

`crates/gclient/src/ui/context_menu.rs` draws `chrome.menu` in the
`Mode::ContextMenu` arm of `render_workspace_with` (no background dim: the menu
is contextual, the workspace stays readable). The popup is anchored at the click
cell, `longest label + 4` wide and `items + 2` tall, flipped left or up when it
would overflow the frame; `render_panel_shell` provides the surface (`panel_bg`
fill, `surface0` border), rows use `text` on the surface, the selected row
`accent` foreground with `REVERSED`, disabled rows `overlay0` with `DIM`, and the
row rects are written back into `ContextMenuState::item_rects` for `menu_hit`.
State is carried by text, weight and reversal, never hue alone; no glyphs,
emoji or exclamation marks; the same rules as `.impeccable.md` lines 177-243
and the existing dialogs.

**Acceptance:**

- 5.2.1 - The rendered menu sits at the anchor, flips to stay inside the frame, marks the selected row with accent plus reversal and disabled rows with dim, and its item rects match the drawn rows. symbol: `crates/gclient/src/ui/context_menu.rs`. test: `crates/gclient/tests/parity/dialogs.rs::context_menu_renders_anchored_and_clamped`.

## V1 Plan Changelog
`kind: framing`

- 2026-09-07: initial draft from the gclient/herdr parity review (findings F1-F9)
  and the #21924 inventory. Decisions recorded in Constraints: capture behind a
  prefs/CLI/settings escape hatch, hit map returned from render, one dispatcher
  in `crates/gclient/src/app/live_loop/mouse/`, forwarding as `terminal_input` bytes, lease semantics kept
  with alt+click as the observe-only modifier, `edit_scrollback` removed,
  `custom_command` left to #20201, #21923 not scheduled.
- 2026-09-07 (close review): the herdr workspace surface (sidebar workspace
  click, drag reorder, worktree block moves, `[+ New]`, list scrollbar,
  workspace menu, settings-overlay clicks) is mapped or disposed by name in the
  Constraints table; the tab bar is the landing surface (2.2), sidebar
  scrollbars (1.3, 2.3) and settings-row clicks (1.3, 4.2) are added.
- 2026-09-07 (sidebar rework): sections 4.2, 4.3, 4.4, 5.1 and 5.2 moved to
  `.gobby/plans/gclient-workspace-sidebar.md` as 4.1 (modal routers), 4.2 (keymap
  overrides), 2.3 (geometry propagation and render pin), 5.1 (context menu
  state and dispatch) and 5.2 (context menu rendering); the row menus for
  project, worktree and agent rows are its 5.3. Their bodies are carried
  verbatim with dependencies rebased on that plan; they are not expanded here.

## Task Mapping
`kind: framing`

| Section | Task |
| --- | --- |

Hold-label guidance: `needs-decision` records an unresolved decision;
`needs-planning` records work needing a dedicated planning pass; `clean-window`
records a blast-radius constraint. Historical task labels above are unchanged.
