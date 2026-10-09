mod mock_daemon;

use crossterm::event::{KeyCode, KeyEvent, KeyModifiers, MouseButton, MouseEvent, MouseEventKind};
use gobby_client::app::apply_live_menu_action;
use gobby_client::app::{
    build_menu, route_modal_key, route_mouse, ArrangeTarget, ContextMenuKind, ControlState,
    MenuAction, ModalOutcome, MouseOutcome, Submenu,
};
use gobby_client::daemon::LiveDaemon;
use gobby_client::key_input::KeyInput;
use gobby_client::prefs::load_prefs;
use gobby_client::theme::ThemeKind;
use gobby_client::ui::chrome::Mode;
use gobby_client::ui::keymap::BINDINGS;
use gobby_client::ui::menu_bar::MenuBarMenu;
use gobby_client::ui::status::Toast;
use gobby_client::ui::{render_workspace, Action, Chrome};
use gobby_client::Workspace;
use ratatui::backend::TestBackend;
use ratatui::layout::Rect;
use ratatui::Terminal;
use serde_json::json;
use std::time::Duration;
use tempfile::TempDir;
use tokio::sync::mpsc::unbounded_channel;
use tokio::time::timeout;

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
        "{:?}:{:?}:{}:{:?}:{:?}:{}:{:?}:{}:{}:{}:{}:{:?}:{}:{}:{}:{}:{pane:?}",
        fixture.chrome.mode,
        fixture.chrome.theme.kind,
        fixture.chrome.prefs.theme,
        fixture.chrome.menu.as_ref().map(|menu| &menu.kind),
        fixture.chrome.dialog,
        fixture.chrome.prefs.monochrome,
        fixture.chrome.sidebar.machine_filter,
        fixture.chrome.sidebar.pinned,
        fixture.chrome.sidebar.overlay,
        fixture.chrome.sidebar.all_projects,
        fixture.chrome.sidebar.all_sessions,
        fixture.chrome.prefs.agent_sort,
        fixture.chrome.is_zoomed(),
        fixture.chrome.toasts.len(),
        fixture.chrome.alert_log.len(),
        fixture.chrome.tabs().tabs.len(),
    ) + &format!(
        ":{:?}:{:?}:{:?}",
        fixture.chrome.focused_pane(),
        fixture.chrome.theme.name,
        fixture.chrome.prefs.palette,
    )
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
fn file_menu_lists_project_actions_and_help_holds_the_alert_log() {
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
            "New project…",
            "Open project…",
            "Rename tab",
            "Close tab",
            "Destroy orphaned terminals…",
            "Detach",
        ]
    );
    assert_eq!(
        labels(MenuBarMenu::Help),
        ["Keybinds", "Alerts…", "Daemon", "About Gobby"]
    );
    assert_eq!(
        labels(MenuBarMenu::Gobby),
        ["Settings", "Reload config", "Quit"]
    );
    assert_eq!(
        BINDINGS
            .iter()
            .find(|binding| binding.name == "new_project")
            .expect("new project binding")
            .description,
        "Create project"
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
            "  Appearance: Dark ▸",
            "  Theme: Restored ▸",
            "  Monochrome",
            "  Show sidebar",
            "  Pin sidebar",
            "✓ Show all machines",
            "✓ Show all projects",
            "✓ Group agents by project",
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
            "Arrange ▸",
        ]
    );

    // Every menu-bar menu, every View submenu under it and Window › Arrange
    // for the fixture's tab.
    let tab = live_menu_fixture(true, false)
        .await
        .chrome
        .active_tab()
        .expect("fixture tab")
        .id
        .clone();
    let kinds: Vec<ContextMenuKind> = MenuBarMenu::ALL
        .into_iter()
        .map(ContextMenuKind::MenuBar)
        .chain(
            [
                Submenu::Appearance,
                Submenu::Theme,
                Submenu::Arrange(ArrangeTarget { tab, pane: None }),
            ]
            .map(ContextMenuKind::Submenu),
        )
        .collect();
    for (focused, held) in [(false, false), (true, false), (true, true)] {
        for kind in &kinds {
            let fixture = live_menu_fixture(focused, held).await;
            let items = build_menu(&fixture.workspace, &fixture.chrome, kind.clone(), (0, 1)).items;
            for item in items.into_iter().filter(|item| item.enabled) {
                let mut fixture = live_menu_fixture(focused, held).await;
                let before = observable_state(&fixture);
                let requests_before = fixture.mock.requests().len();
                let workspace_before = fixture.mock.workspace_requests().len();
                let action = item.action.clone();
                let (outcomes, mut outcomes_rx) = unbounded_channel();
                let exit = apply_live_menu_action(
                    &mut fixture.workspace,
                    &mut fixture.chrome,
                    &outcomes,
                    kind.clone(),
                    action.clone(),
                )
                .await
                .expect("menu action dispatch");
                if action == MenuAction::Noop {
                    assert!(!exit);
                    assert_eq!(observable_state(&fixture), before);
                    assert_eq!(fixture.mock.requests().len(), requests_before);
                    assert_eq!(fixture.mock.workspace_requests().len(), workspace_before);
                    assert!(outcomes_rx.try_recv().is_err());
                    continue;
                }
                // A daemon op runs as a job; its outcome is its effect.
                let effect = exit
                    || observable_state(&fixture) != before
                    || fixture.mock.requests().len() > requests_before
                    || fixture.mock.workspace_requests().len() > workspace_before
                    || timeout(Duration::from_secs(2), outcomes_rx.recv())
                        .await
                        .is_ok_and(|outcome| outcome.is_some());
                assert!(
                    effect,
                    "{kind:?} item {:?} had no effect from {action:?}",
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

/// Open `title`'s menu from the menu bar, then the row that runs `action`,
/// the live loop's way: each press routed and its menu outcome applied, a
/// frame drawn after each. Hands back the row as the menu drew it.
async fn pick_menu_row(
    terminal: &mut Terminal<TestBackend>,
    fixture: &mut LiveMenuFixture,
    title: MenuBarMenu,
    action: MenuAction,
) -> Rect {
    draw(terminal, fixture);
    let view = MenuBarMenu::ALL
        .iter()
        .position(|menu| *menu == title)
        .expect("menu title");
    let title_rect = fixture
        .chrome
        .view
        .menu_title_hit_areas
        .iter()
        .find(|(index, _)| *index == view)
        .map(|(_, rect)| *rect)
        .expect("menu title drawn");
    let press = left_press((title_rect.x + 1, title_rect.y));
    assert_eq!(
        route_mouse(&fixture.workspace, &mut fixture.chrome, &press),
        MouseOutcome::Handled
    );
    draw(terminal, fixture);
    let menu = fixture.chrome.menu.as_ref().expect("menu open");
    let row = menu
        .items
        .iter()
        .position(|item| item.action == action)
        .map(|index| menu.item_rects[index])
        .expect("row drawn");

    let press = left_press((row.x + 1, row.y));
    let outcome = route_mouse(&fixture.workspace, &mut fixture.chrome, &press);
    let MouseOutcome::Menu {
        kind,
        action: picked,
    } = outcome
    else {
        panic!("the row click dispatches: {outcome:?}");
    };
    assert_eq!(kind, ContextMenuKind::MenuBar(title));
    assert_eq!(picked, action);
    let exit = apply_live_menu_action(
        &mut fixture.workspace,
        &mut fixture.chrome,
        &unbounded_channel().0,
        kind,
        picked,
    )
    .await
    .expect("dispatch the row");
    assert!(!exit);
    draw(terminal, fixture);
    row
}

/// Open the Appearance choices through the View menu's Appearance row.
async fn open_the_appearance_choices(
    terminal: &mut Terminal<TestBackend>,
    fixture: &mut LiveMenuFixture,
) -> Rect {
    let row = pick_menu_row(
        terminal,
        fixture,
        MenuBarMenu::View,
        MenuAction::OpenSubmenu(Submenu::Appearance),
    )
    .await;
    assert_eq!(
        fixture.chrome.mode,
        Mode::ContextMenu,
        "the choices stay open"
    );
    row
}

fn key(code: KeyCode) -> KeyInput {
    KeyInput {
        key: KeyEvent::new(code, KeyModifiers::NONE),
        bytes: Vec::new(),
    }
}

fn screen(terminal: &Terminal<TestBackend>) -> String {
    let buffer = terminal.backend().buffer();
    (0..buffer.area.height)
        .map(|y| {
            (0..buffer.area.width)
                .map(|x| buffer[(x, y)].symbol())
                .collect::<String>()
        })
        .collect::<Vec<_>>()
        .join("\n")
}

// Regression: the drawn Appearance row once did nothing when the loop dispatched
// it. The row must open its choices beside the View menu, which stays open
// behind them, and a pick must save.
#[tokio::test]
async fn clicking_the_appearance_row_opens_its_choices_and_a_pick_saves_it() {
    let mut fixture = live_menu_fixture(true, false).await;
    let mut terminal = Terminal::new(TestBackend::new(100, 30)).expect("test backend");
    let row = open_the_appearance_choices(&mut terminal, &mut fixture).await;
    let choices = fixture
        .chrome
        .menu
        .as_ref()
        .expect("appearance choices open");
    assert_eq!(choices.kind, ContextMenuKind::Submenu(Submenu::Appearance));
    let labels: Vec<&str> = choices.items.iter().map(|item| item.label).collect();
    assert_eq!(labels, ["● Dark", "  Light", "  System"]);
    assert!(
        choices.item_rects[0].x > row.right(),
        "beside the View menu"
    );
    assert_eq!(
        choices.item_rects[0].y, row.y,
        "level with the appearance row"
    );
    let parent = choices.parent.as_deref().expect("the View menu stays open");
    assert_eq!(parent.kind, ContextMenuKind::MenuBar(MenuBarMenu::View));
    assert_eq!(
        parent.items[parent.selected].action,
        MenuAction::OpenSubmenu(Submenu::Appearance)
    );
    assert_eq!(
        parent.item_rects[parent.selected], row,
        "drawn where it was"
    );
    let buffer = terminal.backend().buffer();
    let drawn: String = (row.x..row.right())
        .map(|x| buffer[(x, row.y)].symbol())
        .collect();
    assert!(drawn.contains("Appearance: Dark ▸"), "{drawn:?}");

    let light = choices.item_rects[1];
    let press = left_press((light.x + 1, light.y));
    let outcome = route_mouse(&fixture.workspace, &mut fixture.chrome, &press);
    let MouseOutcome::Menu { kind, action } = outcome else {
        panic!("the Light click dispatches: {outcome:?}");
    };
    assert_eq!(action, MenuAction::SetAppearance("light"));
    apply_live_menu_action(
        &mut fixture.workspace,
        &mut fixture.chrome,
        &unbounded_channel().0,
        kind,
        action,
    )
    .await
    .expect("dispatch the pick");
    assert_eq!(fixture.chrome.theme.kind, ThemeKind::Light);
    let saved = load_prefs(fixture.home.path()).expect("load prefs");
    assert_eq!(saved.theme, "light");
    fixture.mock.shutdown().await;
}

// The View menu stays live behind the appearance choices: a press on one of
// its rows acts on that row, and Esc closes both menus.
#[tokio::test]
async fn the_view_menu_behind_the_appearance_choices_stays_live() {
    let mut fixture = live_menu_fixture(true, false).await;
    let mut terminal = Terminal::new(TestBackend::new(100, 30)).expect("test backend");
    open_the_appearance_choices(&mut terminal, &mut fixture).await;
    let parent = fixture
        .chrome
        .menu
        .as_ref()
        .and_then(|menu| menu.parent.as_deref())
        .expect("View menu behind the choices");
    let monochrome = parent
        .items
        .iter()
        .position(|item| item.action == MenuAction::ToggleMonochrome)
        .map(|index| parent.item_rects[index])
        .expect("Monochrome drawn");
    let press = left_press((monochrome.x + 1, monochrome.y));
    assert_eq!(
        route_mouse(&fixture.workspace, &mut fixture.chrome, &press),
        MouseOutcome::Menu {
            kind: ContextMenuKind::MenuBar(MenuBarMenu::View),
            action: MenuAction::ToggleMonochrome,
        }
    );
    assert!(fixture.chrome.menu.is_none(), "the row ran and both closed");

    open_the_appearance_choices(&mut terminal, &mut fixture).await;
    let outcome = route_modal_key(&fixture.workspace, &mut fixture.chrome, &key(KeyCode::Esc));
    assert!(matches!(outcome, ModalOutcome::Close));
    assert!(
        fixture.chrome.menu.is_none(),
        "Esc closes the choices and the View menu"
    );
    assert_eq!(fixture.chrome.mode, Mode::Terminal);
    fixture.mock.shutdown().await;
}

/// On a frame too narrow for the choices right of the View menu, they open
/// on its left rather than over it.
#[tokio::test]
async fn the_appearance_choices_open_left_of_the_view_menu_on_a_narrow_frame() {
    let mut fixture = live_menu_fixture(true, false).await;
    let mut terminal = Terminal::new(TestBackend::new(50, 24)).expect("test backend");
    fixture
        .chrome
        .compute_view(&fixture.workspace, Rect::new(0, 0, 50, 24));
    let row = open_the_appearance_choices(&mut terminal, &mut fixture).await;
    let choices = fixture
        .chrome
        .menu
        .as_ref()
        .expect("appearance choices open");
    let view_left = choices
        .parent
        .as_deref()
        .and_then(|parent| parent.item_rects.iter().map(|rect| rect.x).min())
        .expect("View rows drawn");
    let first = choices.item_rects[0];
    assert!(
        first.right() < view_left,
        "left of the View menu: {first:?}, View rows from x {view_left}"
    );
    assert_eq!(first.y, row.y, "the first choice lines up with Appearance");
    fixture.mock.shutdown().await;
}

/// A search hides the legend; Help opened again from Help › Keybinds drops
/// that search and shows the legend.
#[tokio::test]
async fn help_reopened_from_the_legend_row_drops_the_old_search() {
    let mut fixture = live_menu_fixture(true, false).await;
    let mut terminal = Terminal::new(TestBackend::new(100, 30)).expect("test backend");
    let legend = MenuAction::Act(Action::Help);
    pick_menu_row(
        &mut terminal,
        &mut fixture,
        MenuBarMenu::Help,
        legend.clone(),
    )
    .await;
    assert_eq!(fixture.chrome.mode, Mode::KeybindHelp);
    assert!(
        screen(&terminal).contains("running a turn"),
        "Help opens on the legend"
    );
    for ch in "/split".chars() {
        route_modal_key(
            &fixture.workspace,
            &mut fixture.chrome,
            &key(KeyCode::Char(ch)),
        );
    }
    draw(&mut terminal, &mut fixture);
    assert!(
        !screen(&terminal).contains("running a turn"),
        "the search hides the legend"
    );
    route_modal_key(&fixture.workspace, &mut fixture.chrome, &key(KeyCode::Esc));
    route_modal_key(&fixture.workspace, &mut fixture.chrome, &key(KeyCode::Esc));
    assert_eq!(fixture.chrome.mode, Mode::Terminal, "esc, esc closes Help");

    pick_menu_row(&mut terminal, &mut fixture, MenuBarMenu::Help, legend).await;
    assert_eq!(fixture.chrome.keybind_help.query, "");
    assert!(
        screen(&terminal).contains("running a turn"),
        "the legend is back"
    );
    fixture.mock.shutdown().await;
}

/// From the bottom of Help, the first key back scrolls the view.
#[tokio::test]
async fn the_first_key_back_from_the_bottom_of_help_scrolls() {
    let mut fixture = live_menu_fixture(true, false).await;
    let mut terminal = Terminal::new(TestBackend::new(80, 24)).expect("test backend");
    fixture
        .chrome
        .compute_view(&fixture.workspace, Rect::new(0, 0, 80, 24));
    pick_menu_row(
        &mut terminal,
        &mut fixture,
        MenuBarMenu::Help,
        MenuAction::Act(Action::Help),
    )
    .await;
    let last = fixture.chrome.view.help_last_scroll;
    assert!(last > 1, "Help overflows at 80x24: {last}");
    for _ in 0..=last {
        route_modal_key(
            &fixture.workspace,
            &mut fixture.chrome,
            &key(KeyCode::PageDown),
        );
        draw(&mut terminal, &mut fixture);
    }
    assert_eq!(
        fixture.chrome.keybind_help.scroll, last,
        "PageDown stops at the drawn bottom"
    );
    let bottom = screen(&terminal);
    route_modal_key(
        &fixture.workspace,
        &mut fixture.chrome,
        &key(KeyCode::Char('k')),
    );
    draw(&mut terminal, &mut fixture);
    assert_eq!(fixture.chrome.keybind_help.scroll, last - 1);
    assert_ne!(screen(&terminal), bottom, "the first k scrolls up");
    route_modal_key(&fixture.workspace, &mut fixture.chrome, &key(KeyCode::Up));
    assert_eq!(fixture.chrome.keybind_help.scroll, last - 2);
    fixture.mock.shutdown().await;
}
