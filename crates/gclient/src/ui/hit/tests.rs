//! `hit_test` over frames the real renderers drew.

use super::{hit_test, Hit, SidebarSection};
use crate::app::Workspace;
use crate::daemon::{Checkout, ProjectRow, SidebarRows, WorktreeRow};
use crate::ui::chrome::{Chrome, Mode, ViewState};
use crate::ui::dialogs::{CloseScope, CloseTarget, Dialog};
use crate::ui::render_workspace;
use crate::ui::status::{Toast, ToastKind};
use ratatui::backend::TestBackend;
use ratatui::layout::{Position, Rect};
use ratatui::Terminal;
use serde_json::json;

const WIDTH: u16 = 120;
const HEIGHT: u16 = 40;

/// `tests/screens.rs::split_live`: two live panes split in the first tab, a
/// second tab behind them, one attention prompt on `term-alpha`, and the
/// focused pane (`term-beta`) given scrollback so its scrollbar lane is drawn.
fn split_live() -> (Workspace, Chrome) {
    let mut ws = Workspace::scripted();
    ws.daemon_mut().set_sidebar_rows(SidebarRows {
        projects: vec![ProjectRow {
            id: "proj-alpha".to_string(),
            name: "alpha".to_string(),
            display_name: "alpha".to_string(),
            checkout: Some(Checkout {
                machine_id: "local".to_string(),
                root_path: "/repos/alpha".to_string(),
            }),
            ..ProjectRow::default()
        }],
        worktrees: vec![WorktreeRow {
            id: "wt-1".to_string(),
            project_id: "proj-alpha".to_string(),
            branch_name: Some("worktree/feature".to_string()),
            worktree_path: "/repos/alpha/.worktrees/feature".to_string(),
            status: "active".to_string(),
            workspace_role: "task".to_string(),
            ..WorktreeRow::default()
        }],
        ..SidebarRows::default()
    });
    ws.daemon_mut().set_roster(json!({
        "epoch": "e1",
        "seq": 1,
        "entries": [{
            "entry_id": "run:term-alpha",
            "terminal": {"terminal_id": "term-alpha", "backend": "native"},
            "attention": {"attention_id": "att-1", "kind": "actionable"}
        }]
    }));
    ws.select_project("proj-alpha");
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
    chrome.sidebar.toggle_group("proj-alpha");
    chrome.open_pane(alpha, "alpha");
    chrome.open_pane(beta, "alpha");
    chrome.open_tab(alpha, "second");
    chrome.activate_tab(0);
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

    // Sidebar: divider column, band controls, toggle, rows, then bare cells.
    let sidebar = view.sidebar_rect;
    let divider_x = view.sidebar_divider_x.expect("sidebar divider");
    assert_eq!(at(view, divider_x, sidebar.y), Hit::SidebarDivider);
    for (section, rect) in SidebarSection::ALL.iter().zip(view.sidebar_section_rects) {
        assert!(rect.height > 0, "{section:?} drawn");
        assert_eq!(
            at(view, rect.x, rect.y),
            Hit::SidebarEmpty,
            "{section:?} band"
        );
    }
    let filter = view
        .projects_filter_hit_area
        .expect("projects filter drawn");
    assert_eq!(at(view, filter.x, filter.y), Hit::ProjectsFilter);
    let view_control = view.sessions_view_hit_area.expect("sessions view drawn");
    assert_eq!(at(view, view_control.x, view_control.y), Hit::SessionsView);
    let toggle = view.sidebar_toggle_hit_area.expect("toggle drawn");
    assert_eq!(at(view, toggle.x, toggle.y), Hit::SidebarToggle);
    let (id, rect) = view.machine_hit_areas.first().expect("machine row");
    assert_eq!(at(view, rect.x, rect.y), Hit::Machine(id.clone()));
    let (id, rect) = view.project_hit_areas.first().expect("project card");
    assert_eq!(id, "proj-alpha");
    assert_eq!(at(view, rect.x, rect.y), Hit::Project(id.clone()));
    let (id, rect) = view.group_toggle_hit_areas.first().expect("group toggle");
    assert_eq!(at(view, rect.x, rect.y), Hit::GroupToggle(id.clone()));
    let (id, rect) = view.worktree_hit_areas.first().expect("worktree row");
    assert_eq!(id, "wt-1");
    assert_eq!(at(view, rect.x, rect.y), Hit::Worktree(id.clone()));
    let new = view.projects_new_hit_area.expect("new button");
    assert_eq!(at(view, new.x, new.y), Hit::ProjectsNew);
    let menu = view.projects_menu_hit_area.expect("menu button");
    assert_eq!(at(view, menu.x, menu.y), Hit::ProjectsMenu);
    let (entry, rect) = view.agent_hit_areas.first().expect("agent row");
    assert_eq!(entry, "run:term-alpha");
    assert_eq!(at(view, rect.x, rect.y), Hit::Agent(entry.clone()));
    assert_eq!(at(view, rect.x, rect.y + 1), Hit::Agent(entry.clone()));
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
    // One project card never overflows a 40-row sidebar, so no lane is drawn
    // and the cells beside the rows stay plain sidebar.
    assert_eq!(chrome.view.sidebar_scrollbar_hit_areas, [None; 3]);

    // A lane the renderer reports maps to its section, row by row.
    let lane = Rect::new(24, 7, 1, 3);
    chrome.view.sidebar_scrollbar_hit_areas[SidebarSection::Projects.index()] = Some(lane);
    let hit = Hit::SidebarScrollbar {
        section: SidebarSection::Projects,
        row: lane.y + 2,
    };
    assert_eq!(hit_test(&chrome.view, lane.x, lane.y + 2), hit);
}

#[test]
fn dialog_buttons_hit_first_and_clear_with_the_dialog() {
    let (ws, mut chrome) = split_live();
    chrome.dialog = Some(Dialog::ConfirmClose {
        target: CloseTarget::Tab,
        title: "alpha".to_string(),
        scope: CloseScope::Panes(2),
    });
    chrome.mode = Mode::ConfirmClose;
    rendered(&ws, &mut chrome);
    let view = &chrome.view;

    let buttons = &view.dialog_button_hit_areas;
    assert_eq!(buttons.len(), 2, "close and cancel drawn: {buttons:?}");
    for (index, rect) in buttons.iter().enumerate() {
        assert_eq!(at(view, rect.x, rect.y), Hit::DialogButton(index));
        assert_eq!(at(view, rect.right() - 1, rect.y), Hit::DialogButton(index));
    }
    // Beside a button the dialog sits over a pane, which is what the map
    // still reports there.
    let close = buttons[0];
    assert!(matches!(
        at(view, close.x, close.y - 1),
        Hit::Pane { .. } | Hit::PaneBorder(_)
    ));

    chrome.dialog = None;
    chrome.mode = Mode::Terminal;
    rendered(&ws, &mut chrome);
    assert!(chrome.view.dialog_button_hit_areas.is_empty());
    assert!(!matches!(
        at(&chrome.view, close.x, close.y),
        Hit::DialogButton(_)
    ));
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
    assert_eq!(
        view.dialog_button_hit_areas.len(),
        2,
        "done and close drawn"
    );
    for (index, button) in view.dialog_button_hit_areas.iter().enumerate() {
        assert!(dialog.contains(Position::new(button.x, button.y)));
        assert_eq!(at(view, button.x, button.y), Hit::DialogButton(index));
    }
    let toast = view.toast_hit_area.expect("toast drawn");
    assert_eq!(at(view, toast.x, toast.y), Hit::Toast);
    let bar = view.tab_bar_rect.expect("tab bar drawn");
    let (_, first_tab) = view.tab_hit_areas.first().expect("first tab");
    assert_eq!(at(view, first_tab.x, bar.y), Hit::Tab(0));
}
