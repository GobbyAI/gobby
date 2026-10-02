use super::*;
use crate::app::{ControlState, Workspace};
use crate::ui::settings::AgentSort;
use crate::ui::Action;

/// View › Sidebar › Agents.
const AGENTS: ContextMenuKind = ContextMenuKind::Submenu(Submenu::Section(SidebarSection::Agents));

fn labels(state: &ContextMenuState) -> Vec<&'static str> {
    state.items.iter().map(|item| item.label).collect()
}

#[test]
fn menus_list_items_per_target_and_state() {
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
    let other = if focused == alpha { beta } else { alpha };

    // A plain, observed, unnamed pane that is not focused, in an
    // unzoomed tab. A scripted terminal is named after its id, so the
    // "unnamed" half has to be said out loud: with a name there, the menu
    // rightly offers to clear it, which the named case below covers.
    ws.pane_mut(other).label = None;
    let menu = build_menu(&ws, &chrome, ContextMenuKind::Pane(other), (10, 5));
    assert_eq!(
        labels(&menu),
        [
            "Rename pane",
            "Swap with focused pane",
            "Split right",
            "Split down",
            "Zoom",
            "Arrange ▸",
            "Take control",
            "Copy mode",
            "Send right-clicks to pane",
            "Close pane",
        ]
    );
    let tab_id = chrome.tabs().tabs[0].id.clone();
    assert_eq!(menu.items[1].action, MenuAction::SwapWithFocused(other));
    assert_eq!(
        menu.items[5].action,
        MenuAction::OpenSubmenu(Submenu::Arrange(ArrangeTarget {
            tab: tab_id.clone(),
            pane: Some(other),
        })),
        "the pane's Arrange targets its own tab and remembers the pane"
    );
    assert_eq!(menu.items[6].action, MenuAction::TakeControl(other));
    assert_eq!(menu.items[8].action, MenuAction::TogglePassthrough(other));
    assert_eq!(menu.items[9].action, MenuAction::Act(Action::ClosePane));
    assert_eq!(
        (menu.kind, menu.anchor, menu.selected),
        (ContextMenuKind::Pane(other), (10, 5), 0)
    );

    // The focused pane: held, named, blocked, zoomed and passing
    // right-clicks through.
    {
        let pane = ws.pane_mut(focused);
        pane.label = Some("build".to_string());
        pane.control = ControlState::Held;
        pane.right_click_passthrough = true;
    }
    let entry_id = format!("run:{}", ws.pane(focused).terminal_id);
    ws.attention.entries.push(crate::daemon::RosterEntry {
        entry_id: entry_id.clone(),
        attention: Some(crate::daemon::Attention::default()),
        ..Default::default()
    });
    chrome.toggle_zoom();
    let menu = build_menu(&ws, &chrome, ContextMenuKind::Pane(focused), (10, 5));
    assert_eq!(
        labels(&menu),
        [
            "Rename pane",
            "Clear pane name",
            "Split right",
            "Split down",
            "Unzoom",
            "Arrange ▸",
            "Release control",
            "Respond",
            "Copy mode",
            "Use gclient menu",
            "Close pane",
        ]
    );
    assert_eq!(menu.items[1].action, MenuAction::ClearPaneName(focused));
    assert_eq!(menu.items[6].action, MenuAction::ReleaseControl(focused));
    assert_eq!(menu.items[7].action, MenuAction::Respond(entry_id));

    let menu = build_menu(&ws, &chrome, ContextMenuKind::Tab(0), (3, 0));
    assert_eq!(
        labels(&menu),
        ["New tab", "Rename tab", "Arrange ▸", "Close tab"]
    );
    assert_eq!(
        menu.items[2].action,
        MenuAction::OpenSubmenu(Submenu::Arrange(ArrangeTarget {
            tab: tab_id,
            pane: None,
        }))
    );
    assert_eq!(menu.items[3].action, MenuAction::Act(Action::CloseTab));
    let gone = build_menu(&ws, &chrome, ContextMenuKind::Tab(9), (3, 0));
    assert!(
        !gone.items[2].enabled,
        "a tab index with no tab offers no Arrange"
    );

    let menu = build_menu(&ws, &chrome, ContextMenuKind::Global, (40, 12));
    assert_eq!(
        labels(&menu),
        [
            "New terminal",
            "New tab",
            "New workspace…",
            "Settings",
            "Keybinding help",
            "Reload config",
            "Toggle sidebar",
            "Destroy orphaned terminals…",
            "Detach",
            "Quit",
        ]
    );
    assert_eq!(menu.items[2].action, MenuAction::Act(Action::NewProject));
    assert_eq!(menu.items[5].action, MenuAction::Act(Action::ReloadConfig));
    assert_eq!(menu.items[9].action, MenuAction::Act(Action::Quit));
    assert!(menu.items.iter().all(|item| item.enabled));

    // Rows sit one cell inside the popup at the anchor: `destroy orphaned
    // terminals…` makes it 31 wide, ten items make it 12 tall.
    assert_eq!(
        menu_rect(menu.anchor, &menu.items),
        Rect::new(40, 12, 31, 12)
    );
    assert_eq!(menu.item_rects.len(), 10);
    assert_eq!(menu.item_rects[0], Rect::new(41, 13, 29, 1));
    assert_eq!(menu_hit(&menu, 41, 13), Some(0));
    assert_eq!(menu_hit(&menu, 57, 15), Some(2));
    assert_eq!(menu_hit(&menu, 40, 13), None, "the border is not a row");
    assert_eq!(menu_hit(&menu, 45, 22), Some(9), "the last row");
    assert_eq!(menu_hit(&menu, 45, 23), None, "below the last row");
    let short = [item("Zoom", MenuAction::Act(Action::Zoom))];
    assert_eq!(
        menu_rect((0, 0), &short).width,
        MENU_MIN_WIDTH,
        "short menus keep the floor width"
    );
}

#[test]
fn global_menu_offers_destroy_orphaned_terminals() {
    let ws = Workspace::scripted();
    let chrome = Chrome::dark();
    let menu = build_menu(&ws, &chrome, ContextMenuKind::Global, (0, 0));
    let item = menu
        .items
        .iter()
        .find(|item| item.action == MenuAction::DestroyOrphans)
        .expect("destroy orphans item");
    assert_eq!(item.label, "Destroy orphaned terminals…");
    assert!(item.enabled, "enabled without knowing the candidates");
}

/// The band menus' items regrouped under the menu bar's titles; Edit acts
/// on the focused pane.
#[test]
fn menu_bar_menus_regroup_items_per_title() {
    let mut ws = Workspace::scripted();
    let alpha = ws
        .open_terminal("term-alpha", "native", "epoch")
        .expect("open term-alpha");
    let mut chrome = Chrome::dark();
    chrome.open_pane(alpha, "alpha");
    chrome.compute_view(&ws, Rect::new(0, 0, 100, 30));
    let focused = chrome.focused_pane().expect("focused pane");
    let menu = |ws: &Workspace, chrome: &Chrome, title| {
        build_menu(ws, chrome, ContextMenuKind::MenuBar(title), (0, 1))
    };
    let actions = |state: &ContextMenuState| -> Vec<MenuAction> {
        state.items.iter().map(|item| item.action.clone()).collect()
    };

    let gobby = menu(&ws, &chrome, MenuBarMenu::Gobby);
    assert_eq!(labels(&gobby), ["Settings", "Reload config", "Quit"]);
    assert_eq!(
        actions(&gobby),
        [
            MenuAction::Act(Action::Settings),
            MenuAction::Act(Action::ReloadConfig),
            MenuAction::Act(Action::Quit),
        ]
    );

    let file = menu(&ws, &chrome, MenuBarMenu::File);
    assert_eq!(
        labels(&file),
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
        actions(&file),
        [
            MenuAction::Act(Action::NewTerminal),
            MenuAction::Act(Action::NewTab),
            MenuAction::Act(Action::NewProject),
            MenuAction::Act(Action::RenameTab),
            MenuAction::Act(Action::CloseTab),
            MenuAction::DestroyOrphans,
            MenuAction::Act(Action::Detach),
        ]
    );

    // A scripted terminal is named after its id, so its name can be cleared.
    let edit = menu(&ws, &chrome, MenuBarMenu::Edit);
    assert_eq!(
        labels(&edit),
        [
            "Copy mode",
            "Rename pane",
            "Rename tab",
            "Rename terminal",
            "Clear pane name",
            "Send right-clicks to pane",
        ]
    );
    assert_eq!(edit.items[0].action, MenuAction::Act(Action::CopyMode));
    assert_eq!(
        edit.items[3].action,
        MenuAction::Act(Action::RenameTerminal)
    );
    assert_eq!(edit.items[4].action, MenuAction::ClearPaneName(focused));
    assert_eq!(edit.items[5].action, MenuAction::TogglePassthrough(focused));
    ws.pane_mut(focused).label = None;
    ws.pane_mut(focused).right_click_passthrough = true;
    assert_eq!(
        labels(&menu(&ws, &chrome, MenuBarMenu::Edit)),
        [
            "Copy mode",
            "Rename pane",
            "Rename tab",
            "Rename terminal",
            "Use gclient menu",
        ]
    );

    // View is the theme submenu, the monochrome toggle and the sidebar
    // submenu, all against one margin.
    let view = menu(&ws, &chrome, MenuBarMenu::View);
    assert_eq!(
        labels(&view),
        ["  Theme: Dark ▸", "  Monochrome", "  Sidebar ▸"]
    );
    assert_eq!(
        actions(&view),
        [
            MenuAction::OpenSubmenu(Submenu::Theme),
            MenuAction::ToggleMonochrome,
            MenuAction::OpenSubmenu(Submenu::Sidebar),
        ]
    );
    assert!(view.items.iter().all(|item| item.enabled));

    let window = menu(&ws, &chrome, MenuBarMenu::Window);
    assert_eq!(
        labels(&window),
        [
            "Split right",
            "Split down",
            "Zoom",
            "Close pane",
            "Resize mode",
            "Arrange ▸",
        ]
    );
    let active = ArrangeTarget {
        tab: chrome.active_tab().expect("active tab").id.clone(),
        pane: None,
    };
    assert_eq!(
        actions(&window),
        [
            MenuAction::Act(Action::SplitVertical),
            MenuAction::Act(Action::SplitHorizontal),
            MenuAction::Act(Action::Zoom),
            MenuAction::Act(Action::ClosePane),
            MenuAction::Act(Action::ResizeMode),
            MenuAction::OpenSubmenu(Submenu::Arrange(active.clone())),
        ]
    );
    // The submenu holds the existing choices, each fixed to that tab.
    let arrange = build_menu(
        &ws,
        &chrome,
        ContextMenuKind::Submenu(Submenu::Arrange(active.clone())),
        (0, 0),
    );
    assert_eq!(
        labels(&arrange),
        [
            "Even horizontal",
            "Even vertical",
            "Main horizontal",
            "Main vertical",
            "Tiled",
            "New grid…",
        ]
    );
    let arrange_to = |layout| MenuAction::Arrange {
        layout,
        target: active.clone(),
    };
    assert_eq!(
        actions(&arrange),
        [
            arrange_to(ArrangeLayout::EvenHorizontal),
            arrange_to(ArrangeLayout::EvenVertical),
            arrange_to(ArrangeLayout::MainHorizontal),
            arrange_to(ArrangeLayout::MainVertical),
            arrange_to(ArrangeLayout::Tiled),
            MenuAction::OpenNewGrid,
        ]
    );
    let bare = menu(&ws, &Chrome::dark(), MenuBarMenu::Window);
    assert!(
        !bare.items[5].enabled,
        "with no tab open, Window offers no Arrange"
    );
    let mut empty = Chrome::dark();
    open_menu(
        &ws,
        &mut empty,
        ContextMenuKind::MenuBar(MenuBarMenu::Window),
        (0, 1),
    );
    empty.menu.as_mut().expect("window menu").selected = 5;
    assert_eq!(activate_menu(&mut empty), None);
    assert!(
        empty.menu.is_none() && empty.mode == Mode::Terminal,
        "a disabled Arrange ▸ closes the menu like any disabled row"
    );

    let help = menu(&ws, &chrome, MenuBarMenu::Help);
    assert_eq!(labels(&help), ["Keys", "Alerts…", "Daemon", "About Gobby"]);
    assert_eq!(
        actions(&help),
        [
            MenuAction::Act(Action::Help),
            MenuAction::ShowAlerts,
            MenuAction::ShowDaemon,
            MenuAction::ShowAbout,
        ]
    );

    let agent = menu(&ws, &chrome, MenuBarMenu::Agent);
    assert_eq!(agent.items.len(), 9);
    assert!(!agent.items[0].enabled);
    assert!(!agent.items[1].enabled);

    // With no pane focused, Edit shows its pane items disabled.
    let edit = menu(&ws, &Chrome::dark(), MenuBarMenu::Edit);
    assert_eq!(
        labels(&edit),
        ["Copy mode", "Rename pane", "Rename tab", "Rename terminal"]
    );
    assert!(edit.items.iter().all(|item| !item.enabled));
}

#[test]
fn agent_menu_disables_navigation_when_its_only_attention_is_focused() {
    let mut ws = Workspace::scripted();
    let pane = ws
        .open_terminal("term-attention", "native", "epoch")
        .expect("open terminal");
    let mut chrome = Chrome::dark();
    chrome.open_pane(pane, "attention");
    ws.attention.entries.push(crate::daemon::RosterEntry {
        entry_id: format!("run:{}", ws.pane(pane).terminal_id),
        attention: Some(crate::daemon::Attention::default()),
        ..Default::default()
    });

    let menu = build_menu(
        &ws,
        &chrome,
        ContextMenuKind::MenuBar(MenuBarMenu::Agent),
        (0, 1),
    );
    assert!(menu.items[0].enabled);
    assert!(menu.items[1].enabled);
    assert!(!menu.items[7].enabled, "next would revisit the same pane");
    assert!(
        !menu.items[8].enabled,
        "previous would revisit the same pane"
    );
}

#[test]
fn agents_view_menu_marks_the_view_in_force() {
    let ws = Workspace::scripted();
    let mut chrome = Chrome::dark();
    let items = |chrome: &Chrome| -> Vec<(&'static str, bool)> {
        build_menu(&ws, chrome, AGENTS, (0, 0))
            .items
            .into_iter()
            .map(|item| (item.label, item.enabled))
            .collect()
    };

    // The defaults: the focused project's rows in tab order. Each pair
    // marks what is in force and disables it, so choosing it is a no-op.
    assert_eq!(
        items(&chrome),
        [
            ("✓ This project", false),
            ("  All projects", true),
            ("✓ Grouped", false),
            ("  Priority", true),
        ]
    );

    // The marks follow both axes independently.
    chrome.sidebar.all_sessions = true;
    chrome.prefs.agent_sort = AgentSort::Priority;
    assert_eq!(
        items(&chrome),
        [
            ("  This project", true),
            ("✓ All projects", false),
            ("  Grouped", true),
            ("✓ Priority", false),
        ]
    );

    // The enabled choice of each pair carries the toggle its chord runs.
    let menu = build_menu(&ws, &chrome, AGENTS, (0, 0));
    assert_eq!(
        menu.items[0].action,
        MenuAction::Act(Action::ToggleSessionsScope)
    );
    assert_eq!(
        menu.items[2].action,
        MenuAction::Act(Action::ToggleAgentSort)
    );
}

#[test]
fn row_menu_labels_orphaned_rows() {
    use serde_json::json;

    let mut ws = Workspace::scripted();
    ws.daemon_mut().set_roster(json!({
        "epoch": "attention-1",
        "seq": 1,
        "entries": [
            {
                "entry_id": "run:term-orphan",
                "terminal": {
                    "terminal_id": "term-orphan",
                    "backend": "native",
                    "state": "orphaned",
                },
            },
            {
                "entry_id": "run:term-live",
                "terminal": {
                    "terminal_id": "term-live",
                    "backend": "native",
                    "state": "live",
                },
            },
        ],
    }));
    ws.reconcile_subscribe_first().expect("scripted roster");
    let chrome = Chrome::dark();

    let menu = build_menu(
        &ws,
        &chrome,
        ContextMenuKind::Agent("run:term-orphan".to_string()),
        (3, 20),
    );
    assert_eq!(
        labels(&menu),
        [
            "Focus",
            "Open in new tab",
            "Mark seen",
            "Take control",
            "Destroy orphaned terminal",
        ]
    );
    let enabled: Vec<bool> = menu.items.iter().map(|item| item.enabled).collect();
    assert_eq!(enabled, [true, true, false, false, true]);
    assert_eq!(
        menu.items[4].action,
        MenuAction::DestroyTerminal("term-orphan".to_string())
    );

    let menu = build_menu(
        &ws,
        &chrome,
        ContextMenuKind::Agent("run:term-live".to_string()),
        (3, 22),
    );
    assert_eq!(labels(&menu)[4], "Close terminal");
    assert!(!menu.items[4].enabled, "no pane to close");
}

/// 5.3.1: the sidebar rows' menus. A git project with worktree children
/// lists the worktree items and the fold item by its collapsed state, a
/// plain project only rename and close; a worktree row's rename and
/// close act on the tab tagged with it and are disabled until one is; an
/// agent row lists respond while blocked, enables mark seen only while
/// the roster carries an attention id, takes the lease item from its
/// pane's control state, and disables the pane items without a pane.
#[test]
fn row_menus_list_items_per_target_and_state() {
    use crate::daemon::{Checkout, ProjectRow, SidebarRows, SourceStatus, WorktreeRow};
    use serde_json::json;

    let mut ws = Workspace::scripted();
    let blocked = ws
        .open_terminal("term-blocked", "native", "epoch")
        .expect("open term-blocked");
    let held = ws
        .open_terminal("term-held", "native", "epoch")
        .expect("open term-held");
    let project = |id: &str, name: &str| ProjectRow {
        id: id.to_string(),
        name: name.to_string(),
        display_name: name.to_string(),
        checkout: Some(Checkout {
            machine_id: "local".to_string(),
            root_path: format!("/repos/{name}"),
        }),
        ..ProjectRow::default()
    };
    ws.daemon_mut().set_sidebar_rows(SidebarRows {
        projects: vec![project("proj-git", "git"), project("proj-plain", "plain")],
        statuses: [(
            "proj-git".to_string(),
            SourceStatus {
                current_branch: Some("main".to_string()),
                ..SourceStatus::default()
            },
        )]
        .into_iter()
        .collect(),
        worktrees: vec![WorktreeRow {
            id: "wt-1".to_string(),
            project_id: "proj-git".to_string(),
            branch_name: Some("feature".to_string()),
            worktree_path: "/repos/git/.worktrees/feature".to_string(),
            status: "active".to_string(),
            workspace_role: "client".to_string(),
            ..WorktreeRow::default()
        }],
        ..SidebarRows::default()
    });
    ws.daemon_mut().set_roster(json!({
        "epoch": "attention-1",
        "seq": 1,
        "entries": [
            {
                "entry_id": "run:term-blocked",
                "terminal": {"terminal_id": "term-blocked", "backend": "native"},
                "attention": {"attention_id": "att-1", "kind": "actionable"},
            },
            {
                "entry_id": "run:term-held",
                "terminal": {"terminal_id": "term-held", "backend": "native"},
            },
            {
                "entry_id": "run:term-away",
                "terminal": {"terminal_id": "term-away", "backend": "native"},
            },
        ],
    }));
    ws.select_project("proj-git");
    ws.reconcile_subscribe_first().expect("scripted roster");
    ws.pane_mut(held).control = ControlState::Held;
    let mut chrome = Chrome::dark();
    chrome.open_pane(blocked, "blocked");
    chrome.open_pane(held, "held");
    let enabled = |menu: &ContextMenuState| -> Vec<bool> {
        menu.items.iter().map(|item| item.enabled).collect()
    };
    let git = || ContextMenuKind::Project("proj-git".to_string());

    // The git project with a child, folded then expanded; the plain one.
    let menu = build_menu(&ws, &chrome, git(), (2, 3));
    assert_eq!(
        labels(&menu),
        [
            "Rename",
            "Close",
            "New worktree",
            "Open worktree…",
            "Expand"
        ]
    );
    assert_eq!(
        menu.items[0].action,
        MenuAction::RenameProject("proj-git".to_string())
    );
    assert_eq!(
        menu.items[1].action,
        MenuAction::CloseProject("proj-git".to_string())
    );
    assert_eq!(
        menu.items[2].action,
        MenuAction::NewWorktree("proj-git".to_string())
    );
    assert_eq!(
        menu.items[3].action,
        MenuAction::OpenWorktree("proj-git".to_string())
    );
    assert_eq!(
        menu.items[4].action,
        MenuAction::ToggleGroup("proj-git".to_string())
    );
    assert!(menu.items.iter().all(|item| item.enabled));
    assert_eq!((menu.kind, menu.anchor), (git(), (2, 3)));
    chrome.sidebar.toggle_group("proj-git");
    let menu = build_menu(&ws, &chrome, git(), (2, 3));
    assert_eq!(labels(&menu)[4], "Collapse");
    let menu = build_menu(
        &ws,
        &chrome,
        ContextMenuKind::Project("proj-plain".to_string()),
        (2, 6),
    );
    assert_eq!(labels(&menu), ["Rename", "Close"]);
    assert_eq!(
        menu.items[1].action,
        MenuAction::CloseProject("proj-plain".to_string())
    );

    // The worktree row: rename and close wait for a tab tagged with it.
    let worktree = || ContextMenuKind::Worktree("wt-1".to_string());
    let menu = build_menu(&ws, &chrome, worktree(), (4, 4));
    assert_eq!(
        labels(&menu),
        ["Rename", "Close", "Delete worktree checkout…"]
    );
    assert_eq!(enabled(&menu), [false, false, true]);
    assert_eq!(menu.items[0].action, MenuAction::Act(Action::RenameTab));
    assert_eq!(menu.items[1].action, MenuAction::Act(Action::CloseTab));
    assert_eq!(
        menu.items[2].action,
        MenuAction::RemoveWorktree("wt-1".to_string())
    );
    chrome.active_tab_mut().expect("active tab").worktree_id = Some("wt-1".to_string());
    let menu = build_menu(&ws, &chrome, worktree(), (4, 4));
    assert!(menu.items.iter().all(|item| item.enabled));

    // Agent rows: blocked and observed; plain and held; without a pane.
    let agent = |entry: &str| ContextMenuKind::Agent(entry.to_string());
    let menu = build_menu(&ws, &chrome, agent("run:term-blocked"), (3, 20));
    assert_eq!(
        labels(&menu),
        [
            "Focus",
            "Open in new tab",
            "Respond",
            "Mark seen",
            "Take control",
            "Close terminal",
        ]
    );
    assert_eq!(
        menu.items[0].action,
        MenuAction::FocusAgent("run:term-blocked".to_string())
    );
    assert_eq!(
        menu.items[1].action,
        MenuAction::OpenAgentInNewTab("run:term-blocked".to_string())
    );
    assert_eq!(
        menu.items[2].action,
        MenuAction::Respond("run:term-blocked".to_string())
    );
    assert_eq!(
        menu.items[3].action,
        MenuAction::MarkSeen("run:term-blocked".to_string())
    );
    assert_eq!(menu.items[4].action, MenuAction::TakeControl(blocked));
    assert_ne!(chrome.focused_pane(), Some(blocked));
    assert_eq!(menu.items[5].action, MenuAction::CloseTerminal(blocked));
    assert!(menu.items.iter().all(|item| item.enabled));
    let menu = build_menu(&ws, &chrome, agent("run:term-held"), (3, 22));
    assert_eq!(
        labels(&menu),
        [
            "Focus",
            "Open in new tab",
            "Mark seen",
            "Release control",
            "Close terminal",
        ]
    );
    assert_eq!(enabled(&menu), [true, true, false, true, true]);
    assert_eq!(menu.items[3].action, MenuAction::ReleaseControl(held));
    let menu = build_menu(&ws, &chrome, agent("run:term-away"), (3, 24));
    assert_eq!(
        labels(&menu),
        [
            "Focus",
            "Open in new tab",
            "Mark seen",
            "Take control",
            "Close terminal",
        ]
    );
    assert_eq!(enabled(&menu), [true, true, false, false, false]);
}

#[test]
fn agent_row_close_activates_with_the_row_pane_not_the_focused_one() {
    use serde_json::json;

    let mut ws = Workspace::scripted();
    let blocked = ws
        .open_terminal("term-blocked", "native", "epoch")
        .expect("open term-blocked");
    let held = ws
        .open_terminal("term-held", "native", "epoch")
        .expect("open term-held");
    ws.daemon_mut().set_roster(json!({
        "epoch": "attention-1",
        "seq": 1,
        "entries": [{
            "entry_id": "run:term-blocked",
            "terminal": {"terminal_id": "term-blocked", "backend": "native"},
            "attention": {"attention_id": "att-1", "kind": "actionable"},
        }],
    }));
    ws.reconcile_subscribe_first().expect("scripted roster");
    ws.pane_mut(held).control = ControlState::Held;

    let mut chrome = Chrome::dark();
    chrome.open_pane(blocked, "blocked");
    chrome.open_pane(held, "held");
    assert_eq!(chrome.focused_pane(), Some(held));
    open_menu(
        &ws,
        &mut chrome,
        ContextMenuKind::Agent("run:term-blocked".to_string()),
        (3, 20),
    );
    let menu = chrome.menu.as_mut().expect("agent menu");
    menu.selected = menu
        .items
        .iter()
        .position(|item| item.label == "Close terminal")
        .expect("close terminal item");

    assert_eq!(
        activate_menu(&mut chrome),
        Some((
            ContextMenuKind::Agent("run:term-blocked".to_string()),
            MenuAction::CloseTerminal(blocked),
        ))
    );
    assert_eq!(chrome.focused_pane(), Some(held));
}

/// View › Sidebar › Pin sidebar is a local chrome change both loops apply: it pins
/// the open overlay into a saved column.
#[test]
fn pin_sidebar_pins_the_overlay_into_a_saved_column() {
    let mut ws = Workspace::scripted();
    let home = tempfile::tempdir().expect("temp gobby home");
    ws.set_gobby_home(home.path().to_path_buf());
    let mut chrome = Chrome::dark();
    chrome.sidebar.overlay = true;

    assert!(apply_local_menu_action(
        &mut ws,
        &mut chrome,
        &MenuAction::PinSidebar
    ));
    assert!(chrome.sidebar.pinned && !chrome.sidebar.overlay);
    let saved = crate::prefs::load_prefs(home.path()).expect("load prefs");
    assert!(saved.sidebar_pinned);
}

/// The View menu's theme row opens Dark, Light and System beside it, the
/// first level with the row and the one in force marked; a pick redraws the
/// chrome in it and saves it.
#[test]
fn theme_row_opens_its_choices_beside_it_and_saves_the_pick() {
    let mut ws = Workspace::scripted();
    let home = tempfile::tempdir().expect("temp gobby home");
    ws.set_gobby_home(home.path().to_path_buf());
    let mut chrome = Chrome::dark();
    // The View title drawn at column 10 opens its menu under it at (10, 1).
    let title = MenuBarMenu::ALL
        .iter()
        .position(|menu| *menu == MenuBarMenu::View)
        .expect("view title");
    chrome.view.menu_title_hit_areas = vec![(title, Rect::new(10, 0, 6, 1))];
    open_menu(
        &ws,
        &mut chrome,
        ContextMenuKind::MenuBar(MenuBarMenu::View),
        (10, 1),
    );
    let view = chrome.menu.as_mut().expect("view menu");
    let theme_row = view
        .items
        .iter()
        .position(|item| item.action == MenuAction::OpenSubmenu(Submenu::Theme))
        .expect("theme row");
    assert_eq!(view.items[theme_row].label, "  Theme: Dark ▸");
    view.selected = theme_row;
    let row = view.item_rects[theme_row];

    let (kind, action) = activate_menu(&mut chrome).expect("the theme row is live");
    assert_eq!(kind, ContextMenuKind::MenuBar(MenuBarMenu::View));
    assert_eq!(action, MenuAction::OpenSubmenu(Submenu::Theme));
    assert!(apply_local_menu_action(&mut ws, &mut chrome, &action));
    assert_eq!(chrome.mode, Mode::ContextMenu);
    let choices = chrome.menu.as_mut().expect("theme choices");
    assert_eq!(choices.kind, ContextMenuKind::Submenu(Submenu::Theme));
    assert_eq!(
        choices.item_rects[0].x,
        row.right() + 2,
        "beside the border"
    );
    assert_eq!(choices.item_rects[0].y, row.y, "level with the row");
    assert_eq!(labels(choices), ["● Dark", "  Light", "  System"]);
    let enabled: Vec<bool> = choices.items.iter().map(|item| item.enabled).collect();
    assert_eq!(
        enabled,
        [false, true, true],
        "the one in force is marked and inert"
    );

    choices.selected = 1;
    let (kind, action) = activate_menu(&mut chrome).expect("light is a pick");
    assert_eq!(kind, ContextMenuKind::Submenu(Submenu::Theme));
    assert_eq!(action, MenuAction::SetTheme("light"));
    assert!(apply_local_menu_action(&mut ws, &mut chrome, &action));
    assert_eq!(chrome.theme.kind, crate::theme::ThemeKind::Light);
    assert_eq!(theme_row_label(&chrome), "  Theme: Light ▸");
    let saved = crate::prefs::load_prefs(home.path()).expect("load prefs");
    assert_eq!(saved.theme, "light");
}

/// View › Sidebar › a section cascades two deep with every menu behind it
/// open on the row that led there; left goes back one level, right opens a
/// submenu row. Each section's submenu holds only that section's options.
#[test]
fn sidebar_submenus_cascade_and_hold_one_section_each() {
    use super::super::modal_input::{route_modal_key, ModalOutcome};
    use crate::key_input::KeyInput;
    use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
    let mut ws = Workspace::scripted();
    let mut chrome = Chrome::dark();
    let key = |code| KeyInput {
        key: KeyEvent::new(code, KeyModifiers::NONE),
        bytes: Vec::new(),
    };
    let machines = Submenu::Section(SidebarSection::Machines);
    assert!(apply_local_menu_action(
        &mut ws,
        &mut chrome,
        &MenuAction::OpenSubmenu(machines.clone())
    ));
    let open = chrome.menu.as_ref().expect("machines submenu");
    assert_eq!(open.kind, ContextMenuKind::Submenu(machines.clone()));
    assert_eq!(labels(open), ["✓ This machine", "  All machines"]);
    let sidebar = open.parent.as_deref().expect("sidebar submenu behind");
    assert_eq!(sidebar.kind, ContextMenuKind::Submenu(Submenu::Sidebar));
    assert_eq!(
        labels(sidebar),
        [
            "  Show sidebar",
            "  Pin sidebar",
            "  Machines ▸",
            "  Projects ▸",
            "  Agents ▸",
            "  Terminals ▸",
        ]
    );
    assert_eq!(
        sidebar.items[sidebar.selected].action,
        MenuAction::OpenSubmenu(machines)
    );
    let view = sidebar.parent.as_deref().expect("view menu behind");
    assert_eq!(view.kind, ContextMenuKind::MenuBar(MenuBarMenu::View));
    assert_eq!(
        view.items[view.selected].action,
        MenuAction::OpenSubmenu(Submenu::Sidebar)
    );

    // Left returns to Sidebar; right on its Projects row opens Projects.
    route_modal_key(&ws, &mut chrome, &key(KeyCode::Left));
    let sidebar = chrome.menu.as_mut().expect("sidebar submenu");
    assert_eq!(sidebar.kind, ContextMenuKind::Submenu(Submenu::Sidebar));
    sidebar.selected = 3;
    let ModalOutcome::Menu { action, .. } = route_modal_key(&ws, &mut chrome, &key(KeyCode::Right))
    else {
        panic!("right opens the submenu row");
    };
    apply_local_menu_action(&mut ws, &mut chrome, &action);
    let projects = chrome.menu.as_ref().expect("projects submenu");
    assert_eq!(labels(projects), ["✓ Working projects", "  All projects"]);
    let terminals = build_menu(
        &ws,
        &chrome,
        ContextMenuKind::Submenu(Submenu::Section(SidebarSection::Terminals)),
        (0, 0),
    );
    assert_eq!(
        labels(&terminals),
        ["New terminal", "Destroy orphaned terminals…"]
    );

    // The machine scope sets the filter the rows read.
    apply_local_menu_action(&mut ws, &mut chrome, &MenuAction::SetMachineScope(true));
    assert_eq!(
        chrome.sidebar.machine_filter.as_deref(),
        Some(crate::ui::sidebar::ALL_MACHINES)
    );
    apply_local_menu_action(&mut ws, &mut chrome, &MenuAction::SetMachineScope(false));
    assert_eq!(chrome.sidebar.machine_filter, None);
}

/// Window, a tab's menu and a pane's menu each cascade Arrange ▸ from
/// themselves, by keyboard and by mouse, and every choice keeps the tab the
/// opening menu was built for even after another tab becomes active.
#[test]
fn arrange_submenu_cascades_from_window_tab_and_pane_menus() {
    use super::super::modal_input::{route_modal_key, ModalOutcome};
    use crate::app::{route_mouse, MouseOutcome};
    use crate::key_input::KeyInput;
    use crossterm::event::{
        KeyCode, KeyEvent, KeyModifiers, MouseButton, MouseEvent, MouseEventKind,
    };
    let key = |code| KeyInput {
        key: KeyEvent::new(code, KeyModifiers::NONE),
        bytes: Vec::new(),
    };
    let click = |column, row| MouseEvent {
        kind: MouseEventKind::Down(MouseButton::Left),
        column,
        row,
        modifiers: KeyModifiers::NONE,
    };
    let mut ws = Workspace::scripted();
    let [alpha, beta, gamma] = ["term-alpha", "term-beta", "term-gamma"].map(|id| {
        ws.open_terminal(id, "native", "epoch")
            .expect("open terminal")
    });
    let mut chrome = Chrome::dark();
    chrome.prefs.mouse_capture = true;
    chrome.open_pane(alpha, "alpha");
    chrome.open_pane(beta, "beta");
    chrome.open_tab(gamma, "gamma");
    assert!(chrome.activate_tab(0));
    let first = chrome.tabs().tabs[0].id.clone();
    let second = chrome.tabs().tabs[1].id.clone();
    let open_on_arrange = |ws: &Workspace, chrome: &mut Chrome, kind: &ContextMenuKind| {
        open_menu(ws, chrome, kind.clone(), (10, 3));
        let menu = chrome.menu.as_mut().expect("open menu");
        menu.selected = menu
            .items
            .iter()
            .position(|item| item.label == "Arrange ▸")
            .expect("arrange row");
        menu.item_rects[menu.selected]
    };

    for (kind, target) in [
        (
            ContextMenuKind::MenuBar(MenuBarMenu::Window),
            ArrangeTarget {
                tab: first.clone(),
                pane: None,
            },
        ),
        (
            ContextMenuKind::Tab(1),
            ArrangeTarget {
                tab: second.clone(),
                pane: None,
            },
        ),
        (
            ContextMenuKind::Pane(beta),
            ArrangeTarget {
                tab: first.clone(),
                pane: Some(beta),
            },
        ),
    ] {
        let submenu = ContextMenuKind::Submenu(Submenu::Arrange(target.clone()));

        // Keyboard: right opens it beside the row, left goes back, right
        // again, then j and Enter pick the second choice.
        let row = open_on_arrange(&ws, &mut chrome, &kind);
        let ModalOutcome::Menu { kind: from, action } =
            route_modal_key(&ws, &mut chrome, &key(KeyCode::Right))
        else {
            panic!("right opens Arrange from {kind:?}");
        };
        assert_eq!(from, kind);
        assert!(apply_local_menu_action(&mut ws, &mut chrome, &action));
        let open = chrome.menu.as_ref().expect("arrange submenu");
        assert_eq!(open.kind, submenu);
        assert_eq!(
            open.parent.as_deref().map(|parent| &parent.kind),
            Some(&kind),
            "it cascades from the menu that offered it"
        );
        assert_eq!(open.item_rects[0].x, row.right() + 2, "beside the border");
        assert_eq!(open.item_rects[0].y, row.y, "level with the row");
        route_modal_key(&ws, &mut chrome, &key(KeyCode::Left));
        assert_eq!(chrome.menu.as_ref().map(|menu| &menu.kind), Some(&kind));
        let ModalOutcome::Menu { action, .. } =
            route_modal_key(&ws, &mut chrome, &key(KeyCode::Char('l')))
        else {
            panic!("l opens Arrange again");
        };
        apply_local_menu_action(&mut ws, &mut chrome, &action);
        // Another tab becoming active does not move the choice.
        chrome.activate_tab(if target.tab == first { 1 } else { 0 });
        route_modal_key(&ws, &mut chrome, &key(KeyCode::Char('j')));
        let ModalOutcome::Menu { kind: from, action } =
            route_modal_key(&ws, &mut chrome, &key(KeyCode::Enter))
        else {
            panic!("enter picks a layout");
        };
        assert_eq!(from, submenu);
        assert_eq!(
            action,
            MenuAction::Arrange {
                layout: ArrangeLayout::EvenVertical,
                target: target.clone(),
            }
        );
        assert!(chrome.menu.is_none(), "a pick closes the cascade");
        chrome.activate_tab(0);

        // Esc dismisses the whole cascade.
        open_on_arrange(&ws, &mut chrome, &kind);
        let ModalOutcome::Menu { action, .. } =
            route_modal_key(&ws, &mut chrome, &key(KeyCode::Right))
        else {
            panic!("right opens Arrange");
        };
        apply_local_menu_action(&mut ws, &mut chrome, &action);
        route_modal_key(&ws, &mut chrome, &key(KeyCode::Esc));
        assert!(chrome.menu.is_none());
        assert_eq!(chrome.mode, Mode::Terminal);

        // Mouse: a click on the row opens it, a click on a choice picks it,
        // and a click outside dismisses it.
        let row = open_on_arrange(&ws, &mut chrome, &kind);
        let MouseOutcome::Menu { action, .. } = route_mouse(&ws, &mut chrome, &click(row.x, row.y))
        else {
            panic!("a click on the row opens Arrange");
        };
        apply_local_menu_action(&mut ws, &mut chrome, &action);
        let tiled = chrome.menu.as_ref().expect("arrange submenu").item_rects[4];
        let MouseOutcome::Menu { action, .. } =
            route_mouse(&ws, &mut chrome, &click(tiled.x, tiled.y))
        else {
            panic!("a click picks a layout");
        };
        assert_eq!(
            action,
            MenuAction::Arrange {
                layout: ArrangeLayout::Tiled,
                target: target.clone(),
            }
        );
        let row = open_on_arrange(&ws, &mut chrome, &kind);
        let MouseOutcome::Menu { action, .. } = route_mouse(&ws, &mut chrome, &click(row.x, row.y))
        else {
            panic!("a click on the row opens Arrange");
        };
        apply_local_menu_action(&mut ws, &mut chrome, &action);
        route_mouse(&ws, &mut chrome, &click(0, 29));
        assert!(chrome.menu.is_none(), "a click outside dismisses it");
    }
}

/// View › Monochrome redraws the chrome in grays at once, keeps the theme,
/// and survives a reload of the prefs.
#[test]
fn monochrome_toggle_grays_the_chrome_and_is_saved() {
    let mut ws = Workspace::scripted();
    let home = tempfile::tempdir().expect("temp gobby home");
    ws.set_gobby_home(home.path().to_path_buf());
    let mut chrome = Chrome::dark();
    let gray = |color| match color {
        ratatui::style::Color::Rgb(r, g, b) => r == g && g == b,
        _ => false,
    };
    assert!(!gray(chrome.palette.accent));
    apply_local_menu_action(&mut ws, &mut chrome, &MenuAction::ToggleMonochrome);
    assert!(chrome.prefs.monochrome);
    assert_eq!(chrome.theme.kind, crate::theme::ThemeKind::Dark);
    for color in [
        chrome.palette.accent,
        chrome.palette.peach,
        chrome.palette.red,
    ] {
        assert!(gray(color), "{color:?}");
    }
    let saved = crate::prefs::load_prefs(home.path()).expect("load prefs");
    assert!(saved.monochrome);
    let mut reloaded = Chrome::dark();
    reloaded.apply_prefs(saved);
    assert_eq!(reloaded.palette.accent, chrome.palette.accent);
    apply_local_menu_action(&mut ws, &mut chrome, &MenuAction::ToggleMonochrome);
    assert!(!gray(chrome.palette.accent));
}
