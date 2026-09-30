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
    ws.set_local_machine("local");
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
            "provider": "codex",
            "model": "gpt-6-sol",
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
fn a_right_sidebar_keeps_its_edge_column_first() {
    // On the right the edge faces the panes, so the sections start one
    // column in and run to the sidebar's last column.
    let layout = sidebar_layout(Rect::new(54, 0, 26, 40), SidebarSide::Right, 1, 1, 2);
    assert_eq!(layout.sections[0], Rect::new(55, 0, 25, 3));
    assert_eq!(layout.sections[3], Rect::new(55, 23, 25, 17));
}

#[test]
fn layout_gives_the_top_half_to_machines_and_projects_at_most() {
    let area = Rect::new(0, 0, 26, 40);
    // One machine, one card: each takes its band, its row and the blank
    // row under it. With no bare terminal the Terminals section is gone,
    // heading and all, and the agents take the rest of the column.
    let layout = sidebar_layout(area, SidebarSide::Left, 1, 1, 0);
    assert_eq!(
        layout.sections,
        [
            Rect::new(0, 0, 25, 3),
            Rect::new(0, 3, 25, 3),
            Rect::new(0, 6, 25, 34),
            Rect::new(0, 40, 25, 0),
        ]
    );
    // A bare terminal splits the rest in half, however few the agents.
    let layout = sidebar_layout(area, SidebarSide::Left, 1, 1, 2);
    assert_eq!(layout.sections[2], Rect::new(0, 6, 25, 17));
    assert_eq!(layout.sections[3], Rect::new(0, 23, 25, 17));
    // The machines stop at four rows; the cards at the top half, their
    // blank row charged inside it. An odd row left over goes to the agents.
    let layout = sidebar_layout(area, SidebarSide::Left, 9, 1, 2);
    assert_eq!(layout.sections[0].height, 1 + MACHINES_MAX_ROWS + 1);
    assert_eq!(layout.sections[1], Rect::new(0, 6, 25, 3));
    assert_eq!(layout.sections[2], Rect::new(0, 9, 25, 16));
    assert_eq!(layout.sections[3], Rect::new(0, 25, 25, 15));
    let layout = sidebar_layout(area, SidebarSide::Left, 1, 30, 0);
    assert_eq!(layout.sections[0].height, 3);
    assert_eq!(layout.sections[1], Rect::new(0, 3, 25, 17));
    assert_eq!(layout.sections[2], Rect::new(0, 20, 25, 20));
    assert_eq!(layout.sections[3].height, 0);
    // Two rows hold the machines band and the agents band, terminals or
    // not; one row the agents band alone; three rows the terminals band as
    // well when there is a terminal, the agents both rows otherwise.
    // Nothing fits a one-column area.
    let heights = |rows: u16, terminal_rows: u16| {
        sidebar_layout(
            Rect::new(0, 0, 26, rows),
            SidebarSide::Left,
            1,
            1,
            terminal_rows,
        )
        .sections
        .map(|rect| rect.height)
    };
    assert_eq!(heights(2, 0), [1, 0, 1, 0]);
    assert_eq!(heights(2, 2), [1, 0, 1, 0]);
    assert_eq!(heights(1, 0), [0, 0, 1, 0]);
    assert_eq!(heights(3, 0), [1, 0, 2, 0]);
    assert_eq!(heights(3, 2), [1, 0, 1, 1]);
    assert_eq!(
        sidebar_layout(Rect::new(0, 0, 1, 40), SidebarSide::Left, 1, 1, 0),
        SidebarLayout::default()
    );
    // `section_rects` counts the workspace's rows into the same layout: one
    // machine, one card, one three-line agent and one two-line bare terminal.
    let ws = scripted_workspace();
    assert_eq!(
        section_rects(&ws, &Chrome::dark(), area),
        sidebar_layout(area, SidebarSide::Left, 1, 1, 2).sections
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
    let lines: Vec<&str> = text.lines().map(str::trim_end).collect();
    let blank = |line: &str| line.is_empty();
    // The machines band opens the column.
    assert_eq!(lines[0], " Machines");
    // The hub row carries its blocked agent's state and its name, no mark.
    assert!(lines[1].starts_with(" ⍾ "), "{:?}", lines[1]);
    assert!(!lines[1].contains('·'), "{:?}", lines[1]);
    // A blank row above the projects and agents bands.
    assert!(blank(lines[2]), "{:?}", lines[2]);
    // Headings carry no controls: View › Sidebar holds each section's options.
    assert_eq!(lines[3], " Projects");
    // `working` lists only alpha, folded, on one line with its counts.
    assert_eq!(lines[4], " ⍾ alpha (main ↑2 ↓1)   ▸");
    assert!(!text.contains("○ beta"), "{text}");
    assert!(!text.contains("feature"), "{text}");
    assert!(blank(lines[5]), "{:?}", lines[5]);
    assert_eq!(lines[6], " Agents");
    assert_eq!(lines[7], " ⍾ Codex");
    assert_eq!(lines[8], "   No assigned task");
    assert_eq!(lines[9], "   gpt-6-sol");
    assert!(blank(lines[22]), "{:?}", lines[22]);
    assert_eq!(lines[23], " Terminals");
    assert_eq!(lines[24], " ○ term-beta      gclient");
    assert!(blank(lines[25]), "{:?}", lines[25]);
    // No footer: the terminals run to the last row.
    assert!(blank(lines[39]), "{:?}", lines[39]);
    // The edge column is the bare drag lane: no row writes into it.
    for line in text.lines() {
        assert_eq!(line.chars().nth(25), Some(' '), "{line:?}");
    }

    assert_eq!(hits.machines.len(), 1, "{:?}", hits.machines);
    assert_eq!(hits.machines[0].1, Rect::new(0, 1, 25, 1));
    assert_eq!(
        hits.projects,
        vec![("proj-alpha".to_string(), Rect::new(0, 4, 25, 1))]
    );
    assert!(hits.worktrees.is_empty(), "{:?}", hits.worktrees);
    assert_eq!(
        hits.group_toggles,
        vec![("proj-alpha".to_string(), Rect::new(24, 4, 1, 1))]
    );
    assert_eq!(
        hits.agents,
        vec![
            ("run:term-alpha".to_string(), Rect::new(0, 7, 25, 3)),
            ("terminal:term-beta".to_string(), Rect::new(0, 24, 25, 2)),
        ]
    );
    assert_eq!(hits.scrollbars, [None; 4]);
}

#[test]
fn missing_local_machine_does_not_hide_agents_or_draw_a_phantom_machine() {
    let mut ws = scripted_workspace();
    ws.set_local_machine("");
    let chrome = Chrome::dark();

    assert!(machine_rows(&ws, &chrome).is_empty());
    assert_eq!(agent_rows(&ws, &chrome).len(), 1);
    assert_eq!(terminal_rows(&ws, &chrome).len(), 1);
}

#[test]
fn bare_terminal_titles_stay_still_while_agents_ticker() {
    let mut ws = scripted_workspace();
    let pane = ws
        .pane_for_terminal("term-beta")
        .expect("bare terminal pane");
    ws.pane_mut(pane).label = Some("a-bare-terminal-title-longer-than-the-sidebar".to_owned());
    let mut chrome = Chrome::dark();
    let draw_row = |chrome: &Chrome| {
        let mut terminal = Terminal::new(TestBackend::new(26, 40)).unwrap();
        terminal
            .draw(|frame| {
                render_sidebar(frame, Rect::new(0, 0, 26, 40), &ws, chrome);
            })
            .unwrap();
        screen(&terminal).lines().nth(24).unwrap().to_owned()
    };
    let first = draw_row(&chrome);
    chrome.ticker =
        (crate::ui::sidebar_rows::TICKER_PAUSE + 2) * crate::ui::sidebar_rows::TICKER_STEP;
    assert_eq!(draw_row(&chrome), first);
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
    // No agent runs in the worktree, so it draws no state glyph.
    assert_eq!(lines[5], "   └─   feature · #123         ");
    assert_eq!(lines[7], " Agents                        ");
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
