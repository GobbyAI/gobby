use super::*;
use crate::app::Workspace;

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
            "rename pane",
            "swap with focused pane",
            "split right",
            "split down",
            "zoom",
            "take control",
            "copy mode",
            "send right-clicks to pane",
            "close pane",
        ]
    );
    assert_eq!(menu.items[1].action, MenuAction::SwapWithFocused(other));
    assert_eq!(menu.items[5].action, MenuAction::TakeControl(other));
    assert_eq!(menu.items[7].action, MenuAction::TogglePassthrough(other));
    assert_eq!(menu.items[8].action, MenuAction::Act(Action::ClosePane));
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
        ..Default::default()
    });
    chrome.toggle_zoom();
    let menu = build_menu(&ws, &chrome, ContextMenuKind::Pane(focused), (10, 5));
    assert_eq!(
        labels(&menu),
        [
            "rename pane",
            "clear pane name",
            "split right",
            "split down",
            "unzoom",
            "release control",
            "respond",
            "copy mode",
            "use gclient menu",
            "close pane",
        ]
    );
    assert_eq!(menu.items[1].action, MenuAction::ClearPaneName(focused));
    assert_eq!(menu.items[5].action, MenuAction::ReleaseControl(focused));
    assert_eq!(menu.items[6].action, MenuAction::Respond(entry_id));

    let menu = build_menu(&ws, &chrome, ContextMenuKind::Tab(0), (3, 0));
    assert_eq!(labels(&menu), ["new tab", "rename tab", "close tab"]);
    assert_eq!(menu.items[2].action, MenuAction::Act(Action::CloseTab));

    let menu = build_menu(&ws, &chrome, ContextMenuKind::Global, (40, 12));
    assert_eq!(
        labels(&menu),
        [
            "new terminal",
            "new tab",
            "new project",
            "settings",
            "keybinding help",
            "alerts…",
            "reload config",
            "toggle sidebar",
            "destroy orphaned terminals…",
            "detach",
            "quit",
        ]
    );
    assert_eq!(menu.items[2].action, MenuAction::Act(Action::NewProject));
    assert_eq!(menu.items[5].action, MenuAction::ShowAlerts);
    assert_eq!(menu.items[6].action, MenuAction::Act(Action::ReloadConfig));
    assert_eq!(menu.items[10].action, MenuAction::Act(Action::Quit));
    assert!(menu.items.iter().all(|item| item.enabled));

    // Rows sit one cell inside the popup at the anchor: `destroy orphaned
    // terminals…` makes it 31 wide, eleven items make it 13 tall.
    assert_eq!(
        menu_rect(menu.anchor, &menu.items),
        Rect::new(40, 12, 31, 13)
    );
    assert_eq!(menu.item_rects.len(), 11);
    assert_eq!(menu.item_rects[0], Rect::new(41, 13, 29, 1));
    assert_eq!(menu_hit(&menu, 41, 13), Some(0));
    assert_eq!(menu_hit(&menu, 57, 15), Some(2));
    assert_eq!(menu_hit(&menu, 40, 13), None, "the border is not a row");
    assert_eq!(menu_hit(&menu, 45, 23), Some(10), "the last row");
    assert_eq!(menu_hit(&menu, 45, 24), None, "below the last row");
    let short = [item("zoom", MenuAction::Act(Action::Zoom))];
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
    assert_eq!(item.label, "destroy orphaned terminals…");
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
    assert_eq!(labels(&gobby), ["alerts…", "settings"]);
    assert_eq!(
        actions(&gobby),
        [MenuAction::ShowAlerts, MenuAction::Act(Action::Settings)]
    );

    let file = menu(&ws, &chrome, MenuBarMenu::File);
    assert_eq!(labels(&file), ["new project", "new tab", "new pane"]);
    assert_eq!(
        actions(&file),
        [
            MenuAction::Act(Action::NewProject),
            MenuAction::Act(Action::NewTab),
            MenuAction::Act(Action::NewTerminal),
        ]
    );

    // A scripted terminal is named after its id, so its name can be cleared.
    let edit = menu(&ws, &chrome, MenuBarMenu::Edit);
    assert_eq!(
        labels(&edit),
        [
            "copy mode",
            "rename pane",
            "rename tab",
            "rename terminal",
            "clear pane name",
            "send right-clicks to pane",
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
            "copy mode",
            "rename pane",
            "rename tab",
            "rename terminal",
            "use gclient menu",
        ]
    );

    // View is the sessions band's view menu.
    assert_eq!(
        menu(&ws, &chrome, MenuBarMenu::View).items,
        build_menu(&ws, &chrome, ContextMenuKind::SessionsView, (0, 1)).items
    );

    let window = menu(&ws, &chrome, MenuBarMenu::Window);
    assert_eq!(
        labels(&window),
        ["next tab", "previous tab", "next pane", "previous pane"]
    );
    assert_eq!(
        actions(&window),
        [
            MenuAction::Act(Action::NextTab),
            MenuAction::Act(Action::PreviousTab),
            MenuAction::Act(Action::CyclePaneNext),
            MenuAction::Act(Action::CyclePanePrevious),
        ]
    );

    let help = menu(&ws, &chrome, MenuBarMenu::Help);
    assert_eq!(labels(&help), ["keys"]);
    assert_eq!(actions(&help), [MenuAction::Act(Action::Help)]);

    // The Agent menu's items are section 3.11's.
    assert!(menu(&ws, &chrome, MenuBarMenu::Agent).items.is_empty());

    // With no pane focused, Edit shows its pane items disabled.
    let edit = menu(&ws, &Chrome::dark(), MenuBarMenu::Edit);
    assert_eq!(
        labels(&edit),
        ["copy mode", "rename pane", "rename tab", "rename terminal"]
    );
    assert!(edit.items.iter().all(|item| !item.enabled));
}

#[test]
fn sessions_view_menu_marks_the_view_in_force() {
    let ws = Workspace::scripted();
    let mut chrome = Chrome::dark();
    let items = |chrome: &Chrome| -> Vec<(&'static str, bool)> {
        build_menu(&ws, chrome, ContextMenuKind::SessionsView, (0, 0))
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
            ("✓ this project", false),
            ("  all projects", true),
            ("✓ grouped", false),
            ("  priority", true),
        ]
    );

    // The marks follow both axes independently.
    chrome.sidebar.all_sessions = true;
    chrome.prefs.agent_sort = AgentSort::Priority;
    assert_eq!(
        items(&chrome),
        [
            ("  this project", true),
            ("✓ all projects", false),
            ("  grouped", true),
            ("✓ priority", false),
        ]
    );

    // The enabled choice of each pair carries the toggle its chord runs.
    let menu = build_menu(&ws, &chrome, ContextMenuKind::SessionsView, (0, 0));
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
            "focus",
            "open in new tab",
            "mark seen",
            "take control",
            "destroy orphaned terminal",
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
    assert_eq!(labels(&menu)[4], "close terminal");
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
            "rename",
            "close",
            "new worktree",
            "open worktree…",
            "expand"
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
    assert_eq!(labels(&menu)[4], "collapse");
    let menu = build_menu(
        &ws,
        &chrome,
        ContextMenuKind::Project("proj-plain".to_string()),
        (2, 6),
    );
    assert_eq!(labels(&menu), ["rename", "close"]);
    assert_eq!(
        menu.items[1].action,
        MenuAction::CloseProject("proj-plain".to_string())
    );

    // The worktree row: rename and close wait for a tab tagged with it.
    let worktree = || ContextMenuKind::Worktree("wt-1".to_string());
    let menu = build_menu(&ws, &chrome, worktree(), (4, 4));
    assert_eq!(
        labels(&menu),
        ["rename", "close", "delete worktree checkout…"]
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
            "focus",
            "open in new tab",
            "respond",
            "mark seen",
            "take control",
            "close terminal",
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
            "focus",
            "open in new tab",
            "mark seen",
            "release control",
            "close terminal",
        ]
    );
    assert_eq!(enabled(&menu), [true, true, false, true, true]);
    assert_eq!(menu.items[3].action, MenuAction::ReleaseControl(held));
    let menu = build_menu(&ws, &chrome, agent("run:term-away"), (3, 24));
    assert_eq!(
        labels(&menu),
        [
            "focus",
            "open in new tab",
            "mark seen",
            "take control",
            "close terminal",
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
        .position(|item| item.label == "close terminal")
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
