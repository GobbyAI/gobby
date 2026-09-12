use super::*;
use crate::app::Workspace;
use crate::daemon::{Checkout, ProjectRow, SidebarRows, SourceStatus, WorktreeRow};
use ratatui::backend::TestBackend;
use ratatui::Terminal;
use serde_json::json;

/// Two projects, `alpha` (focused, on `main`, one worktree) and `beta`,
/// plus one attention entry on `term-alpha` and a bare `term-beta`.
fn scripted_workspace() -> Workspace {
    let mut ws = Workspace::scripted();
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
        projects: vec![project("proj-alpha", "alpha"), project("proj-beta", "beta")],
        statuses: [(
            "proj-alpha".to_string(),
            SourceStatus {
                current_branch: Some("main".to_string()),
                ahead: Some(2),
                behind: Some(1),
                ..SourceStatus::default()
            },
        )]
        .into_iter()
        .collect(),
        worktrees: vec![WorktreeRow {
            id: "wt-1".to_string(),
            project_id: "proj-alpha".to_string(),
            task_id: Some("#123".to_string()),
            branch_name: "worktree/feature".to_string(),
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
            "attention": {"attention_id": "att-1", "kind": "actionable", "fingerprint": "fp-1"}
        }]
    }));
    ws.select_project("proj-alpha");
    ws.reconcile_subscribe_first().unwrap();
    ws.open_terminal("term-alpha", "native", "epoch").unwrap();
    ws.open_terminal("term-beta", "native", "epoch").unwrap();
    ws
}

fn screen(terminal: &Terminal<TestBackend>) -> String {
    let buffer = terminal.backend().buffer();
    let width = usize::from(buffer.area.width);
    let cells: Vec<String> = buffer
        .content()
        .iter()
        .map(|c| c.symbol().to_string())
        .collect();
    cells
        .chunks(width)
        .map(|row| row.concat())
        .collect::<Vec<_>>()
        .join("\n")
}

#[test]
fn layout_gives_the_top_half_to_machines_and_projects_at_most() {
    let area = Rect::new(0, 0, 26, 40);
    // One machine, one card: each takes its band and its row, the
    // sessions everything between them and the footer band.
    let layout = sidebar_layout(area, 1, 1);
    assert_eq!(layout.menu, Rect::new(0, 0, 25, 1));
    assert_eq!(
        layout.sections,
        [
            Rect::new(0, 1, 25, 2),
            Rect::new(0, 3, 25, 2),
            Rect::new(0, 5, 25, 34),
        ]
    );
    assert_eq!(layout.footer, Rect::new(0, 39, 25, 1));
    // The machines stop at four rows; the cards at the top half.
    let layout = sidebar_layout(area, 9, 1);
    assert_eq!(layout.sections[0].height, 1 + MACHINES_MAX_ROWS);
    assert_eq!(layout.sections[1], Rect::new(0, 6, 25, 2));
    assert_eq!(layout.sections[2], Rect::new(0, 8, 25, 31));
    let layout = sidebar_layout(area, 1, 30);
    assert_eq!(layout.sections[0].height, 2);
    assert_eq!(layout.sections[1], Rect::new(0, 3, 25, 17));
    assert_eq!(layout.sections[2], Rect::new(0, 20, 25, 19));
    // Three rows: the two bands and one row, which the sessions keep.
    let layout = sidebar_layout(Rect::new(0, 0, 26, 3), 1, 1);
    assert_eq!(layout.menu, Rect::new(0, 0, 25, 1));
    assert_eq!(layout.footer, Rect::new(0, 2, 25, 1));
    assert_eq!(layout.sections[0].height, 0);
    assert_eq!(layout.sections[1].height, 0);
    assert_eq!(layout.sections[2], Rect::new(0, 1, 25, 1));
    // One row holds the menu band alone; nothing fits a one-column area.
    let layout = sidebar_layout(Rect::new(0, 0, 26, 1), 1, 1);
    assert_eq!(layout.menu, Rect::new(0, 0, 25, 1));
    assert_eq!(layout.footer, Rect::default());
    assert_eq!(layout.sections, [Rect::default(); 3]);
    assert_eq!(
        sidebar_layout(Rect::new(0, 0, 1, 40), 1, 1),
        SidebarLayout::default()
    );
}

#[test]
fn expanded_sidebar_draws_the_bands_and_records_the_hits() {
    let ws = scripted_workspace();
    let chrome = Chrome::dark();
    let area = Rect::new(0, 0, 26, 40);
    let mut terminal = Terminal::new(TestBackend::new(26, 40)).unwrap();
    let mut hits = SidebarHits::default();
    terminal
        .draw(|frame| hits = render_sidebar(frame, area, &ws, &chrome))
        .unwrap();
    let text = screen(&terminal);
    let lines: Vec<&str> = text.lines().collect();
    assert_eq!(lines[0], " [Menu]              [+] │");
    assert!(lines[1].starts_with(" Machines "), "{:?}", lines[1]);
    // The hub row carries its blocked agent's state and the local mark.
    assert!(lines[2].starts_with(" ⍾ "), "{:?}", lines[2]);
    assert!(lines[2].contains("· local"), "{:?}", lines[2]);
    assert_eq!(lines[3], " Projects      [working] │");
    // `working` lists only alpha, folded, on one line with its counts.
    assert_eq!(lines[4], " ⍾ alpha (main ↑2 ↓1)   ▸│");
    assert!(!text.contains("○ beta"), "{text}");
    assert!(!text.contains("feature"), "{text}");
    // The sort control does not fit beside the whole title at this width.
    assert_eq!(lines[5], " Sessions      [project] │");
    assert_eq!(lines[6], " ⍾ term-alpha · needs you│");
    assert_eq!(lines[7].trim_end_matches('│').trim(), "");
    assert_eq!(lines[8], " ○ term-beta             │");
    assert_eq!(lines[9], "   native                │");
    assert_eq!(lines[39], "                     [«] │");
    for line in &lines {
        assert!(line.ends_with('│'), "{line:?}");
    }

    assert_eq!(hits.projects_menu, Some(Rect::new(1, 0, 6, 1)));
    assert_eq!(hits.projects_new, Some(Rect::new(21, 0, 3, 1)));
    assert_eq!(hits.machines.len(), 1, "{:?}", hits.machines);
    assert_eq!(hits.machines[0].1, Rect::new(0, 2, 25, 1));
    assert_eq!(hits.projects_filter, Some(Rect::new(15, 3, 9, 1)));
    assert_eq!(
        hits.projects,
        vec![("proj-alpha".to_string(), Rect::new(0, 4, 25, 1))]
    );
    assert!(hits.worktrees.is_empty(), "{:?}", hits.worktrees);
    assert_eq!(
        hits.group_toggles,
        vec![("proj-alpha".to_string(), Rect::new(24, 4, 1, 1))]
    );
    assert_eq!(hits.sessions_scope, Some(Rect::new(15, 5, 9, 1)));
    assert_eq!(hits.agent_sort, None);
    assert_eq!(
        hits.agents,
        vec![
            ("run:term-alpha".to_string(), Rect::new(0, 6, 25, 2)),
            ("terminal:term-beta".to_string(), Rect::new(0, 8, 25, 2)),
        ]
    );
    assert_eq!(hits.scrollbars, [None; 3]);
    assert_eq!(hits.toggle, Some(Rect::new(21, 39, 3, 1)));
}

#[test]
fn wider_sidebar_shows_the_sort_control_and_the_expanded_card() {
    let ws = scripted_workspace();
    let mut chrome = Chrome::dark();
    chrome.sidebar.toggle_group("proj-alpha");
    let area = Rect::new(0, 0, 31, 40);
    let mut terminal = Terminal::new(TestBackend::new(31, 40)).unwrap();
    let mut hits = SidebarHits::default();
    terminal
        .draw(|frame| hits = render_sidebar(frame, area, &ws, &chrome))
        .unwrap();
    let text = screen(&terminal);
    let lines: Vec<&str> = text.lines().collect();
    assert_eq!(lines[5], "   └─ ○ feature · #123        │");
    assert_eq!(lines[6], " Sessions [project] [grouped] │");
    assert_eq!(hits.sessions_scope, Some(Rect::new(10, 6, 9, 1)));
    assert_eq!(hits.agent_sort, Some(Rect::new(20, 6, 9, 1)));
    assert_eq!(
        hits.worktrees,
        vec![("wt-1".to_string(), Rect::new(0, 5, 30, 1))]
    );
    assert_eq!(
        hits.group_toggles,
        vec![("proj-alpha".to_string(), Rect::new(29, 4, 1, 1))]
    );
}

#[test]
fn collapsed_rail_shows_indexes_and_dots() {
    let ws = scripted_workspace();
    let chrome = Chrome::dark();
    let area = Rect::new(0, 0, 4, 12);
    let mut terminal = Terminal::new(TestBackend::new(4, 12)).unwrap();
    let mut hits = SidebarHits::default();
    terminal
        .draw(|frame| hits = render_collapsed_sidebar(frame, area, &ws, &chrome))
        .unwrap();
    let text = screen(&terminal);
    let lines: Vec<&str> = text.lines().collect();
    // The machine dot, then the cards and the sessions under their rules,
    // and the toggle on the last row.
    assert_eq!(lines[0], "  ⍾│");
    assert_eq!(lines[1], "───│");
    assert_eq!(lines[2], "1 ⍾│");
    assert_eq!(lines[3], "   │");
    assert_eq!(lines[6], "───│");
    assert_eq!(lines[7], "1 ⍾│");
    assert_eq!(lines[8], "2 ○│");
    assert_eq!(lines[9], "   │");
    assert_eq!(lines[11], " » │");
    assert_eq!(hits.machines.len(), 1);
    assert_eq!(hits.projects.len(), 1);
    assert!(hits.worktrees.is_empty());
    assert_eq!(hits.agents.len(), 2);
    assert_eq!(hits.agents[0].1, Rect::new(0, 7, 3, 1));
    assert_eq!(hits.agents[1].1, Rect::new(0, 8, 3, 1));
    assert_eq!(hits.toggle, Some(Rect::new(1, 11, 1, 1)));
}

#[test]
fn rail_sections_share_the_rows_under_the_machine_dot() {
    let (rects, dividers) = collapsed_sections(Rect::new(0, 0, 4, 12));
    assert_eq!(dividers, [Some(1), Some(6)]);
    assert_eq!(
        rects,
        [
            Rect::new(0, 0, 3, 1),
            Rect::new(0, 2, 3, 4),
            Rect::new(0, 7, 3, 4),
        ]
    );
    // The odd row goes to the cards.
    let (rects, _) = collapsed_sections(Rect::new(0, 0, 4, 13));
    assert_eq!(rects[1].height, 5);
    assert_eq!(rects[2].height, 4);
    // Under six rows the cards take everything and no rule is drawn.
    let (rects, dividers) = collapsed_sections(Rect::new(0, 0, 4, 5));
    assert_eq!(dividers, [None; 2]);
    assert_eq!(
        rects[SidebarSection::Projects.index()],
        Rect::new(0, 0, 3, 5)
    );
    assert_eq!(rects[SidebarSection::Sessions.index()], Rect::default());
    // `section_rects` follows the rail while collapsed.
    let ws = scripted_workspace();
    let mut chrome = Chrome::dark();
    chrome.sidebar.collapsed = true;
    assert_eq!(
        section_rects(&ws, &chrome, Rect::new(0, 0, 4, 12)),
        collapsed_sections(Rect::new(0, 0, 4, 12)).0
    );
    chrome.sidebar.collapsed = false;
    assert_eq!(
        section_rects(&ws, &chrome, Rect::new(0, 0, 26, 40)),
        sidebar_layout(Rect::new(0, 0, 26, 40), 1, 1).sections
    );
}

#[test]
fn list_scroll_clamps_to_the_last_page() {
    let metrics = list_metrics(10, 4, 99);
    assert_eq!(metrics.max_offset_from_bottom, 6);
    assert_eq!(metrics.offset_from_bottom, 0);
    assert_eq!(metrics.viewport_rows, 4);
    assert!(!should_show_scrollbar(list_metrics(3, 4, 0)));
}
