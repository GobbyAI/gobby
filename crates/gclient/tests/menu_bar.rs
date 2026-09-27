mod mock_daemon;

use crossterm::event::{KeyModifiers, MouseButton, MouseEvent, MouseEventKind};
use gobby_client::app::apply_live_menu_action;
use gobby_client::app::{
    build_menu, route_mouse, ContextMenuKind, ControlState, MenuAction, MouseOutcome,
};
use gobby_client::daemon::LiveDaemon;
use gobby_client::prefs::load_prefs;
use gobby_client::theme::ThemeKind;
use gobby_client::ui::chrome::Mode;
use gobby_client::ui::keymap::BINDINGS;
use gobby_client::ui::menu_bar::MenuBarMenu;
use gobby_client::ui::status::Toast;
use gobby_client::ui::{render_workspace, Chrome};
use gobby_client::Workspace;
use ratatui::backend::TestBackend;
use ratatui::layout::Rect;
use ratatui::Terminal;
use serde_json::json;
use tempfile::TempDir;

use mock_daemon::MockDaemon;

struct LiveMenuFixture {
    mock: MockDaemon,
    workspace: Workspace<LiveDaemon>,
    chrome: Chrome,
    pane: Option<gobby_client::app::PaneId>,
    home: TempDir,
}

async fn live_menu_fixture(focused: bool, held: bool) -> LiveMenuFixture {
    let mock = MockDaemon::start("local-token").await;
    let terminals = if focused {
        vec!["menu-a", "menu-b"]
    } else {
        Vec::new()
    };
    if focused {
        mock.seed_workspace("project-1", &[(&["menu-a", "menu-b"], "menu-a")]);
    } else {
        mock.seed_workspace("project-1", &[]);
    }
    let terminal_page = json!({
        "items": terminals.iter().map(|id| json!({"terminal_id": id, "backend": "native", "state": "live"})).collect::<Vec<_>>(),
        "next_cursor": null,
        "snapshot": {"daemon_epoch": "menu-epoch", "seq": 1},
    });
    for _ in 0..3 {
        mock.enqueue("GET", "/api/terminals?", 200, terminal_page.clone());
    }
    let attention = json!({
        "epoch": "menu-attention",
        "seq": 1,
        "entries": terminals.iter().enumerate().map(|(index, id)| json!({
            "entry_id": format!("run:{id}"),
            "terminal": {"terminal_id": id, "backend": "native"},
            "attention": {
                "attention_id": format!("att-{index}"),
                "state": "blocked",
                "kind": "actionable",
                "fingerprint": format!("fp-{index}"),
                "payload": {"prompt": "Continue?", "options": [{"option": 1, "label": "Yes"}]}
            }
        })).collect::<Vec<_>>(),
    });
    for _ in 0..3 {
        mock.enqueue("GET", "/api/attention/roster", 200, attention.clone());
    }
    let daemon = LiveDaemon::connect(mock.url(), "local-token")
        .await
        .expect("connect mock daemon");
    let mut workspace = Workspace::live(daemon);
    let home = tempfile::tempdir().expect("isolated gobby home");
    workspace.set_gobby_home(home.path().to_path_buf());
    workspace.select_project("project-1");
    workspace
        .reconcile_subscribe_first()
        .await
        .expect("load mock workspace");
    let mut chrome = Chrome::dark();
    let pane = if focused {
        let first = workspace.pane_for_terminal("menu-a").expect("first pane");
        let second = workspace.pane_for_terminal("menu-b").expect("second pane");
        chrome.open_pane(first, "first");
        chrome.open_pane(second, "second");
        chrome.focus_pane(first);
        workspace.pane_mut(first).label = Some("menu pane".to_string());
        if held {
            workspace.pane_mut(first).control = ControlState::Held;
        }
        let mut toast = Toast::info("target");
        toast.target = Some("menu-b".to_string());
        chrome.notify(toast);
        Some(first)
    } else {
        None
    };
    chrome.compute_view(&workspace, Rect::new(0, 0, 100, 30));
    LiveMenuFixture {
        mock,
        workspace,
        chrome,
        pane,
        home,
    }
}

fn observable_state(fixture: &LiveMenuFixture) -> String {
    let pane = fixture.pane.map(|id| {
        let state = fixture.workspace.pane(id);
        format!(
            "{:?}:{:?}:{}:{}",
            state.control,
            state.label,
            state.right_click_passthrough,
            fixture.workspace.awaiting_control(id),
        )
    });
    format!(
        "{:?}:{:?}:{}:{}:{}:{}:{:?}:{}:{}:{}:{}:{pane:?}",
        fixture.chrome.mode,
        fixture.chrome.dialog,
        fixture.chrome.sidebar.pinned,
        fixture.chrome.sidebar.overlay,
        fixture.chrome.sidebar.all_projects,
        fixture.chrome.sidebar.all_sessions,
        fixture.chrome.prefs.agent_sort,
        fixture.chrome.is_zoomed(),
        fixture.chrome.toasts.len(),
        fixture.chrome.alert_log.len(),
        fixture.chrome.tabs().tabs.len(),
    ) + &format!(":{:?}", fixture.chrome.focused_pane())
}

#[test]
fn agent_menu_lists_the_nine_actions_in_order() {
    let workspace = Workspace::scripted();
    let chrome = Chrome::dark();
    let menu = build_menu(
        &workspace,
        &chrome,
        ContextMenuKind::MenuBar(MenuBarMenu::Agent),
        (0, 1),
    );

    let labels: Vec<_> = menu.items.iter().map(|item| item.label).collect();
    assert_eq!(
        labels,
        [
            "Respond",
            "Mark seen",
            "Take control",
            "Release control",
            "Take back",
            "Detach",
            "Open alert target",
            "Next attention",
            "Previous attention",
        ]
    );
    assert!(!menu.items[0].enabled, "no pane can respond");
    assert!(!menu.items[1].enabled, "no pane has unseen attention");
}

#[test]
fn file_menu_says_new_workspace_and_help_holds_the_alert_log() {
    let workspace = Workspace::scripted();
    let chrome = Chrome::dark();
    let labels = |title| {
        build_menu(&workspace, &chrome, ContextMenuKind::MenuBar(title), (0, 1))
            .items
            .iter()
            .map(|item| item.label)
            .collect::<Vec<_>>()
    };

    assert_eq!(
        labels(MenuBarMenu::File),
        [
            "New terminal",
            "New tab",
            "New workspace…",
            "Rename tab",
            "Close tab",
            "Destroy orphaned terminals…",
            "Detach",
        ]
    );
    assert_eq!(
        labels(MenuBarMenu::Help),
        ["Keys", "Alerts…", "Daemon", "About Gobby"]
    );
    assert_eq!(
        labels(MenuBarMenu::Gobby),
        ["Settings", "Reload config", "Quit"]
    );
    assert_eq!(
        BINDINGS
            .iter()
            .find(|binding| binding.name == "new_project")
            .expect("new workspace binding")
            .description,
        "Add a workspace"
    );
}

#[tokio::test]
async fn every_menu_bar_item_dispatches_to_a_handler() {
    let mut workspace = Workspace::scripted();
    let pane = workspace
        .open_terminal("menu-terminal", "native", "epoch")
        .expect("open terminal");
    let mut chrome = Chrome::dark();
    chrome.open_pane(pane, "menu");
    let labels = |title| {
        build_menu(&workspace, &chrome, ContextMenuKind::MenuBar(title), (0, 1))
            .items
            .iter()
            .map(|item| item.label)
            .collect::<Vec<_>>()
    };

    assert_eq!(
        labels(MenuBarMenu::View),
        [
            "✓ This project",
            "  All projects",
            "✓ Grouped",
            "  Priority",
            "Working projects",
            "Show sidebar",
            "Pin sidebar",
            "Theme: Dark ▸",
        ]
    );
    assert_eq!(
        labels(MenuBarMenu::Window),
        [
            "Split right",
            "Split down",
            "Zoom",
            "Close pane",
            "Resize mode",
            "Arrange: even horizontal",
            "Arrange: even vertical",
            "Arrange: main horizontal",
            "Arrange: main vertical",
            "Arrange: tiled",
            "New grid…",
        ]
    );

    for (focused, held) in [(false, false), (true, false), (true, true)] {
        for title in MenuBarMenu::ALL {
            let fixture = live_menu_fixture(focused, held).await;
            let items = build_menu(
                &fixture.workspace,
                &fixture.chrome,
                ContextMenuKind::MenuBar(title),
                (0, 1),
            )
            .items;
            for item in items.into_iter().filter(|item| item.enabled) {
                let mut fixture = live_menu_fixture(focused, held).await;
                let before = observable_state(&fixture);
                let requests_before = fixture.mock.requests().len();
                let workspace_before = fixture.mock.workspace_requests().len();
                let action = item.action.clone();
                let exit = apply_live_menu_action(
                    &mut fixture.workspace,
                    &mut fixture.chrome,
                    ContextMenuKind::MenuBar(title),
                    action.clone(),
                )
                .await
                .expect("menu action dispatch");
                assert!(
                    exit || observable_state(&fixture) != before
                        || fixture.mock.requests().len() > requests_before
                        || fixture.mock.workspace_requests().len() > workspace_before,
                    "{title:?} item {:?} had no effect from {action:?}",
                    item.label,
                );
            }
        }
    }
}

fn left_press((column, row): (u16, u16)) -> MouseEvent {
    MouseEvent {
        kind: MouseEventKind::Down(MouseButton::Left),
        column,
        row,
        modifiers: KeyModifiers::NONE,
    }
}

/// Draw a frame and hand its rects back to the hit tests, as the loop does.
fn draw(terminal: &mut Terminal<TestBackend>, fixture: &mut LiveMenuFixture) {
    let mut hits = None;
    terminal
        .draw(|frame| hits = Some(render_workspace(frame, &fixture.workspace, &fixture.chrome)))
        .expect("draw frame");
    fixture.chrome.apply_hits(hits.expect("frame drawn"));
}

// Regression: the drawn Theme row once did nothing when the loop dispatched
// it. Each click here goes the live loop's way, the press routed and its menu
// outcome applied, so the row must open its choices and a pick must save.
#[tokio::test]
async fn clicking_the_theme_row_opens_its_choices_and_a_pick_saves_it() {
    let mut fixture = live_menu_fixture(true, false).await;
    let mut terminal = Terminal::new(TestBackend::new(100, 30)).expect("test backend");
    draw(&mut terminal, &mut fixture);
    let view = MenuBarMenu::ALL
        .iter()
        .position(|menu| *menu == MenuBarMenu::View)
        .expect("view title");
    let title = fixture
        .chrome
        .view
        .menu_title_hit_areas
        .iter()
        .find(|(index, _)| *index == view)
        .map(|(_, rect)| *rect)
        .expect("View title drawn");
    let press = left_press((title.x + 1, title.y));
    assert_eq!(
        route_mouse(&fixture.workspace, &mut fixture.chrome, &press),
        MouseOutcome::Handled
    );
    draw(&mut terminal, &mut fixture);
    let menu = fixture.chrome.menu.as_ref().expect("View menu open");
    let row = menu
        .items
        .iter()
        .position(|item| item.action == MenuAction::ThemeMenu)
        .map(|index| menu.item_rects[index])
        .expect("theme row drawn");

    let press = left_press((row.x + 1, row.y));
    let outcome = route_mouse(&fixture.workspace, &mut fixture.chrome, &press);
    let MouseOutcome::Menu { kind, action } = outcome else {
        panic!("the theme row click dispatches: {outcome:?}");
    };
    assert_eq!(kind, ContextMenuKind::MenuBar(MenuBarMenu::View));
    assert_eq!(action, MenuAction::ThemeMenu);
    let exit = apply_live_menu_action(&mut fixture.workspace, &mut fixture.chrome, kind, action)
        .await
        .expect("dispatch the theme row");
    assert!(!exit);
    assert_eq!(
        fixture.chrome.mode,
        Mode::ContextMenu,
        "the choices stay open"
    );
    draw(&mut terminal, &mut fixture);
    let choices = fixture.chrome.menu.as_ref().expect("theme choices open");
    assert_eq!(choices.kind, ContextMenuKind::Theme);
    let labels: Vec<&str> = choices.items.iter().map(|item| item.label).collect();
    assert_eq!(labels, ["● Dark", "  Light", "  System"]);
    assert!(
        choices.item_rects[0].x > row.right(),
        "beside the View menu"
    );
    assert_eq!(choices.item_rects[0].y, row.y, "level with the theme row");

    let light = choices.item_rects[1];
    let press = left_press((light.x + 1, light.y));
    let outcome = route_mouse(&fixture.workspace, &mut fixture.chrome, &press);
    let MouseOutcome::Menu { kind, action } = outcome else {
        panic!("the Light click dispatches: {outcome:?}");
    };
    assert_eq!(action, MenuAction::SetTheme("light"));
    apply_live_menu_action(&mut fixture.workspace, &mut fixture.chrome, kind, action)
        .await
        .expect("dispatch the pick");
    assert_eq!(fixture.chrome.theme.kind, ThemeKind::Light);
    let saved = load_prefs(fixture.home.path()).expect("load prefs");
    assert_eq!(saved.theme, "light");
    fixture.mock.shutdown().await;
}
