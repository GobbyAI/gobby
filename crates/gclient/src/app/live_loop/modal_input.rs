//! Key routing for the chrome's modal modes.
//!
//! `route_modal_key` runs before the keymap for every mode that owns the
//! keyboard: the overlays (keybind help, navigator, settings), the dialogs
//! (confirm-close, rename), the context menu and the transient resize and
//! navigate modes. It
//! edits chrome state in place and hands the loop a [`ModalOutcome`] for the
//! parts that need the workspace: focus, actions, closes and renames. Both
//! loops apply outcomes their own way, so nothing here reaches a daemon.

use std::path::Path;

use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use gobby_terminal::layout::NavDirection;

use crate::daemon::Daemon;
use crate::key_input::KeyInput;
use crate::prefs::save_prefs;
use crate::theme::ThemeKind;
use crate::ui::dialogs::{CloseTarget, Dialog, OrphanRow, RenameKind};
use crate::ui::keybind_help::help_lines;
use crate::ui::navigator::{
    navigator_rows, NavigatorRow, NavigatorState, NavigatorStateFilter, NavigatorTarget,
};
use crate::ui::settings::{PassthroughModifier, SettingsRow};
use crate::ui::sidebar::attention_order;
use crate::ui::sidebar_rows::{project_rows, RowKind};
use crate::ui::{Action, Chrome, Mode, WorkspaceView};

use super::super::{PaneId, Workspace};
use super::actions::live_layout_area;
use super::menu::{activate_menu, close_menu, ContextMenuKind, MenuAction};
use super::projects::project_dialog_key;

/// Lines one page key scrolls the keybind help by.
const HELP_PAGE_LINES: usize = 10;
/// Fraction of the layout one resize key moves a split by.
const RESIZE_STEP: f32 = 0.05;
/// Right-click passthrough modifiers in the order the settings row cycles.
const PASSTHROUGH_CYCLE: [PassthroughModifier; 4] = [
    PassthroughModifier::None,
    PassthroughModifier::Shift,
    PassthroughModifier::Alt,
    PassthroughModifier::Ctrl,
];

/// What the loop does after a modal mode saw a key.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ModalOutcome {
    /// The mode handled the key on the chrome alone.
    Consumed,
    /// The mode closed; the chrome is back in `Mode::Terminal`.
    Close,
    /// Focus this pane.
    Focus(PaneId),
    /// The navigator picked a terminal: reveal it, attaching on demand.
    FocusTerminal(String),
    /// Navigate picked a project card: make it the focused project.
    FocusProject(String),
    /// Navigate picked a worktree row: open a shell there.
    OpenWorktree(String),
    /// Run a keymap action.
    Action(Action),
    /// The confirm-close dialog was accepted for this target.
    Confirm(CloseTarget),
    /// The rename dialog was accepted with this value.
    Commit(RenameKind, String),
    /// The new-project dialog asked the daemon to register this path; the
    /// dialog stays open for the answer.
    InitProject(String),
    /// The new-worktree dialog asked for a checkout of `project_id`; the
    /// dialog stays open for the answer.
    CreateWorktree {
        project_id: String,
        branch: String,
        base: Option<String>,
    },
    /// The remove-worktree dialog confirmed; the dialog stays open.
    RemoveWorktree(String),
    /// The destroy-orphans dialog confirmed these checked rows; it is closed.
    DestroyOrphans(Vec<OrphanRow>),
    /// A context menu item was activated for the target it was opened on.
    Menu {
        kind: ContextMenuKind,
        action: MenuAction,
    },
    /// The mode does not own the keyboard; resolve the key against the keymap.
    Passthrough,
}

/// Route one key through the open modal mode, if any.
pub fn route_modal_key<W: WorkspaceView>(
    ws: &W,
    chrome: &mut Chrome,
    key: &KeyInput,
) -> ModalOutcome {
    let key = &key.key;
    match chrome.mode {
        Mode::Terminal | Mode::Prefix | Mode::Copy | Mode::Respond => ModalOutcome::Passthrough,
        Mode::ContextMenu => menu_key(chrome, key),
        Mode::KeybindHelp => keybind_help_key(chrome, key),
        Mode::Navigator => navigator_key(ws, chrome, key),
        Mode::Settings => settings_key(ws, chrome, key),
        Mode::ConfirmClose => confirm_close_key(chrome, key),
        Mode::Rename => rename_key(chrome, key),
        Mode::Resize => resize_key(chrome, key),
        Mode::Navigate => navigate_key(ws, chrome, key),
        Mode::ProjectDialog => project_dialog_key(chrome, key),
    }
}

/// Leave whichever modal mode is open: back to the terminal, dialog gone.
pub(super) fn close_modal(chrome: &mut Chrome) -> ModalOutcome {
    chrome.mode = Mode::Terminal;
    chrome.dialog = None;
    ModalOutcome::Close
}

/// Apply a committed rename: the active tab's title, or the focused pane's
/// label (the daemon has no rename call, and roster pages refresh the
/// pane's own title); an empty value clears the label.
pub fn apply_rename<D: Daemon>(
    workspace: &mut Workspace<D>,
    chrome: &mut Chrome,
    kind: RenameKind,
    value: String,
) {
    match kind {
        RenameKind::Tab => {
            if let Some(tab) = chrome.active_tab_mut() {
                tab.title = value;
            }
        }
        RenameKind::Pane | RenameKind::Terminal => {
            if let Some(pane) = chrome.focused_pane() {
                workspace.pane_mut(pane).label = (!value.is_empty()).then_some(value);
            }
        }
        RenameKind::Project(project_id) => {
            if value.is_empty() {
                chrome.sidebar.project_labels.remove(&project_id);
            } else {
                chrome.sidebar.project_labels.insert(project_id, value);
            }
        }
    }
}

/// A printable character typed without control or alt.
fn typed_char(key: &KeyEvent) -> Option<char> {
    match key.code {
        KeyCode::Char(ch)
            if !key
                .modifiers
                .intersects(KeyModifiers::CONTROL | KeyModifiers::ALT) =>
        {
            Some(ch)
        }
        _ => None,
    }
}

fn is_ctrl(key: &KeyEvent, ch: char) -> bool {
    key.code == KeyCode::Char(ch) && key.modifiers.contains(KeyModifiers::CONTROL)
}

/// `index` moved by `delta`, clamped to `0..=last`.
fn step(index: usize, delta: isize, last: usize) -> usize {
    index.min(last).saturating_add_signed(delta).min(last)
}

/// Keys while a context menu is open: arrows or `j`/`k` move the selection,
/// enter or space activate it, escape closes it; nothing reaches the keymap.
fn menu_key(chrome: &mut Chrome, key: &KeyEvent) -> ModalOutcome {
    let delta = match key.code {
        KeyCode::Esc => {
            close_menu(chrome);
            return ModalOutcome::Close;
        }
        KeyCode::Enter | KeyCode::Char(' ') => {
            return match activate_menu(chrome) {
                Some((kind, action)) => ModalOutcome::Menu { kind, action },
                None => ModalOutcome::Close,
            };
        }
        KeyCode::Up | KeyCode::Char('k') => -1,
        KeyCode::Down | KeyCode::Char('j') => 1,
        _ => return ModalOutcome::Consumed,
    };
    if let Some(menu) = chrome.menu.as_mut() {
        let last = menu.items.len().saturating_sub(1);
        menu.selected = step(menu.selected, delta, last);
    }
    ModalOutcome::Consumed
}

fn keybind_help_key(chrome: &mut Chrome, key: &KeyEvent) -> ModalOutcome {
    let last_line = help_lines(chrome).len().saturating_sub(1);
    let help = &mut chrome.keybind_help;
    let scroll_by = |help: &mut crate::ui::keybind_help::KeybindHelpState, delta: isize| {
        help.scroll = step(help.scroll, delta, last_line);
    };
    if help.search_focused {
        if is_ctrl(key, 'u') {
            help.query.clear();
            help.scroll = 0;
            return ModalOutcome::Consumed;
        }
        match key.code {
            KeyCode::Esc => help.search_focused = false,
            KeyCode::Backspace => {
                help.query.pop();
                help.scroll = 0;
            }
            KeyCode::Up => scroll_by(help, -1),
            KeyCode::Down => scroll_by(help, 1),
            KeyCode::PageUp => scroll_by(help, -(HELP_PAGE_LINES as isize)),
            KeyCode::PageDown => scroll_by(help, HELP_PAGE_LINES as isize),
            _ => {
                if let Some(ch) = typed_char(key) {
                    help.query.push(ch);
                    help.scroll = 0;
                }
            }
        }
        return ModalOutcome::Consumed;
    }
    match key.code {
        KeyCode::Esc | KeyCode::Enter => return close_modal(chrome),
        KeyCode::Char('/') => help.search_focused = true,
        KeyCode::Char('j') | KeyCode::Down => scroll_by(help, 1),
        KeyCode::Char('k') | KeyCode::Up => scroll_by(help, -1),
        KeyCode::PageUp => scroll_by(help, -(HELP_PAGE_LINES as isize)),
        KeyCode::PageDown => scroll_by(help, HELP_PAGE_LINES as isize),
        _ => {}
    }
    ModalOutcome::Consumed
}

fn navigator_key<W: WorkspaceView>(ws: &W, chrome: &mut Chrome, key: &KeyEvent) -> ModalOutcome {
    let rows = navigator_rows(ws, chrome);
    let last = rows.len().saturating_sub(1);
    let nav = &mut chrome.navigator;
    let mut filter = None;
    if nav.search_focused {
        if is_ctrl(key, 'u') {
            nav.query.clear();
            nav.selected = 0;
            nav.scroll = 0;
            return ModalOutcome::Consumed;
        }
        match key.code {
            KeyCode::Esc => nav.search_focused = false,
            KeyCode::Enter => return pick_navigator_row(ws, chrome, rows),
            KeyCode::Backspace => {
                nav.query.pop();
                nav.selected = 0;
                nav.scroll = 0;
            }
            KeyCode::Up => move_navigator_selection(nav, -1, last),
            KeyCode::Down => move_navigator_selection(nav, 1, last),
            _ => {
                if let Some(ch) = typed_char(key) {
                    nav.query.push(ch);
                    nav.selected = 0;
                    nav.scroll = 0;
                }
            }
        }
        return ModalOutcome::Consumed;
    }
    match key.code {
        KeyCode::Esc => return close_modal(chrome),
        KeyCode::Enter => return pick_navigator_row(ws, chrome, rows),
        KeyCode::Char('/') => nav.search_focused = true,
        KeyCode::Char('j') | KeyCode::Down => move_navigator_selection(nav, 1, last),
        KeyCode::Char('k') | KeyCode::Up => move_navigator_selection(nav, -1, last),
        KeyCode::Char('a') => filter = Some(NavigatorStateFilter::All),
        KeyCode::Char('b') => filter = Some(NavigatorStateFilter::Attention),
        KeyCode::Char('w') => filter = Some(NavigatorStateFilter::Working),
        KeyCode::Char('i') => filter = Some(NavigatorStateFilter::Idle),
        KeyCode::Tab => {
            filter = Some(match nav.filter {
                NavigatorStateFilter::All => NavigatorStateFilter::Attention,
                NavigatorStateFilter::Attention => NavigatorStateFilter::Working,
                NavigatorStateFilter::Working => NavigatorStateFilter::Idle,
                NavigatorStateFilter::Idle => NavigatorStateFilter::All,
            });
        }
        _ => {}
    }
    if let Some(filter) = filter {
        nav.filter = filter;
        nav.selected = 0;
        nav.scroll = 0;
    }
    ModalOutcome::Consumed
}

fn move_navigator_selection(nav: &mut NavigatorState, delta: isize, last: usize) {
    nav.selected = step(nav.selected, delta, last);
    nav.scroll = nav.scroll.min(nav.selected);
}

/// Switch to the selected navigator row: a terminal row focuses its pane, an
/// attention row hands the loop its 1-based `FocusAttention` index so the
/// attention jump stays in one place.
fn pick_navigator_row<W: WorkspaceView>(
    ws: &W,
    chrome: &mut Chrome,
    rows: Vec<NavigatorRow>,
) -> ModalOutcome {
    let Some(row) = rows.into_iter().nth(chrome.navigator.selected) else {
        return ModalOutcome::Consumed;
    };
    let outcome = match row.target {
        NavigatorTarget::Terminal(terminal_id) => Some(ModalOutcome::FocusTerminal(terminal_id)),
        NavigatorTarget::Attention(entry) => attention_order(ws, chrome)
            .iter()
            .position(|candidate| *candidate == entry)
            .and_then(|index| u8::try_from(index + 1).ok())
            .map(|index| ModalOutcome::Action(Action::FocusAttention(index)))
            .or_else(|| row.pane.map(ModalOutcome::Focus)),
    };
    match outcome {
        Some(outcome) => {
            close_modal(chrome);
            outcome
        }
        None => ModalOutcome::Consumed,
    }
}

fn settings_key<W: WorkspaceView>(ws: &W, chrome: &mut Chrome, key: &KeyEvent) -> ModalOutcome {
    let last = SettingsRow::ALL.len() - 1;
    match key.code {
        KeyCode::Esc | KeyCode::Enter => return close_modal(chrome),
        KeyCode::Char('k') | KeyCode::Up => {
            chrome.settings.selected = step(chrome.settings.selected, -1, last);
        }
        KeyCode::Char('j') | KeyCode::Down => {
            chrome.settings.selected = step(chrome.settings.selected, 1, last);
        }
        KeyCode::Char(' ') => activate_settings_row(ws, chrome),
        KeyCode::Left => step_settings_row(ws, chrome, -1),
        KeyCode::Right => step_settings_row(ws, chrome, 1),
        _ => {}
    }
    ModalOutcome::Consumed
}

/// Toggle or advance the selected settings row and write the prefs.
pub(super) fn activate_settings_row<W: WorkspaceView>(ws: &W, chrome: &mut Chrome) {
    step_settings_row(ws, chrome, 1);
}

/// Change the selected row by `delta`: booleans flip, the theme and the
/// passthrough modifier cycle, and the sidebar width steps by that many
/// columns. The change applies to the chrome at once and is written to the
/// prefs file.
fn step_settings_row<W: WorkspaceView>(ws: &W, chrome: &mut Chrome, delta: isize) {
    let Some(row) = SettingsRow::ALL.get(chrome.settings.selected) else {
        return;
    };
    let prefs = &mut chrome.prefs;
    match row {
        SettingsRow::Theme => {
            let kind = match prefs.theme_kind() {
                ThemeKind::Dark => ThemeKind::Light,
                ThemeKind::Light => ThemeKind::Dark,
            };
            prefs.theme = match kind {
                ThemeKind::Dark => "dark",
                ThemeKind::Light => "light",
            }
            .to_owned();
            chrome.set_theme(kind);
        }
        SettingsRow::MouseCapture => {
            prefs.mouse_capture = !prefs.mouse_capture;
            chrome.pending_mouse_capture = Some(prefs.mouse_capture);
        }
        SettingsRow::PaneBorders => prefs.pane_borders = !prefs.pane_borders,
        SettingsRow::PaneScrollbars => prefs.pane_scrollbars = !prefs.pane_scrollbars,
        SettingsRow::PaneGaps => prefs.pane_gaps = !prefs.pane_gaps,
        SettingsRow::ConfirmClose => prefs.confirm_close = !prefs.confirm_close,
        SettingsRow::HideTabBarWhenSingleTab => {
            prefs.hide_tab_bar_when_single_tab = !prefs.hide_tab_bar_when_single_tab;
        }
        SettingsRow::SidebarWidth => {
            let (min, max) = (chrome.sidebar.min_width, chrome.sidebar.max_width);
            let width = usize::from(prefs.sidebar_width)
                .saturating_add_signed(delta)
                .clamp(usize::from(min), usize::from(max));
            let width = u16::try_from(width).unwrap_or(max);
            prefs.sidebar_width = width;
            chrome.sidebar.width = width;
        }
        SettingsRow::RightClickPassthrough => {
            let current = PASSTHROUGH_CYCLE
                .iter()
                .position(|candidate| *candidate == prefs.right_click_passthrough_modifier)
                .unwrap_or(0);
            let next = (current as isize + delta).rem_euclid(PASSTHROUGH_CYCLE.len() as isize);
            prefs.right_click_passthrough_modifier = PASSTHROUGH_CYCLE[next as usize];
        }
        SettingsRow::AgentSort => prefs.agent_sort = prefs.agent_sort.toggled(),
    }
    persist_prefs(ws.gobby_home(), chrome);
}

/// Write the prefs when the workspace knows its Gobby home; the `modified`
/// marker stays on while the file does not match the chrome.
pub(super) fn persist_prefs(home: Option<&Path>, chrome: &mut Chrome) {
    let written = match home.map(|home| save_prefs(home, &chrome.prefs)) {
        Some(Ok(_)) => true,
        Some(Err(error)) => {
            chrome.status_message = Some(format!("Could not save preferences: {error}"));
            false
        }
        None => false,
    };
    chrome.settings.dirty = !written;
}

fn confirm_close_key(chrome: &mut Chrome, key: &KeyEvent) -> ModalOutcome {
    match key.code {
        KeyCode::Char('y') | KeyCode::Enter => {
            let Some(Dialog::ConfirmClose { target, .. }) = chrome.dialog.take() else {
                return close_modal(chrome);
            };
            close_modal(chrome);
            ModalOutcome::Confirm(target)
        }
        KeyCode::Char('n') | KeyCode::Esc => close_modal(chrome),
        _ => ModalOutcome::Consumed,
    }
}

fn rename_key(chrome: &mut Chrome, key: &KeyEvent) -> ModalOutcome {
    let Some(Dialog::Rename {
        kind,
        value,
        cursor,
    }) = &mut chrome.dialog
    else {
        return close_modal(chrome);
    };
    match key.code {
        KeyCode::Enter => {
            let committed = (kind.clone(), std::mem::take(value));
            close_modal(chrome);
            return ModalOutcome::Commit(committed.0, committed.1);
        }
        KeyCode::Esc => return close_modal(chrome),
        _ => edit_text(value, cursor, key),
    }
    ModalOutcome::Consumed
}

/// One line-editing key on `value` at the char index `cursor`: arrows,
/// home/end, backspace/delete, and printable characters.
pub(super) fn edit_text(value: &mut String, cursor: &mut usize, key: &KeyEvent) {
    match key.code {
        KeyCode::Left => *cursor = cursor.saturating_sub(1),
        KeyCode::Right => *cursor = (*cursor + 1).min(value.chars().count()),
        KeyCode::Home => *cursor = 0,
        KeyCode::End => *cursor = value.chars().count(),
        KeyCode::Backspace => {
            if let Some(previous) = cursor.checked_sub(1) {
                remove_char(value, previous);
                *cursor = previous;
            }
        }
        KeyCode::Delete => remove_char(value, *cursor),
        _ => {
            if let Some(ch) = typed_char(key) {
                let at = byte_offset(value, *cursor);
                value.insert(at, ch);
                *cursor += 1;
            }
        }
    }
}

/// Byte offset of the `cursor`th character, or the end of `value`.
fn byte_offset(value: &str, cursor: usize) -> usize {
    value
        .char_indices()
        .nth(cursor)
        .map_or(value.len(), |(offset, _)| offset)
}

fn remove_char(value: &mut String, cursor: usize) {
    let at = byte_offset(value, cursor);
    if at < value.len() {
        value.remove(at);
    }
}

fn resize_key(chrome: &mut Chrome, key: &KeyEvent) -> ModalOutcome {
    let direction = match key.code {
        KeyCode::Char('h') | KeyCode::Left => NavDirection::Left,
        KeyCode::Char('j') | KeyCode::Down => NavDirection::Down,
        KeyCode::Char('k') | KeyCode::Up => NavDirection::Up,
        KeyCode::Char('l') | KeyCode::Right => NavDirection::Right,
        KeyCode::Enter | KeyCode::Esc => return close_modal(chrome),
        _ => return ModalOutcome::Consumed,
    };
    let area = live_layout_area(chrome);
    if let Some(focus) = chrome.focus_slot() {
        if let Some(tab) = chrome.active_tab_mut() {
            tab.layout.resize_pane(focus, direction, RESIZE_STEP, area);
        }
    }
    ModalOutcome::Consumed
}

/// Navigate walks the projects section's rows: cards and their visible
/// worktree rows; Enter takes the row under the cursor.
fn navigate_key<W: WorkspaceView>(ws: &W, chrome: &mut Chrome, key: &KeyEvent) -> ModalOutcome {
    let rows = project_rows(ws, chrome);
    let last = rows.len().saturating_sub(1);
    match key.code {
        KeyCode::Up => chrome.sidebar.selected = step(chrome.sidebar.selected, -1, last),
        KeyCode::Down => chrome.sidebar.selected = step(chrome.sidebar.selected, 1, last),
        KeyCode::Enter => {
            chrome.mode = Mode::Terminal;
            return match rows.into_iter().nth(chrome.sidebar.selected) {
                Some(row) if row.kind == RowKind::Project => ModalOutcome::FocusProject(row.id),
                Some(row) if row.kind == RowKind::Worktree => ModalOutcome::OpenWorktree(row.id),
                _ => ModalOutcome::Consumed,
            };
        }
        KeyCode::Esc => return close_modal(chrome),
        _ => return ModalOutcome::Passthrough,
    }
    ModalOutcome::Consumed
}
