mod mock_daemon;

use gobby_client::app::apply_live_menu_action;
use gobby_client::app::{build_menu, ContextMenuKind, ControlState};
use gobby_client::daemon::LiveDaemon;
use gobby_client::ui::keymap::BINDINGS;
use gobby_client::ui::menu_bar::MenuBarMenu;
use gobby_client::ui::status::Toast;
use gobby_client::ui::Chrome;
use gobby_client::Workspace;
use ratatui::layout::Rect;
use serde_json::json;
use tempfile::TempDir;

use mock_daemon::MockDaemon;

struct LiveMenuFixture {
    mock: MockDaemon,
    workspace: Workspace<LiveDaemon>,
    chrome: Chrome,
    pane: Option<gobby_client::app::PaneId>,
    _home: TempDir,
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
        _home: home,
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
        "{:?}:{:?}:{}:{}:{}:{}:{:?}:{}:{}:{}:{pane:?}",
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
            "respond",
            "mark seen",
            "take control",
            "release control",
            "take back",
            "detach",
            "open alert target",
            "next attention",
            "previous attention",
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
            "new terminal",
            "new tab",
            "new workspace…",
            "rename tab",
            "close tab",
            "destroy orphaned terminals…",
            "detach",
        ]
    );
    assert_eq!(labels(MenuBarMenu::Help), ["keys", "alerts…"]);
    assert_eq!(
        labels(MenuBarMenu::Gobby),
        ["settings", "reload config", "quit"]
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
            "✓ this project",
            "  all projects",
            "✓ grouped",
            "  priority",
            "working projects",
            "show sidebar",
            "pin sidebar",
        ]
    );
    assert_eq!(
        labels(MenuBarMenu::Window),
        [
            "split right",
            "split down",
            "zoom",
            "close pane",
            "resize mode"
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
