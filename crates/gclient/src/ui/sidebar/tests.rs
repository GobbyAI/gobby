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
    // One machine, one card: each takes its band, its row and the blank
    // row under it; the sessions everything from their band to the last row.
    let layout = sidebar_layout(area, 1, 1);
    assert_eq!(
        layout.sections,
        [
            Rect::new(0, 0, 25, 3),
            Rect::new(0, 3, 25, 3),
            Rect::new(0, 6, 25, 34),
        ]
    );
    // The machines stop at four rows; the cards at the top half, their
    // blank row charged inside it.
    let layout = sidebar_layout(area, 9, 1);
    assert_eq!(layout.sections[0].height, 1 + MACHINES_MAX_ROWS + 1);
    assert_eq!(layout.sections[1], Rect::new(0, 6, 25, 3));
    assert_eq!(layout.sections[2], Rect::new(0, 9, 25, 31));
    let layout = sidebar_layout(area, 1, 30);
    assert_eq!(layout.sections[0].height, 3);
    assert_eq!(layout.sections[1], Rect::new(0, 3, 25, 17));
    assert_eq!(layout.sections[2], Rect::new(0, 20, 25, 20));
    // Two rows hold the machines band and the sessions band; one row the
    // sessions band alone; nothing fits a one-column area.
    let layout = sidebar_layout(Rect::new(0, 0, 26, 2), 1, 1);
    assert_eq!(layout.sections.map(|rect| rect.height), [1, 0, 1]);
    let layout = sidebar_layout(Rect::new(0, 0, 26, 1), 1, 1);
    assert_eq!(layout.sections.map(|rect| rect.height), [0, 0, 1]);
    assert_eq!(
        sidebar_layout(Rect::new(0, 0, 1, 40), 1, 1),
        SidebarLayout::default()
    );
    // `section_rects` counts the workspace's rows into the same layout.
    let ws = scripted_workspace();
    assert_eq!(
        section_rects(&ws, &Chrome::dark(), area),
        sidebar_layout(area, 1, 1).sections
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
    let blank = |line: &str| line.trim_end_matches('│').trim().is_empty();
    // The machines band opens the column.
    assert!(lines[0].starts_with(" Machines "), "{:?}", lines[0]);
    // The hub row carries its blocked agent's state and the local mark.
    assert!(lines[1].starts_with(" ⍾ "), "{:?}", lines[1]);
    assert!(lines[1].contains("· local"), "{:?}", lines[1]);
    // A blank row above the projects and sessions bands.
    assert!(blank(lines[2]), "{:?}", lines[2]);
    assert_eq!(lines[3], " Projects      [working] │");
    // `working` lists only alpha, folded, on one line with its counts.
    assert_eq!(lines[4], " ⍾ alpha (main ↑2 ↓1)   ▸│");
    assert!(!text.contains("○ beta"), "{text}");
    assert!(!text.contains("feature"), "{text}");
    assert!(blank(lines[5]), "{:?}", lines[5]);
    // One control, so the band fits the default width; the scope and the
    // order live in its menu.
    assert_eq!(lines[6], " Sessions         [view] │");
    assert_eq!(lines[7], " ⍾ term-alpha · needs you│");
    assert!(blank(lines[8]), "{:?}", lines[8]);
    assert_eq!(lines[9], " ○ term-beta             │");
    assert_eq!(lines[10], "   gclient               │");
    // No footer: the sessions run to the last row.
    assert!(blank(lines[39]), "{:?}", lines[39]);
    for line in &lines {
        assert!(line.ends_with('│'), "{line:?}");
    }

    assert_eq!(hits.machines.len(), 1, "{:?}", hits.machines);
    assert_eq!(hits.machines[0].1, Rect::new(0, 1, 25, 1));
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
    assert_eq!(hits.sessions_view, Some(Rect::new(18, 6, 6, 1)));
    assert_eq!(
        hits.agents,
        vec![
            ("run:term-alpha".to_string(), Rect::new(0, 7, 25, 2)),
            ("terminal:term-beta".to_string(), Rect::new(0, 9, 25, 2)),
        ]
    );
    assert_eq!(hits.scrollbars, [None; 3]);
}

#[test]
fn wider_sidebar_shows_the_expanded_card() {
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
    assert_eq!(lines[7], " Sessions              [view] │");
    assert_eq!(hits.sessions_view, Some(Rect::new(23, 7, 6, 1)));
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
fn list_scroll_clamps_to_the_last_page() {
    let metrics = list_metrics(10, 4, 99);
    assert_eq!(metrics.max_offset_from_bottom, 6);
    assert_eq!(metrics.offset_from_bottom, 0);
    assert_eq!(metrics.viewport_rows, 4);
    assert!(!should_show_scrollbar(list_metrics(3, 4, 0)));
}
