mod mock_daemon;

use gobby_client::app::{
    apply_live_menu_action, build_menu, plan_arrange, sync_live_chrome, ArrangeLayout,
    ArrangeTarget, ContextMenuKind, MenuAction, ModalOutcome, Submenu,
};
use gobby_client::daemon::{LayoutAxis, LayoutNode, LiveDaemon, WorkspaceOp};
use gobby_client::ui::dialogs::{render_dialog, Dialog};
use gobby_client::ui::menu_bar::MenuBarMenu;
use gobby_client::ui::status::ToastKind;
use gobby_client::ui::{Chrome, Mode, WorkspaceView};
use gobby_client::Workspace;
use mock_daemon::MockDaemon;
use ratatui::backend::TestBackend;
use ratatui::crossterm::event::{KeyCode, KeyEvent};
use ratatui::layout::Rect;
use ratatui::Terminal;
use serde_json::json;
use tokio::sync::mpsc::unbounded_channel;

const WINDOW: ContextMenuKind = ContextMenuKind::MenuBar(MenuBarMenu::Window);

/// The target of the enabled Arrange ▸ row in the menu `kind` opens.
fn arrange_target<W: WorkspaceView>(
    ws: &W,
    chrome: &Chrome,
    kind: ContextMenuKind,
) -> ArrangeTarget {
    build_menu(ws, chrome, kind, (0, 0))
        .items
        .into_iter()
        .find_map(|item| match item.action {
            MenuAction::OpenSubmenu(Submenu::Arrange(target)) if item.enabled => Some(target),
            _ => None,
        })
        .expect("an enabled Arrange ▸ row")
}

/// Choose `layout` from the Arrange ▸ submenu cascading from `kind`.
async fn choose(
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
    kind: ContextMenuKind,
    layout: ArrangeLayout,
) {
    let target = arrange_target(workspace, chrome, kind);
    apply_live_menu_action(
        workspace,
        chrome,
        &unbounded_channel().0,
        ContextMenuKind::Submenu(Submenu::Arrange(target.clone())),
        MenuAction::Arrange { layout, target },
    )
    .await
    .expect("arrange dispatch");
}

/// Ops that lay a tab out.
fn layout_requests(mock: &MockDaemon) -> Vec<serde_json::Value> {
    mock.workspace_requests()
        .into_iter()
        .filter(|request| matches!(request["op"].as_str(), Some("pane.move" | "pane.resize")))
        .collect()
}

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

    choose(
        &mut workspace,
        &mut chrome,
        WINDOW,
        ArrangeLayout::EvenHorizontal,
    )
    .await;
    let toast = &chrome.toasts.last().expect("one-pane notice").toast;
    assert_eq!(toast.kind, ToastKind::Info);
    assert_eq!(toast.title, "Nothing to arrange: one pane");

    let pane = workspace.pane_for_terminal("only").expect("seeded pane");
    chrome.open_tab(pane, "local pane");
    choose(
        &mut workspace,
        &mut chrome,
        WINDOW,
        ArrangeLayout::EvenHorizontal,
    )
    .await;
    let toast = &chrome.toasts.last().expect("local-tab notice").toast;
    assert_eq!(toast.kind, ToastKind::Warning);
    assert!(toast.title.contains("local"));
    assert!(mock
        .workspace_requests()
        .iter()
        .all(|request| !matches!(request["op"].as_str(), Some("pane.move" | "pane.resize"))));
}

/// A live workspace of two daemon tabs, two panes each, with the first
/// active, and the isolated gobby home it saves into.
async fn two_tabs() -> (MockDaemon, Workspace<LiveDaemon>, Chrome, tempfile::TempDir) {
    let mock = MockDaemon::start("local-token").await;
    mock.seed_workspace("project-1", &[(&["a1", "a2"], "a1"), (&["b1", "b2"], "b1")]);
    let items = ["a1", "a2", "b1", "b2"]
        .map(|id| json!({"terminal_id": id, "backend": "native", "state": "live"}));
    let page = json!({
        "items": items,
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
    assert_eq!(chrome.tabs().tabs.len(), 2);
    chrome.activate_tab(0);
    (mock, workspace, chrome, home)
}

/// A tab's menu and a pane's menu arrange the tab they were opened on,
/// shown first, never the tab that was active.
#[tokio::test]
async fn tab_and_pane_menus_arrange_their_own_tab() {
    for opener in ["tab", "pane"] {
        let (mock, mut workspace, mut chrome, _home) = two_tabs().await;
        let second = mock.tab_for_terminal("b1").expect("second tab");
        assert_eq!(chrome.tabs().tabs[1].id, second);
        let kind = match opener {
            "tab" => ContextMenuKind::Tab(1),
            _ => ContextMenuKind::Pane(workspace.pane_for_terminal("b2").expect("b2 pane")),
        };
        choose(
            &mut workspace,
            &mut chrome,
            kind,
            ArrangeLayout::EvenVertical,
        )
        .await;
        assert_eq!(chrome.active_index(), 1, "{opener}: the arranged tab shows");
        let ops = layout_requests(&mock);
        assert!(!ops.is_empty(), "{opener}: the layout went out");
        for op in ops.iter().filter(|op| op["op"] == "pane.move") {
            assert_eq!(op["tab"], second.as_str(), "{opener}: {op}");
        }
    }
}

/// A choice whose tab closed, or whose pane left its tab, is refused with a
/// notice, sends nothing and leaves the active tab alone.
#[tokio::test]
async fn stale_arrange_targets_are_refused() {
    let (mock, mut workspace, mut chrome, _home) = two_tabs().await;
    let first = chrome.tabs().tabs[0].id.clone();
    let moved = workspace.pane_for_terminal("b1").expect("b1 pane");
    for (target, notice) in [
        (
            ArrangeTarget {
                tab: "closed-tab".to_owned(),
                pane: None,
            },
            "Cannot arrange: that tab has closed",
        ),
        (
            ArrangeTarget {
                tab: first.clone(),
                pane: Some(moved),
            },
            "Cannot arrange: that pane has moved or closed",
        ),
    ] {
        chrome.activate_tab(1);
        apply_live_menu_action(
            &mut workspace,
            &mut chrome,
            &unbounded_channel().0,
            ContextMenuKind::Submenu(Submenu::Arrange(target.clone())),
            MenuAction::Arrange {
                layout: ArrangeLayout::Tiled,
                target,
            },
        )
        .await
        .expect("stale arrange");
        let toast = &chrome.toasts.last().expect("refusal notice").toast;
        assert_eq!(
            (toast.kind, toast.title.as_str()),
            (ToastKind::Warning, notice)
        );
        assert_eq!(chrome.active_index(), 1, "no fallback to another tab");
        assert!(layout_requests(&mock).is_empty());
    }
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
        &unbounded_channel().0,
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
        &unbounded_channel().0,
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
