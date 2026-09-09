//! herdr `src/ui/dialogs.rs` (4) keep-set render tests.

use crossterm::event::{KeyCode, KeyEvent, KeyModifiers, MouseButton, MouseEvent, MouseEventKind};
use gobby_client::app::{
    route_modal_key, route_mouse, ContextMenuKind, ContextMenuState, MenuAction, MenuItem,
    ModalOutcome, MouseOutcome,
};
use gobby_client::key_input::KeyInput;
use gobby_client::ui::chrome::{Chrome, Mode};
use gobby_client::ui::dialogs::{render_dialog, CloseScope, CloseTarget, Dialog, RenameKind};
use gobby_client::ui::navigator::NavigatorState;
use gobby_client::ui::widgets::{action_button_row_rects, centered_popup_rect, ActionButtonSpec};
use gobby_client::ui::{render_workspace, Action};
use gobby_client::Workspace;
use ratatui::layout::Rect;
use ratatui::style::Modifier;
use serde_json::json;

use super::fixtures::{cell, rect_rows, render};
use super::token_map::theme;

/// Frame the dialog tests render into.
const AREA: Rect = Rect {
    x: 0,
    y: 0,
    width: 100,
    height: 30,
};

/// gclient `CONFIRM_CLOSE_POPUP_WIDTH` / `CONFIRM_CLOSE_POPUP_HEIGHT` (private
/// to `ui/dialogs.rs`), used to locate the popup's inner rows.
const CONFIRM_CLOSE_POPUP: (u16, u16) = (64, 6);

/// herdr's confirm-close acts on the sidebar's selected workspace. gclient's
/// sidebar rows are roster terminals, so its close scope is the terminal.
const CLOSE_TARGET: CloseTarget = CloseTarget::Terminal;

/// herdr `confirm_close_overlay_text(app, runtimes) -> (title, detail)`.
///
/// gclient derives no dialog text from workspace state: `Dialog::ConfirmClose`
/// carries the selected row's name as a caller-supplied `title` and its pane
/// count, and the renderer writes `Close <noun>?` over `<title> — <n> panes`.
/// herdr's display-name precedence (custom name, then live runtime cwd, then
/// terminal cwd, then identity cwd) therefore maps onto the name passed
/// here, and both rows are read back from the rendered popup.
fn confirm_close_overlay_text(name: &str, panes: usize) -> (String, String) {
    confirm_close_overlay_text_for(CLOSE_TARGET, name, CloseScope::Panes(panes))
}

/// `confirm_close_overlay_text` for any target and scope: a project closes
/// with its tab count, a project with worktree children as a group.
fn confirm_close_overlay_text_for(
    target: CloseTarget,
    name: &str,
    scope: CloseScope,
) -> (String, String) {
    let mut chrome = Chrome::new(theme());
    chrome.dialog = Some(Dialog::ConfirmClose {
        target,
        title: name.to_string(),
        scope,
    });
    let terminal = render(AREA.width, AREA.height, |frame| {
        render_dialog(frame, AREA, &chrome);
    });
    let popup = centered_popup_rect(AREA, CONFIRM_CLOSE_POPUP.0, CONFIRM_CLOSE_POPUP.1)
        .expect("confirm-close popup fits the test area");
    let inner = Rect::new(
        popup.x + 1,
        popup.y + 1,
        popup.width.saturating_sub(2),
        popup.height.saturating_sub(2),
    );
    let rows = rect_rows(&terminal, inner);
    (rows[0].trim().to_string(), rows[1].trim().to_string())
}

parity_tests! {
    "src/ui/dialogs.rs" => {
        fn confirm_close_text_uses_live_workspace_cwd_label() {
            // herdr: no custom name, identity cwd `/projects/original`, and the
            // attached terminal's live cwd `/projects/current`; the live cwd's
            // basename names the workspace. herdr's `workspace` noun is
            // gclient's `terminal` (plan 3.1 keymap/noun mapping).
            let (title, detail) = confirm_close_overlay_text("current", 1);

            assert_eq!(title, "Close terminal?");
            assert_eq!(detail, "current — 1 pane");
        }

        fn confirm_close_text_prefers_live_runtime_cwd_over_stale_terminal_cwd() {
            tokio::runtime::Builder::new_current_thread()
                .enable_all()
                .build()
                .expect("current-thread runtime")
                .block_on(async {
                    // herdr spawns a `/bin/sh` runtime in `<tmp>/current` while
                    // the terminal record still says `<tmp>/original`; the
                    // runtime's cwd wins. gclient terminals run in the daemon,
                    // so there is no local runtime to spawn.
                    let (_, detail) = confirm_close_overlay_text("current", 1);

                    assert_eq!(detail, "current — 1 pane");
                });
        }

        fn confirm_close_text_uses_selected_custom_name_instead_of_active_workspace_cwd() {
            // herdr: workspace `active` is active, workspace `selected` is the
            // sidebar selection with terminal cwd `/projects/current`; the
            // selected workspace's custom name wins over both.
            let (_, detail) = confirm_close_overlay_text("selected", 1);

            assert_eq!(detail, "selected — 1 pane");
        }

        // herdr's `main` is the repo checkout of a worktree space whose linked
        // worktree `issue` is also open, so closing the parent closes the
        // group and the dialog reports the group's scope. gclient: a project
        // with a worktree child closes as `CloseTarget::WorktreeGroup` (plan
        // 3.3, decision 10) and the scope counts the group's workspaces.
        fn confirm_close_text_reports_parent_group_scope() {
            let (title, detail) = confirm_close_overlay_text_for(
                CloseTarget::WorktreeGroup("project-main".into()),
                "main",
                CloseScope::Group {
                    workspaces: 2,
                    panes: 2,
                },
            );

            assert_eq!(title, "Close worktree group?");
            assert_eq!(detail, "main — 2 workspaces, 2 panes");
        }
    }
}

/// One bare key as the modal routers see it; the pane bytes never matter here.
fn key(code: KeyCode) -> KeyInput {
    KeyInput {
        key: KeyEvent::new(code, KeyModifiers::NONE),
        bytes: Vec::new(),
    }
}

fn press(ws: &Workspace, chrome: &mut Chrome, code: KeyCode) -> ModalOutcome {
    route_modal_key(ws, chrome, &key(code))
}

fn type_text(ws: &Workspace, chrome: &mut Chrome, text: &str) {
    for ch in text.chars() {
        assert_eq!(press(ws, chrome, KeyCode::Char(ch)), ModalOutcome::Consumed);
    }
}

fn left_click(column: u16, row: u16) -> MouseEvent {
    MouseEvent {
        kind: MouseEventKind::Down(MouseButton::Left),
        column,
        row,
        modifiers: KeyModifiers::NONE,
    }
}

/// The confirm-close dialog draws `↵ close` and `esc cancel` as buttons; a
/// left click on either must do what its key does. Before #22002 every mouse
/// event in `Mode::ConfirmClose` was swallowed, so the buttons were paint.
fn confirm_close_dialog(chrome: &mut Chrome) {
    chrome.dialog = Some(Dialog::ConfirmClose {
        target: CloseTarget::Tab,
        title: "%836".to_string(),
        scope: CloseScope::Panes(1),
    });
    chrome.mode = Mode::ConfirmClose;
}

/// `[close, cancel]` button rects the confirm-close renderer draws inside
/// the popup centred on `area`.
fn confirm_close_button_rects(area: Rect) -> Vec<Rect> {
    let popup = centered_popup_rect(area, CONFIRM_CLOSE_POPUP.0, CONFIRM_CLOSE_POPUP.1)
        .expect("confirm-close popup fits the area");
    let inner = Rect::new(
        popup.x + 1,
        popup.y + 1,
        popup.width.saturating_sub(2),
        popup.height.saturating_sub(2),
    );
    action_button_row_rects(
        inner,
        &[
            ActionButtonSpec {
                hint: Some("↵"),
                label: "close",
            },
            ActionButtonSpec {
                hint: Some("esc"),
                label: "cancel",
            },
        ],
        2,
        3,
    )
}

#[test]
fn confirm_close_buttons_take_clicks() {
    let ws = modal_workspace();
    let mut chrome = Chrome::new(theme());
    chrome.prefs.mouse_capture = true;
    confirm_close_dialog(&mut chrome);
    chrome.compute_view(&ws, AREA);
    let mut hits = None;
    render(AREA.width, AREA.height, |frame| {
        hits = Some(render_workspace(frame, &ws, &chrome));
    });
    chrome.view.apply_hits(hits.expect("frame drawn"));
    let rects = confirm_close_button_rects(chrome.view.terminal_area);
    let [close, cancel] = rects[..] else {
        panic!("two buttons drawn: {rects:?}");
    };

    let outcome = route_mouse(&ws, &mut chrome, &left_click(cancel.x + 1, cancel.y));
    assert_eq!(outcome, MouseOutcome::Handled);
    assert!(chrome.dialog.is_none(), "cancel dismisses the dialog");
    assert_eq!(chrome.mode, Mode::Terminal);

    confirm_close_dialog(&mut chrome);
    let outcome = route_mouse(&ws, &mut chrome, &left_click(close.x + 1, close.y));
    assert!(
        chrome.dialog.is_none(),
        "close dismisses the dialog: {outcome:?}"
    );
    assert_eq!(chrome.mode, Mode::Terminal);
    assert_eq!(
        outcome,
        MouseOutcome::Confirm(CloseTarget::Tab),
        "close asks the loop to act, as Enter does"
    );
}

/// Two roster terminals plus one blocked attention entry on the first.
fn modal_workspace() -> Workspace {
    let mut ws = Workspace::scripted();
    ws.daemon_mut().set_roster(json!({
        "epoch": "e1",
        "seq": 1,
        "entries": [{
            "entry_id": "run:term-alpha",
            "terminal": {"terminal_id": "term-alpha", "backend": "native"},
            "attention": {"attention_id": "att-1", "kind": "actionable"}
        }]
    }));
    ws.reconcile_subscribe_first().expect("scripted roster");
    ws.open_terminal("term-alpha", "native", "epoch")
        .expect("open term-alpha");
    ws.open_terminal("term-beta", "native", "epoch")
        .expect("open term-beta");
    ws
}

fn pane_rects(chrome: &Chrome, area: Rect) -> Vec<Rect> {
    chrome
        .active_tab()
        .expect("active tab")
        .layout
        .panes(area)
        .into_iter()
        .map(|pane| pane.rect)
        .collect()
}

fn confirm_close_terminal() -> Dialog {
    Dialog::ConfirmClose {
        target: CloseTarget::Terminal,
        title: "term-alpha".to_string(),
        scope: CloseScope::Panes(1),
    }
}

/// 4.1.1: every modal mode consumes its own keys and reports what the loop
/// must do next; Terminal mode passes keys through to the keymap.
#[test]
fn modal_keys_drive_every_mode() {
    let ws = modal_workspace();
    let alpha = ws.pane_for_terminal("term-alpha").expect("alpha pane");
    let beta = ws.pane_for_terminal("term-beta").expect("beta pane");
    let mut chrome = Chrome::new(theme());
    chrome.open_tab(alpha, "");

    assert_eq!(
        press(&ws, &mut chrome, KeyCode::Char('j')),
        ModalOutcome::Passthrough
    );
    chrome.mode = Mode::Copy;
    assert_eq!(
        press(&ws, &mut chrome, KeyCode::Esc),
        ModalOutcome::Passthrough
    );

    // KeybindHelp: `/` focuses the search, typing filters, esc backs out,
    // then esc closes the overlay.
    chrome.mode = Mode::KeybindHelp;
    assert_eq!(
        press(&ws, &mut chrome, KeyCode::Char('/')),
        ModalOutcome::Consumed
    );
    assert!(chrome.keybind_help.search_focused);
    type_text(&ws, &mut chrome, "spl");
    assert_eq!(chrome.keybind_help.query, "spl");
    assert_eq!(
        press(&ws, &mut chrome, KeyCode::Esc),
        ModalOutcome::Consumed
    );
    assert!(!chrome.keybind_help.search_focused);
    assert_eq!(press(&ws, &mut chrome, KeyCode::Esc), ModalOutcome::Close);
    assert_eq!(chrome.mode, Mode::Terminal);

    // Navigator: a query narrows the rows to term-beta and enter focuses it.
    chrome.mode = Mode::Navigator;
    chrome.navigator = NavigatorState::default();
    assert_eq!(
        press(&ws, &mut chrome, KeyCode::Char('/')),
        ModalOutcome::Consumed
    );
    type_text(&ws, &mut chrome, "bet");
    assert_eq!(chrome.navigator.query, "bet");
    assert_eq!(
        press(&ws, &mut chrome, KeyCode::Enter),
        ModalOutcome::Focus(beta)
    );
    assert_eq!(chrome.mode, Mode::Terminal);

    // Navigator: the attention row after both terminals answers with its
    // 1-based attention index so the loop can reuse `FocusAttention`.
    chrome.mode = Mode::Navigator;
    chrome.navigator = NavigatorState::default();
    assert_eq!(
        press(&ws, &mut chrome, KeyCode::Down),
        ModalOutcome::Consumed
    );
    assert_eq!(
        press(&ws, &mut chrome, KeyCode::Down),
        ModalOutcome::Consumed
    );
    assert_eq!(chrome.navigator.selected, 2);
    assert_eq!(
        press(&ws, &mut chrome, KeyCode::Enter),
        ModalOutcome::Action(Action::FocusAttention(1))
    );
    assert_eq!(chrome.mode, Mode::Terminal);

    // ConfirmClose: n cancels, y confirms the dialog's target.
    chrome.dialog = Some(confirm_close_terminal());
    chrome.mode = Mode::ConfirmClose;
    assert_eq!(
        press(&ws, &mut chrome, KeyCode::Char('n')),
        ModalOutcome::Close
    );
    assert_eq!(chrome.mode, Mode::Terminal);
    assert_eq!(chrome.dialog, None);
    chrome.dialog = Some(confirm_close_terminal());
    chrome.mode = Mode::ConfirmClose;
    assert_eq!(
        press(&ws, &mut chrome, KeyCode::Char('y')),
        ModalOutcome::Confirm(CloseTarget::Terminal)
    );
    assert_eq!(chrome.mode, Mode::Terminal);
    assert_eq!(chrome.dialog, None);

    // Rename: edits land at the cursor and enter commits the value.
    chrome.dialog = Some(Dialog::Rename {
        kind: RenameKind::Tab,
        value: "ab".to_string(),
        cursor: 2,
    });
    chrome.mode = Mode::Rename;
    assert_eq!(
        press(&ws, &mut chrome, KeyCode::Left),
        ModalOutcome::Consumed
    );
    assert_eq!(
        press(&ws, &mut chrome, KeyCode::Char('x')),
        ModalOutcome::Consumed
    );
    assert_eq!(
        press(&ws, &mut chrome, KeyCode::Enter),
        ModalOutcome::Commit(RenameKind::Tab, "axb".to_string())
    );
    assert_eq!(chrome.mode, Mode::Terminal);
    assert_eq!(chrome.dialog, None);

    // Resize: l moves the split under the focused pane; enter leaves the mode.
    chrome.open_pane(beta, "");
    let area = Rect::new(0, 0, 120, 40);
    let before = pane_rects(&chrome, area);
    chrome.mode = Mode::Resize;
    assert_eq!(
        press(&ws, &mut chrome, KeyCode::Char('l')),
        ModalOutcome::Consumed
    );
    assert_ne!(pane_rects(&chrome, area), before);
    assert_eq!(press(&ws, &mut chrome, KeyCode::Enter), ModalOutcome::Close);
    assert_eq!(chrome.mode, Mode::Terminal);

    // Navigate: down walks the project rows (none here, so the cursor stays
    // put) and enter leaves the mode; `parity::sidebar` covers the rows.
    chrome.mode = Mode::Navigate;
    chrome.sidebar.selected = 0;
    assert_eq!(
        press(&ws, &mut chrome, KeyCode::Down),
        ModalOutcome::Consumed
    );
    assert_eq!(chrome.sidebar.selected, 0);
    assert_eq!(
        press(&ws, &mut chrome, KeyCode::Enter),
        ModalOutcome::Consumed
    );
    assert_eq!(chrome.mode, Mode::Terminal);

    // Settings: enter toggles the selected row and queues the capture switch;
    // right steps the sidebar width; esc closes.
    chrome.mode = Mode::Settings;
    chrome.settings.selected = 0;
    assert_eq!(
        press(&ws, &mut chrome, KeyCode::Down),
        ModalOutcome::Consumed
    );
    assert_eq!(chrome.settings.selected, 1);
    assert_eq!(
        press(&ws, &mut chrome, KeyCode::Enter),
        ModalOutcome::Consumed
    );
    assert!(!chrome.prefs.mouse_capture);
    assert_eq!(chrome.pending_mouse_capture, Some(false));
    chrome.settings.selected = 7;
    let width = chrome.prefs.sidebar_width;
    assert_eq!(
        press(&ws, &mut chrome, KeyCode::Right),
        ModalOutcome::Consumed
    );
    assert_eq!(chrome.prefs.sidebar_width, width + 1);
    assert_eq!(chrome.sidebar.width, width + 1);
    assert_eq!(press(&ws, &mut chrome, KeyCode::Esc), ModalOutcome::Close);
    assert_eq!(chrome.mode, Mode::Terminal);
}

/// 4.1.3: a click on a settings row selects and activates it, the wheel moves
/// the selection, and a click outside the popup closes it.
#[test]
fn settings_rows_respond_to_clicks() {
    let ws = modal_workspace();
    let alpha = ws.pane_for_terminal("term-alpha").expect("alpha pane");
    let mut chrome = Chrome::new(theme());
    chrome.open_tab(alpha, "");
    chrome.mode = Mode::Settings;
    let area = Rect::new(0, 0, 100, 30);
    chrome.compute_view(&ws, area);
    let mut hits = None;
    render(area.width, area.height, |frame| {
        hits = Some(render_workspace(frame, &ws, &chrome));
    });
    chrome.view.apply_hits(hits.expect("frame drawn"));
    let rows = chrome.view.settings_row_hit_areas.clone();
    let row = |index: usize| {
        rows.iter()
            .find(|(row, _)| *row == index)
            .map(|(_, rect)| *rect)
            .expect("settings row drawn")
    };

    let borders = row(2);
    let borders_before = chrome.prefs.pane_borders;
    assert_eq!(
        route_mouse(&ws, &mut chrome, &left_click(borders.x + 2, borders.y)),
        MouseOutcome::Handled
    );
    assert_eq!(chrome.settings.selected, 2);
    assert_eq!(chrome.prefs.pane_borders, !borders_before);

    let theme_row = row(0);
    route_mouse(&ws, &mut chrome, &left_click(theme_row.x + 2, theme_row.y));
    assert_eq!(chrome.settings.selected, 0);
    assert_eq!(chrome.prefs.theme, "light");

    let wheel = MouseEvent {
        kind: MouseEventKind::ScrollDown,
        column: theme_row.x + 2,
        row: theme_row.y,
        modifiers: KeyModifiers::NONE,
    };
    assert_eq!(route_mouse(&ws, &mut chrome, &wheel), MouseOutcome::Handled);
    assert_eq!(chrome.settings.selected, 1);

    assert_eq!(
        route_mouse(&ws, &mut chrome, &left_click(0, 0)),
        MouseOutcome::Handled
    );
    assert_eq!(chrome.mode, Mode::Terminal);
}

/// 5.2.1: the context menu popup sits at its anchor, flips left and up when
/// the anchor is too near the frame's right and bottom edges, carries the
/// selected and disabled rows by weight and reversal over an undimmed
/// workspace, and hands the rows it drew back for `menu_hit`.
#[test]
fn context_menu_renders_anchored_and_clamped() {
    let ws = modal_workspace();
    let alpha = ws.pane_for_terminal("term-alpha").expect("alpha pane");
    let mut chrome = Chrome::new(theme());
    chrome.open_tab(alpha, "");
    chrome.compute_view(&ws, AREA);
    let (accent, text, overlay0) = (
        chrome.palette.accent,
        chrome.palette.text,
        chrome.palette.overlay0,
    );
    let item = |label: &'static str, enabled: bool| MenuItem {
        label,
        action: MenuAction::Act(Action::NewTab),
        enabled,
    };
    let items = vec![
        item("rename pane", true),
        item("swap with focused pane", false),
        item("close pane", true),
    ];
    // `longest label + 4` wide and `items + 2` tall.
    let (width, height) = (26, 5);
    let open_at = |chrome: &mut Chrome, anchor: (u16, u16)| {
        chrome.mode = Mode::ContextMenu;
        chrome.menu = Some(ContextMenuState {
            kind: ContextMenuKind::Global,
            anchor,
            items: items.clone(),
            selected: 0,
            item_rects: Vec::new(),
        });
    };
    let draw = |chrome: &mut Chrome| {
        let mut hits = None;
        let terminal = render(AREA.width, AREA.height, |frame| {
            hits = Some(render_workspace(frame, &ws, chrome));
        });
        chrome.apply_hits(hits.expect("frame drawn"));
        terminal
    };
    let drawn_rows = |popup: Rect| -> Vec<Rect> {
        (0..3)
            .map(|index| Rect::new(popup.x + 1, popup.y + 1 + index, width - 2, 1))
            .collect()
    };
    let popup_rows = vec![
        format!("┌{}┐", "─".repeat(24)),
        format!("│{:<24}│", " rename pane"),
        format!("│{:<24}│", " swap with focused pane"),
        format!("│{:<24}│", " close pane"),
        format!("└{}┘", "─".repeat(24)),
    ];

    // At the anchor: the popup's top-left corner is the click cell.
    let anchor = (20, 6);
    open_at(&mut chrome, anchor);
    let terminal = draw(&mut chrome);
    let popup = Rect::new(anchor.0, anchor.1, width, height);
    assert_eq!(
        rect_rows(&terminal, popup),
        popup_rows,
        "popup at the anchor"
    );
    let menu = chrome.menu.as_ref().expect("menu stays open");
    assert_eq!(
        menu.item_rects,
        drawn_rows(popup),
        "item rects follow the drawn rows"
    );
    let selected = cell(&terminal, popup.x + 2, popup.y + 1);
    assert_eq!(selected.fg, accent);
    assert!(
        selected
            .modifier
            .contains(Modifier::REVERSED | Modifier::BOLD),
        "selected row is reversed and bold: {:?}",
        selected.modifier
    );
    let disabled = cell(&terminal, popup.x + 2, popup.y + 2);
    assert_eq!(disabled.fg, overlay0);
    assert!(disabled.modifier.contains(Modifier::DIM));
    assert!(!disabled.modifier.contains(Modifier::REVERSED));
    let plain = cell(&terminal, popup.x + 2, popup.y + 3);
    assert_eq!(plain.fg, text);
    assert_eq!(plain.modifier, Modifier::empty());
    let far = chrome.view.terminal_area;
    assert!(
        !cell(&terminal, far.right() - 2, far.bottom() - 2)
            .modifier
            .contains(Modifier::DIM),
        "the workspace under a menu is not dimmed"
    );

    // Near the far edges the popup flips so the click cell is its
    // bottom-right corner.
    let anchor = (AREA.width - 3, AREA.height - 2);
    open_at(&mut chrome, anchor);
    let terminal = draw(&mut chrome);
    let popup = Rect::new(anchor.0 + 1 - width, anchor.1 + 1 - height, width, height);
    assert_eq!(
        rect_rows(&terminal, popup),
        popup_rows,
        "popup flipped from the anchor"
    );
    assert_eq!(
        cell(&terminal, anchor.0, anchor.1).symbol(),
        "┘",
        "the click cell is the flipped popup's corner"
    );
    let menu = chrome.menu.as_ref().expect("menu stays open");
    assert_eq!(
        menu.item_rects,
        drawn_rows(popup),
        "item rects follow the flipped rows"
    );
}
