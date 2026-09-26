mod mock_daemon;

use gobby_client::app::{
    apply_live_menu_action, build_menu, plan_arrange, sync_live_chrome, ArrangeLayout,
    ContextMenuKind, MenuAction, ModalOutcome,
};
use gobby_client::daemon::{LayoutAxis, LayoutNode, LiveDaemon, WorkspaceOp};
use gobby_client::ui::dialogs::{render_dialog, Dialog};
use gobby_client::ui::menu_bar::MenuBarMenu;
use gobby_client::ui::status::ToastKind;
use gobby_client::ui::{Chrome, Mode};
use gobby_client::Workspace;
use mock_daemon::MockDaemon;
use ratatui::backend::TestBackend;
use ratatui::crossterm::event::{KeyCode, KeyEvent};
use ratatui::layout::Rect;
use ratatui::Terminal;
use serde_json::json;

fn panes(count: usize) -> Vec<String> {
    (0..count).map(|index| format!("pane-{index}")).collect()
}

fn move_beside(pane: usize, beside: usize, axis: LayoutAxis) -> WorkspaceOp {
    WorkspaceOp::PaneMove {
        pane: format!("pane-{pane}"),
        tab: "tab-1".into(),
        beside: Some(format!("pane-{beside}")),
        axis: Some(axis),
        node: None,
    }
}

fn resize(pane: usize, ratio: f64) -> WorkspaceOp {
    WorkspaceOp::PaneResize {
        pane: format!("pane-{pane}"),
        ratio,
        node: None,
    }
}

#[test]
fn each_layout_plans_moves_then_ratios_for_three_panes() {
    let panes = panes(3);
    let cases = [
        (
            ArrangeLayout::EvenHorizontal,
            vec![
                move_beside(1, 0, LayoutAxis::Horizontal),
                move_beside(2, 1, LayoutAxis::Horizontal),
                resize(0, 1.0 / 3.0),
                resize(1, 0.5),
            ],
        ),
        (
            ArrangeLayout::EvenVertical,
            vec![
                move_beside(1, 0, LayoutAxis::Vertical),
                move_beside(2, 1, LayoutAxis::Vertical),
                resize(0, 1.0 / 3.0),
                resize(1, 0.5),
            ],
        ),
        (
            ArrangeLayout::MainVertical,
            vec![
                move_beside(1, 0, LayoutAxis::Horizontal),
                move_beside(2, 1, LayoutAxis::Vertical),
                resize(0, 0.5),
                resize(1, 0.5),
            ],
        ),
        (
            ArrangeLayout::MainHorizontal,
            vec![
                move_beside(1, 0, LayoutAxis::Vertical),
                move_beside(2, 1, LayoutAxis::Horizontal),
                resize(0, 0.5),
                resize(1, 0.5),
            ],
        ),
        (
            ArrangeLayout::Tiled,
            vec![
                move_beside(2, 0, LayoutAxis::Vertical),
                resize(0, 0.5),
                move_beside(1, 0, LayoutAxis::Horizontal),
                resize(0, 0.5),
            ],
        ),
    ];
    for (layout, expected) in cases {
        assert_eq!(
            plan_arrange(layout, "tab-1", &panes),
            expected,
            "{layout:?}"
        );
    }
}

#[test]
fn tiled_uses_ceil_sqrt_rows_and_even_ratios() {
    assert_eq!(
        plan_arrange(ArrangeLayout::Tiled, "tab-1", &panes(5)),
        vec![
            move_beside(2, 0, LayoutAxis::Vertical),
            move_beside(4, 2, LayoutAxis::Vertical),
            resize(0, 1.0 / 3.0),
            resize(2, 0.5),
            move_beside(1, 0, LayoutAxis::Horizontal),
            move_beside(3, 2, LayoutAxis::Horizontal),
            resize(0, 0.5),
            resize(2, 0.5),
        ]
    );
}

#[tokio::test]
async fn a_single_pane_tab_plans_nothing_and_says_so() {
    assert!(plan_arrange(ArrangeLayout::EvenHorizontal, "tab-1", &panes(1)).is_empty());
    let mock = MockDaemon::start("local-token").await;
    mock.seed_workspace("project-1", &[(&["only"], "only")]);
    let page = json!({
        "items": [{"terminal_id": "only", "backend": "native", "state": "live"}],
        "next_cursor": null,
        "snapshot": {"daemon_epoch": "arrange-epoch", "seq": 1},
    });
    for _ in 0..3 {
        mock.enqueue("GET", "/api/terminals?", 200, page.clone());
    }
    let daemon = LiveDaemon::connect(mock.url(), "local-token")
        .await
        .expect("connect isolated daemon");
    let mut workspace = Workspace::live(daemon);
    let home = tempfile::tempdir().expect("isolated gobby home");
    workspace.set_gobby_home(home.path().to_path_buf());
    workspace.select_project("project-1");
    workspace
        .reconcile_subscribe_first()
        .await
        .expect("load seeded workspace");
    let mut chrome = Chrome::dark();
    sync_live_chrome(&mut workspace, &mut chrome);
    assert!(!chrome.active_tab().expect("seeded tab").is_local());

    apply_live_menu_action(
        &mut workspace,
        &mut chrome,
        ContextMenuKind::MenuBar(MenuBarMenu::Window),
        MenuAction::Arrange(ArrangeLayout::EvenHorizontal),
    )
    .await
    .expect("single pane arrange");
    let toast = &chrome.toasts.last().expect("one-pane notice").toast;
    assert_eq!(toast.kind, ToastKind::Info);
    assert_eq!(toast.title, "Nothing to arrange: one pane");

    let pane = workspace.pane_for_terminal("only").expect("seeded pane");
    chrome.open_tab(pane, "local pane");
    apply_live_menu_action(
        &mut workspace,
        &mut chrome,
        ContextMenuKind::MenuBar(MenuBarMenu::Window),
        MenuAction::Arrange(ArrangeLayout::EvenHorizontal),
    )
    .await
    .expect("local arrange");
    let toast = &chrome.toasts.last().expect("local-tab notice").toast;
    assert_eq!(toast.kind, ToastKind::Warning);
    assert!(toast.title.contains("local"));
    assert!(mock
        .workspace_requests()
        .iter()
        .all(|request| !matches!(request["op"].as_str(), Some("pane.move" | "pane.resize"))));
}

#[test]
fn arrange_entries_appear_in_window_and_pane_menus() {
    let mut workspace = Workspace::scripted();
    let pane = workspace
        .open_terminal("menu-pane", "native", "epoch-menu")
        .expect("scripted terminal");
    let mut chrome = Chrome::dark();
    chrome.open_tab(pane, "menu tab");
    let expected = [
        "arrange: even horizontal",
        "arrange: even vertical",
        "arrange: main horizontal",
        "arrange: main vertical",
        "arrange: tiled",
        "new grid…",
    ];
    let window = build_menu(
        &workspace,
        &chrome,
        ContextMenuKind::MenuBar(MenuBarMenu::Window),
        (0, 0),
    );
    let window_labels: Vec<_> = window.items.iter().map(|item| item.label).collect();
    assert!(window_labels.ends_with(&expected), "{window_labels:?}");

    let pane_menu = build_menu(&workspace, &chrome, ContextMenuKind::Pane(pane), (0, 0));
    let pane_labels: Vec<_> = pane_menu.items.iter().map(|item| item.label).collect();
    let zoom = pane_labels
        .iter()
        .position(|label| *label == "zoom")
        .expect("pane zoom item");
    assert!(
        pane_labels[zoom + 1..].starts_with(&expected),
        "{pane_labels:?}"
    );
    assert!(matches!(
        window.items.last().map(|item| &item.action),
        Some(MenuAction::OpenNewGrid)
    ));
}

#[test]
fn new_grid_dialog_clamps_to_one_through_four_and_confirms_dims() {
    let mut chrome = Chrome::dark();
    chrome.mode = Mode::ProjectDialog;
    chrome.dialog = Some(Dialog::NewGrid { rows: 2, cols: 2 });
    let mut terminal = Terminal::new(TestBackend::new(80, 24)).expect("test terminal");
    terminal
        .draw(|frame| {
            render_dialog(frame, Rect::new(0, 0, 80, 24), &chrome);
        })
        .expect("render grid preview");
    let buffer = terminal.backend().buffer();
    assert_eq!(buffer[(18, 5)].symbol(), "┌");
    assert_eq!(buffer[(61, 18)].symbol(), "┘");
    let visible: String = buffer.content().iter().map(|cell| cell.symbol()).collect();
    assert!(visible.contains("2 × 2 shells"), "{visible}");
    assert!(visible.contains("←→ columns · ↑↓ rows"), "{visible}");
    assert!(visible.contains("enter create · esc cancel"), "{visible}");

    for _ in 0..8 {
        gobby_client::app::project_dialog_key(&mut chrome, &KeyEvent::from(KeyCode::Left));
        gobby_client::app::project_dialog_key(&mut chrome, &KeyEvent::from(KeyCode::Up));
    }
    assert!(matches!(
        chrome.dialog,
        Some(Dialog::NewGrid { rows: 1, cols: 1 })
    ));
    for _ in 0..8 {
        gobby_client::app::project_dialog_key(&mut chrome, &KeyEvent::from(KeyCode::Right));
        gobby_client::app::project_dialog_key(&mut chrome, &KeyEvent::from(KeyCode::Down));
    }
    assert!(matches!(
        chrome.dialog,
        Some(Dialog::NewGrid { rows: 4, cols: 4 })
    ));
    assert_eq!(
        gobby_client::app::project_dialog_key(&mut chrome, &KeyEvent::from(KeyCode::Enter)),
        ModalOutcome::Menu {
            kind: ContextMenuKind::Global,
            action: MenuAction::NewGrid { rows: 4, cols: 4 },
        }
    );
}

#[tokio::test]
async fn new_grid_menu_opens_dialog() {
    let mock = MockDaemon::start("local-token").await;
    let daemon = LiveDaemon::connect(mock.url(), "local-token")
        .await
        .expect("connect isolated daemon");
    let mut workspace = Workspace::live(daemon);
    let mut chrome = Chrome::dark();
    apply_live_menu_action(
        &mut workspace,
        &mut chrome,
        ContextMenuKind::MenuBar(MenuBarMenu::Window),
        MenuAction::OpenNewGrid,
    )
    .await
    .expect("open new grid");
    assert_eq!(chrome.mode, Mode::ProjectDialog);
    assert!(matches!(
        chrome.dialog,
        Some(Dialog::NewGrid { rows: 2, cols: 2 })
    ));
}

fn cell_sizes(node: &LayoutNode, width: f64, height: f64, out: &mut Vec<(f64, f64)>) {
    match node {
        LayoutNode::Pane { .. } => out.push((width, height)),
        LayoutNode::Split {
            axis,
            ratio,
            children,
        } => match axis {
            LayoutAxis::Horizontal => {
                cell_sizes(&children[0], width * ratio, height, out);
                cell_sizes(&children[1], width * (1.0 - ratio), height, out);
            }
            LayoutAxis::Vertical => {
                cell_sizes(&children[0], width, height * ratio, out);
                cell_sizes(&children[1], width, height * (1.0 - ratio), out);
            }
        },
    }
}

#[tokio::test]
async fn create_grid_spawns_rows_times_cols_shells_and_evens_them() {
    let mock = MockDaemon::start("local-token").await;
    mock.seed_workspace("project-1", &[(&["existing"], "existing")]);
    let page = json!({
        "items": [{"terminal_id": "existing", "backend": "native", "state": "live"}],
        "next_cursor": null,
        "snapshot": {"daemon_epoch": "grid-epoch", "seq": 1},
    });
    for _ in 0..3 {
        mock.enqueue("GET", "/api/terminals?", 200, page.clone());
    }
    let daemon = LiveDaemon::connect(mock.url(), "local-token")
        .await
        .expect("connect isolated daemon");
    let mut workspace = Workspace::live(daemon);
    let home = tempfile::tempdir().expect("isolated gobby home");
    workspace.set_gobby_home(home.path().to_path_buf());
    workspace.select_project("project-1");
    workspace
        .reconcile_subscribe_first()
        .await
        .expect("load seeded workspace");
    let workspace_id = workspace
        .workspace_model()
        .expect("loaded workspace")
        .workspace
        .id
        .clone();
    let mut chrome = Chrome::dark();
    sync_live_chrome(&mut workspace, &mut chrome);

    apply_live_menu_action(
        &mut workspace,
        &mut chrome,
        ContextMenuKind::MenuBar(MenuBarMenu::Window),
        MenuAction::NewGrid { rows: 2, cols: 3 },
    )
    .await
    .expect("create owned grid");

    let operations = mock.workspace_requests();
    let creates: Vec<_> = operations
        .iter()
        .filter(|op| matches!(op["op"].as_str(), Some("tab.create" | "pane.split")))
        .collect();
    assert_eq!(creates.len(), 6, "{operations:?}");
    assert!(creates.iter().all(|op| op["terminal_id"].is_null()));
    assert_eq!(creates[0]["op"], "tab.create");
    assert_eq!(creates[1]["axis"], "horizontal");
    assert_eq!(creates[2]["axis"], "horizontal");
    assert!(creates[3..].iter().all(|op| op["axis"] == "vertical"));

    let snapshot = workspace
        .daemon()
        .workspace_snapshot(None, &workspace_id)
        .await
        .expect("mock grid snapshot");
    let tab = snapshot
        .tabs
        .iter()
        .find(|tab| tab.title.as_deref() == Some("new grid"))
        .expect("new grid tab");
    workspace
        .drain_live_events()
        .await
        .expect("apply grid creation events");
    sync_live_chrome(&mut workspace, &mut chrome);
    assert_eq!(
        chrome.active_tab().expect("active tab after grid").id,
        tab.id,
        "the new grid becomes the visible tab"
    );
    assert_eq!(
        snapshot
            .panes
            .iter()
            .filter(|pane| pane.tab_id == tab.id)
            .count(),
        6
    );
    let mut cells = Vec::new();
    cell_sizes(&tab.layout, 1.0, 1.0, &mut cells);
    assert_eq!(cells.len(), 6);
    for (width, height) in cells {
        assert!((width - 1.0 / 3.0).abs() < 0.001, "width={width}");
        assert!((height - 0.5).abs() < 0.001, "height={height}");
    }
}
