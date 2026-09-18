//! Mouse dispatch shared by the live and scripted loops.
//!
//! `route_mouse` classifies one `RawInputEvent::Mouse` against the hit map the
//! last draw wrote into `ViewState` and says what the loop should do. Nothing
//! here reaches the daemon: the loop applies the outcome, so the lease model
//! stays in `control.rs` and this module stays pure. Gesture state lives on
//! `Chrome::gesture`: a press may start one, drags feed it, and a release
//! always ends it, handing it to `pointer::up` to finish.

use crossterm::event::{KeyModifiers, MouseButton, MouseEvent, MouseEventKind};
use gobby_terminal::layout;

use crate::ui::dialogs::{CloseTarget, Dialog};
use crate::ui::hit::{hit_test, Hit, SidebarSection};
use crate::ui::settings::SettingsRow;
use crate::ui::{Action, Chrome, Mode, WorkspaceView};

use super::super::PaneId;
use super::menu::{activate_menu, close_menu, menu_hit, ContextMenuKind, MenuAction};
use super::modal_input::{activate_settings_row, close_modal};

mod forward;
mod links;
mod pointer;
mod select;
mod wheel;

pub use select::{anchor_selection, extend_selection, finish_selection, ClickRun, DOUBLE_CLICK_MS};

/// A press-and-drag in progress, keyed by what went down under the pointer.
///
/// Each variant is started by the section that owns its surface: the tab bar,
/// the sidebar and its scrollbar, split borders and pane scrollbars, selection
/// and button forwarding. Every gesture ends on release.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum MouseGesture {
    /// A tab is being dragged; `moved` flips once it travels past the threshold.
    TabDrag {
        index: usize,
        origin_col: u16,
        moved: bool,
    },
    /// A project card is being dragged to reorder the projects.
    ProjectDrag {
        project_id: String,
        origin_row: u16,
        moved: bool,
    },
    /// A split border is being dragged to resize its panes.
    SplitDrag { border: usize },
    /// The sidebar edge is being dragged to resize the sidebar.
    SidebarDrag,
    /// A sidebar section's scrollbar thumb is being dragged.
    SidebarScrollbarDrag {
        section: SidebarSection,
        grab_offset: u16,
    },
    /// A pane scrollbar thumb is being dragged.
    ScrollbarDrag {
        slot: layout::PaneId,
        grab_offset: u16,
    },
    /// A selection is being extended inside a pane.
    Select { slot: layout::PaneId },
    /// A button is held inside a pane whose app tracks the mouse: drags and
    /// the release go to that pane wherever the pointer travels, with the
    /// passthrough modifier `strip` hidden from the app.
    Forwarding {
        slot: layout::PaneId,
        strip: KeyModifiers,
    },
}

/// Where a mouse-spawned terminal lands.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Placement {
    Tab,
    SplitRight,
    SplitDown,
}

/// What the loop does with a mouse event.
#[derive(Debug, Clone, PartialEq)]
pub enum MouseOutcome {
    /// Consumed by chrome; nothing else sees it.
    Handled,
    /// Focus `pane`; `observe_only` (alt+click) moves focus without taking
    /// control, the gobby lease escape.
    Focus { pane: PaneId, observe_only: bool },
    /// A keymap action, dispatched exactly as its chord would be.
    Action(Action),
    /// Spawn a terminal and place it.
    Spawn { placement: Placement },
    /// Bytes for a pane: a forwarded SGR report where it reports mouse, or
    /// the arrow keys a wheel notch means on an alternate screen.
    Write { pane: PaneId, bytes: Vec<u8> },
    /// Scroll `pane`'s viewport to `rows` above the live edge; the loop sends
    /// `SetScrollOffset` on the pane's frame source.
    Scroll { pane: PaneId, rows: u32 },
    /// A project card was clicked: make it the focused project.
    FocusProject(String),
    /// A session row was clicked: open its daemon-hosted terminal on demand.
    FocusAgent(String),
    /// A worktree row was clicked: focus its project and open a shell there,
    /// or reveal the tab that already shows it.
    OpenWorktree(String),
    /// A selection inside a pane was finalized: the loop copies it through
    /// OSC 52 and keeps the text for middle-click paste.
    Copy,
    /// A ctrl+click landed on a link: the loop hands the URL to
    /// `Chrome::link_opener`.
    OpenLink(String),
    /// A press in a pane whose app tracks the mouse while another pane has
    /// focus: focus it as `Focus` would (control follows) and forward `bytes`
    /// once the pane is writable.
    FocusWrite { pane: PaneId, bytes: Vec<u8> },
    /// A context menu item was activated for the target it was opened on.
    Menu {
        kind: ContextMenuKind,
        action: MenuAction,
    },
    /// The confirm-close dialog's `close` button was clicked: close
    /// `target`, exactly as Enter in the dialog does.
    Confirm(CloseTarget),
    /// A tab drag ended on another tab of a daemon tab bar: the daemon
    /// orders its tabs, so ask it to move `tab` to `position`.
    MoveTab { tab: String, position: u32 },
    /// A split-border drag ended on a daemon tab: send the ratio it
    /// reached through `slot`, a pane directly under the split.
    ResizeSplit { slot: layout::PaneId, ratio: f32 },
    /// Not ours: later routers (copy-mode selection) may still claim it.
    Ignore,
}

/// Columns a pressed tab travels before its drag becomes a move. herdr moves
/// on 1; one more keeps a click with a hair of jitter a click.
pub const TAB_DRAG_THRESHOLD: u16 = 2;

/// Rows a pressed project card travels before its drag becomes a reorder
/// (herdr `WORKSPACE_DRAG_THRESHOLD`).
pub const PROJECT_DRAG_THRESHOLD: u16 = 1;

/// Rows one wheel notch moves a sidebar list (herdr `scroll_workspace_list`).
pub const MOUSE_SCROLL_LINES: usize = 3;

/// Route one mouse event against the last drawn chrome.
///
/// Runs before every key router. With capture off the guard never arms the
/// terminal, so no mouse event should arrive; one that does is ignored rather
/// than routed. A release ends whatever gesture the press started, whichever
/// mode owns the screen; what the release itself does belongs to the
/// gesture's own surface (`pointer::up`). Modal modes own the whole screen
/// while they are up, and copy mode leaves the mouse to the selection router.
/// Motion with no button down records what the pointer is over
/// (`Chrome::hover`) for hover styling; over a pane whose app tracks all
/// motion it is also reported there, unless shift is held, and otherwise
/// stays unclaimed.
pub fn route_mouse<W: WorkspaceView>(
    ws: &W,
    chrome: &mut Chrome,
    mouse: &MouseEvent,
) -> MouseOutcome {
    if !chrome.prefs.mouse_capture {
        return MouseOutcome::Ignore;
    }
    let released = matches!(mouse.kind, MouseEventKind::Up(_))
        .then(|| chrome.gesture.take())
        .flatten();
    match chrome.mode {
        Mode::Copy => return MouseOutcome::Ignore,
        Mode::Settings => return settings_mouse(ws, chrome, mouse),
        Mode::ConfirmClose => return confirm_close_mouse(chrome, mouse),
        Mode::Rename
        | Mode::Respond
        | Mode::ProjectDialog
        | Mode::KeybindHelp
        | Mode::Navigator => return MouseOutcome::Handled,
        Mode::ContextMenu => return menu_mouse(chrome, mouse),
        Mode::Terminal | Mode::Navigate | Mode::Prefix | Mode::Resize => {}
    }
    let hit = hit_test(&chrome.view, mouse.column, mouse.row);
    match mouse.kind {
        MouseEventKind::Down(button) => pointer::down(ws, chrome, hit, button, mouse),
        MouseEventKind::Drag(_) => pointer::drag(ws, chrome, mouse),
        MouseEventKind::Up(_) => pointer::up(ws, chrome, hit, released, mouse),
        MouseEventKind::ScrollUp
        | MouseEventKind::ScrollDown
        | MouseEventKind::ScrollLeft
        | MouseEventKind::ScrollRight => wheel::wheel(ws, chrome, hit, mouse),
        MouseEventKind::Moved => {
            let outcome = match hit {
                Hit::Pane { slot, col, row } if !mouse.modifiers.contains(KeyModifiers::SHIFT) => {
                    chrome
                        .pane_for_slot(slot)
                        .filter(|pane| on_roster(ws, *pane))
                        .and_then(|pane| {
                            forward::report(ws, pane, mouse, KeyModifiers::empty(), (col, row))
                        })
                        .unwrap_or(MouseOutcome::Ignore)
                }
                _ => MouseOutcome::Ignore,
            };
            chrome.hover = Some(hit);
            outcome
        }
    }
}

/// The pointer while a menu is open: motion over a row selects it, a left
/// press on a row activates it, and a press anywhere else closes the menu
/// and goes no further (herdr: the closing click never reaches what is
/// under it). Releases and wheel notches are swallowed.
fn menu_mouse(chrome: &mut Chrome, mouse: &MouseEvent) -> MouseOutcome {
    let hit = chrome
        .menu
        .as_ref()
        .and_then(|menu| menu_hit(menu, mouse.column, mouse.row));
    match (mouse.kind, hit) {
        (MouseEventKind::Moved, Some(index)) => {
            if let Some(menu) = chrome.menu.as_mut() {
                menu.selected = index;
            }
        }
        (MouseEventKind::Down(MouseButton::Left), Some(index)) => {
            if let Some(menu) = chrome.menu.as_mut() {
                menu.selected = index;
            }
            if let Some((kind, action)) = activate_menu(chrome) {
                return MouseOutcome::Menu { kind, action };
            }
        }
        (MouseEventKind::Down(_), None) => close_menu(chrome),
        _ => {}
    }
    MouseOutcome::Handled
}

/// Confirm-close dialog: a left press on `close` confirms the pending target
/// and one on `cancel` dismisses the dialog, as their keys do; every other
/// mouse event stays with the dialog.
fn confirm_close_mouse(chrome: &mut Chrome, mouse: &MouseEvent) -> MouseOutcome {
    if mouse.kind != MouseEventKind::Down(MouseButton::Left) {
        return MouseOutcome::Handled;
    }
    match hit_test(&chrome.view, mouse.column, mouse.row) {
        Hit::DialogButton(0) => {
            let target = match chrome.dialog.take() {
                Some(Dialog::ConfirmClose { target, .. }) => Some(target),
                _ => None,
            };
            close_modal(chrome);
            target.map_or(MouseOutcome::Handled, MouseOutcome::Confirm)
        }
        Hit::DialogButton(_) => {
            close_modal(chrome);
            MouseOutcome::Handled
        }
        _ => MouseOutcome::Handled,
    }
}

/// Settings overlay: a left press on a row selects and activates it, the
/// wheel moves the selection, and a press on a footer button or outside the
/// popup closes it (every change is already written, so `done` is a close).
fn settings_mouse<W: WorkspaceView>(
    ws: &W,
    chrome: &mut Chrome,
    mouse: &MouseEvent,
) -> MouseOutcome {
    let last = SettingsRow::ALL.len() - 1;
    let selected = chrome.settings.selected;
    match (mouse.kind, hit_test(&chrome.view, mouse.column, mouse.row)) {
        (MouseEventKind::Down(MouseButton::Left), Hit::SettingsRow(index)) => {
            chrome.settings.selected = index.min(last);
            activate_settings_row(ws, chrome);
        }
        (MouseEventKind::Down(MouseButton::Left), Hit::DialogButton(_)) => {
            close_modal(chrome);
        }
        (MouseEventKind::Down(_), Hit::SettingsRow(_) | Hit::SettingsDialog) => {}
        (MouseEventKind::Down(_), _) => {
            close_modal(chrome);
        }
        (MouseEventKind::ScrollUp, Hit::SettingsRow(_) | Hit::SettingsDialog) => {
            chrome.settings.selected = selected.saturating_sub(1);
        }
        (MouseEventKind::ScrollDown, Hit::SettingsRow(_) | Hit::SettingsDialog) => {
            chrome.settings.selected = (selected + 1).min(last);
        }
        _ => {}
    }
    MouseOutcome::Handled
}

/// Focus the active tab's focused pane. A tab whose slots have all gone is
/// still activated; there is just nothing to focus.
fn focus_active_tab(chrome: &Chrome, observe_only: bool) -> MouseOutcome {
    match chrome.focused_pane() {
        Some(pane) => MouseOutcome::Focus { pane, observe_only },
        None => MouseOutcome::Handled,
    }
}

/// Whether `pane` still backs a roster terminal: the same set the keyboard
/// cycle (`focus_relative_live_pane`) focuses through. A slot whose pane has
/// left the roster is stale until the next chrome sync, and `ws.pane` on it
/// would panic.
pub(super) fn on_roster<W: WorkspaceView>(ws: &W, pane: PaneId) -> bool {
    ws.roster_terminal_ids()
        .iter()
        .any(|terminal_id| ws.pane_for_terminal(terminal_id) == Some(pane))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::app::Workspace;
    use crate::ui::hit::Hit;
    use crate::ui::settings::PassthroughModifier;
    use crossterm::event::{KeyModifiers, MouseButton};
    use gobby_terminal::protocol::{FrameData, PaneModes};
    use ratatui::layout::Rect;

    /// `pane`'s app tracks every motion and asked for SGR reports.
    fn track_mouse(ws: &mut Workspace, pane: PaneId) {
        ws.pane_mut(pane).latest_frame = Some(FrameData {
            cells: Vec::new(),
            width: 0,
            height: 0,
            cursor: None,
            hyperlinks: Vec::new(),
            graphics: Vec::new(),
            modes: PaneModes {
                mouse_all: true,
                mouse_sgr: true,
                ..PaneModes::default()
            },
        });
    }

    /// The right-click opened the pane menu on `pane`; close it again.
    fn menu_opened(chrome: &mut Chrome, pane: PaneId) {
        assert_eq!(chrome.mode, Mode::ContextMenu);
        assert_eq!(
            chrome.menu.as_ref().map(|menu| menu.kind.clone()),
            Some(ContextMenuKind::Pane(pane))
        );
        close_menu(chrome);
    }

    /// Two panes split in one tab, drawn once so the hit map is populated.
    /// Returns the workspace, the chrome, the focused pane and a cell inside
    /// each pane's content: `(ws, chrome, focused_cell, other_pane, other_cell)`.
    fn split_chrome() -> (Workspace, Chrome, (u16, u16), PaneId, (u16, u16)) {
        let mut ws = Workspace::scripted();
        let alpha = ws
            .open_terminal("term-alpha", "native", "epoch")
            .expect("open term-alpha");
        let beta = ws
            .open_terminal("term-beta", "native", "epoch")
            .expect("open term-beta");
        let mut chrome = Chrome::dark();
        chrome.open_pane(alpha, "alpha");
        chrome.open_pane(beta, "beta");
        chrome.compute_view(&ws, Rect::new(0, 0, 100, 30));
        let focused = chrome.focused_pane().expect("focused pane");
        let cell_of = |pane: PaneId| {
            let info = chrome
                .view
                .pane_infos
                .iter()
                .find(|info| chrome.pane_for_slot(info.id) == Some(pane))
                .expect("pane drawn");
            (info.inner_rect.x + 1, info.inner_rect.y + 1)
        };
        let other = if focused == alpha { beta } else { alpha };
        let focused_cell = cell_of(focused);
        let other_cell = cell_of(other);
        (ws, chrome, focused_cell, other, other_cell)
    }

    fn event(kind: MouseEventKind, column: u16, row: u16, modifiers: KeyModifiers) -> MouseEvent {
        MouseEvent {
            kind,
            column,
            row,
            modifiers,
        }
    }

    fn down(column: u16, row: u16, modifiers: KeyModifiers) -> MouseEvent {
        event(
            MouseEventKind::Down(MouseButton::Left),
            column,
            row,
            modifiers,
        )
    }

    #[test]
    fn route_mouse_ignores_when_capture_is_off() {
        let (ws, mut chrome, _, other, (col, row)) = split_chrome();
        assert_eq!(
            route_mouse(&ws, &mut chrome, &down(col, row, KeyModifiers::NONE)),
            MouseOutcome::Focus {
                pane: other,
                observe_only: false
            },
            "with capture on the same click focuses the other pane"
        );
        chrome.prefs.mouse_capture = false;
        assert_eq!(
            route_mouse(&ws, &mut chrome, &down(col, row, KeyModifiers::NONE)),
            MouseOutcome::Ignore
        );
    }

    #[test]
    fn route_mouse_focuses_the_other_pane_and_alt_observes() {
        let (ws, mut chrome, (fcol, frow), other, (col, row)) = split_chrome();
        assert_eq!(
            route_mouse(&ws, &mut chrome, &down(fcol, frow, KeyModifiers::NONE)),
            MouseOutcome::Handled,
            "a click on the focused pane starts a selection"
        );
        assert!(matches!(chrome.gesture, Some(MouseGesture::Select { .. })));
        assert!(chrome.selection.is_some());
        assert_eq!(
            route_mouse(&ws, &mut chrome, &down(col, row, KeyModifiers::ALT)),
            MouseOutcome::Focus {
                pane: other,
                observe_only: true
            }
        );
        chrome.mode = Mode::Settings;
        assert_eq!(
            route_mouse(&ws, &mut chrome, &down(col, row, KeyModifiers::NONE)),
            MouseOutcome::Handled,
            "a modal owns the screen"
        );
        chrome.mode = Mode::Copy;
        assert_eq!(
            route_mouse(&ws, &mut chrome, &down(col, row, KeyModifiers::NONE)),
            MouseOutcome::Ignore,
            "copy mode leaves the mouse to the selection router"
        );
    }

    #[test]
    fn route_mouse_ignores_a_slot_whose_pane_left_the_roster() {
        let (_, mut chrome, _, _, (col, row)) = split_chrome();
        let empty = Workspace::scripted();
        assert_eq!(
            route_mouse(&empty, &mut chrome, &down(col, row, KeyModifiers::NONE)),
            MouseOutcome::Ignore,
            "a stale slot is not focused before the next chrome sync"
        );
    }

    #[test]
    fn route_mouse_motion_records_hover_and_stays_unclaimed() {
        let (ws, mut chrome, _, _, (col, row)) = split_chrome();
        let status = chrome.view.status_rect;
        let moved = |column, row| event(MouseEventKind::Moved, column, row, KeyModifiers::NONE);
        assert_eq!(
            chrome.hover, None,
            "nothing is hovered before the pointer moves"
        );
        assert_eq!(
            route_mouse(&ws, &mut chrome, &moved(status.x, status.y)),
            MouseOutcome::Ignore
        );
        assert_eq!(
            chrome.hover,
            Some(hit_test(&chrome.view, status.x, status.y)),
            "motion records what the pointer is over"
        );
        assert_eq!(
            route_mouse(&ws, &mut chrome, &moved(col, row)),
            MouseOutcome::Ignore
        );
        assert!(
            matches!(chrome.hover, Some(Hit::Pane { .. })),
            "moving on replaces the hover: {:?}",
            chrome.hover
        );
    }

    #[test]
    fn route_mouse_release_ends_the_gesture() {
        let (ws, mut chrome, _, _, (col, row)) = split_chrome();
        chrome.gesture = Some(MouseGesture::SidebarDrag);
        let drag = event(
            MouseEventKind::Drag(MouseButton::Left),
            col,
            row,
            KeyModifiers::NONE,
        );
        assert_eq!(route_mouse(&ws, &mut chrome, &drag), MouseOutcome::Handled);
        assert_eq!(
            chrome.gesture,
            Some(MouseGesture::SidebarDrag),
            "a drag keeps the gesture alive"
        );
        let up = event(
            MouseEventKind::Up(MouseButton::Left),
            col,
            row,
            KeyModifiers::NONE,
        );
        assert_eq!(route_mouse(&ws, &mut chrome, &up), MouseOutcome::Handled);
        assert_eq!(chrome.gesture, None, "a release ends the gesture");
        chrome.gesture = Some(MouseGesture::SidebarDrag);
        chrome.mode = Mode::KeybindHelp;
        assert_eq!(route_mouse(&ws, &mut chrome, &up), MouseOutcome::Handled);
        assert_eq!(
            chrome.gesture, None,
            "a release under a modal still ends it"
        );
    }

    #[test]
    fn route_mouse_forwards_reports_and_captures_the_button() {
        let (mut ws, mut chrome, (fcol, frow), other, (col, row)) = split_chrome();
        let focused = chrome.focused_pane().expect("focused pane");
        track_mouse(&mut ws, focused);
        track_mouse(&mut ws, other);
        let write = |pane, bytes: &[u8]| MouseOutcome::Write {
            pane,
            bytes: bytes.to_vec(),
        };
        let at = |kind, column, row, modifiers| event(kind, column, row, modifiers);
        assert_eq!(
            route_mouse(&ws, &mut chrome, &down(fcol, frow, KeyModifiers::NONE)),
            write(focused, b"\x1b[<0;2;2M"),
            "a press in a tracking pane is reported at its pane cell"
        );
        assert!(
            matches!(chrome.gesture, Some(MouseGesture::Forwarding { .. })),
            "the press captures the button: {:?}",
            chrome.gesture
        );
        assert_eq!(
            route_mouse(
                &ws,
                &mut chrome,
                &at(
                    MouseEventKind::Drag(MouseButton::Left),
                    0,
                    0,
                    KeyModifiers::NONE
                )
            ),
            write(focused, b"\x1b[<32;1;1M"),
            "a drag goes to the captured pane, clamped to its content"
        );
        assert_eq!(
            route_mouse(
                &ws,
                &mut chrome,
                &at(
                    MouseEventKind::Up(MouseButton::Left),
                    fcol,
                    frow,
                    KeyModifiers::NONE
                )
            ),
            write(focused, b"\x1b[<0;2;2m")
        );
        assert_eq!(chrome.gesture, None, "the release frees the button");
        assert_eq!(
            route_mouse(
                &ws,
                &mut chrome,
                &at(MouseEventKind::Moved, fcol, frow, KeyModifiers::NONE)
            ),
            write(focused, b"\x1b[<35;2;2M"),
            "any-motion tracking gets the pointer with no button down"
        );
        assert_eq!(
            route_mouse(&ws, &mut chrome, &down(fcol, frow, KeyModifiers::SHIFT)),
            MouseOutcome::Handled,
            "shift bypasses the app and selects instead"
        );
        assert!(matches!(chrome.gesture, Some(MouseGesture::Select { .. })));
        route_mouse(
            &ws,
            &mut chrome,
            &at(
                MouseEventKind::Up(MouseButton::Left),
                fcol,
                frow,
                KeyModifiers::SHIFT,
            ),
        );
        assert_eq!(
            route_mouse(&ws, &mut chrome, &down(col, row, KeyModifiers::NONE)),
            MouseOutcome::FocusWrite {
                pane: other,
                bytes: b"\x1b[<0;2;2M".to_vec()
            },
            "a press in another tracking pane focuses it before the report"
        );
        route_mouse(
            &ws,
            &mut chrome,
            &at(
                MouseEventKind::Up(MouseButton::Left),
                col,
                row,
                KeyModifiers::NONE,
            ),
        );
        assert_eq!(
            route_mouse(&ws, &mut chrome, &down(col, row, KeyModifiers::ALT)),
            MouseOutcome::Focus {
                pane: other,
                observe_only: true
            },
            "alt+click observes without writing"
        );
        assert_eq!(
            route_mouse(
                &ws,
                &mut chrome,
                &at(MouseEventKind::ScrollUp, fcol, frow, KeyModifiers::NONE)
            ),
            write(focused, b"\x1b[<64;2;2M")
        );
        assert_eq!(
            route_mouse(
                &ws,
                &mut chrome,
                &at(MouseEventKind::ScrollLeft, fcol, frow, KeyModifiers::NONE)
            ),
            write(focused, b"\x1b[<66;2;2M"),
            "a horizontal notch reaches a tracking pane"
        );
    }

    #[test]
    fn route_mouse_right_click_passthrough() {
        let (mut ws, mut chrome, (col, row), _, _) = split_chrome();
        let focused = chrome.focused_pane().expect("focused pane");
        track_mouse(&mut ws, focused);
        let right = |kind, modifiers| event(kind, col, row, modifiers);
        let write = |bytes: &[u8]| MouseOutcome::Write {
            pane: focused,
            bytes: bytes.to_vec(),
        };
        let press = MouseEventKind::Down(MouseButton::Right);
        let release = MouseEventKind::Up(MouseButton::Right);
        assert_eq!(
            route_mouse(&ws, &mut chrome, &right(press, KeyModifiers::NONE)),
            MouseOutcome::Handled,
            "a plain right-click is the pane menu's"
        );
        assert_eq!(chrome.gesture, None);
        menu_opened(&mut chrome, focused);
        ws.pane_mut(focused).right_click_passthrough = true;
        assert_eq!(
            route_mouse(&ws, &mut chrome, &right(press, KeyModifiers::NONE)),
            write(b"\x1b[<2;2;2M"),
            "the pane flag passes a plain right-click through"
        );
        assert_eq!(
            route_mouse(&ws, &mut chrome, &right(release, KeyModifiers::NONE)),
            write(b"\x1b[<2;2;2m")
        );
        assert_eq!(
            route_mouse(&ws, &mut chrome, &right(press, KeyModifiers::ALT)),
            MouseOutcome::Handled,
            "the flag wants no modifier held"
        );
        menu_opened(&mut chrome, focused);
        ws.pane_mut(focused).right_click_passthrough = false;
        chrome.prefs.right_click_passthrough_modifier = PassthroughModifier::Alt;
        assert_eq!(
            route_mouse(&ws, &mut chrome, &right(press, KeyModifiers::ALT)),
            write(b"\x1b[<2;2;2M"),
            "the configured modifier is hidden from the app"
        );
        assert_eq!(
            route_mouse(
                &ws,
                &mut chrome,
                &right(MouseEventKind::Drag(MouseButton::Right), KeyModifiers::ALT)
            ),
            write(b"\x1b[<34;2;2M")
        );
        assert_eq!(
            route_mouse(&ws, &mut chrome, &right(release, KeyModifiers::ALT)),
            write(b"\x1b[<2;2;2m")
        );
        assert_eq!(
            route_mouse(&ws, &mut chrome, &right(press, KeyModifiers::CONTROL)),
            MouseOutcome::Handled,
            "another modifier is not the configured one"
        );
        menu_opened(&mut chrome, focused);
        assert_eq!(
            route_mouse(&ws, &mut chrome, &right(press, KeyModifiers::NONE)),
            MouseOutcome::Handled,
            "with a modifier configured a plain right-click stays the menu's"
        );
        menu_opened(&mut chrome, focused);
    }
}
