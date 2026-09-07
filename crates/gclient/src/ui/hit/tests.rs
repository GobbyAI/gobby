//! `hit_test` over frames the real renderers drew.

use super::{hit_test, Hit, SidebarSection};
use crate::app::Workspace;
use crate::ui::chrome::{Chrome, Mode, ViewState};
use crate::ui::render_workspace;
use crate::ui::status::{Toast, ToastKind};
use ratatui::backend::TestBackend;
use ratatui::layout::Rect;
use ratatui::Terminal;
use serde_json::json;

const WIDTH: u16 = 120;
const HEIGHT: u16 = 40;

/// `tests/screens.rs::split_live`: two live panes split in the first tab, a
/// second tab behind them, one attention prompt on `term-alpha`, and the
/// focused pane (`term-beta`) given scrollback so its scrollbar lane is drawn.
fn split_live() -> (Workspace, Chrome) {
    let mut ws = Workspace::scripted();
    ws.daemon_mut().set_roster(json!({
        "epoch": "e1",
        "seq": 1,
        "entries": [{"entry_id": "run:term-alpha", "kind": "blocked"}]
    }));
    ws.reconcile_subscribe_first().expect("install roster");
    for terminal_id in ["term-alpha", "term-beta", "term-gamma"] {
        ws.open_terminal(terminal_id, "native", "epoch")
            .expect("open terminal");
    }
    let alpha = ws.pane_for_terminal("term-alpha").expect("term-alpha pane");
    let beta = ws.pane_for_terminal("term-beta").expect("term-beta pane");
    ws.reattach_frames(beta).expect("reattach term-beta");
    ws.apply_scroll_applied(beta, 0, 50);

    let mut chrome = Chrome::dark();
    chrome.open_pane(alpha, "alpha");
    chrome.open_pane(beta, "alpha");
    chrome.open_tab(alpha, "second");
    chrome.active_tab = 0;
    assert!(chrome.focus_pane(beta), "focus term-beta");
    (ws, chrome)
}

/// Draw the frame the way the run loop does and write the hits back.
fn rendered(ws: &Workspace, chrome: &mut Chrome) {
    let area = Rect::new(0, 0, WIDTH, HEIGHT);
    chrome.compute_view(ws, area);
    let mut terminal = Terminal::new(TestBackend::new(WIDTH, HEIGHT)).expect("test backend");
    let mut hits = None;
    terminal
        .draw(|frame| {
            hits = Some(render_workspace(frame, ws, chrome));
        })
        .expect("draw frame");
    chrome.view.apply_hits(hits.expect("frame drawn"));
}

fn at(view: &ViewState, x: u16, y: u16) -> Hit {
    hit_test(view, x, y)
}

#[test]
fn hit_test_covers_split_live_layout() {
    let (ws, mut chrome) = split_live();
    rendered(&ws, &mut chrome);
    let view = &chrome.view;

    // Tab bar: both tabs, the new-tab button, then the bare bar.
    let bar = view.tab_bar_rect.expect("tab bar drawn");
    assert_eq!(view.tab_hit_areas.len(), 2);
    for (index, rect) in &view.tab_hit_areas {
        assert_eq!(at(view, rect.x, bar.y), Hit::Tab(*index));
    }
    let new_tab = view.new_tab_hit_area.expect("new-tab button drawn");
    assert_eq!(at(view, new_tab.x, new_tab.y), Hit::NewTab);
    assert_eq!(at(view, bar.right() - 1, bar.y), Hit::TabBarEmpty);

    // Sidebar: divider column, section rule, toggle, rows, then bare cells.
    let sidebar = view.sidebar_rect;
    let divider_x = view.sidebar_divider_x.expect("sidebar divider");
    assert_eq!(at(view, divider_x, sidebar.y), Hit::SidebarDivider);
    let section_y = view.sidebar_section_divider_y.expect("section divider");
    assert_eq!(at(view, sidebar.x, section_y), Hit::SidebarSectionDivider);
    let toggle = view.sidebar_toggle_hit_area.expect("toggle drawn");
    assert_eq!(at(view, toggle.x, toggle.y), Hit::SidebarToggle);
    assert_eq!(view.roster_hit_areas.len(), 3);
    for (id, rect) in &view.roster_hit_areas {
        assert_eq!(at(view, rect.x, rect.y), Hit::Roster(id.clone()));
    }
    let (entry, rect) = view.attention_hit_areas.first().expect("attention row");
    assert_eq!(entry, "run:term-alpha");
    assert_eq!(at(view, rect.x, rect.y), Hit::Attention(entry.clone()));
    assert_eq!(at(view, sidebar.x, sidebar.y), Hit::SidebarEmpty);

    // Panes: content offsets, the hidden gutter, the frame, the split, and the
    // focused pane's scrollbar lane.
    assert_eq!(view.pane_infos.len(), 2);
    let alpha = view
        .pane_infos
        .iter()
        .find(|info| !info.is_focused)
        .expect("unfocused pane");
    let beta = view
        .pane_infos
        .iter()
        .find(|info| info.is_focused)
        .expect("focused pane");
    let inner = alpha.inner_rect;
    let origin = Hit::Pane {
        slot: alpha.id,
        col: 0,
        row: 0,
    };
    assert_eq!(at(view, inner.x, inner.y), origin);
    let corner = Hit::Pane {
        slot: alpha.id,
        col: inner.width - 1,
        row: inner.height - 1,
    };
    assert_eq!(at(view, inner.right() - 1, inner.bottom() - 1), corner);
    assert_eq!(alpha.scrollbar_rect, None);
    assert_eq!(at(view, inner.right(), inner.y), Hit::PaneBorder(alpha.id));
    assert_eq!(
        at(view, alpha.rect.x, alpha.rect.y),
        Hit::PaneBorder(alpha.id)
    );
    let split = view.split_borders.first().expect("split border");
    let row = view.terminal_area.y + 1;
    assert_eq!(at(view, split.pos, row), Hit::SplitBorder(0));
    assert_eq!(at(view, split.pos - 1, row), Hit::SplitBorder(0));
    let lane = beta.scrollbar_rect.expect("focused pane scrollbar");
    let scrollbar = Hit::PaneScrollbar {
        slot: beta.id,
        row: lane.y,
    };
    assert_eq!(at(view, lane.x, lane.y), scrollbar);

    // Status line: the control indicator, then the rest of the row.
    let status = view.status_rect;
    let indicator = view.control_indicator_hit_area.expect("indicator drawn");
    assert_eq!(at(view, indicator.x, status.y), Hit::ControlIndicator);
    assert_eq!(at(view, indicator.right(), status.y), Hit::Status);

    // Nothing inside the frame is unowned; everything outside is.
    assert_eq!(at(view, WIDTH, 0), Hit::Empty);
    assert_eq!(at(view, 0, HEIGHT), Hit::Empty);
    let unowned: Vec<(u16, u16)> = (0..HEIGHT)
        .flat_map(|y| (0..WIDTH).map(move |x| (x, y)))
        .filter(|(x, y)| at(view, *x, *y) == Hit::Empty)
        .collect();
    assert!(unowned.is_empty(), "unowned cells: {unowned:?}");
}

#[test]
fn sidebar_scrollbar_lane_hits_by_section() {
    let (ws, mut chrome) = split_live();
    rendered(&ws, &mut chrome);
    // Three roster rows never overflow a 40-row sidebar, so no lane is drawn
    // and the cells beside the rows stay plain sidebar.
    assert_eq!(chrome.view.roster_scrollbar_hit_area, None);
    assert_eq!(chrome.view.attention_scrollbar_hit_area, None);

    // A lane the renderer reports maps to its section, row by row.
    let lane = Rect::new(24, 2, 1, 3);
    chrome.view.roster_scrollbar_hit_area = Some(lane);
    let hit = Hit::SidebarScrollbar {
        section: SidebarSection::Roster,
        row: lane.y + 2,
    };
    assert_eq!(hit_test(&chrome.view, lane.x, lane.y + 2), hit);
}

#[test]
fn settings_and_toast_hits_take_precedence() {
    let (ws, mut chrome) = split_live();
    chrome.mode = Mode::Settings;
    chrome.toast = Some(Toast {
        kind: ToastKind::Success,
        title: "done".to_string(),
        body: None,
        target: None,
    });
    rendered(&ws, &mut chrome);
    let view = &chrome.view;

    let dialog = view.settings_dialog_area.expect("settings popup drawn");
    assert!(!view.settings_row_hit_areas.is_empty());
    for (index, rect) in &view.settings_row_hit_areas {
        assert_eq!(at(view, rect.x, rect.y), Hit::SettingsRow(*index));
    }
    assert_eq!(at(view, dialog.x, dialog.y), Hit::SettingsDialog);
    let toast = view.toast_hit_area.expect("toast drawn");
    assert_eq!(at(view, toast.x, toast.y), Hit::Toast);
    let bar = view.tab_bar_rect.expect("tab bar drawn");
    let (_, first_tab) = view.tab_hit_areas.first().expect("first tab");
    assert_eq!(at(view, first_tab.x, bar.y), Hit::Tab(0));
}
